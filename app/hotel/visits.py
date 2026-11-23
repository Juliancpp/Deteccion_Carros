"""Canonical visit identities. Call mutators inside the caller's write transaction."""

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/Guayaquil")
ENTRIES = {"ENTRADA_NORMAL", "ENTRADA_POR_SALIDA"}
EXITS = {"SALIDA_NORMAL", "SALIDA_POR_ENTRADA", "SALIDA_SIN_ENTRADA"}
CATEGORIES = {
    "visits_completed": "exit_event_id IS NOT NULL AND kind='entry'",
    "entries_unpaired": "kind='entry' AND exit_event_id IS NULL",
    "exits_unpaired": "kind='exit'",
    "review_pending": "pending=1",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS event_plate_corrections (
    event_id TEXT PRIMARY KEY REFERENCES hotel_vehicle_events(event_id),
    plate_manual TEXT NOT NULL CHECK(length(plate_manual)>0),
    corrected_by_user_id INTEGER NOT NULL REFERENCES users(id), corrected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visit_cases (
    id TEXT PRIMARY KEY REFERENCES hotel_vehicle_events(event_id),
    kind TEXT NOT NULL CHECK(kind IN ('entry','exit','unknown')),
    instant REAL NOT NULL, session TEXT, physical_track INTEGER,
    review INTEGER NOT NULL CHECK(review IN (0,1)), exceptional_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(exceptional_reason IS NULL OR kind='entry')
);
CREATE INDEX IF NOT EXISTS visit_window ON visit_cases(kind,instant);
CREATE TABLE IF NOT EXISTS visit_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_event_id TEXT NOT NULL REFERENCES visit_cases(id),
    exit_event_id TEXT NOT NULL REFERENCES visit_cases(id),
    origin TEXT NOT NULL, confidence REAL CHECK(confidence BETWEEN 0 AND 1),
    actor TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, revoked_at TEXT,
    CHECK(entry_event_id<>exit_event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS current_entry ON visit_links(entry_event_id) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS current_exit ON visit_links(exit_event_id) WHERE revoked_at IS NULL;
CREATE TRIGGER IF NOT EXISTS visit_link_guard BEFORE INSERT ON visit_links
WHEN NOT EXISTS (SELECT 1 FROM visit_cases a,visit_cases b WHERE a.id=NEW.entry_event_id
AND b.id=NEW.exit_event_id AND a.kind='entry' AND b.kind='exit' AND a.instant<=b.instant
AND a.exceptional_reason IS NULL)
BEGIN SELECT RAISE(ABORT,'invalid visit transition'); END;
CREATE TRIGGER IF NOT EXISTS visit_link_immutable BEFORE UPDATE ON visit_links
WHEN NEW.entry_event_id<>OLD.entry_event_id OR NEW.exit_event_id<>OLD.exit_event_id
OR NEW.origin<>OLD.origin OR NEW.actor<>OLD.actor OR NEW.id<>OLD.id
OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
BEGIN SELECT RAISE(ABORT,'immutable visit association'); END;
CREATE TABLE IF NOT EXISTS visit_rooms (
    id INTEGER PRIMARY KEY, visit_id TEXT REFERENCES visit_cases(id), source_key TEXT UNIQUE NOT NULL,
    number INTEGER CHECK(number>0), instant REAL NOT NULL, camera TEXT, session TEXT, track INTEGER,
    ambiguous INTEGER NOT NULL DEFAULT 0 CHECK(ambiguous IN (0,1)), origin TEXT NOT NULL, evidence_path TEXT, confidence REAL CHECK(confidence BETWEEN 0 AND 1), evidence_event TEXT REFERENCES hotel_vehicle_events(event_id)
);
CREATE INDEX IF NOT EXISTS visit_room_owner ON visit_rooms(visit_id,instant);
CREATE INDEX IF NOT EXISTS room_track ON visit_rooms(camera,session,track,instant);
CREATE TABLE IF NOT EXISTS visit_history (
    id INTEGER PRIMARY KEY, visit_id TEXT NOT NULL REFERENCES visit_cases(id),
    action TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS visit_history_owner ON visit_history(visit_id,id);
CREATE TABLE IF NOT EXISTS visit_commands (
    actor TEXT, token TEXT, fingerprint TEXT NOT NULL, result TEXT NOT NULL,
    PRIMARY KEY(actor,token)
);
CREATE TABLE IF NOT EXISTS visit_imports(source TEXT, source_id TEXT, PRIMARY KEY(source,source_id));
CREATE VIEW IF NOT EXISTS current_visits AS
SELECT v.*, e.camera_id,e.plate,p.plate_manual,e.plate_status,e.plate_confidence,e.movement,e.occurred_at,e.evidence_path,
    CASE WHEN v.kind='entry' THEN v.id END entry_event_id,
    CASE WHEN v.kind='exit' THEN v.id ELSE l.exit_event_id END exit_event_id,
    CASE WHEN v.kind='entry' THEN e.occurred_at END entry_at,
    CASE WHEN v.kind='exit' THEN e.occurred_at ELSE d.occurred_at END exit_at,
    CASE WHEN l.origin IN ('manual','historical_manual') THEN CAST(l.actor AS INTEGER) END linked_by_user_id,
    l.created_at linked_at,l.id association_id,l.origin association_origin,l.confidence association_confidence,
    CASE WHEN l.origin='track_session' THEN 'CONTINUIDAD_VISUAL' WHEN l.origin='plate_room' THEN 'CORROBORADA'
         WHEN l.origin IN ('manual','historical_manual') THEN 'VALIDADA_MANUALMENTE' END association_basis,
    (SELECT CASE WHEN ambiguous=0 THEN number END FROM visit_rooms r WHERE r.visit_id=v.id
     ORDER BY (origin='manual') DESC, instant DESC,id DESC LIMIT 1) room_number,
    MAX(v.review,coalesce(x.review,0)) pending
FROM visit_cases v JOIN hotel_vehicle_events e ON e.event_id=v.id
LEFT JOIN event_plate_corrections p ON p.event_id=v.id
LEFT JOIN visit_links l ON l.entry_event_id=v.id AND l.revoked_at IS NULL
LEFT JOIN hotel_vehicle_events d ON d.event_id=l.exit_event_id
LEFT JOIN visit_cases x ON x.id=l.exit_event_id
WHERE NOT EXISTS(SELECT 1 FROM visit_links b WHERE b.exit_event_id=v.id AND b.revoked_at IS NULL);
"""


def epoch(value):
    dt = datetime.fromisoformat(str(value))
    return (dt if dt.tzinfo else dt.replace(tzinfo=ZONE)).timestamp()


def window(name="HOTEL_VISIT_MAX_SECONDS", default=86400):
    seconds = int(os.getenv(name, str(default)))
    if seconds <= 0:
        raise ValueError(f"{name} debe ser positivo")
    return seconds


def record(c, key, action, actor, **detail):
    c.execute(
        "INSERT INTO visit_history(visit_id,action,actor,detail) VALUES(?,?,?,?)",
        (key, action, str(actor), json.dumps(detail, ensure_ascii=False)),
    )
    c.execute("UPDATE visit_cases SET updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (key,))


def link(c, entry, departure, origin, actor, confidence=None, historical_id=None):
    rows = [
        c.execute("SELECT * FROM visit_cases WHERE id=?", (key,)).fetchone() for key in (entry, departure)
    ]
    if any(r is None for r in rows):
        raise LookupError("Evento sin caso canónico")
    a, b = rows
    if a["kind"] != "entry" or b["kind"] != "exit" or b["instant"] < a["instant"] or a["exceptional_reason"]:
        raise ValueError("Secuencia de visita inválida")
    existing = c.execute(
        "SELECT * FROM visit_links WHERE revoked_at IS NULL AND (entry_event_id=? OR exit_event_id=?)",
        (entry, departure),
    ).fetchone()
    if existing:
        if existing["entry_event_id"] == entry and existing["exit_event_id"] == departure:
            return existing["id"]
        raise FileExistsError("Evento asociado a otra visita")
    cur = c.execute(
        """INSERT INTO visit_links(id,entry_event_id,exit_event_id,origin,actor,confidence)
                       VALUES(?,?,?,?,?,?)""",
        (historical_id, entry, departure, origin, str(actor), confidence),
    )
    record(
        c,
        entry,
        "SALIDA_VINCULADA",
        actor,
        exit_event_id=departure,
        origin=origin,
        association_id=cur.lastrowid,
    )
    return cur.lastrowid


def detach(c, association, actor):
    row = c.execute("SELECT * FROM visit_links WHERE id=?", (association,)).fetchone()
    if not row:
        raise LookupError("Asociación inexistente")
    if row["revoked_at"] is None:
        c.execute("UPDATE visit_links SET revoked_at=CURRENT_TIMESTAMP WHERE id=?", (association,))
        c.execute(
            "UPDATE visit_cases SET review=1 WHERE id IN (?,?)", (row["entry_event_id"], row["exit_event_id"])
        )
        record(
            c,
            row["entry_event_id"],
            "SALIDA_DESVINCULADA",
            actor,
            association_id=association,
            exit_event_id=row["exit_event_id"],
        )


def available(path):
    try:
        return bool(path and Path(path).is_file() and Path(path).stat().st_size)
    except (OSError, ValueError):
        return False


def candidates(c, e, instant, session, track):
    track_limit = window("HOTEL_VISIT_TRACK_MAX_SECONDS", 120)
    if e["movement"].upper() != "OUTWARD":
        return []
    options = c.execute(
        """SELECT * FROM current_visits WHERE kind='entry' AND exit_event_id IS NULL
                           AND exceptional_reason IS NULL AND instant BETWEEN ? AND ?""",
        (instant - window(), instant),
    )
    matches = []
    for a in options:
        if a["movement"].upper() != "INWARD":
            continue
        same_track = bool(
            track is not None
            and session
            and a["session"] == session
            and a["camera_id"] == e["camera_id"]
            and a["physical_track"] == track
            and instant - a["instant"] <= track_limit
        )
        identified = a["plate_manual"] or a["plate"]
        entry_identified = bool(
            a["plate_manual"]
            or (a["plate"] and a["plate_status"] == "LEIDA" and a["plate_confidence"] >= 0.65)
        )
        reliable = bool(
            entry_identified and e["plate"] and e["plate_status"] == "LEIDA" and e["plate_confidence"] >= 0.65
        )
        if reliable and identified != e["plate"]:
            continue
        same_room = (
            a["room_number"] and a["room_number"] == e["room_number"] and e["room_status"] == "CONFIRMADA"
        )
        if a["room_number"] and e["room_status"] == "CONFIRMADA" and not same_room:
            continue
        if same_track or reliable or same_room:
            strong = bool(
                (same_track or (reliable and same_room))
                and available(e["evidence_path"])
                and available(a["evidence_path"])
            )
            matches.append((a["id"], strong, "track_session" if same_track else "plate_room"))
    return matches


def ingest(c, event_id, session=None, track=None, historical=False):
    e = c.execute("SELECT * FROM hotel_vehicle_events WHERE event_id=?", (event_id,)).fetchone()
    kind = "entry" if e["event_type"] in ENTRIES else "exit" if e["event_type"] in EXITS else "unknown"
    instant = epoch(e["occurred_at"])
    cur = c.execute(
        """INSERT INTO visit_cases(id,kind,instant,session,physical_track,review)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
        (
            event_id,
            kind,
            instant,
            session,
            track,
            int(bool(e["review_required"] or kind != "entry" or e["plate_status"] != "LEIDA")),
        ),
    )
    if not cur.rowcount:
        return
    record(c, event_id, "REGISTRADA", "historical" if historical else "detector", kind=kind)
    if kind == "entry":
        if e["room_number"] and e["room_status"] == "CONFIRMADA":
            c.execute(
                """INSERT INTO visit_rooms(visit_id,source_key,number,instant,camera,session,track,origin,confidence,evidence_event)
                         VALUES(?,?,?,?,?,?,?,'event',?,?)""",
                (
                    event_id,
                    "event:" + event_id,
                    e["room_number"],
                    instant,
                    e["camera_id"],
                    session,
                    track,
                    e["room_confidence"],
                    event_id,
                ),
            )
        if session:
            waiting = c.execute(
                "SELECT * FROM visit_rooms WHERE visit_id IS NULL AND session=? AND camera=? AND track=?",
                (session, e["camera_id"], track),
            ).fetchall()
            for r in waiting:
                assign_geometry(c, r)
    if kind == "exit" and not historical:
        matches = candidates(c, e, instant, session, track)
        if len(matches) == 1 and matches[0][1]:
            owner, _, reason = matches[0]
            c.execute(
                "UPDATE visit_cases SET review=? WHERE id=?", (int(bool(e["review_required"])), event_id)
            )
            link(
                c,
                owner,
                event_id,
                reason,
                "detector",
                min(e["plate_confidence"], 1) if reason == "plate_room" else None,
            )
        elif matches:
            record(c, event_id, "ASOCIACION_PENDIENTE", "detector", candidates=[m[0] for m in matches])


def assign_geometry(c, r):
    owners = c.execute(
        """SELECT id FROM current_visits WHERE kind='entry' AND session=? AND camera_id=?
        AND physical_track=? AND instant BETWEEN ? AND ? AND exceptional_reason IS NULL
        AND (exit_event_id IS NULL OR (SELECT instant FROM visit_cases WHERE id=exit_event_id)>=?)""",
        (r["session"], r["camera"], r["track"], r["instant"] - window(), r["instant"], r["instant"]),
    ).fetchall()
    if len(owners) == 1 and r["number"]:
        key = owners[0]["id"]
        c.execute("UPDATE visit_rooms SET visit_id=? WHERE id=?", (key, r["id"]))
        different = c.execute(
            """SELECT 1 FROM visit_rooms WHERE visit_id=? AND instant=?
                               AND number<>? AND origin<>'manual'""",
            (key, r["instant"], r["number"]),
        ).fetchone()
        if different:
            c.execute(
                "UPDATE visit_rooms SET ambiguous=1 WHERE visit_id=? AND instant=? AND origin<>'manual'",
                (key, r["instant"]),
            )
            c.execute("UPDATE visit_cases SET review=1 WHERE id=?", (key,))
        record(c, key, "HABITACION_CONFIRMADA", "geometry", observation=r["id"], number=r["number"])
    elif len(owners) > 1:
        for owner in owners:
            c.execute("UPDATE visit_cases SET review=1 WHERE id=?", (owner["id"],))


def geometry(c, e):
    if e.event_type != "ROOM_ENTRY":
        return
    match = re.fullmatch(r"hab-(\d+)", e.feature_id)
    key = json.dumps([e.camera_id, e.session_id, e.track_id, e.source_sequence, e.feature_id])
    cur = c.execute(
        """INSERT INTO visit_rooms(source_key,number,instant,camera,session,track,evidence_path,origin)
                       VALUES(?,?,?,?,?,?,?,'geometry') ON CONFLICT(source_key) DO NOTHING""",
        (
            key,
            int(match[1]) if match else None,
            epoch(e.occurred_at),
            e.camera_id,
            e.session_id,
            e.track_id,
            e.evidence_path,
        ),
    )
    if cur.rowcount:
        assign_geometry(c, c.execute("SELECT * FROM visit_rooms WHERE id=?", (cur.lastrowid,)).fetchone())


def query(category=None, month=None):
    clauses, args = [], []
    if category:
        if category not in CATEGORIES:
            raise ValueError("Indicador desconocido")
        clauses.append(CATEGORIES[category])
    if month:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("Mes inválido")
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=ZONE)
        end = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
        clauses.append("instant>=? AND instant<?")
        args.extend([start.timestamp(), end.timestamp()])
    source = "current_visits"
    if category == "review_pending":
        source = """(SELECT id,kind,instant,pending FROM current_visits UNION ALL
                     SELECT 'room-review:'||min(id),'room_review',min(instant),1 FROM visit_rooms
                     WHERE visit_id IS NULL GROUP BY camera,session,track)"""
    return " FROM " + source + (" WHERE " + " AND ".join(clauses) if clauses else ""), args


def indicators(c, month=None):
    counts = {}
    for category in CATEGORIES:
        sql, args = query(category, month)
        counts[category] = c.execute("SELECT count(*)" + sql, args).fetchone()[0]
    return counts


def state(r):
    if r["exceptional_reason"]:
        return "CIERRE_EXCEPCIONAL"
    if r["kind"] == "exit":
        return "SALIDA_HUERFANA"
    if r["kind"] == "unknown":
        return "SIN_RESOLVER"
    if r["exit_event_id"]:
        return "COMPLETADA"
    return "HABITACION_CONFIRMADA" if r["room_number"] else "ACTIVA"


def export_rows(c):
    result = []
    for r in c.execute("""SELECT v.*,d.occurred_at exit_at,d.camera_id exit_camera,d.event_type exit_type
                          FROM current_visits v LEFT JOIN hotel_vehicle_events d ON d.event_id=v.exit_event_id
                          ORDER BY v.instant DESC"""):
        result.append(
            dict(
                visit_id=r["id"],
                status=state(r),
                room_number=r["room_number"],
                plate_status=r["plate_status"],
                plate=r["plate_manual"] or r["plate"],
                entry_at=r["occurred_at"] if r["entry_event_id"] else None,
                entry_camera=r["camera_id"] if r["entry_event_id"] else None,
                exit_at=r["exit_at"],
                exit_camera=r["exit_camera"],
                exit_type=r["exit_type"],
                review_required=r["pending"],
            )
        )
    return result


def case(c, key):
    row = c.execute("SELECT * FROM current_visits WHERE id=?", (key,)).fetchone()
    if not row:
        raise LookupError("Visita inexistente o salida ya vinculada")
    return row


def change_room(c, key, number, actor):
    r = case(c, key)
    if r["kind"] != "entry" or r["exceptional_reason"]:
        raise ValueError("La asignación requiere una visita de entrada")
    latest = c.execute(
        "SELECT * FROM visit_rooms WHERE visit_id=? AND origin='manual' ORDER BY id DESC LIMIT 1", (key,)
    ).fetchone()
    if latest and latest["number"] == number:
        return
    now = datetime.now(timezone.utc).timestamp()
    c.execute(
        """INSERT INTO visit_rooms(visit_id,source_key,number,instant,origin) VALUES(?,?,?,?,'manual')""",
        (key, str(uuid.uuid4()), number, now),
    )
    record(c, key, "HABITACION_CORREGIDA", actor, previous=r["room_number"], number=number)


def review(c, key, actor):
    r = case(c, key)
    if r["pending"]:
        c.execute("UPDATE visit_cases SET review=0 WHERE id IN (?,?)", (key, r["exit_event_id"]))
        record(c, key, "REVISION_CONFIRMADA", actor)


def exceptional(c, key, actor, reason):
    r = case(c, key)
    if r["exceptional_reason"] == reason:
        return
    if r["kind"] != "entry" or r["exit_event_id"] or r["exceptional_reason"]:
        raise ValueError("Cierre excepcional incompatible con el estado actual")
    c.execute("UPDATE visit_cases SET exceptional_reason=?,review=1 WHERE id=?", (reason, key))
    record(c, key, "CIERRE_EXCEPCIONAL", actor, reason=reason)


def link_room(c, key, observation, actor):
    owner = case(c, key)
    r = c.execute("SELECT * FROM visit_rooms WHERE id=? AND origin='geometry'", (observation,)).fetchone()
    if not r:
        raise LookupError("Observación inexistente")
    if r["visit_id"] == key:
        return
    if r["visit_id"]:
        raise FileExistsError("La observación ya pertenece a otra visita")
    end = c.execute("SELECT instant FROM visit_cases WHERE id=?", (owner["exit_event_id"],)).fetchone()
    if owner["kind"] != "entry" or owner["instant"] > r["instant"] or end and end[0] < r["instant"]:
        raise ValueError("La observación queda fuera de la visita")
    c.execute("UPDATE visit_rooms SET visit_id=? WHERE id=?", (key, observation))
    record(c, key, "OBSERVACION_VINCULADA", actor, observation=observation)


def command(c, actor, token, payload, execute):
    if token is None:
        return execute()
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    prior = c.execute(
        "SELECT * FROM visit_commands WHERE actor=? AND token=?", (str(actor), token)
    ).fetchone()
    if prior:
        if prior["fingerprint"] != fingerprint:
            raise FileExistsError("Clave de reintento reutilizada para otra operación")
        return json.loads(prior["result"])
    result = execute()
    c.execute(
        "INSERT INTO visit_commands VALUES(?,?,?,?)", (str(actor), token, fingerprint, json.dumps(result))
    )
    return result
