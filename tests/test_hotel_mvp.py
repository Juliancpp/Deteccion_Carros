from datetime import datetime, timezone

import numpy as np

from app.export.excel_exporter import HotelExcelExporter
from app.hotel.frame_buffer import TrackFrameBuffer
from app.hotel.gate_detector import DirectionalGateDetector
from app.hotel.models import HotelVehicleEvent
from app.hotel.plate_voting import vote_plate
from app.hotel.room_detector import RoomDetector, RoomZone
from app.hotel.store import HotelStore, classify_event


def event(event_type="ENTRADA_NORMAL", plate="PDA3463", camera_id="A1", movement="inward"):
    return HotelVehicleEvent(
        event_id=f"{event_type}-{camera_id}-{movement}",
        occurred_at=datetime.now(timezone.utc),
        camera_id=camera_id,
        camera_name=f"Camara_{camera_id}",
        track_id=1,
        movement=movement,
        movement_confidence=0.85,
        event_type=event_type,
        plate=plate,
        plate_confidence=0.8 if plate else 0.0,
        plate_status="LEIDA" if plate else "NO_LEGIBLE",
        room_number=1 if camera_id == "A1" else 4,
        room_confidence=0.7,
        room_status="PROBABLE",
        vehicle_class="Auto",
        vehicle_confidence=0.9,
        evidence_path=None,
        review_required=plate is None,
    )


def test_classification_rules_and_visit_lifecycle(tmp_path):
    store = HotelStore(str(tmp_path / "hotel.db"))
    try:
        assert classify_event("A1", "inward", "PDA3463", store)[0] == "ENTRADA_NORMAL"
        store.save_event_and_update_visit(event())
        assert store.count_open_visits() == 1
        assert classify_event("A5", "outward", "PDA3463", store)[0] == "SALIDA_NORMAL"
        store.save_event_and_update_visit(event("SALIDA_NORMAL", camera_id="A5", movement="outward"))
        assert store.count_open_visits() == 0
        assert classify_event("A1", "outward", "XXX0001", store)[0] == "SALIDA_SIN_ENTRADA"
        assert classify_event("A5", "inward", "ABC1234", store)[0] == "ENTRADA_POR_SALIDA"
    finally:
        store.close()


def test_unreadable_plate_saved_for_review(tmp_path):
    store = HotelStore(str(tmp_path / "hotel.db"))
    try:
        store.save_event_and_update_visit(event(plate=None))
        saved = store.list_events()[0]
        assert saved["review_required"] == 1
        assert saved["plate_status"] == "NO_LEGIBLE"
        assert store.count_open_visits() == 0
    finally:
        store.close()


def test_plate_voting_weighted_majority_and_low_confidence():
    result = vote_plate([
        {"plate": "PDA3463", "confidence": 0.81},
        {"plate": "PDA3463", "confidence": 0.74},
        {"plate": "PDA3468", "confidence": 0.48},
        {"plate": "PDA3463", "confidence": 0.67},
    ])
    assert result["plate"] == "PDA3463"

    low = vote_plate([{"plate": "ABC1234", "confidence": 0.2}], min_confidence=0.55)
    assert low["plate"] is None
    assert low["status"] == "NO_LEGIBLE"


def test_room_detector_requires_multiple_observations_and_camera_zones():
    detector = RoomDetector("A1", [RoomZone(1, ((0.1, 0.1), (0.5, 0.1), (0.5, 0.5), (0.1, 0.5)))])
    bbox = [20, 20, 30, 30]

    first = detector.update(track_id=1, bbox=bbox, frame_width=100, frame_height=100)
    second = detector.update(track_id=1, bbox=bbox, frame_width=100, frame_height=100)

    assert first.room_status == "NO_DETERMINADA"
    assert second.room_number == 1
    assert second.room_status == "PROBABLE"


def test_gate_detects_both_movements():
    gate = DirectionalGateDetector("A1", (0.2, 0.5), (0.8, 0.5), min_observations=2, min_delta=0.01)
    now = datetime.now(timezone.utc)
    assert gate.update(track_id=1, bbox=[45, 30, 55, 40], frame_width=100, frame_height=100, captured_at=now, source_sequence=1) is None
    inward = gate.update(track_id=1, bbox=[45, 60, 55, 70], frame_width=100, frame_height=100, captured_at=now, source_sequence=2)
    assert inward is not None
    assert inward.movement == "inward"

    gate2 = DirectionalGateDetector("A1", (0.2, 0.5), (0.8, 0.5), min_observations=2, min_delta=0.01)
    gate2.update(track_id=2, bbox=[45, 60, 55, 70], frame_width=100, frame_height=100, captured_at=now, source_sequence=1)
    outward = gate2.update(track_id=2, bbox=[45, 30, 55, 40], frame_width=100, frame_height=100, captured_at=now, source_sequence=2)
    assert outward.movement == "outward"


def test_frame_buffer_is_bounded():
    buffer = TrackFrameBuffer(max_candidates=3)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    vehicle = {"bbox": [10, 10, 40, 40], "confidence": 0.9}
    for _ in range(10):
        buffer.add("A1", 1, frame, vehicle, datetime.now(timezone.utc))
    assert len(buffer.best("A1", 1, limit=10)) == 3


def test_sqlite_and_excel_export(tmp_path):
    store = HotelStore(str(tmp_path / "hotel.db"))
    try:
        store.save_event_and_update_visit(event())
        path = HotelExcelExporter(store, str(tmp_path / "control.xlsx")).export()
        assert path.endswith("control.xlsx")
        assert (tmp_path / "control.xlsx").exists()
    finally:
        store.close()
