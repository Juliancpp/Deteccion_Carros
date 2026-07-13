from __future__ import annotations

from datetime import datetime, timezone
import asyncio
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException, Response

from app.evidence import EvidenceCapture
from app.multicamera.event_candidate import EventCandidate
from app.multicamera.event_consumer import MultiCameraEventConsumer
from app.runtime import atomic_write_jpeg, runtime_snapshot_path, snapshot_info
from app.safety import safe_crop

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DummyCoordinator:
    def __init__(self):
        import queue

        self._event_queue = queue.Queue()

    def get_event(self, timeout=None):
        return None


def make_candidate(frame, bbox=(10, 10, 80, 80), event_id="evt"):
    now = datetime.now(timezone.utc)
    return EventCandidate(
        event_id=event_id,
        camera_id="A1",
        camera_name="Entrada",
        track_id=7,
        direction="INWARD",
        occurred_at=now,
        source_sequence=1,
        class_name="car",
        class_id=2,
        confidence=0.9,
        bbox=bbox,
        center=(30, 30),
        frame_width=100,
        frame_height=100,
        frame=frame,
        captured_at=now,
    )


def make_consumer(tmp_path):
    return MultiCameraEventConsumer(
        coordinator=DummyCoordinator(),
        db_path=None,
        plate_reader=object(),
        evidence=EvidenceCapture(enabled=True, capture_dir=str(tmp_path), padding=4),
        analyzers=None,
        frame_buffer=None,
        hotel_db_path=None,
    )


def test_safe_crop_rejects_bad_frames_and_bboxes():
    assert safe_crop(None, (0, 0, 1, 1)).reason == "FRAME_NONE"
    assert safe_crop(np.zeros((0, 0, 3), dtype=np.uint8), (0, 0, 1, 1)).reason == "FRAME_EMPTY"
    assert safe_crop(np.zeros((20, 20, 3), dtype=np.uint8), (30, 30, 40, 40)).reason == "BBOX_ZERO_AREA"
    assert safe_crop(np.zeros((20, 20, 3), dtype=np.uint8), (5, 5, 5, 10)).reason == "BBOX_ZERO_AREA"


def test_consumer_survives_invalid_event_and_processes_next(tmp_path):
    consumer = make_consumer(tmp_path)
    consumer._process_candidate(make_candidate(None, event_id="bad"))
    consumer._process_candidate(make_candidate(np.zeros((100, 100, 3), dtype=np.uint8), event_id="good"))
    status = consumer.get_status()
    assert status.failed_events == 1
    assert status.processed_events == 1
    assert status.consumer_alive is False


def test_consumer_survives_ocr_exception(tmp_path):
    class BrokenReader:
        def read(self, _image):
            raise RuntimeError("ocr boom")

    consumer = make_consumer(tmp_path)
    consumer.plate_reader = BrokenReader()
    consumer._process_candidate(make_candidate(np.zeros((100, 100, 3), dtype=np.uint8)))
    status = consumer.get_status()
    assert status.processed_events == 1
    assert status.ocr_failures == 1


def test_snapshot_atomic_write_and_status(monkeypatch, tmp_path):
    import app.runtime as runtime

    monkeypatch.setattr(runtime, "RUNTIME_DIR", tmp_path)
    frame = np.zeros((24, 32, 3), dtype=np.uint8)
    assert atomic_write_jpeg(runtime_snapshot_path("A1"), frame)
    assert not (tmp_path / "A1_latest.jpg.tmp").exists()
    info = snapshot_info("A1")
    assert info.available is True
    assert info.size_bytes > 0


def test_snapshot_endpoint_responses(monkeypatch, tmp_path):
    import app.runtime as runtime
    import app.api as api

    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "false")
    monkeypatch.setattr(runtime, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(api, "runtime_snapshot_path", runtime.runtime_snapshot_path)
    monkeypatch.setattr(api, "snapshot_info", runtime.snapshot_info)
    with pytest.raises(HTTPException) as missing:
        api.get_camera_snapshot("A1", authenticated=True)
    assert missing.value.status_code == 404
    assert atomic_write_jpeg(runtime_snapshot_path("A1"), np.zeros((20, 20, 3), dtype=np.uint8))
    ok = api.get_camera_snapshot("A1", authenticated=True)
    assert ok.media_type == "image/jpeg"
    assert "no-store" in ok.headers["cache-control"]
    with pytest.raises(HTTPException) as traversal:
        api.get_camera_snapshot("../../secret", authenticated=True)
    assert traversal.value.status_code == 400


def test_login_logout_cookie_and_csrf(monkeypatch):
    import app.api as api

    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "long-test-secret-value")
    monkeypatch.setenv("DASHBOARD_LOGIN_MAX_ATTEMPTS", "5")
    api._login_failures.clear()
    response = Response()
    csrf = api._set_session_cookie(response, "admin", None)
    set_cookie = "\n".join(response.headers.getlist("set-cookie")).lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "secure" not in set_cookie
    secure_response = Response()

    class HttpsRequest:
        headers = {"X-Forwarded-Proto": "https"}

    api._set_session_cookie(secure_response, "admin", HttpsRequest())
    assert "secure" in "\n".join(secure_response.headers.getlist("set-cookie")).lower()
    token = response.headers.getlist("set-cookie")[0].split("=", 1)[1].split(";", 1)[0]
    assert api._verify_session(token) == "admin"

    class Client:
        host = "testclient"

    class Request:
        method = "POST"
        client = Client()
        cookies = {api.SESSION_COOKIE: token, api.CSRF_COOKIE: csrf}
        headers = {}

    with pytest.raises(HTTPException) as blocked:
        api.check_auth(Request(), credentials=None)
    assert blocked.value.status_code == 403

    Request.headers = {"X-CSRF-Token": csrf}
    assert api.check_auth(Request(), credentials=None) is True


def test_login_lockout(monkeypatch):
    import app.api as api

    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "long-test-secret-value")
    monkeypatch.setenv("DASHBOARD_LOGIN_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("DASHBOARD_LOGIN_LOCK_SECONDS", "300")
    api._login_failures.clear()

    class Client:
        host = "lockout-client"

    class Request:
        client = Client()

        async def json(self):
            return {"username": "admin", "password": "bad"}

    async def run_attempts():
        first = await api.login(Request(), Response())
        second = await api.login(Request(), Response())
        third = await api.login(Request(), Response())
        return first.status_code, second.status_code, third.status_code

    assert asyncio.run(run_attempts()) == (401, 401, 429)


def test_consumer_handles_100_mock_events_with_periodic_failures(tmp_path):
    class Reader:
        def __init__(self):
            self.n = 0

        def read(self, _image):
            self.n += 1
            if self.n % 10 == 0:
                raise RuntimeError("ocr injected")
            return {"plate": "ABC1234", "confidence": 0.9}

    class Evidence(EvidenceCapture):
        def __init__(self):
            super().__init__(enabled=True, capture_dir=str(tmp_path))
            self.n = 0

        def capture(self, frame, vehicle, event):
            self.n += 1
            if self.n % 15 == 0:
                raise RuntimeError("evidence injected")
            return super().capture(frame, vehicle, event)

    class DB:
        def __init__(self):
            self.n = 0

        def save_event(self, _event):
            self.n += 1
            if self.n % 12 == 0:
                raise RuntimeError("database is locked")
            return self.n

    consumer = MultiCameraEventConsumer(
        coordinator=DummyCoordinator(),
        db_path=None,
        plate_reader=Reader(),
        evidence=Evidence(),
        analyzers=None,
        frame_buffer=None,
        hotel_db_path=None,
    )
    consumer.db_manager = DB()
    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    for i in range(100):
        bbox = (10, 10, 80, 80)
        event_frame = frame
        if i % 25 == 0:
            bbox = (10, 10, 10, 80)
        if i % 33 == 0:
            event_frame = None
        consumer._process_candidate(make_candidate(event_frame, bbox=bbox, event_id=f"evt-{i}"))

    status = consumer.get_status()
    assert status.processed_events > 0
    assert status.failed_events > 0
    assert status.ocr_failures > 0
    assert status.evidence_failures > 0
    assert status.database_failures > 0
    assert status.processed_events + status.failed_events == 100


def test_queue_task_done_always_for_consumer_loop(tmp_path):
    coordinator = DummyCoordinator()
    consumer = make_consumer(tmp_path)
    consumer.coordinator = coordinator
    coordinator._event_queue.put(make_candidate(np.zeros((40, 40, 3), dtype=np.uint8), bbox=(1, 1, 20, 20)))
    coordinator._event_queue.get_nowait()
    try:
        consumer._process_candidate_safely(make_candidate(None), None)
    finally:
        consumer._mark_queue_task_done()
    assert coordinator._event_queue.unfinished_tasks == 0


def test_health_safe_and_api_does_not_import_yolo_or_rtsp(monkeypatch):
    import app.api as api

    health = api.health()
    assert "password" not in str(health).lower()
    assert "rtsp://" not in str(health).lower()
    probe = subprocess.run(
        [
            str(PROJECT_ROOT / "venv/bin/python"),
            "-c",
            "import sys, app.api; print('app.detector' in sys.modules, 'app.video.rtsp_stream' in sys.modules)",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
    )
    assert probe.stdout.strip() == "False False"


def test_release_scripts_and_portable_paths():
    assert subprocess.run(["bash", "-n", "scripts/install_production.sh"], cwd=PROJECT_ROOT).returncode == 0
    assert subprocess.run(["bash", "-n", "scripts/build_private_deploy_bundle.sh"], cwd=PROJECT_ROOT).returncode == 0
    assert subprocess.run([str(PROJECT_ROOT / "venv/bin/python"), "scripts/check_portable_paths.py"], cwd=PROJECT_ROOT).returncode == 0
    ignored = subprocess.run(["git", "check-ignore", "-q", "dist/deteccion-carros-private.tar.gz"], cwd=PROJECT_ROOT)
    assert ignored.returncode == 0
