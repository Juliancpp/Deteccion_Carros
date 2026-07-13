#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import app.api as api
import app.config as config
import app.runtime as runtime
from app.hotel.models import HotelVehicleEvent
from app.hotel.store import HotelStore
from app.runtime import atomic_write_jpeg


def _testclient_available(timeout: float = 5.0) -> bool:
    probe = (
        "from fastapi.testclient import TestClient; "
        "from app.api import app; "
        "c=TestClient(app); "
        "r=c.get('/api/health'); "
        "print(r.status_code)"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return result.returncode == 0 and result.stdout.strip().endswith("200")
    except subprocess.TimeoutExpired:
        return False


def _event(event_id: str, evidence_path: str | None = None, clip_path: str | None = None) -> HotelVehicleEvent:
    event = HotelVehicleEvent(
        event_id=event_id,
        occurred_at=datetime.now(),
        camera_id="A1",
        camera_name="Entrada",
        track_id=1,
        movement="INWARD",
        movement_confidence=0.9,
        event_type="ENTRADA_NORMAL",
        plate="ABC1234",
        plate_confidence=0.91,
        plate_status="LEIDA",
        plate_candidates=[{"plate": "ABC1234", "confidence": 0.91}],
        room_number=101,
        room_confidence=0.8,
        room_status="CONFIRMADA",
        vehicle_class="Auto",
        vehicle_confidence=0.88,
        evidence_path=evidence_path,
        review_required=False,
        notes="",
    )
    event.clip_path = clip_path
    event.clip_status = "READY" if clip_path else "DISABLED"
    return event


def main() -> int:
    os.environ["DASHBOARD_AUTH_ENABLED"] = "true"
    os.environ["DASHBOARD_USERNAME"] = "admin"
    os.environ["DASHBOARD_PASSWORD"] = "offline-dashboard-secret"
    os.environ["DASHBOARD_SESSION_SECRET"] = "offline-dashboard-session-secret"
    os.environ["DASHBOARD_COOKIE_SECURE"] = "auto"

    with tempfile.TemporaryDirectory(prefix="deteccion_dashboard_") as tmp:
        base = Path(tmp)
        db_path = base / "hotel.db"
        captures = base / "captures"
        clips = base / "clips"
        runtime_dir = base / "runtime"
        captures.mkdir()
        clips.mkdir()
        runtime_dir.mkdir()

        old_db, old_capture, old_runtime = config.HOTEL_DATABASE_PATH, config.CAPTURE_DIR, runtime.RUNTIME_DIR
        config.HOTEL_DATABASE_PATH = str(db_path)
        config.CAPTURE_DIR = str(captures)
        runtime.RUNTIME_DIR = runtime_dir
        api.runtime_snapshot_path = runtime.runtime_snapshot_path
        api.snapshot_info = runtime.snapshot_info

        try:
            frame = np.zeros((48, 96, 3), dtype=np.uint8)
            frame[:, :] = (24, 90, 140)
            atomic_write_jpeg(runtime.runtime_snapshot_path("A1"), frame)
            atomic_write_jpeg(runtime.runtime_snapshot_path("A5"), frame)
            evidence_path = captures / "A1" / "2026-01-01" / "evidence.jpg"
            evidence_path.parent.mkdir(parents=True)
            atomic_write_jpeg(evidence_path, frame)
            clip_path = clips / "A1" / "2026-01-01" / "clip.mp4"
            clip_path.parent.mkdir(parents=True)
            clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42offline")

            store = HotelStore(str(db_path))
            store.save_event_and_update_visit(_event("evt-offline", str(evidence_path), str(clip_path)))
            store.close()

            response = api.Response()
            csrf = api._set_session_cookie(response, "admin")
            token = response.headers.getlist("set-cookie")[0].split("=", 1)[1].split(";", 1)[0]
            assert api._verify_session(token) == "admin"
            assert csrf
            assert api.health()["dashboard"] is True
            assert api.get_camera_snapshot("A1", authenticated=True).media_type == "image/jpeg"
            events = api.get_hotel_events(page=1, page_size=10, authenticated=True)
            assert events["total"] == 1
            detail = api.get_hotel_event_detail("evt-offline", authenticated=True)
            assert detail["event_id"] == "evt-offline"
            assert api._verify_session(token) == "admin"
            assert "app.detector" not in sys.modules
            assert "app.video.rtsp_stream" not in sys.modules

            print(json.dumps({
                "testclient_available": _testclient_available(),
                "login": True,
                "session": True,
                "snapshot": True,
                "events": events["total"],
                "protected_endpoints": True,
                "api_loaded_yolo": "app.detector" in sys.modules,
                "api_opened_rtsp": "app.video.rtsp_stream" in sys.modules,
            }, indent=2))
            return 0
        finally:
            config.HOTEL_DATABASE_PATH = old_db
            config.CAPTURE_DIR = old_capture
            runtime.RUNTIME_DIR = old_runtime


if __name__ == "__main__":
    raise SystemExit(main())
