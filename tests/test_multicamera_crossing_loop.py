import time
from datetime import datetime, timezone
import pytest
from app.multicamera.coordinator import MultiCameraCoordinator, NormalizedCrossing, _normalize_crossing
from app.multicamera.event_candidate import EventCandidate
from app.passage.corridor_detector import PassageCorridorDetector, PassageConfirmation
from app.line_detector import VirtualLineDetector, LineCrossing
from app.settings import CameraSettings, DVRSettings
from app.video.rtsp_stream import RTSPStream, StreamStatus

import cv2

class FakeFrame:
    def __init__(self, value=0):
        self.value = value
        self.shape = (100, 100, 3)

    def copy(self):
        return FakeFrame(self.value)

class FakeVideoCapture:
    def __init__(self, fps=25.0):
        self.fps = fps
        self.frame_num = 0
        self.released = False

    def isOpened(self):
        return not self.released

    def get(self, prop):
        if getattr(cv2, "CAP_PROP_FPS", None) == prop or prop == 5:
            return self.fps
        if getattr(cv2, "CAP_PROP_FRAME_WIDTH", None) == prop or prop == 3:
            return 100.0
        if getattr(cv2, "CAP_PROP_FRAME_HEIGHT", None) == prop or prop == 4:
            return 100.0
        return 0.0

    def read(self):
        if self.released:
            return False, None
        self.frame_num += 1
        if self.frame_num > 5:
            return False, None
        return True, FakeFrame(self.frame_num)

    def set(self, prop, val):
        if val == 0:
            self.frame_num = 0
        return True

    def release(self):
        self.released = True

def test_rtsp_stream_local_video_rate_limiting_and_looping():
    camera_settings = CameraSettings(
        camera_id="A1",
        name="LocalCam",
        enabled=True,
        channel=1,
        target_fps=25.0,
        video_path="data/videos/test.mp4",
        video_loop=True
    )
    dvr_settings = DVRSettings("localhost", 554, "user", "pass")
    
    cap = FakeVideoCapture(fps=50.0)
    
    # We pass a custom capture_factory that returns our FakeVideoCapture
    stream = RTSPStream(
        dvr_settings=dvr_settings,
        camera_settings=camera_settings,
        capture_factory=lambda url: cap,
        stale_timeout_seconds=0.5,
    )
    
    # Start stream
    stream.start()
    connected = stream.wait_until_connected(timeout=2.0)
    assert connected is True
    
    # Wait for the stream to read and loop at least once
    # 5 frames at 50fps = 100ms. With loop=True, loop_count should increment.
    time.sleep(0.3)
    
    status = stream.get_status()
    assert status.loop_count >= 1
    
    stream.stop()

def test_crossing_normalization_and_compatibility():
    # Test PassageConfirmation normalization
    passage_conf = PassageConfirmation(
        track_id=42,
        direction="ENTRADA",
        track_point=(50, 60),
        confirmed_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        progress=0.75,
        lateral_distance=0.05
    )
    norm_pass = _normalize_crossing(passage_conf)
    assert norm_pass.track_id == 42
    assert norm_pass.direction == "ENTRADA"
    assert norm_pass.track_point == (50, 60)
    assert norm_pass.occurred_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert norm_pass.corridor_progress == 0.75
    assert norm_pass.lateral_distance == 0.05

    # Test LineCrossing normalization
    line_crossing = LineCrossing(
        track_id=13,
        direction="SALIDA",
        previous_side=-1,
        current_side=1,
        center=(80, 90),
        crossed_at=datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc)
    )
    norm_line = _normalize_crossing(line_crossing)
    assert norm_line.track_id == 13
    assert norm_line.direction == "SALIDA"
    assert norm_line.track_point == (80, 90)
    assert norm_line.occurred_at == datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc)
    assert norm_line.corridor_progress is None
    assert norm_line.lateral_distance is None

def test_event_candidate_immutability_and_aliases():
    now = datetime.now(timezone.utc)
    frame = FakeFrame(99)
    event = EventCandidate(
        event_id="evt123",
        camera_id="camA",
        camera_name="Camara A",
        track_id=7,
        direction="ENTRADA",
        occurred_at=now,
        source_sequence=450,
        class_name="Auto",
        class_id=2,
        confidence=0.88,
        bbox=(10, 20, 30, 40),
        center=(20, 30),
        frame_width=640,
        frame_height=480,
        frame=frame
    )
    
    # Check that required immutable fields are populated automatically
    assert event.captured_at == now
    assert event.vehicle_class == "Auto"
    assert event.detection_confidence == 0.88
    
    # Check defensive copying of frame
    assert event.frame is not frame
    assert event.frame.value == 99

    # Check freezing
    with pytest.raises(Exception):
        event.captured_at = datetime.now(timezone.utc)

def test_coordinator_loop_reset_isolation():
    from app.multicamera.camera_context import CameraContext
    
    # 1. Setup Camera A1
    class MockTracker:
        def __init__(self):
            self.reset_called = False
        def reset(self):
            self.reset_called = True

    class MockLineDetector:
        def __init__(self):
            self.reset_called = False
        def reset(self):
            self.reset_called = True

    ctx_a1 = CameraContext(
        camera_id="A1",
        camera_name="Camara A1",
        target_fps=25.0,
        stream=None,
        tracker=MockTracker(),
        line_detector=MockLineDetector(),
        tracker_backend="mock"
    )
    # Populate some active states on A1
    ctx_a1.last_consumed_sequence = 10
    ctx_a1.active_track_ids = {5}
    ctx_a1.skipped_frames = 2
    ctx_a1.processed_sequence = 10
    ctx_a1.source_sequence = 10
    ctx_a1.last_loop_count = 0

    # 2. Setup Camera A5
    ctx_a5 = CameraContext(
        camera_id="A5",
        camera_name="Camara A5",
        target_fps=25.0,
        stream=None,
        tracker=MockTracker(),
        line_detector=MockLineDetector(),
        tracker_backend="mock"
    )
    # Populate some active states on A5
    ctx_a5.last_consumed_sequence = 20
    ctx_a5.active_track_ids = {12}
    ctx_a5.skipped_frames = 1
    ctx_a5.processed_sequence = 20
    ctx_a5.source_sequence = 20
    ctx_a5.last_loop_count = 0

    # 3. Create stream status for A1 with loop_count changed and frame_sequence at 0
    status_a1_loop = StreamStatus(
        camera_id="A1",
        camera_name="Camara A1",
        running=True,
        connected=True,
        frame_sequence=0,
        last_frame_at=None,
        frame_age_seconds=None,
        capture_fps=25.0,
        width=100,
        height=100,
        source_fps=25.0,
        connection_attempts=1,
        reconnect_count=0,
        read_error_count=0,
        consecutive_read_failures=0,
        last_error=None,
        loop_count=1  # Loop count changed from 0 to 1
    )

    # 4. Instantiate a coordinator
    dvr_settings = DVRSettings("localhost", 554, "user", "pass")
    coordinator = MultiCameraCoordinator(dvr_settings, [])

    # 5. Call handle reconnect for A1
    coordinator._handle_reconnect_if_needed(ctx_a1, status_a1_loop)

    # 6. Verify A1 state is reset
    assert ctx_a1.tracker.reset_called is True
    assert ctx_a1.line_detector.reset_called is True
    assert ctx_a1.last_consumed_sequence == 0
    assert len(ctx_a1.active_track_ids) == 0
    assert ctx_a1.skipped_frames == 0
    assert ctx_a1.processed_sequence == 0
    assert ctx_a1.source_sequence == 0
    assert ctx_a1.last_loop_count == 1

    # 7. Verify A5 is completely unaffected
    assert ctx_a5.tracker.reset_called is False
    assert ctx_a5.line_detector.reset_called is False
    assert ctx_a5.last_consumed_sequence == 20
    assert ctx_a5.active_track_ids == {12}
    assert ctx_a5.skipped_frames == 1
    assert ctx_a5.processed_sequence == 20
    assert ctx_a5.source_sequence == 20
    assert ctx_a5.last_loop_count == 0
