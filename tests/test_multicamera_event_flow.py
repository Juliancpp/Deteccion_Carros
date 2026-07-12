import time
from datetime import datetime, timezone

from app.multicamera.coordinator import MultiCameraCoordinator
from app.passage.corridor_detector import PassageCorridorDetector
from app.settings import CameraSettings, DVRSettings
from app.video.rtsp_stream import LatestFrame, StreamStatus


class FakeFrame:
    def __init__(self, value=0):
        self.value = value
        self.shape = (100, 100, 3)
        self.copy_count = 0

    def copy(self):
        copied = FakeFrame(self.value)
        self.copy_count += 1
        return copied


class MovingStream:
    def __init__(self, camera_id):
        self.camera_id = camera_id
        self.started = False
        self.stopped = False
        self.sequence = 0
        self.reconnect_count = 0
        self.connected = True

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return True

    def get_latest_frame(self, after_sequence=None, copy_frame=True):
        if not self.connected:
            return None
        if after_sequence is not None and self.sequence > after_sequence:
            return None
        self.sequence += 1
        frame = FakeFrame(self.sequence)
        return LatestFrame(frame.copy() if copy_frame else frame, datetime.now(timezone.utc), self.sequence)

    def get_status(self):
        return StreamStatus(
            camera_id=self.camera_id,
            camera_name=f"Camara_{self.camera_id}",
            running=self.started and not self.stopped,
            connected=self.connected,
            frame_sequence=self.sequence,
            last_frame_at=datetime.now(timezone.utc),
            frame_age_seconds=0.01,
            capture_fps=25.0,
            width=100,
            height=100,
            source_fps=25.0,
            connection_attempts=1,
            reconnect_count=self.reconnect_count,
            read_error_count=0,
            consecutive_read_failures=0,
            last_error=None,
        )


class CrossingDetector:
    def detect(self, frame):
        y2 = max(20, 90 - frame.value * 20)
        return [{"class": "Auto", "class_id": 2, "confidence": 0.9, "bbox": [45, y2 - 10, 55, y2]}]

    def draw_detections(self, frame, detections):
        return frame.copy()


class TwoCrossingDetector(CrossingDetector):
    def detect(self, frame):
        y2 = max(20, 90 - frame.value * 20)
        return [
            {"class": "Auto", "class_id": 2, "confidence": 0.9, "bbox": [45, y2 - 10, 55, y2]},
            {"class": "Moto", "class_id": 3, "confidence": 0.8, "bbox": [65, y2 - 10, 75, y2]},
        ]


class PassThroughTracker:
    def __init__(self, camera_id):
        self.camera_id = camera_id
        self.reset_count = 0
        self.tracker_backend = "fake"

    def update(self, detections, frame_shape=None, timestamp=None, frame=None):
        return [{**detection, "id": index + 1} for index, detection in enumerate(detections)]

    def reset(self):
        self.reset_count += 1

    def get_active_ids(self):
        return [1]


def dvr():
    return DVRSettings("192.168.0.100", 554, "detector", "secret")


def cameras():
    return [
        CameraSettings("A1", "Camara_A1", True, 101, 100.0),
        CameraSettings("A5", "Camara_A5", True, 501, 100.0),
    ]


def make_coordinator(detector=None, streams=None):
    stream_map = streams or {}

    def stream_factory(_dvr, camera):
        return stream_map.get(camera.camera_id, MovingStream(camera.camera_id))

    return MultiCameraCoordinator(
        dvr(),
        cameras(),
        detector=detector or CrossingDetector(),
        stream_factory=stream_factory,
        tracker_factory=lambda camera_id: PassThroughTracker(camera_id),
        line_detector_factory=lambda _camera: PassageCorridorDetector(
            points=((0.5, 0.85), (0.5, 0.15)),
            half_width=0.12,
            event_direction="ENTRADA",
            trigger_progress=0.30,
            min_observations=2,
            min_progress_delta=0.10,
        ),
        idle_sleep_seconds=0.001,
        status_window_seconds=0.5,
    )


def wait_for(predicate, timeout=1.0):
    end_at = time.monotonic() + timeout
    while time.monotonic() < end_at:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_event_candidate_is_published_from_crossing():
    coordinator = make_coordinator()
    coordinator.start()
    assert wait_for(lambda: coordinator.get_pending_event_count() >= 1)
    coordinator.stop()

    event = coordinator.get_event(timeout=0)
    assert event.camera_id in {"A1", "A5"}
    assert event.direction == "ENTRADA"
    assert event.class_name == "Auto"
    assert event.frame is not None
    assert event.track_point is not None
    assert event.corridor_progress is not None


def test_event_queue_is_bounded_and_drop_is_counted(monkeypatch):
    monkeypatch.setattr("app.config.EVENT_QUEUE_MAXSIZE", 1)
    coordinator = make_coordinator(detector=TwoCrossingDetector())
    coordinator.start()
    assert wait_for(lambda: coordinator.get_status().event_queue_dropped >= 1)
    coordinator.stop()

    status = coordinator.get_status()
    assert status.pending_events == 1
    assert status.event_queue_dropped >= 1


def test_reconnect_resets_only_that_camera_line_state():
    streams = {"A1": MovingStream("A1"), "A5": MovingStream("A5")}
    coordinator = make_coordinator(streams=streams)
    coordinator.start()
    assert wait_for(lambda: all(ctx.line_detector._states for ctx in coordinator.contexts))
    a1_resets_before = coordinator.contexts[0].tracker.reset_count
    a5_resets_before = coordinator.contexts[1].tracker.reset_count

    streams["A1"].reconnect_count += 1
    assert wait_for(lambda: coordinator.contexts[0].tracker.reset_count > a1_resets_before)
    coordinator.stop()

    assert coordinator.contexts[0].tracker.reset_count > a1_resets_before
    assert coordinator.contexts[1].tracker.reset_count == a5_resets_before
    assert coordinator.contexts[1].line_detector._states


def test_status_and_event_do_not_expose_secret():
    coordinator = make_coordinator()
    status = coordinator.get_status()

    assert "secret" not in str(status)
