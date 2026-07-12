import signal
import sys
import threading

from scripts import test_rtsp_streams


class FakeStatus:
    def __init__(self, camera_id="A1", frame_sequence=1):
        self.camera_id = camera_id
        self.connected = False
        self.frame_sequence = frame_sequence
        self.capture_fps = 0.0
        self.frame_age_seconds = None
        self.reconnect_count = 0
        self.read_error_count = 0


class FakeStream:
    def __init__(self, camera_id="A1", stop_result=True, alive=False, frames=1):
        self.status = FakeStatus(camera_id, frames)
        self.stop_calls = 0
        self.stop_result = stop_result
        self.alive = alive

    def stop(self):
        self.stop_calls += 1
        return self.stop_result

    def get_status(self):
        return self.status

    def is_worker_alive(self):
        return self.alive


def test_stop_streams_reports_success_once(capsys):
    streams = [FakeStream("A1"), FakeStream("A5")]

    assert test_rtsp_streams._stop_streams(streams) is True
    output = capsys.readouterr().out

    assert streams[0].stop_calls == 1
    assert streams[1].stop_calls == 1
    assert "[RTSP] A1 detenido correctamente" in output
    assert "[RTSP] A5 detenido correctamente" in output
    assert output.count("[RTSP] Todos los capturadores finalizaron") == 1


def test_stop_streams_returns_false_for_live_worker(capsys):
    streams = [FakeStream("A1", stop_result=False, alive=True)]

    assert test_rtsp_streams._stop_streams(streams) is False
    captured = capsys.readouterr()

    assert "ERROR: A1 no termino dentro del timeout" in captured.err
    assert "ERROR: A1 continua activo" in captured.err


def test_second_sigint_does_not_repeat_shutdown(monkeypatch, capsys):
    handlers = {}

    def fake_signal(signum, handler):
        handlers[signum] = handler

    monkeypatch.setattr(signal, "signal", fake_signal)
    event = test_rtsp_streams._install_signal_handlers()

    handlers[signal.SIGINT](signal.SIGINT, None)
    handlers[signal.SIGINT](signal.SIGINT, None)
    output = capsys.readouterr().out

    assert event.is_set()
    assert output.count("Interrupcion solicitada") == 1
    assert output.count("Cierre ya en progreso") == 1


def test_main_cleanup_runs_once_on_signal(monkeypatch):
    stop_calls = []
    event = threading.Event()

    class Args:
        camera = "all"
        duration = 60
        snapshot_dir = "unused"
        save_snapshots = False

    class Camera:
        enabled = True
        camera_id = "A1"

    class Settings:
        dvr = object()
        cameras = [Camera()]

    class Stream:
        def __init__(self, _dvr, _camera):
            self.status = FakeStatus("A1", 1)

        def start(self):
            event.set()

        def stop(self):
            stop_calls.append(1)
            return True

        def get_status(self):
            return self.status

        def is_worker_alive(self):
            return False

    monkeypatch.setattr(test_rtsp_streams, "parse_args", lambda: Args())
    monkeypatch.setattr(test_rtsp_streams, "load_settings", lambda override_env=True: Settings())
    monkeypatch.setattr(test_rtsp_streams, "RTSPStream", Stream)

    shutdown_event = threading.Event()

    def install_handlers():
        return shutdown_event

    monkeypatch.setattr(test_rtsp_streams, "_install_signal_handlers", install_handlers)

    def print_statuses(_streams):
        shutdown_event.set()

    monkeypatch.setattr(test_rtsp_streams, "_print_statuses", print_statuses)

    assert test_rtsp_streams.main() == 0
    assert stop_calls == [1]


def test_main_returns_nonzero_if_worker_does_not_stop(monkeypatch):
    class Args:
        camera = "all"
        duration = 0
        snapshot_dir = "unused"
        save_snapshots = False

    class Camera:
        enabled = True
        camera_id = "A1"

    class Settings:
        dvr = object()
        cameras = [Camera()]

    class Stream:
        def __init__(self, *_args):
            pass

        def start(self):
            pass

        def stop(self):
            return False

        def get_status(self):
            return FakeStatus("A1", 1)

        def is_worker_alive(self):
            return True

    monkeypatch.setattr(test_rtsp_streams, "parse_args", lambda: Args())
    monkeypatch.setattr(test_rtsp_streams, "load_settings", lambda override_env=True: Settings())
    monkeypatch.setattr(test_rtsp_streams, "RTSPStream", Stream)
    monkeypatch.setattr(test_rtsp_streams, "_install_signal_handlers", threading.Event)

    assert test_rtsp_streams.main() == 1
