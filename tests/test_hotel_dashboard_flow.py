import os
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import numpy as np
import pytest

from app.media.clip_buffer import CameraClipBuffer
from app.media.clip_manager import ClipManager
from app.hotel.store import HotelStore
from app.hotel.models import HotelVehicleEvent
from app.api import (
    get_hotel_summary,
    get_hotel_events,
    get_hotel_event_detail,
    patch_hotel_event,
    post_rebuild_visits,
    _verify_path_safe
)

@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "hotel_test.db"
    store = HotelStore(str(db_file))
    yield store
    store.close()

def test_camera_clip_buffer_rate_limiting_and_selection():
    # 5 FPS buffer
    buf = CameraClipBuffer(sample_fps=5, buffer_seconds=1)
    
    # 1. Test frame addition spacing
    now_dt = datetime.now()
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    
    # First frame added
    added = buf.add_frame(now_dt, 1, frame)
    assert added is True
    
    # Immediate second frame added (should be skipped due to 5 FPS limit = 0.2s interval)
    added = buf.add_frame(now_dt, 2, frame)
    assert added is False
    
    # Simulate time passed
    buf.last_inserted_mono_time -= 0.25
    added = buf.add_frame(now_dt + timedelta(seconds=0.25), 3, frame)
    assert added is True

    # 2. Test range selection
    anchor = now_dt + timedelta(seconds=2)
    
    # Reset deque and add mock sequence
    buf.clear()
    buf.last_inserted_mono_time = 0.0
    
    for i in range(10):
        t_item = now_dt + timedelta(seconds=i * 0.2)
        buf.add_frame(t_item, i, frame)
        buf.last_inserted_mono_time = 0.0 # reset rate limit check
        
    # Range: [0.5s, 1.5s] -> should select items around that range
    selected = buf.get_frames_in_range(now_dt + timedelta(seconds=1.0), pre_seconds=0.5, post_seconds=0.5)
    assert len(selected) > 0
    for s in selected:
        assert now_dt + timedelta(seconds=0.5) <= s["captured_at"] <= now_dt + timedelta(seconds=1.5)


def test_path_traversal_protection(tmp_path):
    safe_base = tmp_path / "safe"
    safe_base.mkdir()
    
    safe_file = safe_base / "photo.jpg"
    safe_file.write_text("ok")
    
    unsafe_file = tmp_path / "secret.txt"
    unsafe_file.write_text("secret")
    
    # Verification function should allow safe file
    _verify_path_safe(safe_base, safe_file)
    
    # Verification function should raise HTTPException (403) on path traversal
    with pytest.raises(Exception) as excinfo:
        _verify_path_safe(safe_base, unsafe_file)
    assert "403" in str(excinfo.value) or "Access denied" in str(excinfo.value) or "Path traversal" in str(excinfo.value)


def test_api_endpoints_and_manual_correction(tmp_path):
    db_file = tmp_path / "hotel_test.db"
    store = HotelStore(str(db_file))
    
    # Insert test events
    now_dt = datetime.now()
    
    e1 = HotelVehicleEvent(
        event_id="evt-1",
        occurred_at=now_dt - timedelta(minutes=10),
        camera_id="A1",
        camera_name="Entrada",
        track_id=10,
        movement="inward",
        movement_confidence=0.9,
        event_type="ENTRADA_NORMAL",
        plate="ABC1234",
        plate_confidence=0.95,
        plate_status="LEIDA",
        plate_candidates=[{"plate": "ABC1234", "confidence": 0.95}],
        room_number=102,
        room_confidence=0.8,
        room_status="CONFIRMADA",
        vehicle_class="Auto",
        vehicle_confidence=0.99,
        evidence_path=None,
        review_required=False,
        notes=""
    )
    store.save_event_and_update_visit(e1)
    
    e2 = HotelVehicleEvent(
        event_id="evt-2",
        occurred_at=now_dt - timedelta(minutes=5),
        camera_id="A5",
        camera_name="Salida",
        track_id=11,
        movement="outward",
        movement_confidence=0.85,
        event_type="SALIDA_NORMAL",
        plate="ABC1234",
        plate_confidence=0.90,
        plate_status="LEIDA",
        plate_candidates=[{"plate": "ABC1234", "confidence": 0.90}],
        room_number=102,
        room_confidence=0.8,
        room_status="CONFIRMADA",
        vehicle_class="Auto",
        vehicle_confidence=0.99,
        evidence_path=None,
        review_required=False,
        notes=""
    )
    store.save_event_and_update_visit(e2)
    
    # Point the config to our test DB file
    import app.config as config
    original_db_path = config.HOTEL_DATABASE_PATH
    config.HOTEL_DATABASE_PATH = str(db_file)
    
    try:
        from app import administration
        from app.api import PlatePayload
        from fastapi import HTTPException
        administration.migrate()
        administration.create_user('reviewer', 'test-password-long', 'operator')
        user = {'id': 1, 'username': 'reviewer', 'role': 'operator'}
        # 1. Test Summary stats
        data = get_hotel_summary(authenticated=True)
        assert data["entradas_hoy"] == 1
        assert data["salidas_hoy"] == 1
        assert data["visits_open"] == 0 # Closed by exit normal
        
        # 2. Test events filtering
        events_data = get_hotel_events(plate="ABC1234", page=1, page_size=10, authenticated=True)
        assert len(events_data["events"]) == 2
        
        # 3. Test single event detail
        event_detail = get_hotel_event_detail("evt-1", authenticated=True)
        assert event_detail["plate"] == "ABC1234"
        
        # 4. Corrections preserve original event evidence and record the actual user.
        res = patch_hotel_event("evt-1", PlatePayload(plate="XYZ7890"), authenticated=user)
        assert res["success"] is True
        
        # Check database update
        updated_evt = get_hotel_event_detail("evt-1", authenticated=True)
        assert updated_evt["plate"] == "XYZ7890"
        assert updated_evt["room_number"] == 102
        assert updated_evt["plate_original"] == "ABC1234"
        
        # Check audit trail logs
        assert len(updated_evt["audit_logs"]) == 1
        audit_fields = [log["field_name"] for log in updated_evt["audit_logs"]]
        assert "plate" in audit_fields
        assert updated_evt["audit_logs"][0]["changed_by"] == 'reviewer'
        
        # 5. Test manual rebuild visits
        with pytest.raises(HTTPException) as blocked:
            post_rebuild_visits(authenticated=user)
        assert blocked.value.status_code == 409
        
    finally:
        store.close()
        config.HOTEL_DATABASE_PATH = original_db_path
