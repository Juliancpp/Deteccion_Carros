from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from app import administration
from app.hotel import visits
from app.hotel.geometry import GeometryEvent
from app.hotel.store import HotelStore
from scripts.reconcile_visits import run
from test_hotel_mvp import event
from test_manual_administration import client, login


TIME = datetime(2026, 10, 1, 4, 59, tzinfo=timezone.utc)


def sample(key, minutes=0, *, departure=False, **fields):
    kind = "SALIDA_NORMAL" if departure else "ENTRADA_NORMAL"
    camera = "A5" if departure else "A1"
    base = replace(
        event(kind, camera_id=camera),
        event_id=key,
        occurred_at=TIME + timedelta(minutes=minutes),
        movement="OUTWARD" if departure else "INWARD",
        room_status="CONFIRMADA",
        room_number=3,
        room_confidence=0.85,
        plate_confidence=0.91,
        evidence_path="fixture.jpg",
        capture_session=camera + "-session",
        physical_track_id=3,
        ocr_diagnostics_json="{}",
    )
    return replace(base, **fields)


@pytest.fixture
def store(tmp_path, monkeypatch):
    import cv2
    import numpy as np

    monkeypatch.chdir(tmp_path)
    assert cv2.imwrite("fixture.jpg", np.zeros((20, 40, 3), np.uint8))
    db = HotelStore(str(tmp_path / "hotel.db"))
    yield db
    db.close()


def test_cycle_room_delivered_before_entry_and_consecutive_vehicles(store):
    db = store.conn
    g = GeometryEvent("A1-session", "A1", 3, 99, TIME + timedelta(seconds=10), "hab-3", "ROOM_ENTRY")
    store.save_geometry_events([g, g])
    store.save_event_and_update_visit(sample("in", room_number=None, room_status="NO_DETERMINADA"))
    assert visits.case(db, "in")["room_number"] == 3
    assert db.execute("SELECT count(*) FROM visit_rooms").fetchone()[0] == 1
    store.save_event_and_update_visit(sample("out", 2, departure=True))
    assert visits.state(visits.case(db, "in")) == "COMPLETADA"
    store.save_event_and_update_visit(sample("in2", 3))
    store.save_event_and_update_visit(sample("out2", 4, departure=True))
    assert visits.indicators(db)["visits_completed"] == 2
    assert visits.case(db, "in2")["exit_event_id"] == "out2"
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize(
    "days,second,weak,room",
    [(100, False, False, 3), (0, True, False, 3), (0, False, True, 3), (0, False, False, 5)],
)
def test_unsafe_closures_remain_pending(store, days, second, weak, room):
    store.save_event_and_update_visit(sample("first"))
    if second:
        store.save_event_and_update_visit(sample("second", 1, physical_track_id=9))
    store.save_event_and_update_visit(
        sample(
            "out",
            2 + days * 1440,
            departure=True,
            room_number=room,
            plate_status="PARCIAL" if weak else "LEIDA",
        )
    )
    counts = visits.indicators(store.conn)
    assert counts["exits_unpaired"] == 1 and counts["visits_completed"] == 0
    assert visits.case(store.conn, "out")["pending"]


def test_plate_alone_not_enough_and_window_configurable(store, monkeypatch):
    store.save_event_and_update_visit(sample("in"))
    store.save_event_and_update_visit(sample("nop", 1, departure=True, room_status="NO_DETERMINADA"))
    monkeypatch.setenv("HOTEL_VISIT_MAX_SECONDS", "90")
    store.save_event_and_update_visit(sample("late", 2, departure=True))
    assert visits.indicators(store.conn)["exits_unpaired"] == 2


@pytest.mark.parametrize("session,completed", [("A1-session", 1), ("another", 0)])
def test_unreadable_plate_requires_same_session_camera_and_track(store, session, completed):
    store.save_event_and_update_visit(sample("in", plate=None, plate_status="NO_LEGIBLE"))
    store.save_event_and_update_visit(
        sample(
            "out",
            1,
            departure=True,
            camera_id="A1",
            capture_session=session,
            plate=None,
            plate_status="NO_LEGIBLE",
        )
    )
    assert visits.indicators(store.conn)["visits_completed"] == completed


def test_retry_after_restart_preserves_review_and_changes(tmp_path):
    path = str(tmp_path / "hotel.db")
    s = HotelStore(path)
    entry = sample("stable")
    s.save_event_and_update_visit(entry)
    with s.conn:
        visits.change_room(s.conn, "stable", 6, "operator")
    s.close()
    s = HotelStore(path)
    try:
        s.save_event_and_update_visit(replace(entry, plate="ABC0000", room_number=1))
        assert visits.case(s.conn, "stable")["room_number"] == 6
        assert s.get_event("stable")["plate"] == entry.plate
        assert len(s.list_events()) == 1
        with s.conn:
            visits.exceptional(s.conn, "stable", "admin", "Verificado en recepción")
        assert visits.state(visits.case(s.conn, "stable")) == "CIERRE_EXCEPCIONAL"
        assert visits.indicators(s.conn)["entries_unpaired"] == 1
    finally:
        s.close()


def test_month_boundary_and_review_overlap(store):
    store.save_event_and_update_visit(sample("in", review_required=True))
    store.save_event_and_update_visit(sample("out", 2, departure=True))
    store.save_event_and_update_visit(sample("naive", occurred_at=datetime(2026, 10, 1, 0, 0)))
    sep, october = [visits.indicators(store.conn, m) for m in ("2026-09", "2026-10")]
    assert sep == dict(visits_completed=1, entries_unpaired=0, exits_unpaired=0, review_pending=1)
    assert october["entries_unpaired"] == 1 and october["visits_completed"] == 0


def test_ambiguous_rooms_and_manual_precedence(store):
    store.save_event_and_update_visit(sample("in"))
    g = GeometryEvent("A1-session", "A1", 3, 1, TIME + timedelta(seconds=30), "hab-1", "ROOM_ENTRY")
    store.save_geometry_events([g, replace(g, feature_id="hab-2")])
    assert visits.case(store.conn, "in")["room_number"] is None
    assert visits.case(store.conn, "in")["pending"]
    with store.conn:
        visits.change_room(store.conn, "in", 2, "operator")
    store.save_geometry_events(
        [replace(g, source_sequence=4, occurred_at=TIME + timedelta(seconds=40), feature_id="hab-5")]
    )
    assert visits.case(store.conn, "in")["room_number"] == 2


def test_api_identity_retry_unlink_and_case_review(client):
    assert client.get("/api/hotel/visit-cases").status_code == 401
    login(client)
    base = "/api/hotel/visit-cases"
    assert client.patch(base + "/entry/room", json={"room_number": 3}).status_code == 200
    assert client.patch(base + "/entry/plate", json={"plate": "XYZ0123"}).status_code == 200
    assert client.post(base + "/entry/exceptional-close", json={"reason": "test"}).status_code == 403
    room_key = {"Idempotency-Key": "room-update"}
    room = base + "/entry/room"
    assert client.patch(room, json={"room_number": 4}, headers=room_key).status_code == 200
    assert client.patch(room, json={"room_number": 5}).status_code == 200
    assert client.patch(room, json={"room_number": 4}, headers=room_key).status_code == 200
    assert client.get(base + "/entry").json()["room_number"] == 5
    assert client.patch(room, json={"room_number": 6}, headers=room_key).status_code == 409
    action = "/api/hotel/visits/associate"
    h = {"Idempotency-Key": "attempt"}
    payload = {"entry_event_id": "entry", "exit_event_id": "exit"}
    first = client.post(action, json=payload, headers=h).json()["visit"]
    assert client.get(base + "/entry").json()["plate_manual"] == "XYZ0123"
    assert client.get(base + "/entry").json()["exit_at"]
    assert client.delete(f"/api/hotel/visits/{first['id']}/association").status_code == 200
    assert client.post(action, json=payload, headers=h).json()["visit"]["id"] == first["id"]
    assert client.get(base + "/entry").json()["exit_event_id"] is None
    assert client.post(action, json=dict(payload, exit_event_id="similar"), headers=h).status_code == 409
    assert client.post(base + "/entry/review").status_code == 200
    for category, n in client.get(base + "/indicators?month=2026-09").json().items():
        assert client.get(base, params=dict(category=category, month="2026-09")).json()["total"] == n
    assert client.get(base + "/indicators?month=2026-13").status_code == 422


def test_migration_preserves_sources_and_explicit_manual_link(store, tmp_path):
    for e in (sample("e"), sample("x", 1, departure=True, evidence_path=None)):
        store.save_event_and_update_visit(e)
    c = store.conn
    with c:
        c.execute(
            "CREATE TABLE manual_visits(id PRIMARY KEY, entry_event_id,exit_event_id,linked_by_user_id)"
        )
        c.execute("INSERT INTO manual_visits VALUES(47,'e','x',1)")
    output = tmp_path / "copy.db"
    before = Path(store.db_path).read_bytes()
    assert run(Path(store.db_path))["reconstructed_links"] == 1
    assert run(Path(store.db_path), output)["visits_completed"] == 1
    assert Path(store.db_path).read_bytes() == before
    assert run(output)["reconstructed_links"] == 0
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT id FROM visit_links").fetchone()[0] == 47
        assert db.execute("SELECT count(*) FROM manual_visits").fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_durable_crossing_identity_does_not_depend_on_process_track_counter():
    from types import SimpleNamespace
    from app.multicamera.coordinator import MultiCameraCoordinator, NormalizedCrossing
    import numpy as np

    c = object.__new__(MultiCameraCoordinator)
    vehicle = {"bbox": (2, 3, 30, 40), "id": 3, "class": "car", "confidence": 0.9}
    image = np.zeros((50, 60, 3), np.uint8)
    ctx = SimpleNamespace(camera_id="A1", camera_name="A1", geometry_detector=None, session_id="old")
    cross = NormalizedCrossing(3, "INWARD", (16, 40), TIME)
    a = c._event_from_confirmation(ctx, cross, vehicle, image, 15, 60, 50)
    ctx.session_id = "restart"
    b = c._event_from_confirmation(ctx, replace(cross, track_id=99), vehicle, image, 1, 60, 50)
    assert a.event_id == b.event_id


def test_room_queue_and_manual_assignment(client):
    login(client)
    g = GeometryEvent("restarted", "A1", 888, 42, datetime(2026, 9, 1, 13, 10), "hab-4", "ROOM_ENTRY")
    with administration.connection() as c, c:
        visits.geometry(c, g)
    response = client.get("/api/hotel/visit-cases", params={"category": "review_pending"}).json()
    orphan = next(r for r in response["visits"] if r["kind"] == "room_review")
    observation = client.get("/api/hotel/visit-cases/" + orphan["id"]).json()["rooms"][0]["id"]
    assert client.post(f"/api/hotel/visit-cases/entry/room-observations/{observation}").status_code == 200
    assert client.get("/api/hotel/visit-cases/entry").json()["room_number"] == 4
    assert client.get("/api/hotel/visit-cases/indicators").json()["review_pending"] == response["total"] - 1


def test_conflicting_legacy_sources_stay_separate(store, tmp_path):
    for e in (
        sample("e"),
        sample("x", 1, departure=True, plate=None),
        sample("y", 2, departure=True, plate=None),
    ):
        store.save_event_and_update_visit(e)
    with store.conn as c:
        c.execute("CREATE TABLE manual_visits(id PRIMARY KEY,entry_event_id,exit_event_id,linked_by_user_id)")
        c.execute("INSERT INTO manual_visits VALUES(83,'e','x',1)")
        c.execute(
            "INSERT INTO hotel_visits(visit_id,plate,entry_event_id,exit_event_id,status,review_required) VALUES('old','ABC','e','y','CERRADA',0)"
        )
    report = run(Path(store.db_path), tmp_path / "migrated.db")
    assert report["ambiguous"] == 2 and report["visits_completed"] == 0
    assert report["exits_unpaired"] == 2


def test_legacy_mirror_delivery_is_idempotent(tmp_path):
    from app.database import DatabaseManager

    path = str(tmp_path / "legacy.db")
    payload = dict(source_event_id="durable", timestamp=TIME, plate=None, type="INWARD", vehicle_id=1)
    db = DatabaseManager(path)
    first = db.save_event(payload)
    db.engine.dispose()
    db = DatabaseManager(path)
    try:
        assert db.save_event(dict(payload, plate="ABC0000")) == first
        assert len(db.get_events()) == 1 and db.get_events()[0]["placa"] == ""
    finally:
        db.engine.dispose()
