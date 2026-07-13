from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
import numpy as np
import pytest

from app.hotel.track_fragment_linker import TrackFragmentLinker
from app.hotel.room_detector import RoomDetector, RoomZone, RoomDecision
from app.hotel.plate_voting import vote_plates_multiframe, vote_plate
from app.hotel.frame_buffer import TrackFrameBuffer, candidate_score
from app.hotel.event_source import validate_hotel_event_source
from app.hotel.models import HotelVehicleEvent
from app.hotel.store import HotelStore
from app.ocr import PlateReader


def test_track_fragment_linker_basic_link():
    # Gap < 2.0s and proximity < 0.15 normalized distance on the same camera
    linker = TrackFragmentLinker(max_gap_seconds=2.0, max_distance=0.15)
    
    camera_id = "A1"
    track_id_1 = 5
    bbox_1 = (100, 100, 200, 200)  # Center (150, 150) -> normalized (0.156, 0.277) on 960x540
    
    # 1. Register active track
    logical_id_1 = linker.register_track_active(
        camera_id=camera_id,
        track_id=track_id_1,
        vehicle_class="Auto",
        bbox=bbox_1,
        movement="INWARD"
    )
    assert logical_id_1 == track_id_1  # Brand new track, gets itself as logical ID
    
    # 2. Register inactive track (simulates track loss)
    linker.register_track_inactive(
        camera_id=camera_id,
        track_id=track_id_1,
        vehicle_class="Auto",
        last_bbox=bbox_1,
        movement="INWARD"
    )
    
    # 3. Register a new track close in space and time
    track_id_2 = 12
    bbox_2 = (110, 110, 210, 210)  # Very close to bbox_1
    
    logical_id_2 = linker.register_track_active(
        camera_id=camera_id,
        track_id=track_id_2,
        vehicle_class="Auto",
        bbox=bbox_2,
        movement="INWARD"
    )
    
    # Verify that track_id_2 maps to the logical ID of track_id_1
    assert logical_id_2 == logical_id_1
    assert linker.get_logical_id(camera_id, track_id_2) == logical_id_1
    assert linker.get_linked_track_ids(camera_id, logical_id_1) == {track_id_1, track_id_2}


def test_track_fragment_linker_no_link_due_to_gap():
    linker = TrackFragmentLinker(max_gap_seconds=0.01, max_distance=0.15)
    camera_id = "A1"
    
    linker.register_track_active(camera_id, 1, "Auto", (100, 100, 200, 200), "INWARD")
    linker.register_track_inactive(camera_id, 1, "Auto", (100, 100, 200, 200), "INWARD")
    
    # Sleep to exceed the 0.01s gap
    time.sleep(0.02)
    
    logical_id_2 = linker.register_track_active(camera_id, 2, "Auto", (105, 105, 205, 205), "INWARD")
    # Should NOT link
    assert logical_id_2 == 2


def test_room_detector_trajectory_inward_confirmed():
    # Define a single room zone (Room 101)
    # Polygon covers top-left quadrant
    zone = RoomZone(101, ((0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)))
    detector = RoomDetector("A1", [zone], probable_observations=2, confirmed_observations=4)
    
    # Ingress (INWARD) track: starts outside, moves inside, and ends inside
    track_id = 9
    
    # Observations 1 and 2: outside (corridor)
    detector.update(track_id=track_id, bbox=(600, 400, 700, 500), frame_width=1000, frame_height=1000) # center (650, 450) -> normalized (0.65, 0.45)
    detector.update(track_id=track_id, bbox=(550, 400, 650, 500), frame_width=1000, frame_height=1000)
    
    # Observations 3, 4, 5, 6: inside room zone (Room 101)
    detector.update(track_id=track_id, bbox=(100, 100, 200, 200), frame_width=1000, frame_height=1000) # normalized center (0.15, 0.15)
    detector.update(track_id=track_id, bbox=(100, 100, 200, 200), frame_width=1000, frame_height=1000)
    detector.update(track_id=track_id, bbox=(100, 100, 200, 200), frame_width=1000, frame_height=1000)
    
    # Active decision: since we have 3 observations inside (> 2), it should be PROBABLE or CONFIRMADA
    dec1 = detector.update(track_id=track_id, bbox=(100, 100, 200, 200), frame_width=1000, frame_height=1000)
    assert dec1.room_number == 101
    assert dec1.room_status in ("PROBABLE", "CONFIRMADA")
    
    # Finalized decision inside the room: must be CONFIRMADA
    dec_final = detector.decision(track_id=track_id, movement="INWARD", finalized=True)
    assert dec_final.room_number == 101
    assert dec_final.room_status == "CONFIRMADA"


def test_room_detector_passing_by_not_confirmed():
    zone = RoomZone(101, ((0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)))
    detector = RoomDetector("A1", [zone], probable_observations=2, confirmed_observations=4)
    track_id = 15
    
    # Enters room zone briefly
    detector.update(track_id=track_id, bbox=(100, 100, 200, 200), frame_width=1000, frame_height=1000)
    detector.update(track_id=track_id, bbox=(100, 100, 200, 200), frame_width=1000, frame_height=1000)
    detector.update(track_id=track_id, bbox=(100, 100, 200, 200), frame_width=1000, frame_height=1000)
    detector.update(track_id=track_id, bbox=(100, 100, 200, 200), frame_width=1000, frame_height=1000)
    
    # Then leaves it and continues down the corridor (passing by)
    detector.update(track_id=track_id, bbox=(700, 700, 800, 800), frame_width=1000, frame_height=1000) # Outside (0.75, 0.75)
    detector.update(track_id=track_id, bbox=(800, 800, 900, 900), frame_width=1000, frame_height=1000) # Outside
    
    # Decision must be NO_DETERMINADA or at least not CONFIRMADA because it went out
    dec = detector.decision(track_id=track_id, movement="INWARD", finalized=True)
    assert dec.room_status != "CONFIRMADA"


def test_room_detector_outward_origin_check():
    zone = RoomZone(102, ((0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)))
    detector = RoomDetector("A1", [zone], probable_observations=2, confirmed_observations=4)
    
    # Case A: Starts inside the room -> Valid outward
    track_id_valid = 21
    detector.update(track_id=track_id_valid, bbox=(100, 100, 200, 200), frame_width=1000, frame_height=1000)
    detector.update(track_id=track_id_valid, bbox=(100, 100, 200, 200), frame_width=1000, frame_height=1000)
    detector.update(track_id=track_id_valid, bbox=(100, 100, 200, 200), frame_width=1000, frame_height=1000)
    detector.update(track_id=track_id_valid, bbox=(100, 100, 200, 200), frame_width=1000, frame_height=1000)
    detector.update(track_id=track_id_valid, bbox=(700, 700, 800, 800), frame_width=1000, frame_height=1000) # Exiting
    
    dec_valid = detector.decision(track_id=track_id_valid, movement="OUTWARD", finalized=True)
    assert dec_valid.room_number == 102
    assert dec_valid.room_status == "CONFIRMADA"
    
    # Case B: Starts in corridor, never visited room first -> Invalid outward room assignment
    track_id_invalid = 22
    detector.update(track_id=track_id_invalid, bbox=(700, 700, 800, 800), frame_width=1000, frame_height=1000)
    detector.update(track_id=track_id_invalid, bbox=(100, 100, 200, 200), frame_width=1000, frame_height=1000)
    
    dec_invalid = detector.decision(track_id=track_id_invalid, movement="OUTWARD", finalized=True)
    assert dec_invalid.room_status == "NO_DETERMINADA"
    assert dec_invalid.room_number is None


def test_plate_voting_uniqueness_requirement():
    # 1. Single physical frame read 5 times (same frame_id)
    candidates_single_frame = [
        {"plate": "ABC1234", "confidence": 0.90, "frame_score": 1.0, "frame_id": "frame_100"},
        {"plate": "ABC1234", "confidence": 0.85, "frame_score": 1.0, "frame_id": "frame_100"},
        {"plate": "ABC1234", "confidence": 0.88, "frame_score": 1.0, "frame_id": "frame_100"},
    ]
    res_single = vote_plates_multiframe(candidates_single_frame)
    # Must not be LEIDA because it's only one physical frame!
    assert res_single["status"] == "PROBABLE"
    
    # 2. Two distinct physical frames (different frame_ids)
    candidates_multi_frame = [
        {"plate": "ABC1234", "confidence": 0.90, "frame_score": 1.0, "frame_id": "frame_100"},
        {"plate": "ABC1234", "confidence": 0.85, "frame_score": 1.0, "frame_id": "frame_101"},
    ]
    res_multi = vote_plates_multiframe(candidates_multi_frame)
    assert res_multi["status"] == "LEIDA"
    assert res_multi["plate"] == "ABC1234"


def test_three_crops_same_frame_never_leida():
    candidates = [
        {"plate": "PDA3463", "confidence": 0.95, "frame_id": "A5:track5:sequence812", "crop_id": "f812:full", "crop_type": "full"},
        {"plate": "PDA3463", "confidence": 0.93, "frame_id": "A5:track5:sequence812", "crop_id": "f812:lower", "crop_type": "lower_half"},
        {"plate": "PDA3463", "confidence": 0.91, "frame_id": "A5:track5:sequence812", "crop_id": "f812:center", "crop_type": "lower_center"},
    ]

    result = vote_plates_multiframe(candidates)

    assert result["status"] == "PROBABLE"
    assert result["unique_frames_supporting"] == 1
    assert "single_physical_frame" in result["review_reasons"]


def test_three_variants_same_crop_never_leida():
    candidates = [
        {"plate": "PDA3463", "confidence": 0.95, "frame_id": "frame1", "crop_id": "crop1", "variant_type": "original"},
        {"plate": "PDA3463", "confidence": 0.94, "frame_id": "frame1", "crop_id": "crop1", "variant_type": "clahe"},
        {"plate": "PDA3463", "confidence": 0.93, "frame_id": "frame1", "crop_id": "crop1", "variant_type": "otsu"},
    ]

    result = vote_plates_multiframe(candidates)

    assert result["status"] == "PROBABLE"
    assert result["supporting_frame_ids"] == ("frame1",)


def test_two_physical_frames_can_be_leida_and_frame_ids_preserved():
    result = vote_plates_multiframe([
        {"plate": "PDA3463", "confidence": 0.80, "frame_id": "frame1", "crop_id": "crop1"},
        {"plate": "PDA3463", "confidence": 0.78, "frame_id": "frame2", "crop_id": "crop2"},
    ])

    assert result["status"] == "LEIDA"
    assert result["supporting_frame_ids"] == ("frame1", "frame2")


def test_contradictory_candidates_trigger_review():
    result = vote_plates_multiframe([
        {"plate": "PDA3463", "confidence": 0.75, "frame_id": "frame1"},
        {"plate": "PDA3468", "confidence": 0.73, "frame_id": "frame2"},
    ])

    assert result["status"] != "LEIDA"
    assert result["review_required"] is True


def test_invalid_candidate_is_not_forced_to_plate():
    result = vote_plates_multiframe([
        {"plate": "PDA34", "confidence": 0.99, "frame_id": "frame1"},
        {"plate": "HOTEL", "confidence": 0.99, "frame_id": "frame2"},
    ])

    assert result["plate"] is None
    assert result["status"] == "NO_LEGIBLE"


def test_frame_quality_is_deterministic_and_buffer_keeps_distinct_frames():
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    frame[20:90, 30:130] = 180
    bbox = (30, 20, 130, 90)

    assert candidate_score(frame, bbox, 0.9) == candidate_score(frame, bbox, 0.9)

    buffer = TrackFrameBuffer(max_candidates=2)
    for sequence in [1, 2, 3]:
        buffer.add("A1", 7, frame, {"bbox": bbox, "confidence": 0.9, "source_sequence": sequence}, datetime.now(timezone.utc))

    best = buffer.best("A1", 7, limit=10)
    assert len(best) == 2
    assert len({candidate.frame_id for candidate in best}) == 2


def test_room_unknown_never_confirmed_and_one_observation_never_confirms():
    zone = RoomZone(1, ((0.1, 0.1), (0.5, 0.1), (0.5, 0.5), (0.1, 0.5)))
    detector = RoomDetector("A1", [zone], probable_observations=2, confirmed_observations=4)

    detector.update(track_id=1, bbox=(150, 100, 250, 200), frame_width=1000, frame_height=1000)
    assert detector.decision(1, movement="UNKNOWN", finalized=True).room_status != "CONFIRMADA"


def test_four_tangential_observations_do_not_confirm_room():
    zone = RoomZone(1, ((0.1, 0.1), (0.5, 0.1), (0.5, 0.5), (0.1, 0.5)))
    detector = RoomDetector("A1", [zone], probable_observations=2, confirmed_observations=4)
    for bbox in [
        (100, 410, 160, 490),
        (180, 410, 240, 490),
        (260, 410, 320, 490),
        (340, 410, 400, 490),
        (700, 700, 760, 780),
    ]:
        detector.update(track_id=2, bbox=bbox, frame_width=1000, frame_height=1000)

    decision = detector.decision(2, movement="INWARD", finalized=True)

    assert decision.room_status != "CONFIRMADA"
    assert decision.passed_in_front_only is True


def test_event_source_ambiguity_is_rejected():
    assert validate_hotel_event_source({"phase1_analyzer"}) == "phase1_analyzer"
    with pytest.raises(ValueError):
        validate_hotel_event_source({"phase1_analyzer", "virtual_line"})


def test_hotel_store_migration_is_idempotent_and_keeps_audit_columns(tmp_path):
    db_path = tmp_path / "hotel.db"
    store = HotelStore(str(db_path))
    store.close()
    store = HotelStore(str(db_path))
    try:
        columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(hotel_vehicle_events)").fetchall()}
        assert "logical_vehicle_id" in columns
        assert "plate_vote_margin" in columns
        assert "room_score_breakdown_json" in columns

        event = HotelVehicleEvent(
            event_id="ev-hardening",
            occurred_at=datetime.now(timezone.utc),
            camera_id="A1",
            camera_name="A1",
            track_id=1,
            movement="INWARD",
            movement_confidence=0.8,
            event_type="EVENTO_NO_RESUELTO",
            plate=None,
            plate_confidence=0.0,
            plate_status="NO_LEGIBLE",
            room_number=None,
            room_confidence=0.0,
            room_status="NO_DETERMINADA",
            vehicle_class="Auto",
            vehicle_confidence=0.9,
            evidence_path=None,
            review_required=True,
            logical_vehicle_id=10,
            plate_vote_margin=0.2,
            room_score_breakdown_json='{"room_score":0.0}',
        )
        store.save_event_and_update_visit(event)
        saved = store.get_event("ev-hardening")
        assert saved["logical_vehicle_id"] == 10
        assert saved["plate_vote_margin"] == 0.2
    finally:
        store.close()
