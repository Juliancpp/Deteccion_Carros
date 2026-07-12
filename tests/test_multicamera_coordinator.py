import threading
import time
from datetime import datetime, timezone

from app.multicamera.coordinator import MultiCameraCoordinator
from app.settings import CameraSettings, DVRSettings
from app.video.rtsp_stream import LatestFrame, StreamStatus


class FakeFrame:
    def __init__(self, value=1):
        self.value = value
        self.shape = (720, 1280, 3)

    def copy(self):
        return FakeFrame(self.value)


class FakeStream:
    def __init__(self, camera_id, frames=None, connected=True):
        self.camera_id = camera_id
        self.frames = list(frames or [FakeFrame(1)])
        self.connected = connected
        self.started = False
        self.stopped = False
        self.sequence = 0
        self.reconnect_count = 0
        self.last_frame = None

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return True

    def get_latest_frame(self, after_sequence=None, copy_frame=True):
        if not self.connected or not self.frames:
            return None
        if self.last_frame is None or after_sequence == self.sequence:
            self.sequence += 1
            frame = self.frames[-1]
            self.last_frame = LatestFrame(frame.copy(), datetime.now(timezone.utc), self.sequence)
        if after_sequence is not None and self.last_frame.sequence <= after_sequence:
            return None
        frame = self.last_frame.frame.copy() if copy_frame else self.last_frame.frame
        return LatestFrame(frame, self.last_frame.captured_at, self.last_frame.sequence)

    def get_status(self):
        return StreamStatus(
            camera_id=self.camera_id,
            camera_name=f"Camara_{self.camera_id}",
            running=self.started and not self.stopped,
            connected=self.connected,
            frame_sequence=self.sequence,
            last_frame_at=datetime.now(timezone.utc) if self.sequence else None,
            frame_age_seconds=0.01 if self.sequence else None,
            capture_fps=25.0,
            width=1280,
            height=720,
            source_fps=25.0,
            connection_attempts=1,
            reconnect_count=self.reconnect_count,
            read_error_count=0,
            consecutive_read_failures=0,
            last_error=None,
        )

    def is_worker_alive(self):
        return False


class FakeDetector:
    instances = 0
    active_calls = 0
    max_concurrent = 0

    def __init__(self, delay=0.0, fail=False):
        type(self).instances += 1
        self.delay = delay
        self.fail = fail
        self.calls = []

    def detect(self, frame):
        type(self).active_calls += 1
        type(self).max_concurrent = max(type(self).max_concurrent, type(self).active_calls)
        try:
            if self.fail:
                raise RuntimeError("boom")
            if self.delay:
                time.sleep(self.delay)
            self.calls.append(frame.value)
            return [{"class": "Auto", "class_id": 2, "confidence": 0.9, "bbox": [10, 10, 40, 40]}]
        finally:
            type(self).active_calls -= 1

    def draw_detections(self, frame, detections):
        return frame.copy()


class FakeTracker:
    def __init__(self, camera_id):
        self.camera_id = camera_id
        self.calls = 0
        self.reset_count = 0
        self.active_ids = []

    def update(self, detections, frame_shape=None, timestamp=None, frame=None):
        del frame
        self.calls += 1
        self.active_ids = [5]
        return [{**detections[0], "id": 5}] if detections else []

    def reset(self):
        self.reset_count += 1
        self.active_ids = []

    def get_active_ids(self):
        return list(self.active_ids)


def dvr():
    return DVRSettings("192.168.0.100", 554, "detector", "secret")


def cameras():
    return [
        CameraSettings("A1", "Camara_A1", True, 101, 20.0),
        CameraSettings("A5", "Camara_A5", True, 501, 20.0),
    ]


def wait_for(predicate, timeout=1.0):
    end_at = time.monotonic() + timeout
    while time.monotonic() < end_at:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def make_coordinator(detector=None, streams=None, tracker_cls=FakeTracker, target_cameras=None):
    stream_map = streams or {}

    def stream_factory(_dvr, camera):
        return stream_map.get(camera.camera_id, FakeStream(camera.camera_id))

    return MultiCameraCoordinator(
        dvr(),
        target_cameras or cameras(),
        detector=detector or FakeDetector(),
        stream_factory=stream_factory,
        tracker_factory=lambda camera_id: tracker_cls(camera_id),
        idle_sleep_seconds=0.001,
        status_window_seconds=0.5,
    )


def test_single_detector_shared_by_two_cameras():
    FakeDetector.instances = 0
    detector = FakeDetector()
    coordinator = make_coordinator(detector=detector)

    assert coordinator.detector is detector
    assert coordinator.contexts[0].tracker is not coordinator.contexts[1].tracker
    assert FakeDetector.instances == 1


def test_scheduler_processes_both_cameras_round_robin():
    coordinator = make_coordinator()
    coordinator.start()
    assert wait_for(lambda: all(ctx.inference_count >= 1 for ctx in coordinator.contexts))
    coordinator.stop()

    counts = [ctx.inference_count for ctx in coordinator.contexts]
    assert min(counts) >= 1


def test_disconnected_camera_does_not_block_other_camera():
    streams = {"A1": FakeStream("A1", connected=False), "A5": FakeStream("A5", connected=True)}
    coordinator = make_coordinator(streams=streams)
    coordinator.start()
    assert wait_for(lambda: coordinator.contexts[1].inference_count >= 1)
    coordinator.stop()

    assert coordinator.contexts[0].inference_count == 0
    assert coordinator.contexts[1].inference_count >= 1


def test_no_reprocess_same_sequence():
    class StaticStream(FakeStream):
        def get_latest_frame(self, after_sequence=None, copy_frame=True):
            if self.last_frame is None:
                self.sequence = 1
                self.last_frame = LatestFrame(FakeFrame(1), datetime.now(timezone.utc), 1)
            if after_sequence is not None and self.last_frame.sequence <= after_sequence:
                return None
            return LatestFrame(self.last_frame.frame.copy(), self.last_frame.captured_at, self.last_frame.sequence)

    stream = StaticStream("A1", connected=True)
    cameras_one = [CameraSettings("A1", "Camara_A1", True, 101, 50.0)]
    coordinator = make_coordinator(streams={"A1": stream}, target_cameras=cameras_one)
    coordinator.start()
    assert wait_for(lambda: coordinator.contexts[0].inference_count >= 1)
    first_count = coordinator.contexts[0].inference_count
    time.sleep(0.05)
    coordinator.stop()

    assert coordinator.contexts[0].inference_count == first_count


def test_only_latest_frame_is_processed_and_skipped_count_increases():
    stream = FakeStream("A1", connected=True)
    stream.sequence = 10
    stream.last_frame = LatestFrame(FakeFrame(10), datetime.now(timezone.utc), 10)
    cameras_one = [CameraSettings("A1", "Camara_A1", True, 101, 50.0)]
    coordinator = make_coordinator(streams={"A1": stream}, target_cameras=cameras_one)
    coordinator.start()
    assert wait_for(lambda: coordinator.contexts[0].processed_sequence == 11)
    coordinator.stop()

    assert coordinator.contexts[0].skipped_frames >= 9


def test_inferences_are_not_concurrent():
    FakeDetector.max_concurrent = 0
    detector = FakeDetector(delay=0.01)
    coordinator = make_coordinator(detector=detector)
    coordinator.start()
    assert wait_for(lambda: sum(ctx.inference_count for ctx in coordinator.contexts) >= 2)
    coordinator.stop()

    assert FakeDetector.max_concurrent == 1


def test_yolo_error_does_not_kill_scheduler():
    detector = FakeDetector(fail=True)
    coordinator = make_coordinator(detector=detector)
    coordinator.start()
    assert wait_for(lambda: any(ctx.last_error for ctx in coordinator.contexts))
    assert coordinator.is_running()
    coordinator.stop()


def test_tracker_error_affects_only_one_camera():
    class SometimesFailTracker(FakeTracker):
        def update(self, detections, frame_shape=None, timestamp=None, frame=None):
            if self.camera_id == "A1":
                raise RuntimeError("tracker fail")
            return super().update(detections, frame_shape, timestamp, frame)

    coordinator = make_coordinator(tracker_cls=SometimesFailTracker)
    coordinator.start()
    assert wait_for(lambda: coordinator.contexts[1].inference_count >= 1)
    coordinator.stop()

    assert coordinator.contexts[0].last_error is not None or coordinator.contexts[0].inference_count >= 1
    assert coordinator.contexts[1].tracker.calls >= 1


def test_reconnect_resets_only_reconnected_tracker():
    streams = {"A1": FakeStream("A1"), "A5": FakeStream("A5")}
    coordinator = make_coordinator(streams=streams)
    coordinator.start()
    assert wait_for(lambda: all(ctx.inference_count >= 1 for ctx in coordinator.contexts))
    streams["A1"].reconnect_count += 1
    assert wait_for(lambda: coordinator.contexts[0].tracker.reset_count >= 1)
    coordinator.stop()

    assert coordinator.contexts[0].tracker.reset_count >= 1
    assert coordinator.contexts[1].tracker.reset_count <= coordinator.contexts[0].tracker.reset_count


def test_stop_is_idempotent_and_no_threads_alive():
    coordinator = make_coordinator()
    coordinator.start()
    assert coordinator.stop() is True
    assert coordinator.stop() is True
    assert coordinator.is_running() is False


def test_status_does_not_expose_secret():
    coordinator = make_coordinator()
    status = coordinator.get_status()

    assert "secret" not in str(status)


def test_same_numeric_ids_are_isolated_by_camera_result():
    coordinator = make_coordinator()
    coordinator.start()
    assert wait_for(lambda: all(ctx.last_result is not None for ctx in coordinator.contexts))
    a1 = coordinator.get_latest_result("A1")
    a5 = coordinator.get_latest_result("A5")
    coordinator.stop()

    assert a1.tracked_vehicles[0]["id"] == 5
    assert a5.tracked_vehicles[0]["id"] == 5
    assert a1.camera_id == "A1"
    assert a5.camera_id == "A5"


def test_output_schema_contains_required_fields():
    coordinator = make_coordinator()
    coordinator.start()
    assert wait_for(lambda: coordinator.get_latest_result("A1") is not None)
    result = coordinator.get_latest_result("A1")
    coordinator.stop()

    vehicle = result.tracked_vehicles[0]
    assert {"id", "class", "class_id", "confidence", "bbox"}.issubset(vehicle.keys())


def test_local_main_import_still_works():
    import app.config as config

    assert config.VIDEO_PATH.endswith("data/videos/test.mp4")
