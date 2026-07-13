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


# ──── Pruebas para MultiCameraEventConsumer (Fase 3) ────

import numpy as np
from app.multicamera.event_candidate import EventCandidate
from app.multicamera.event_consumer import MultiCameraEventConsumer, MultiCameraEvidenceCapture
from app.settings import DVRSettings

class MockPlateReader:
    def __init__(self, should_fail=False, return_val=None, no_plate=False):
        self.should_fail = should_fail
        self.return_val = None if no_plate else (return_val or {"plate": "ABC1234", "confidence": 0.9})

    def read(self, img):
        if self.should_fail:
            raise RuntimeError("OCR Failed")
        return self.return_val


class MockEvidenceCapture:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail

    def capture(self, frame, vehicle, event):
        if self.should_fail:
            raise RuntimeError("Capture Failed")
        return f"/evidence/{event['camera_id']}/{event['vehicle_id']}.jpg"


class MockDBManager:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.saved = []

    def save_event(self, event):
        if self.should_fail:
            raise RuntimeError("DB Failed")
        self.saved.append(event)
        return len(self.saved)


def test_consumer_successful_event(tmp_path):
    coordinator = make_coordinator()
    consumer = MultiCameraEventConsumer(
        coordinator=coordinator,
        db_path=str(tmp_path / "test.db"),
        plate_reader=MockPlateReader(),
        evidence=MockEvidenceCapture()
    )
    consumer.db_manager = MockDBManager()

    img = np.ones((100, 100, 3), dtype=np.uint8)
    cand = EventCandidate(
        event_id="e1", camera_id="A1", camera_name="Camara A1", track_id=5,
        direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
        class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 90, 90),
        center=(50, 50), frame_width=100, frame_height=100, frame=img
    )

    consumer._process_candidate(cand)
    status = consumer.get_status()

    assert status.processed_events == 1
    assert status.failed_events == 0
    assert len(consumer.db_manager.saved) == 1
    assert consumer.db_manager.saved[0]["plate"] == "ABC1234"
    assert consumer.db_manager.saved[0]["image_path"] == "/evidence/A1/5.jpg"
    assert consumer.db_manager.saved[0]["camera"] == "A1"


def test_consumer_ocr_no_result(tmp_path):
    coordinator = make_coordinator()
    # Mock return_val=None (no plate detected)
    consumer = MultiCameraEventConsumer(
        coordinator=coordinator,
        db_path=str(tmp_path / "test.db"),
        plate_reader=MockPlateReader(no_plate=True),
        evidence=MockEvidenceCapture()
    )
    consumer.db_manager = MockDBManager()

    img = np.ones((100, 100, 3), dtype=np.uint8)
    cand = EventCandidate(
        event_id="e1", camera_id="A1", camera_name="Camara A1", track_id=5,
        direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
        class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 90, 90),
        center=(50, 50), frame_width=100, frame_height=100, frame=img
    )

    consumer._process_candidate(cand)
    status = consumer.get_status()

    assert status.processed_events == 1
    assert status.ocr_failures == 0  # No detection is not an exception failure
    assert consumer.db_manager.saved[0]["plate"] is None


def test_consumer_ocr_exception(tmp_path):
    coordinator = make_coordinator()
    consumer = MultiCameraEventConsumer(
        coordinator=coordinator,
        db_path=str(tmp_path / "test.db"),
        plate_reader=MockPlateReader(should_fail=True),
        evidence=MockEvidenceCapture()
    )
    consumer.db_manager = MockDBManager()

    img = np.ones((100, 100, 3), dtype=np.uint8)
    cand = EventCandidate(
        event_id="e1", camera_id="A1", camera_name="Camara A1", track_id=5,
        direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
        class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 90, 90),
        center=(50, 50), frame_width=100, frame_height=100, frame=img
    )

    consumer._process_candidate(cand)
    status = consumer.get_status()

    assert status.processed_events == 1
    assert status.ocr_failures == 1
    assert consumer.db_manager.saved[0]["plate"] is None  # still persisted


def test_consumer_evidence_failure(tmp_path):
    coordinator = make_coordinator()
    consumer = MultiCameraEventConsumer(
        coordinator=coordinator,
        db_path=str(tmp_path / "test.db"),
        plate_reader=MockPlateReader(),
        evidence=MockEvidenceCapture(should_fail=True)
    )
    consumer.db_manager = MockDBManager()

    img = np.ones((100, 100, 3), dtype=np.uint8)
    cand = EventCandidate(
        event_id="e1", camera_id="A1", camera_name="Camara A1", track_id=5,
        direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
        class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 90, 90),
        center=(50, 50), frame_width=100, frame_height=100, frame=img
    )

    consumer._process_candidate(cand)
    status = consumer.get_status()

    assert status.processed_events == 1
    assert status.evidence_failures == 1
    assert consumer.db_manager.saved[0]["image_path"] is None  # saved without image path


def test_consumer_database_failure(tmp_path):
    coordinator = make_coordinator()
    consumer = MultiCameraEventConsumer(
        coordinator=coordinator,
        db_path=str(tmp_path / "test.db"),
        plate_reader=MockPlateReader(),
        evidence=MockEvidenceCapture()
    )
    consumer.db_manager = MockDBManager(should_fail=True)

    img = np.ones((100, 100, 3), dtype=np.uint8)
    cand = EventCandidate(
        event_id="e1", camera_id="A1", camera_name="Camara A1", track_id=5,
        direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
        class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 90, 90),
        center=(50, 50), frame_width=100, frame_height=100, frame=img
    )

    consumer._process_candidate(cand)
    status = consumer.get_status()

    assert status.processed_events == 0
    assert status.failed_events == 1
    assert status.database_failures == 1


def test_consumer_invalid_event(tmp_path):
    coordinator = make_coordinator()
    consumer = MultiCameraEventConsumer(
        coordinator=coordinator,
        db_path=str(tmp_path / "test.db"),
        plate_reader=MockPlateReader(),
        evidence=MockEvidenceCapture()
    )
    consumer.db_manager = MockDBManager()

    # 1. frame is None
    cand_none = EventCandidate(
        event_id="e1", camera_id="A1", camera_name="Camara A1", track_id=5,
        direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
        class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 90, 90),
        center=(50, 50), frame_width=100, frame_height=100, frame=None
    )
    consumer._process_candidate(cand_none)

    # 2. frame is empty
    empty_img = np.array([], dtype=np.uint8)
    cand_empty = EventCandidate(
        event_id="e2", camera_id="A1", camera_name="Camara A1", track_id=5,
        direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
        class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 90, 90),
        center=(50, 50), frame_width=100, frame_height=100, frame=empty_img
    )
    consumer._process_candidate(cand_empty)

    # 3. bbox area is zero
    img = np.ones((100, 100, 3), dtype=np.uint8)
    cand_zero_bbox = EventCandidate(
        event_id="e3", camera_id="A1", camera_name="Camara A1", track_id=5,
        direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
        class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 10, 90),
        center=(50, 50), frame_width=100, frame_height=100, frame=img
    )
    consumer._process_candidate(cand_zero_bbox)

    # 4. bbox out of bounds (completely outside, e.g. cx1 == cx2 after clip)
    cand_out_bbox = EventCandidate(
        event_id="e4", camera_id="A1", camera_name="Camara A1", track_id=5,
        direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
        class_name="Auto", class_id=2, confidence=0.9, bbox=(150, 150, 180, 180),
        center=(50, 50), frame_width=100, frame_height=100, frame=img
    )
    consumer._process_candidate(cand_out_bbox)

    status = consumer.get_status()
    assert status.invalid_events == 4
    assert status.failed_events == 4
    assert len(consumer.db_manager.saved) == 0


def test_consumer_defensive_copy(tmp_path):
    coordinator = make_coordinator()
    consumer = MultiCameraEventConsumer(
        coordinator=coordinator,
        db_path=str(tmp_path / "test.db"),
        plate_reader=MockPlateReader(),
        evidence=MockEvidenceCapture()
    )
    consumer.db_manager = MockDBManager()

    # Create original frame and event candidate
    img = np.ones((100, 100, 3), dtype=np.uint8)
    cand = EventCandidate(
        event_id="e1", camera_id="A1", camera_name="Camara A1", track_id=5,
        direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
        class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 90, 90),
        center=(50, 50), frame_width=100, frame_height=100, frame=img
    )

    # Modify the original frame content AFTER creating candidate
    img[::] = 255

    # The consumer processes candidate.frame, which was defensibly copied
    assert cand.frame is not img
    assert np.all(cand.frame == 1)

    consumer._process_candidate(cand)
    assert consumer.get_status().processed_events == 1


def test_consumer_two_cameras_isolation(tmp_path):
    coordinator = make_coordinator()
    consumer = MultiCameraEventConsumer(
        coordinator=coordinator,
        db_path=str(tmp_path / "test.db"),
        plate_reader=MockPlateReader(),
        evidence=MockEvidenceCapture()
    )
    consumer.db_manager = MockDBManager()

    img = np.ones((100, 100, 3), dtype=np.uint8)
    cand_a1 = EventCandidate(
        event_id="e1", camera_id="A1", camera_name="Camara A1", track_id=5,
        direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
        class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 90, 90),
        center=(50, 50), frame_width=100, frame_height=100, frame=img
    )
    cand_a5 = EventCandidate(
        event_id="e2", camera_id="A5", camera_name="Camara A5", track_id=12,
        direction="SALIDA", occurred_at=datetime.now(timezone.utc), source_sequence=2,
        class_name="Moto", class_id=3, confidence=0.8, bbox=(10, 10, 90, 90),
        center=(50, 50), frame_width=100, frame_height=100, frame=img
    )

    consumer._process_candidate(cand_a1)
    consumer._process_candidate(cand_a5)

    saved = consumer.db_manager.saved
    assert len(saved) == 2
    assert saved[0]["camera"] == "A1"
    assert saved[0]["vehicle_id"] == 5
    assert saved[0]["image_path"] == "/evidence/A1/5.jpg"

    assert saved[1]["camera"] == "A5"
    assert saved[1]["vehicle_id"] == 12
    assert saved[1]["image_path"] == "/evidence/A5/12.jpg"


def test_consumer_stop_drain_true(tmp_path):
    coordinator = make_coordinator()
    consumer = MultiCameraEventConsumer(
        coordinator=coordinator,
        db_path=str(tmp_path / "test.db"),
        plate_reader=MockPlateReader(),
        evidence=MockEvidenceCapture()
    )
    consumer.db_manager = MockDBManager()

    img = np.ones((100, 100, 3), dtype=np.uint8)
    for i in range(3):
        cand = EventCandidate(
            event_id=f"e{i}", camera_id="A1", camera_name="Camara A1", track_id=i,
            direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
            class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 90, 90),
            center=(50, 50), frame_width=100, frame_height=100, frame=img
        )
        coordinator._event_queue.put(cand)

    consumer.start()
    assert consumer.is_running is True

    # Stop draining the queue
    consumer.stop(drain=True)
    assert consumer.is_running is False

    status = consumer.get_status()
    assert status.processed_events == 3
    assert status.queue_size == 0


def test_consumer_stop_drain_false(tmp_path):
    coordinator = make_coordinator()
    consumer = MultiCameraEventConsumer(
        coordinator=coordinator,
        db_path=str(tmp_path / "test.db"),
        plate_reader=MockPlateReader(),
        evidence=MockEvidenceCapture()
    )
    consumer.db_manager = MockDBManager()

    img = np.ones((100, 100, 3), dtype=np.uint8)
    for i in range(10):
        cand = EventCandidate(
            event_id=f"e{i}", camera_id="A1", camera_name="Camara A1", track_id=i,
            direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
            class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 90, 90),
            center=(50, 50), frame_width=100, frame_height=100, frame=img
        )
        coordinator._event_queue.put(cand)

    consumer.start()
    time.sleep(0.01)
    consumer.stop(drain=False)
    assert consumer.is_running is False

    status = consumer.get_status()
    # Should not have processed all 10 events
    assert status.processed_events < 10


def test_consumer_recovery(tmp_path):
    coordinator = make_coordinator()
    consumer = MultiCameraEventConsumer(
        coordinator=coordinator,
        db_path=str(tmp_path / "test.db"),
        plate_reader=MockPlateReader(),
        evidence=MockEvidenceCapture()
    )
    
    # DB manager fails first, then succeeds
    class RecoveryDBManager:
        def __init__(self):
            self.fail = True
            self.saved = []
        def save_event(self, event):
            if self.fail:
                self.fail = False
                raise RuntimeError("Temporary DB Error")
            self.saved.append(event)
            return len(self.saved)

    consumer.db_manager = RecoveryDBManager()

    img = np.ones((100, 100, 3), dtype=np.uint8)
    cand1 = EventCandidate(
        event_id="e1", camera_id="A1", camera_name="Camara A1", track_id=1,
        direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=1,
        class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 90, 90),
        center=(50, 50), frame_width=100, frame_height=100, frame=img
    )
    cand2 = EventCandidate(
        event_id="e2", camera_id="A1", camera_name="Camara A1", track_id=2,
        direction="ENTRADA", occurred_at=datetime.now(timezone.utc), source_sequence=2,
        class_name="Auto", class_id=2, confidence=0.9, bbox=(10, 10, 90, 90),
        center=(50, 50), frame_width=100, frame_height=100, frame=img
    )

    consumer._process_candidate(cand1)  # fails
    consumer._process_candidate(cand2)  # recovers and succeeds

    status = consumer.get_status()
    assert status.processed_events == 1
    assert status.failed_events == 1
    assert status.database_failures == 1
    assert len(consumer.db_manager.saved) == 1
