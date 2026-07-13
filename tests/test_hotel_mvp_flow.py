from datetime import datetime, timezone, timedelta
import os
import numpy as np
import pytest
from openpyxl import load_workbook

from app.hotel.models import HotelVehicleEvent
from app.hotel.store import HotelStore, classify_event
from app.hotel.plate_voting import vote_plates_multiframe, correct_plate_characters
from app.hotel.frame_buffer import TrackFrameBuffer
from app.multicamera.event_candidate import EventCandidate
from app.multicamera.event_consumer import MultiCameraEventConsumer, HotelEvidenceCapture
from app.export.excel_exporter import HotelExcelExporter


def test_character_correction():
    # AAA-1234 format: alpha, alpha, alpha, digit, digit, digit, digit
    # Probar que O en posiciones numéricas se convierta en 0
    assert correct_plate_characters("ABC12O4") == "ABC1204"
    assert correct_plate_characters("ABC1O34") == "ABC1034"
    # Probar que I/l en posiciones numéricas se convierta en 1
    assert correct_plate_characters("ABC12l4") == "ABC1214"
    assert correct_plate_characters("ABC1I34") == "ABC1134"


def test_multiframe_voting_logic():
    # Escenario: mayoría consenso con correcciones de caracteres
    candidates = [
        {"plate": "GBA12O4", "confidence": 0.8, "frame_score": 1.0},
        {"plate": "GBA1204", "confidence": 0.9, "frame_score": 1.2},
        {"plate": "GBAl204", "confidence": 0.7, "frame_score": 0.8},
    ]
    res = vote_plates_multiframe(candidates)
    # Todos deben corregirse a GBA1204
    assert res["plate"] == "GBA1204"
    assert res["status"] == "LEIDA"
    assert res["confidence"] > 0.75


def test_hotel_mvp_store_lifecycle(tmp_path):
    db_path = str(tmp_path / "hotel.db")
    store = HotelStore(db_path)
    try:
        # 1. Entrada A1 -> ENTRADA_NORMAL
        ev1, rev1, notes1 = classify_event("A1", "inward", "ABC1234", store)
        assert ev1 == "ENTRADA_NORMAL"
        assert not rev1

        event1 = HotelVehicleEvent(
            event_id="ev1",
            occurred_at=datetime.now(timezone.utc),
            camera_id="A1",
            camera_name="Camara A1",
            track_id=1,
            movement="inward",
            movement_confidence=0.9,
            event_type=ev1,
            plate="ABC1234",
            plate_confidence=0.9,
            plate_status="LEIDA",
            room_number=12,
            room_confidence=0.8,
            room_status="CONFIRMADA",
            vehicle_class="Auto",
            vehicle_confidence=0.95,
            evidence_path="/path/to/evidence.jpg",
            review_required=rev1,
            notes=notes1,
            plate_candidates=[{"plate": "ABC1234", "confidence": 0.9}]
        )
        store.save_event_and_update_visit(event1)
        assert store.count_open_visits() == 1

        # 2. Salida A5 -> SALIDA_NORMAL
        ev2, rev2, notes2 = classify_event("A5", "outward", "ABC1234", store)
        assert ev2 == "SALIDA_NORMAL"
        assert not rev2

        event2 = HotelVehicleEvent(
            event_id="ev2",
            occurred_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            camera_id="A5",
            camera_name="Camara A5",
            track_id=2,
            movement="outward",
            movement_confidence=0.9,
            event_type=ev2,
            plate="ABC1234",
            plate_confidence=0.9,
            plate_status="LEIDA",
            room_number=12,
            room_confidence=0.8,
            room_status="CONFIRMADA",
            vehicle_class="Auto",
            vehicle_confidence=0.95,
            evidence_path="/path/to/evidence.jpg",
            review_required=rev2,
            notes=notes2,
            plate_candidates=[{"plate": "ABC1234", "confidence": 0.9}]
        )
        store.save_event_and_update_visit(event2)
        assert store.count_open_visits() == 0
    finally:
        store.close()


def test_excel_exporter(tmp_path):
    db_path = str(tmp_path / "hotel.db")
    store = HotelStore(db_path)
    
    # Crear archivo físico de imagen para validar hipervínculos reales
    dummy_img = str(tmp_path / "evidence.jpg")
    with open(dummy_img, "wb") as f:
        f.write(b"")

    try:
        event = HotelVehicleEvent(
            event_id="ev1",
            occurred_at=datetime.now(timezone.utc),
            camera_id="A1",
            camera_name="Camara A1",
            track_id=1,
            movement="inward",
            movement_confidence=0.9,
            event_type="ENTRADA_NORMAL",
            plate="ABC1234",
            plate_confidence=0.9,
            plate_status="LEIDA",
            room_number=12,
            room_confidence=0.8,
            room_status="CONFIRMADA",
            vehicle_class="Auto",
            vehicle_confidence=0.95,
            evidence_path=dummy_img,
            review_required=False,
            notes="",
            plate_candidates=[{"plate": "ABC1234", "confidence": 0.9}]
        )
        store.save_event_and_update_visit(event)

        excel_path = str(tmp_path / "report.xlsx")
        exporter = HotelExcelExporter(store, excel_path)
        exporter.export()

        assert os.path.exists(excel_path)
        wb = load_workbook(excel_path)
        assert "Eventos" in wb.sheetnames
        assert "Visitas" in wb.sheetnames
        assert "Revisión" in wb.sheetnames

        # Validar existencia de hipervínculo en columna Evidencia
        ws = wb["Eventos"]
        hyperlink_found = False
        for row in ws.iter_rows(values_only=False):
            for cell in row:
                if cell.value == dummy_img:
                    assert cell.hyperlink is not None
                    hyperlink_found = True
        assert hyperlink_found
    finally:
        store.close()
