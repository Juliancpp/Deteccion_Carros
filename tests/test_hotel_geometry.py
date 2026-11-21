import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from app.hotel.geometry import GeometryDetector, Layout
from app.hotel.store import HotelStore
from app.multicamera.event_consumer import MultiCameraEventConsumer
from app.multicamera.coordinator import MultiCameraCoordinator
from app.settings import SettingsError, load_settings
from scripts import calibrate_geometry
from test_multicamera_coordinator import FakeDetector, FakeStream, FakeTracker, cameras, dvr

NOW = datetime.now(timezone.utc)
GATE = [[0.2, 0.5], [0.8, 0.5]]
ROOM = [[0.4, 0.6], [0.7, 0.6], [0.7, 0.9], [0.4, 0.9]]
FEATURES = [
    dict(kind="hotel_entry", id="in", points=GATE),
    dict(kind="hotel_exit", id="out", points=GATE, invert_direction=True),
    dict(kind="room", id="grupo-1-2", points=ROOM),
    dict(kind="approach", id="paso-3", points=ROOM),
    dict(kind="room", id="off", points=ROOM, enabled=False),
]


def layout(features=FEATURES, **kwargs):
    return Layout.parse(dict(camera_id="A1", features=features, **kwargs), "A1")


def frame(detector, seq, y, x=50, ids=(1,), now=None):
    boxes = [dict(id=i, bbox=[x - 2, y - 5, x + 2, y]) for i in ids]
    return detector.update(boxes, 100, 100, NOW, seq, seq / 5 if now is None else now)


def test_transitions_and_independent_tracks():
    detector = GeometryDetector(layout())
    events = []
    for seq, y in enumerate([35, 35, 50, 55, 55, 65, 65, 65, 59, 65, 55, 55, 35, 35, 68, 68]):
        events.extend(frame(detector, seq, y, ids=(1, 7)))
        assert not frame(detector, seq, y, ids=(1, 7))
    for track in (1, 7):
        assert [e.event_type for e in events if e.track_id == track] == [
            "HOTEL_ENTRY",
            "ROOM_ENTRY",
            "ROOM_APPROACH",
            "ROOM_EXIT",
            "HOTEL_EXIT",
            "HOTEL_ENTRY",
            "ROOM_ENTRY",
            "ROOM_APPROACH",
        ]
    assert all(e.feature_id != "off" for e in events)


def test_invalid_crossing_and_initial_occupancy():
    detector = GeometryDetector(layout())
    assert not any(frame(detector, seq, y, x=95) for seq, y in enumerate([35, 35, 65, 65]))
    detector.reset()
    assert not any(frame(detector, seq, y) for seq, y in enumerate([65, 65, 60, 60, 65]))
    assert not frame(detector, 6, 30, now=99)
    assert not frame(detector, 7, 30, now=100)


@pytest.mark.parametrize(
    "changes",
    [
        {"points": [GATE[0], GATE[0]]},
        {"points": [[1.1, 0.0], GATE[0]]},
        {"kind": "room", "points": [ROOM[i] for i in (0, 2, 1, 3)]},
        {"kind": "room", "points": [[0, 0], [0.5, 0.5], [1, 1]]},
        {"enabled": "false"},
        {"kind": "gate"},
        {"typo": True},
    ],
)
def test_invalid_shapes(changes):
    with pytest.raises(ValueError):
        layout([{**FEATURES[0], **changes}])


def test_load_export_and_edit(tmp_path, monkeypatch):
    src = tmp_path / "cam.json"
    src.write_text(json.dumps(dict(camera_id="A1", features=FEATURES)))
    env = {
        f"CAMERA_{cam}_{k}": v
        for cam in ("A1", "A5")
        for k, v in dict(ID=cam, NAME=cam, ENABLED="false", CHANNEL="1", TARGET_FPS="5").items()
    }
    assert load_settings(environ=env).cameras[0].geometry is None
    env["CAMERA_A1_GEOMETRY_FILE"] = str(src)
    assert load_settings(environ=env).cameras[0].geometry == layout()
    env["CAMERA_A5_GEOMETRY_FILE"] = str(src)
    with pytest.raises(SettingsError):
        load_settings(environ=env)
    img = tmp_path / "image.png"
    cv2.imwrite(str(img), np.zeros((300, 400, 3), np.uint8))
    dest = tmp_path / "review"
    from scripts import geometry_editor

    def edit_session(camera, image, camera_frame, config, output, resume, edit):
        drawing = geometry_editor.Drawing(Layout.load(config, camera))
        drawing.figures.append(dict(kind=edit["kind"], id=edit["id"], points=ROOM))
        geometry_editor.save_revision(tmp_path, drawing, cv2.imread(str(image)), output)
        return 0

    monkeypatch.setattr(geometry_editor, "desktop", edit_session)
    args = [
        "--image",
        str(img),
        "--camera",
        "A1",
        "--config",
        str(src),
        "--edit",
        "grupo",
        "--kind",
        "room",
        "--output",
        str(dest),
    ]
    assert calibrate_geometry.main(args) == 0
    assert cv2.imread(str(dest / "preview.jpg")).any()
    assert Layout.load(dest / "geometry.json", "A1").features[-1].id == "grupo"
    with pytest.raises(SystemExit):
        calibrate_geometry.main(args)
    assert len(Layout.load(src, "A1").features) == len(FEATURES)


def test_pipeline_sqlite_and_reconnect(tmp_path):
    # Inject only stream, model and tracker. Exercise the actual scheduler frame path.
    image = np.zeros((100, 100, 3), np.uint8)
    stream = FakeStream("A1", frames=[image])
    model = FakeDetector()
    ys = iter([35, 35, 68, 68, 35, 35])
    model.detect = lambda _: [dict(id=5, bbox=[45, 25, 55, next(ys)], class_id=2)]
    store = HotelStore(str(tmp_path / "hotel.db"))

    def save_twice(events):
        store.save_geometry_events(events)
        store.save_geometry_events(events)

    co = MultiCameraCoordinator(
        dvr(),
        [replace(cameras()[0], geometry=layout(use_for_visits=True))],
        detector=model,
        tracker_factory=FakeTracker,
        stream_factory=lambda *_: stream,
        geometry_sink=save_twice,
    )
    ctx = co.contexts[0]
    for seq in range(4):
        assert co._process_camera(ctx, seq / 5)
    assert ctx.last_result.annotated_frame.any()
    assert not ctx.last_result.raw_frame.any()
    stream.reconnect_count += 1
    for seq in range(4, 6):
        assert co._process_camera(ctx, seq / 5)
    assert [r[0] for r in store.conn.execute("SELECT event_type FROM geometry_events ORDER BY rowid")] == [
        "HOTEL_ENTRY",
        "ROOM_ENTRY",
        "ROOM_APPROACH",
    ]
    candidate = co.get_event(timeout=0)
    assert candidate.geometry_gate and candidate.direction == "INWARD"
    assert co.get_event(timeout=0) is None
    assert store.count_open_visits() == 0
    consumer = MultiCameraEventConsumer(
        co,
        evidence=SimpleNamespace(capture=lambda *_: None),
        plate_reader=SimpleNamespace(read=lambda *_: {"plate": "ABC1234", "confidence": 0.98}),
    )
    consumer.hotel_store = store
    consumer._process_resolved_candidate(
        candidate,
        SimpleNamespace(
            movement="OUTWARD",
            movement_confidence=0.1,
            room_number=2,
            room_confidence=0.7,
            room_status="PROBABLE",
        ),
    )
    saved = store.get_event(candidate.event_id)
    assert saved["event_type"] == "ENTRADA_NORMAL" and saved["room_number"] == 2
    store.close()


@pytest.mark.parametrize("between", [[(92, 49), (92, 51)], [(92, 50)]])
def test_track_goes_around_endpoint_inside_margin(between):
    detector = GeometryDetector(layout(margin=0.03))
    route = [(50, 40), (50, 40), *between, (50, 55), (50, 55)]
    assert not any(frame(detector, seq, y, x=x) for seq, (x, y) in enumerate(route))


def test_separate_main_gates():
    detector = GeometryDetector(
        layout(
            [
                dict(kind="hotel_entry", id="porton", points=[[0.1, 0.3], [0.4, 0.3]]),
                dict(kind="hotel_exit", id="salida", points=[[0.6, 0.7], [0.9, 0.7]]),
            ],
            observations=1,
        )
    )
    route = [(25, 20), (25, 40), (25, 75), (75, 75), (75, 60), (75, 80)]
    events = [e for seq, (x, y) in enumerate(route) for e in frame(detector, seq, y, x=x)]
    assert [(e.feature_id, e.event_type) for e in events] == [
        ("porton", "HOTEL_ENTRY"),
        ("salida", "HOTEL_EXIT"),
    ]


@pytest.mark.parametrize("use_for_visits", [False, True])
def test_same_crossing_one_candidate_even_with_both_detectors_configured(tmp_path, use_for_visits):
    from app.line_detector import VirtualLineDetector

    image = np.zeros((100, 100, 3), np.uint8)
    ys = iter([30, 30, 75, 75])
    model = FakeDetector()
    model.detect = lambda _: [dict(bbox=[45, y - 10, 55, y], class_id=2) for y in [next(ys)]]
    legacy = VirtualLineDetector(normalized_start=(0.2, 0.5), normalized_end=(0.8, 0.5))
    store = HotelStore(str(tmp_path / "hotel.db"))
    co = MultiCameraCoordinator(
        dvr(),
        [replace(cameras()[0], geometry=layout(use_for_visits=use_for_visits))],
        detector=model,
        tracker_factory=FakeTracker,
        stream_factory=lambda *_: FakeStream("A1", frames=[image]),
        line_detector_factory=lambda _: legacy,
        geometry_sink=store.save_geometry_events,
    )
    for i in range(4):
        co._process_camera(co.contexts[0], i / 5)
    cand = co.get_event(timeout=0)
    assert cand and cand.geometry_gate == use_for_visits
    assert co.get_event(timeout=0) is None
    assert legacy.crossing_candidates_count == (0 if use_for_visits else 1)
    assert (
        store.conn.execute("SELECT COUNT(*) FROM geometry_events WHERE event_type='HOTEL_ENTRY'").fetchone()[
            0
        ]
        == 1
    )
    assert len(store.list_visits()) == 0
    store.close()


def test_id_loss_reconnect_and_pending_ocr_are_isolated(tmp_path):
    from app.hotel.frame_buffer import TrackFrameBuffer

    detector = GeometryDetector(layout())
    assert not frame(detector, 1, 35, ids=(2,))
    assert not frame(detector, 2, 35, ids=(2,))
    assert not frame(detector, 3, 70, ids=(3,))
    assert not frame(detector, 4, 70, ids=(3,))

    image = np.full((100, 100, 3), 80, np.uint8)
    stream = FakeStream("A1", frames=[image])
    model = FakeDetector()
    ys = iter([30, 30, 75, 75, 75])
    model.detect = lambda _: [dict(bbox=[40, 20, 60, next(ys)], class_id=2)]
    co = MultiCameraCoordinator(
        dvr(),
        [replace(cameras()[0], geometry=layout(use_for_visits=True))],
        detector=model,
        tracker_factory=FakeTracker,
        stream_factory=lambda *_: stream,
    )
    ctx = co.contexts[0]
    for i in range(4):
        co._process_camera(ctx, i / 5)
    old = co.get_event(timeout=0)
    stream.reconnect_count += 1
    stream.sequence, stream.last_frame = 0, None
    co._process_camera(ctx, 1)
    new = co.get_latest_result("A1", after_sequence=4, after_session=old.session_id)
    assert new.source_sequence == 1 and new.session_id != old.session_id
    assert not co.get_event(timeout=0)
    buf = TrackFrameBuffer()
    buf.add("A1", old.track_id, image, {"bbox": [1, 1, 30, 30]}, NOW, session_id=new.session_id)
    assert buf.best("A1", old.track_id, session_id=new.session_id)
    assert not buf.best("A1", old.track_id, session_id=old.session_id)
    store = HotelStore(str(tmp_path / "hotel.db"))
    ocr = SimpleNamespace(read=lambda _: {"plate": "ABC1234", "confidence": 0.98})
    consumer = MultiCameraEventConsumer(
        co,
        frame_buffer=buf,
        plate_reader=ocr,
        evidence=SimpleNamespace(capture=lambda *_: "evidence.jpg"),
        analyzers={"A1": SimpleNamespace(session_id=new.session_id)},
    )
    consumer.hotel_store = store
    assert consumer._candidate_analyzer(old) is None
    consumer._process_resolved_candidate(old, None)
    saved = store.get_event(old.event_id)
    assert saved["plate_status"] == "PROBABLE" and saved["room_number"] is None
    assert saved["review_required"] and not store.list_visits()
    store.close()


def test_reviewed_geometry_cannot_mutate_visits_on_retry_or_rebuild(tmp_path):
    from test_hotel_mvp import event

    path = str(tmp_path / "hotel.db")
    store = HotelStore(path)
    confirmed = replace(event(), source="geometry_gate", evidence_path="capture.jpg")
    weak = replace(
        event("SALIDA_NORMAL", camera_id="A5"),
        source="geometry_gate",
        plate_status="PROBABLE",
        evidence_path="capture.jpg",
        review_required=True,
    )
    for ev in [confirmed, weak, confirmed, weak]:
        store.save_event_and_update_visit(ev)
    assert len(store.list_events()) == 2 and store.count_open_visits() == 1
    store.close()
    store = HotelStore(path)
    store.rebuild_hotel_visits()
    assert store.count_open_visits() == 1
    store.save_event_and_update_visit(
        replace(weak, event_id="no-photo", evidence_path=None, plate_status="LEIDA", review_required=False)
    )
    assert store.count_open_visits() == 1
    store.save_event_and_update_visit(
        replace(weak, event_id="exit-confirmed", plate_status="LEIDA", review_required=False)
    )
    assert store.count_open_visits() == 0 and len(store.list_visits()) == 1
    store.close()


def test_additive_migration_preserves_2014_rows_and_rejects_invalid_batch(tmp_path):
    import sqlite3
    from app.hotel.geometry import GeometryEvent
    from test_hotel_mvp import event

    path = str(tmp_path / "hotel.db")
    store = HotelStore(path)
    with store.conn:
        store.conn.execute("DROP TABLE geometry_events")
    for i in range(2014):
        store.save_event_and_update_visit(
            replace(event(plate=None), event_id=str(i), source="legacy_eventos" if i < 1914 else "mvp_hotel")
        )

    def snapshot(db):
        return [tuple(row) for row in db.execute("SELECT * FROM hotel_vehicle_events ORDER BY event_id")]

    before = snapshot(store.conn)
    store.close()
    good = GeometryEvent("session", "A1", 1, 7, NOW, "hab-2", "ROOM_ENTRY")
    for _ in range(2):
        store = HotelStore(path)
        assert store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert snapshot(store.conn) == before
        with pytest.raises(sqlite3.IntegrityError):
            store.save_geometry_events([replace(good, track_id=9), replace(good, event_type="invalid")])
        store.save_geometry_events([good, good])
        assert store.conn.execute("SELECT COUNT(*) FROM geometry_events").fetchone()[0] == 1
        assert not store.list_visits()
        store.close()


def test_passage_in_front_only_or_grazing_door():
    door = dict(kind="room", id="door", points=ROOM)
    approach = dict(
        kind="approach", id="forecourt", points=[[0.2, 0.4], [0.85, 0.4], [0.85, 0.6], [0.2, 0.6]]
    )
    detector = GeometryDetector(layout([door, approach]))
    route = [(10, 55), (10, 55), (30, 55), (45, 55), (55, 61), (65, 55), (80, 55), (90, 55)]
    events = [e.event_type for seq, (x, y) in enumerate(route) for e in frame(detector, seq, y, x=x)]
    assert events == ["ROOM_APPROACH"]


def test_gate_ocr_consensus_and_saved_photo_open_one_visit(tmp_path):
    from datetime import timedelta
    from pathlib import Path
    from app.hotel.frame_buffer import TrackFrameBuffer
    from app.multicamera.event_candidate import EventCandidate
    from app.multicamera.event_consumer import HotelEvidenceCapture

    image = np.full((100, 100, 3), 80, np.uint8)
    box = (20, 20, 70, 70)
    cand = EventCandidate(
        "crossing-1",
        "A1",
        "A1",
        3,
        "INWARD",
        NOW,
        7,
        "Auto",
        2,
        0.98,
        box,
        (45, 70),
        100,
        100,
        image,
        geometry_gate=True,
        session_id="actual",
    )
    buffer = TrackFrameBuffer()
    for i in range(2):
        buffer.add("A1", 3, image, {"bbox": box}, NOW + timedelta(milliseconds=200 * i), session_id="actual")
    consumer = MultiCameraEventConsumer(
        None,
        frame_buffer=buffer,
        plate_reader=SimpleNamespace(read=lambda _: {"plate": "ABC1234", "confidence": 0.98}),
        evidence=HotelEvidenceCapture(str(tmp_path / "captures")),
    )
    store = HotelStore(str(tmp_path / "hotel.db"))
    consumer.hotel_store = store
    room = SimpleNamespace(
        movement="INWARD",
        movement_confidence=0.9,
        room_number=2,
        room_confidence=0.9,
        room_status="CONFIRMADA",
    )
    consumer._process_resolved_candidate(cand, room)
    row = store.get_event(cand.event_id)
    assert row["source"] == "geometry_gate" and row["plate_status"] == "LEIDA"
    assert Path(row["evidence_path"]).is_file()
    assert len(json.loads(row["plate_supporting_frames_json"])) == 2
    assert not row["review_required"] and store.count_open_visits() == 1
    store.close()
