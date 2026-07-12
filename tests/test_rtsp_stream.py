import time
import threading

from app.settings import CameraSettings, DVRSettings
from app.video.rtsp_stream import RTSPStream


class FakeCapture:
    def __init__(self, frames=None, opened=True, read_delay=0.0, error_on_read=None):
        self.frames = list(frames or [])
        self.opened = opened
        self.read_delay = read_delay
        self.error_on_read = error_on_read
        self.read_calls = 0
        self.released = False

    def isOpened(self):
        return self.opened

    def read(self):
        self.read_calls += 1
        if self.read_delay:
            time.sleep(self.read_delay)
        if self.error_on_read is not None:
            raise self.error_on_read
        if self.frames:
            return True, self.frames.pop(0)
        return False, None

    def release(self):
        self.released = True
        self.opened = False

    def get(self, _prop):
        return 0


class ThreadTrackingCapture:
    def __init__(self, frames=None, opened=True, block_until_stop=False, error_on_read=None):
        self.frames = list(frames or [])
        self.opened = opened
        self.block_until_stop = block_until_stop
        self.error_on_read = error_on_read
        self.created_thread = threading.get_ident()
        self.read_threads = []
        self.release_threads = []
        self.release_calls = 0
        self.stop_read = threading.Event()

    def isOpened(self):
        return self.opened

    def read(self):
        self.read_threads.append(threading.get_ident())
        if self.block_until_stop:
            self.stop_read.wait(0.02)
        if self.error_on_read is not None:
            raise self.error_on_read
        if self.frames:
            return True, self.frames.pop(0)
        return False, None

    def release(self):
        self.release_threads.append(threading.get_ident())
        self.release_calls += 1
        self.opened = False
        self.stop_read.set()

    def get(self, _prop):
        return 0


class FakeFrame:
    def __init__(self, value):
        self.value = value

    def copy(self):
        return FakeFrame(self.value)


def dvr(password="secret"):
    return DVRSettings(
        host="192.168.0.100",
        rtsp_port=554,
        username="detector",
        password=password,
    )


def camera():
    return CameraSettings(
        camera_id="A1",
        name="Camara_A1",
        enabled=True,
        channel=101,
        target_fps=5.0,
    )


def frame(value):
    return FakeFrame(value)


def make_stream(factory, **overrides):
    defaults = {
        "stale_timeout_seconds": 0.05,
        "reconnect_initial_seconds": 0.01,
        "reconnect_max_seconds": 0.02,
        "max_consecutive_read_failures": 2,
        "stop_join_timeout_seconds": 0.5,
    }
    defaults.update(overrides)
    return RTSPStream(dvr(), camera(), capture_factory=factory, **defaults)


def wait_for(predicate, timeout=0.5):
    end_at = time.monotonic() + timeout
    while time.monotonic() < end_at:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_initial_status_disconnected():
    stream = make_stream(lambda _url: FakeCapture(opened=False))

    status = stream.get_status()

    assert status.running is False
    assert status.connected is False
    assert status.frame_sequence == 0
    assert stream.get_latest_frame() is None


def test_start_does_not_start_two_threads():
    calls = []

    def factory(_url):
        calls.append(1)
        return FakeCapture(frames=[frame(1)], read_delay=0.01)

    stream = make_stream(factory)
    stream.start()
    first_thread = stream._thread
    stream.start()

    assert stream._thread is first_thread
    stream.stop()


def test_receives_frame_and_sequence_increases():
    stream = make_stream(lambda _url: FakeCapture(frames=[frame(7)], read_delay=0.01))

    stream.start()
    assert wait_for(lambda: stream.get_latest_frame() is not None)
    latest = stream.get_latest_frame()
    stream.stop()

    assert latest is not None
    assert latest.sequence >= 1
    assert latest.frame.value == 7


def test_latest_frame_replaces_previous_frame():
    stream = make_stream(lambda _url: FakeCapture(frames=[frame(1), frame(2)], read_delay=0.005))

    stream.start()
    assert wait_for(lambda: stream.get_status().frame_sequence >= 2)
    latest = stream.get_latest_frame()
    stream.stop()

    assert latest is not None
    assert latest.sequence >= 2
    assert latest.frame.value == 2


def test_after_sequence_avoids_reprocessing_same_frame():
    stream = make_stream(lambda _url: FakeCapture(frames=[frame(3)], read_delay=0.01))

    stream.start()
    assert wait_for(lambda: stream.get_latest_frame() is not None)
    latest = stream.get_latest_frame()
    repeated = stream.get_latest_frame(after_sequence=latest.sequence)
    stream.stop()

    assert repeated is None


def test_stop_is_idempotent():
    stream = make_stream(lambda _url: FakeCapture(frames=[frame(1)], read_delay=0.01))

    stream.start()
    stream.stop()
    stream.stop()

    assert stream.is_running() is False
    assert stream.is_worker_alive() is False


def test_open_failure_increments_attempts_and_sets_error():
    stream = make_stream(lambda _url: FakeCapture(opened=False))

    stream.start()
    assert wait_for(lambda: stream.get_status().connection_attempts >= 2)
    status = stream.get_status()
    stream.stop()

    assert status.connected is False
    assert status.connection_attempts >= 2
    assert status.last_error


def test_consecutive_read_failures_increment_counts():
    stream = make_stream(lambda _url: FakeCapture(frames=[]))

    stream.start()
    assert wait_for(lambda: stream.get_status().read_error_count >= 2)
    status = stream.get_status()
    stream.stop()

    assert status.read_error_count >= 2
    assert status.consecutive_read_failures >= 2 or status.connected is False


def test_reconnect_after_read_failure_receives_frame():
    captures = [FakeCapture(frames=[]), FakeCapture(frames=[frame(9)], read_delay=0.005)]

    def factory(_url):
        return captures.pop(0) if captures else FakeCapture(frames=[frame(9)], read_delay=0.01)

    stream = make_stream(factory)
    stream.start()
    assert wait_for(lambda: stream.get_status().frame_sequence >= 1, timeout=1.0)
    status = stream.get_status()
    latest = stream.get_latest_frame()
    stream.stop()

    assert status.connection_attempts >= 2
    assert status.reconnect_count >= 1
    assert latest is not None
    assert latest.frame.value == 9


def test_stale_frame_is_cleared_on_disconnect():
    stream = make_stream(lambda _url: FakeCapture(frames=[frame(4)]))

    stream.start()
    assert wait_for(lambda: stream.get_status().frame_sequence >= 1)
    assert wait_for(lambda: stream.get_status().connected is False, timeout=1.0)
    latest = stream.get_latest_frame()
    stream.stop()

    assert latest is None


def test_status_and_repr_do_not_expose_password():
    password = "secret@:/#%"

    def factory(_url):
        raise RuntimeError(f"fallo con {_url}")

    stream = RTSPStream(
        dvr(password=password),
        camera(),
        capture_factory=factory,
        reconnect_initial_seconds=0.01,
        reconnect_max_seconds=0.02,
    )
    stream.start()
    assert wait_for(lambda: stream.get_status().last_error is not None)
    status = stream.get_status()
    stream.stop()

    assert password not in repr(stream)
    assert password not in status.last_error
    assert "secret" not in status.last_error


def test_status_does_not_expose_encoded_password():
    password = "@:/#%"

    def factory(_url):
        raise RuntimeError(f"fallo con {_url}")

    stream = RTSPStream(
        dvr(password=password),
        camera(),
        capture_factory=factory,
        reconnect_initial_seconds=0.01,
        reconnect_max_seconds=0.02,
    )
    stream.start()
    assert wait_for(lambda: stream.get_status().last_error is not None)
    status = stream.get_status()
    stream.stop()

    assert "%40%3A%2F%23%25" not in str(status)
    assert password not in str(status)


def test_logs_do_not_expose_password(caplog):
    password = "@:/#%"

    def factory(_url):
        raise RuntimeError(f"fallo con {_url}")

    stream = RTSPStream(
        dvr(password=password),
        camera(),
        capture_factory=factory,
        reconnect_initial_seconds=0.01,
        reconnect_max_seconds=0.02,
    )

    with caplog.at_level("WARNING", logger="app.video.rtsp_stream"):
        stream.start()
        assert wait_for(lambda: stream.get_status().last_error is not None)
        stream.stop()

    log_text = caplog.text
    assert password not in log_text
    assert "%40%3A%2F%23%25" not in log_text


def test_backoff_does_not_exceed_maximum():
    stream = make_stream(lambda _url: FakeCapture(opened=False), reconnect_max_seconds=0.03)

    delay = 0.03
    for _ in range(4):
        delay = stream._wait_before_reconnect(delay)

    assert delay == 0.03


def test_reconnect_wait_is_interrupted_by_stop():
    stream = make_stream(
        lambda _url: FakeCapture(opened=False),
        reconnect_initial_seconds=1.0,
        reconnect_max_seconds=1.0,
    )

    stream.start()
    assert wait_for(lambda: stream.get_status().connection_attempts >= 1)
    started = time.monotonic()
    stream.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5


def test_worker_creates_reads_and_releases_capture_in_same_thread():
    captures = []

    def factory(_url):
        capture = ThreadTrackingCapture(frames=[frame(1)])
        captures.append(capture)
        return capture

    stream = make_stream(factory)
    stream.start()
    assert wait_for(lambda: captures and captures[0].read_threads)
    assert stream.stop() is True

    capture = captures[0]
    assert capture.created_thread != threading.get_ident()
    assert capture.read_threads
    assert capture.release_calls == 1
    assert capture.release_threads == [capture.created_thread]
    assert all(thread_id == capture.created_thread for thread_id in capture.read_threads)


def test_stop_does_not_release_from_main_thread_while_reading():
    captures = []

    def factory(_url):
        capture = ThreadTrackingCapture(frames=[frame(1)], block_until_stop=True)
        captures.append(capture)
        return capture

    stream = make_stream(factory, stale_timeout_seconds=0.2)
    stream.start()
    assert wait_for(lambda: captures and captures[0].read_threads)
    main_thread = threading.get_ident()
    assert stream.stop() is True

    assert captures[0].release_calls == 1
    assert main_thread not in captures[0].release_threads


def test_start_immediate_stop_sets_stopped_event():
    stream = make_stream(lambda _url: ThreadTrackingCapture(frames=[frame(1)], block_until_stop=True))

    stream.start()
    assert stream.stop() is True

    assert stream.wait_until_stopped(0.1) is True
    assert stream.is_running() is False
    assert stream.is_worker_alive() is False
    assert stream.get_status().connected is False


def test_two_streams_close_without_live_workers():
    captures = []

    def factory(_url):
        capture = ThreadTrackingCapture(frames=[frame(1)], block_until_stop=True)
        captures.append(capture)
        return capture

    streams = [
        make_stream(factory, stop_join_timeout_seconds=1.0),
        RTSPStream(dvr(), CameraSettings("A5", "Camara_A5", True, 501, 5.0), capture_factory=factory),
    ]
    for stream in streams:
        stream.start()
    assert wait_for(lambda: len(captures) == 2 and all(capture.read_threads for capture in captures))

    results = []
    threads = [threading.Thread(target=lambda s=stream: results.append(s.stop())) for stream in streams]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1.0)

    assert results == [True, True] or sorted(results) == [True, True]
    assert all(not stream.is_worker_alive() for stream in streams)
    assert all(capture.release_calls == 1 for capture in captures)


def test_unopened_capture_is_released_once():
    captures = []

    def factory(_url):
        capture = ThreadTrackingCapture(opened=False)
        captures.append(capture)
        return capture

    stream = make_stream(factory)
    stream.start()
    assert wait_for(lambda: captures and captures[0].release_calls == 1)
    stream.stop()

    assert captures[0].release_calls == 1


def test_capture_raising_during_read_is_released_once():
    captures = []

    def factory(_url):
        capture = ThreadTrackingCapture(error_on_read=RuntimeError("read failed"))
        captures.append(capture)
        return capture

    stream = make_stream(factory)
    stream.start()
    assert wait_for(lambda: captures and captures[0].release_calls == 1)
    stream.stop()

    assert captures[0].release_calls == 1


def test_reconnect_releases_previous_capture_before_next_capture():
    captures = []

    def factory(_url):
        capture = ThreadTrackingCapture(frames=[] if not captures else [frame(2)])
        captures.append(capture)
        return capture

    stream = make_stream(factory)
    stream.start()
    assert wait_for(lambda: len(captures) >= 2, timeout=1.0)
    stream.stop()

    assert captures[0].release_calls == 1
    assert captures[1].created_thread == captures[0].created_thread


def test_stop_returns_false_if_worker_does_not_finish():
    captures = []

    class BlockingCapture(ThreadTrackingCapture):
        def read(self):
            self.read_threads.append(threading.get_ident())
            time.sleep(0.2)
            return True, frame(1)

    def factory(_url):
        capture = BlockingCapture()
        captures.append(capture)
        return capture

    stream = make_stream(factory, stop_join_timeout_seconds=0.01)
    stream.start()
    assert wait_for(lambda: captures and captures[0].read_threads)

    assert stream.stop() is False
    assert stream.is_worker_alive() is True

    captures[0].stop_read.set()
    assert stream.wait_until_stopped(0.5) is True
