import inspect

import numpy as np
import pytest

from app.tracking import bytetrack_adapter
from app.tracking.bytetrack_adapter import ByteTrackAdapter, ByteTrackUnavailableError


def detection(x1=10, y1=20, x2=100, y2=120, confidence=0.95):
    return {
        "class": "Auto",
        "class_id": 2,
        "confidence": confidence,
        "bbox": [x1, y1, x2, y2],
    }


def test_real_bytetrack_api_is_detectable():
    from ultralytics.trackers.byte_tracker import BYTETracker

    update_signature = inspect.signature(BYTETracker.update)

    assert "results" in update_signature.parameters
    assert "img" in update_signature.parameters


def test_real_backend_reports_ultralytics_bytetrack():
    tracker = ByteTrackAdapter("A1")
    tracker.ensure_ready()

    assert tracker.tracker_backend == "ultralytics-bytetrack"


def test_real_backend_output_schema_preserves_detection_fields():
    tracker = ByteTrackAdapter("A1")
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    tracked = tracker.update([detection()], frame=frame)

    assert len(tracked) == 1
    vehicle = tracked[0]
    assert {"id", "class", "class_id", "confidence", "bbox"}.issubset(vehicle)
    assert vehicle["id"] == 1
    assert vehicle["class"] == "Auto"
    assert vehicle["class_id"] == 2
    assert vehicle["confidence"] == pytest.approx(0.95)
    assert vehicle["bbox"] == [10, 20, 100, 120]


def test_two_trackers_are_distinct_and_can_reuse_numeric_ids():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    tracker_a1 = ByteTrackAdapter("A1")
    tracker_a5 = ByteTrackAdapter("A5")

    tracked_a1 = tracker_a1.update([detection()], frame=frame)
    tracked_a5 = tracker_a5.update([detection()], frame=frame)

    assert tracker_a1 is not tracker_a5
    assert tracker_a1._tracker is not tracker_a5._tracker
    assert tracked_a1[0]["id"] == 1
    assert tracked_a5[0]["id"] == 1


def test_reset_affects_only_selected_tracker():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    tracker_a1 = ByteTrackAdapter("A1")
    tracker_a5 = ByteTrackAdapter("A5")
    tracker_a1.update([detection()], frame=frame)
    tracker_a5.update([detection()], frame=frame)

    tracker_a1.reset()

    assert tracker_a1.get_active_ids() == []
    assert tracker_a5.get_active_ids() == [1]
    assert tracker_a1.reset_count == 1
    assert tracker_a5.reset_count == 0


def test_production_does_not_silently_fallback(monkeypatch):
    def fail_create():
        raise RuntimeError("missing bytetrack")

    monkeypatch.setattr(bytetrack_adapter, "_create_ultralytics_bytetrack", fail_create)

    tracker = ByteTrackAdapter("A1", allow_iou_fallback=False)
    with pytest.raises(ByteTrackUnavailableError):
        tracker.ensure_ready()

    assert tracker.tracker_backend == "uninitialized"


def test_iou_fallback_requires_explicit_opt_in(monkeypatch):
    def fail_create():
        raise RuntimeError("missing bytetrack")

    monkeypatch.setattr(bytetrack_adapter, "_create_ultralytics_bytetrack", fail_create)

    tracker = ByteTrackAdapter("A1", allow_iou_fallback=True)
    tracker.ensure_ready()
    tracked = tracker.update([detection()])

    assert tracker.tracker_backend == "iou-test-fallback"
    assert tracked[0]["id"] == 1
