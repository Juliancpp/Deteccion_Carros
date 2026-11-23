import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from app.hotel.plate_voting import vote_plates_multiframe
from app.hotel.store import HotelStore
from app.multicamera.event_candidate import EventCandidate
from app.multicamera.event_consumer import HotelEvidenceCapture, MultiCameraEventConsumer
from app.ocr import PlateReader
from scripts.evaluate_plate_accuracy import edit_distance, evaluate
from test_manual_administration import client, login


def reader_with(text, confidence):
    reader = PlateReader(enabled=False)
    reader.enabled = True
    box = [[2, 4], [18, 4], [18, 12], [2, 12]]
    reader.reader = SimpleNamespace(readtext=lambda *args, **kwargs: [(box, text, confidence)])
    return reader


@pytest.mark.parametrize("text,confidence,frames,status", [
    ("abc-1234", .96, ["one", "two"], "LEIDA"),
    ("ABC1234", .96, ["one", "one"], "PROBABLE"),
    ("ABC1234", .12, ["one", "two"], "PROBABLE"),
    ("ABC12O4", .98, ["one", "two"], "PARCIAL"),
    ("ABC?123", .99, ["one", "two"], "PARCIAL"),
    ("AB12", .25, ["one"], "PARCIAL"),
    ("ABC1234", .99, [None, None], "PROBABLE"),
])
def test_observed_text_decision(text, confidence, frames, status):
    image = np.zeros((35, 100, 3), np.uint8)
    reader = reader_with(text, confidence)
    candidates = []
    for frame in frames:
        found, metrics = reader.inspect_frame(image, frame)
        assert metrics["engine_calls"] == 3
        assert {r["variant"] for r in found} == {"clahe", "otsu", "adaptive"}
        assert all(r["raw_text"] == text and r["confidence"] == confidence for r in found)
        assert found[0]["text_polygon"][0] == [1., 2.]
        candidates.extend(found)
    result = vote_plates_multiframe(candidates, observed_only=True)
    assert result["status"] == status
    if status == "PARCIAL":
        assert result["plate"] is None
        assert result["partial_text"] == text


def test_missing_engine(monkeypatch):
    monkeypatch.setitem(sys.modules, "easyocr", None)
    r = PlateReader()
    readings, metrics = r.inspect_frame(None)
    assert not readings and metrics["engine_status"] == "MOTOR_NO_DISPONIBLE"
    assert metrics["engine_calls"] == 0


def test_no_crop_vs_no_text_vs_exception(caplog):
    reader = reader_with("PRIVATE", 1)
    tiny = np.zeros((5, 5, 3), np.uint8)
    assert reader.inspect_frame(tiny)[1]["engine_status"] == "SIN_CANDIDATO"
    image = np.zeros((32, 90, 3), np.uint8)
    reader.reader.readtext = lambda *a, **kw: []
    found, metrics = reader.inspect_frame(image)
    assert metrics["engine_status"] == "OK" and not found
    assert vote_plates_multiframe(found, observed_only=True)["status"] == "NO_LEGIBLE"

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE")

    reader.reader.readtext = fail
    found, metrics = reader.inspect_frame(image)
    assert metrics["engine_status"] == "ERROR_OCR" and metrics["errors"] == metrics["engine_calls"] > 0
    assert "PRIVATE" not in caplog.text


def test_partial_persisted_with_original_pixels_and_no_sensitive_log(tmp_path, caplog):
    raw = "PrI-12?"
    caplog.set_level(logging.INFO)
    consumer = MultiCameraEventConsumer(None, plate_reader=reader_with(raw, .28),
                                       evidence=HotelEvidenceCapture(str(tmp_path / "captures")))
    path = tmp_path / "hotel.db"
    consumer.hotel_store = HotelStore(str(path))
    now = datetime.now(timezone.utc)
    event = EventCandidate("event-id", "A5", "A5", 6, "OUTWARD", now, 15, "car", 2, .99,
                           (5, 5, 68, 28), (36, 17), 80, 40, np.full((40, 80, 3), 99, np.uint8),
                           session_id="test")
    consumer._process_resolved_candidate(event, None)
    consumer.hotel_store.close()
    store = HotelStore(str(path))
    try:
        row = store.get_event("event-id")
        diagnostic = json.loads(row["ocr_diagnostics_json"])
        readings = json.loads(row["plate_candidates"])
        assert diagnostic["engine_status"] == "OK"
        assert diagnostic["partial_text"] == raw
        assert row["plate"] is None and row["review_required"] and row["plate_status"] == "PARCIAL"
        assert all(r["raw_text"] == raw and r["camera_id"] == "A5" for r in readings)
        frame, = diagnostic["frames"]
        assert all(r["frame_id"] == frame["frame_id"] for r in readings)
        assert frame["source_sequence"] == 15 and frame["captured_at"] == str(now)
        assert frame["evidence_path"] == diagnostic["selected_evidence"] == row["evidence_path"]
        image = Path(frame["evidence_path"])
        assert len(image.stem) == 32 and raw not in str(image)
        assert cv2.imread(str(image)).mean() == 99
        assert len(list(tmp_path.rglob("*.jpg"))) == 1
        assert raw not in caplog.text and "ocr_event" in caplog.text
        assert store.count_open_visits() == 0
    finally:
        store.close()


def test_historical_event_and_manual_status(client):
    login(client)
    path = "/api/hotel/events/entry"
    before = client.get(path).json()
    assert before["ocr_diagnostics"].get("frames", []) == []
    assert before["verification_status"] == before["plate_status"]
    token = client.get("/api/auth/me").json()["csrf_token"]
    assert client.patch(path, headers={"X-CSRF-Token": token}, json={"plate": "ABC9999"}).status_code == 200
    after = client.get(path).json()
    assert after["verification_status"] == "CORREGIDA_MANUALMENTE"
    assert after["plate_original"] == before["plate_original"]


def test_evaluation_denominators_and_edit_distance():
    assert edit_distance("ABC123", "AB12") == 2
    assert edit_distance("ABC", "XABCY") == 2
    assert edit_distance("ABC", "AXC") == 1
    labels = {"a": dict(true_plate="ABC123", plate_visible="1"), "b": dict(true_plate="", plate_visible="0")}
    predictions = {"a": dict(plate_text="ABC", plate_status="PARCIAL", plate_iou="0"),
                   "b": dict(plate_text="ABC123", plate_status="LEIDA")}
    metrics = evaluate(labels, predictions)
    assert metrics["cer"] == .5 and metrics["partial_fraction"] == 1
    assert metrics["false_positive_fraction"] == 1 and metrics["localized_at_iou50"] == 0
    with pytest.raises(ValueError):
        evaluate(labels, {"a": predictions["a"]})


def test_replay_scores_missed_vehicles_and_unmatched_events(tmp_path):
    from scripts.evaluate_plate_accuracy import replay_predictions
    import sqlite3

    path = tmp_path / "replay.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE hotel_vehicle_events(event_id, camera_id, plate, plate_status)")
        conn.executemany("INSERT INTO hotel_vehicle_events VALUES(?,?,?,?)",
                         [("actual", "A1", "ABC1234", "LEIDA"), ("extra", "A5", None, "NO_LEGIBLE")])
    labels = {"one": dict(event_id="actual", camera_id="A1", plate_visible="1", true_plate="ABC1234"),
              "two": dict(event_id="", camera_id="A5", plate_visible="1", true_plate="DEF5678")}
    predictions, extra = replay_predictions(path, labels)
    assert extra == 1 and predictions["two"]["plate_status"] == "SIN_EVENTO"
    assert evaluate(labels, predictions)["exact_fraction"] == .5


@pytest.mark.parametrize("mode,expected", [
    ("missing", "MOTOR_NO_DISPONIBLE"), ("error", "ERROR_OCR"), ("blank", "OK"), ("tiny", "SIN_CANDIDATO"),
])
def test_technical_state_survives_event_write(tmp_path, mode, expected):
    reader = reader_with("", 0)
    reader.reader.readtext = lambda *a, **kw: []
    if mode == "missing":
        reader.enabled = False
    elif mode == "error":
        def fail(*args, **kwargs):
            raise ValueError("synthetic failure")
        reader.reader.readtext = fail
    size = 5 if mode == "tiny" else 50
    image = np.zeros((size, size, 3), np.uint8)
    consumer = MultiCameraEventConsumer(None, plate_reader=reader,
                                       evidence=HotelEvidenceCapture(str(tmp_path / "captures")))
    consumer.hotel_store = HotelStore(str(tmp_path / "hotel.db"))
    now = datetime.now(timezone.utc)
    event = EventCandidate("test", "A1", "A1", 1, "INWARD", now, 2, "car", 2, .99,
                           (0, 0, size, size), (2, 2), size, size, image, session_id="test")
    try:
        consumer._process_resolved_candidate(event, None)
        row = consumer.hotel_store.get_event("test")
        assert row["plate"] is None and row["plate_status"] == "NO_LEGIBLE" and row["review_required"]
        diagnostic = json.loads(row["ocr_diagnostics_json"])
        assert diagnostic["engine_status"] == expected
        assert (diagnostic["metrics"]["engine_calls"] > 0) == (mode in {"error", "blank"})
    finally:
        consumer.hotel_store.close()
