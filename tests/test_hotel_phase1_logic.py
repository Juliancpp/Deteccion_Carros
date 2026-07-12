from datetime import datetime, timezone

import numpy as np

from app.hotel.directional_corridor import DirectionalCorridor, bottom_center
from app.hotel.phase1_analyzer import HotelPhase1Analyzer
from app.settings import CameraCorridorSettings, CameraSettings, RoomZoneSettings


def test_directional_corridor_inward_outward_and_jitter():
    corridor = DirectionalCorridor("A1", ((0.5, 0.9), (0.5, 0.1)), half_width=0.12)
    now = datetime.now(timezone.utc)

    for y in [90, 75, 60]:
        result = corridor.update(track_id=1, point=(50, y), frame_width=100, frame_height=100, observed_at=now)
    assert result.movement == "INWARD"
    assert result.confidence > 0

    corridor = DirectionalCorridor("A1", ((0.5, 0.9), (0.5, 0.1)), half_width=0.12)
    for y in [60, 61, 60, 60]:
        result = corridor.update(track_id=2, point=(50, y), frame_width=100, frame_height=100, observed_at=now)
    assert result.movement == "UNKNOWN"

    corridor = DirectionalCorridor("A1", ((0.5, 0.9), (0.5, 0.1)), half_width=0.12)
    for y in [40, 55, 70]:
        result = corridor.update(track_id=3, point=(50, y), frame_width=100, frame_height=100, observed_at=now)
    assert result.movement == "OUTWARD"


def test_directional_corridor_inversion_changes_direction():
    corridor = DirectionalCorridor("A1", ((0.5, 0.9), (0.5, 0.1)), half_width=0.12, invert_direction=True)
    now = datetime.now(timezone.utc)

    for y in [90, 75, 60]:
        result = corridor.update(track_id=1, point=(50, y), frame_width=100, frame_height=100, observed_at=now)

    assert result.movement == "OUTWARD"


def test_projection_and_bottom_center():
    corridor = DirectionalCorridor("A1", ((0.1, 0.9), (0.5, 0.5), (0.9, 0.5)), half_width=0.2)
    progress, distance = corridor.project_normalized_point((0.7, 0.5))

    assert 0.6 < progress < 1.0
    assert distance == 0
    assert bottom_center([10, 10, 30, 40]) == (20, 40)


def camera(camera_id="A1"):
    rooms = (1, 2, 3) if camera_id == "A1" else (4, 5, 6)
    return CameraSettings(
        camera_id=camera_id,
        name=f"Camara_{camera_id}",
        enabled=True,
        channel=101 if camera_id == "A1" else 501,
        target_fps=5.0,
        corridor=CameraCorridorSettings(
            enabled=True,
            points=((0.5, 0.9), (0.5, 0.1)),
            half_width=0.15,
            invert_direction=False,
        ),
        rooms=tuple(
            RoomZoneSettings(room, ((0.35, 0.35), (0.65, 0.35), (0.65, 0.65), (0.35, 0.65)))
            for room in rooms[:1]
        ),
    )


def vehicle(track_id, y, room=True):
    x = 50 if room else 80
    return {
        "id": track_id,
        "class": "Auto",
        "class_id": 2,
        "confidence": 0.9,
        "bbox": [x - 5, y - 10, x + 5, y],
    }


def test_phase1_analyzer_probable_confirmed_and_frame_copy_only_on_changes():
    analyzer = HotelPhase1Analyzer(camera("A1"))
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    now = datetime.now(timezone.utc)

    results = []
    for y in [90, 60, 55, 55]:
        results.extend(
            analyzer.update(
                tracked_vehicles=[vehicle(1, y)],
                frame=frame,
                captured_at=now,
                frame_width=100,
                frame_height=100,
            )
        )

    latest = analyzer.latest_results()[-1]
    assert latest.movement == "INWARD"
    assert latest.room_number == 1
    assert latest.room_status in {"PROBABLE", "CONFIRMADA"}
    assert any(result.evidence_frame is not None for result in results)


def test_phase1_missing_grace_finalizes_after_delay():
    now_value = [0.0]
    analyzer = HotelPhase1Analyzer(camera("A1"), time_fn=lambda: now_value[0])
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    observed = datetime.now(timezone.utc)
    analyzer.update(tracked_vehicles=[vehicle(1, 90)], frame=frame, captured_at=observed, frame_width=100, frame_height=100)
    analyzer.update(tracked_vehicles=[vehicle(1, 60)], frame=frame, captured_at=observed, frame_width=100, frame_height=100)

    now_value[0] = 0.5
    assert not any(result.finalized for result in analyzer.update(tracked_vehicles=[], frame=frame, captured_at=observed, frame_width=100, frame_height=100))
    now_value[0] = 2.0
    finalized = analyzer.update(tracked_vehicles=[], frame=frame, captured_at=observed, frame_width=100, frame_height=100)

    assert any(result.finalized for result in finalized)


def test_phase1_camera_state_isolated_for_same_ids():
    a1 = HotelPhase1Analyzer(camera("A1"))
    a5 = HotelPhase1Analyzer(camera("A5"))
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    now = datetime.now(timezone.utc)

    a1.update(tracked_vehicles=[vehicle(5, 90)], frame=frame, captured_at=now, frame_width=100, frame_height=100)
    a5.update(tracked_vehicles=[vehicle(5, 40)], frame=frame, captured_at=now, frame_width=100, frame_height=100)
    a1.reset()

    assert a1.latest_results() == []
    assert a5.latest_results()
