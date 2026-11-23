"""Operator-owned records. Never writes detector events or automatic hotel_visits."""
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import secrets
import sqlite3
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError

from app.hotel import visits
from app import config

PASSWORDS = PasswordHasher()
DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))
ENTRY_TYPES = ('ENTRADA_NORMAL', 'ENTRADA_POR_SALIDA')
EXIT_TYPES = ('SALIDA_NORMAL', 'SALIDA_POR_ENTRADA', 'SALIDA_SIN_ENTRADA')


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@contextmanager
def connection():
    conn = sqlite3.connect(config.HOTEL_DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
    finally:
        conn.close()


def migrate():
    with connection() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','operator')),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_login TEXT
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            csrf_token TEXT NOT NULL, expires_at REAL NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS manual_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, entry_event_id TEXT NOT NULL UNIQUE REFERENCES hotel_vehicle_events(event_id),
            exit_event_id TEXT NOT NULL UNIQUE REFERENCES hotel_vehicle_events(event_id),
            status TEXT NOT NULL DEFAULT 'COMPLETED' CHECK(status='COMPLETED'),
            linked_by_user_id INTEGER NOT NULL REFERENCES users(id), linked_at TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            CHECK(entry_event_id <> exit_event_id)
        );
        CREATE TABLE IF NOT EXISTS event_plate_corrections (
            event_id TEXT PRIMARY KEY REFERENCES hotel_vehicle_events(event_id),
            plate_manual TEXT NOT NULL CHECK(length(plate_manual)>0),
            corrected_by_user_id INTEGER NOT NULL REFERENCES users(id), corrected_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            old_value TEXT, new_value TEXT, created_at TEXT NOT NULL
        );
        ''')


def create_user(username, password, role):
    username = username.strip()
    if not username or len(username) > 100 or role not in ('admin', 'operator'):
        raise ValueError('Usuario o rol inválido.')
    if len(password) < 12 or len(password) > 1024:
        raise ValueError('La contraseña debe tener entre 12 y 1024 caracteres.')
    hashed = PASSWORDS.hash(password)
    with connection() as c, c:
        c.execute('INSERT INTO users(username,password_hash,role,created_at,updated_at) VALUES(?,?,?,?,?)',
                  (username, hashed, role, now(), now()))


def authenticate(username, password):
    with connection() as c:
        row = c.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        try:
            PASSWORDS.verify(row['password_hash'] if row else DUMMY_HASH, password)
        except (VerificationError, InvalidHashError):
            return None
        if not row or not row['is_active']:
            return None
        with c:
            if PASSWORDS.check_needs_rehash(row['password_hash']):
                c.execute('UPDATE users SET password_hash=?,updated_at=? WHERE id=?',
                          (PASSWORDS.hash(password), now(), row['id']))
            c.execute('UPDATE users SET last_login=? WHERE id=?', (now(), row['id']))
        return {k: row[k] for k in ('id', 'username', 'role')}


def new_session(user_id, seconds):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with connection() as c, c:
        c.execute('DELETE FROM auth_sessions WHERE expires_at<=?', (time.time(),))
        c.execute('INSERT INTO auth_sessions VALUES(?,?,?,?,?)',
                  (digest(token), user_id, csrf, time.time() + seconds, now()))
    return token, csrf


def session(token):
    if not token:
        return None
    with connection() as c:
        row = c.execute('''SELECT u.id,u.username,u.role,s.csrf_token FROM auth_sessions s
            JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.is_active=1''',
                        (digest(token), time.time())).fetchone()
        return dict(row) if row else None


def revoke(token):
    if token:
        with connection() as c, c:
            c.execute('DELETE FROM auth_sessions WHERE token_hash=?', (digest(token),))


def normalize_plate(value):
    if not isinstance(value, str):
        raise ValueError('La placa debe ser texto.')
    value = ''.join(value.upper().split()).replace('-', '')
    if not value or len(value) > 64 or not any(ch.isalnum() for ch in value):
        raise ValueError('Introduce una placa válida (1–64 caracteres).')
    return value


EVENT_SELECT = '''SELECT e.*, e.plate AS plate_original, p.plate_manual,
    COALESCE(p.plate_manual,e.plate) AS plate_effective,
    p.corrected_by_user_id AS plate_corrected_by_user_id,p.corrected_at AS plate_corrected_at,
    v.id AS visit_id FROM hotel_vehicle_events e
    LEFT JOIN event_plate_corrections p ON p.event_id=e.event_id
    LEFT JOIN visit_links v ON v.revoked_at IS NULL AND (v.entry_event_id=e.event_id OR v.exit_event_id=e.event_id)'''


def event_dict(row):
    value = dict(row)
    value['plate'] = value['plate_effective']
    value['verification_status'] = 'CORREGIDA_MANUALMENTE' if value['plate_manual'] else value['plate_status']
    value['visit_status'] = 'COMPLETED' if value['visit_id'] else None
    return value


def events(c):
    return [event_dict(r) for r in c.execute(EVENT_SELECT + ' ORDER BY e.occurred_at DESC')]


def event(c, event_id):
    row = c.execute(EVENT_SELECT + ' WHERE e.event_id=?', (event_id,)).fetchone()
    if not row:
        raise LookupError('Evento no encontrado.')
    return event_dict(row)


def audit(c, user_id, action, entity, entity_id, old, new):
    c.execute('INSERT INTO audit_log(user_id,action,entity_type,entity_id,old_value,new_value,created_at) VALUES(?,?,?,?,?,?,?)',
              (user_id, action, entity, str(entity_id), json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False), now()))


def correct_plate(event_id, plate, user, require_case=False, token=None):
    plate = normalize_plate(plate)
    with connection() as c, c:
        c.execute("BEGIN IMMEDIATE")
        visits.command(
            c,
            user["id"],
            token,
            ["plate", event_id, plate, require_case],
            lambda: _correct_plate(c, event_id, plate, user, require_case),
        )
        return event(c, event_id)


def _correct_plate(c, event_id, plate, user, require_case):
    if require_case:
        visits.case(c, event_id)
    original = event(c, event_id)
    if original["plate_manual"] == plate:
        return
    c.execute(
        """INSERT INTO event_plate_corrections VALUES(?,?,?,?) ON CONFLICT(event_id)
        DO UPDATE SET plate_manual=excluded.plate_manual,corrected_by_user_id=excluded.corrected_by_user_id,
        corrected_at=excluded.corrected_at""",
        (event_id, plate, user["id"], now()),
    )
    owner = c.execute(
        "SELECT entry_event_id FROM visit_links WHERE revoked_at IS NULL AND exit_event_id=?", (event_id,)
    ).fetchone()
    if c.execute("SELECT 1 FROM visit_cases WHERE id=?", (event_id,)).fetchone():
        visits.record(c, owner[0] if owner else event_id, "PLACA_CORREGIDA", user["id"], event_id=event_id)
    audit(c, user["id"], "PLATE_CHANGED", "event", event_id, original["plate"], plate)


def timestamp(value):
    return visits.epoch(value)


def unpaired(kind, entry_id=None):
    with connection() as c:
        candidates = [
            e
            for e in events(c)
            if not e["visit_id"] and e["event_type"] in (ENTRY_TYPES if kind == "entries" else EXIT_TYPES)
        ]
        if entry_id:
            entry = event(c, entry_id)
            if entry["event_type"] not in ENTRY_TYPES or entry["visit_id"]:
                raise ValueError("La entrada no está disponible.")
            candidates = [
                e for e in candidates if timestamp(e["occurred_at"]) >= timestamp(entry["occurred_at"])
            ]

            def rank(e):
                a, b = entry["plate"] or "", e["plate"] or ""
                a, b = (
                    a.replace("-", "").replace(" ", "").upper(),
                    b.replace("-", "").replace(" ", "").upper(),
                )
                similarity = SequenceMatcher(None, a, b).ratio() if a and b else 0
                return (
                    0 if a and a == b else 1 if similarity >= 0.7 else 2,
                    -similarity if similarity >= 0.7 else 0,
                    timestamp(e["occurred_at"]),
                    e["event_id"],
                )

            candidates.sort(key=rank)
        return candidates


def associate(entry_id, exit_id, user, token=None):
    with connection() as c, c:
        c.execute("BEGIN IMMEDIATE")
        return visits.command(
            c,
            user["id"],
            token,
            ["associate", entry_id, exit_id],
            lambda: _associate(c, entry_id, exit_id, user),
        )


def _associate(c, entry_id, exit_id, user):
    entry, departure = event(c, entry_id), event(c, exit_id)
    if entry["event_type"] not in ENTRY_TYPES or departure["event_type"] not in EXIT_TYPES:
        raise ValueError("Selecciona una entrada y una salida reales.")
    if entry["visit_id"] and entry["visit_id"] == departure["visit_id"]:
        return dict(
            id=entry["visit_id"],
            canonical_id=entry_id,
            status="COMPLETED",
            entry_event_id=entry_id,
            exit_event_id=exit_id,
        )
    association = visits.link(c, entry_id, exit_id, "manual", user["id"])
    visit = dict(
        c.execute("SELECT * FROM visit_links WHERE id=?", (association,)).fetchone(),
        status="COMPLETED",
        canonical_id=entry_id,
    )
    audit(c, user["id"], "VISIT_LINKED", "visit", visit["id"], None, visit)
    return visit


def unlink(visit_id, user, token=None):
    with connection() as c, c:
        c.execute("BEGIN IMMEDIATE")
        return visits.command(c, user["id"], token, ["unlink", visit_id], lambda: _unlink(c, visit_id, user))


def _unlink(c, visit_id, user):
    row = c.execute("SELECT * FROM visit_links WHERE id=?", (visit_id,)).fetchone()
    if not row:
        raise LookupError("Visita no encontrada.")
    if row["revoked_at"] is not None:
        return
    visits.detach(c, visit_id, user["id"])
    audit(c, user["id"], "VISIT_UNLINKED", "visit", visit_id, dict(row), None)


def summary(month=None, rows=None):
    with connection() as c:
        c.execute("BEGIN")
        sql, args = visits.query(None, month)
        totals = dict(c.execute("SELECT kind,count(*)" + sql + " GROUP BY kind", args))
        # Linked departures are hidden from the case list but still count as legacy events.
        time_filter = " WHERE instant>=? AND instant<?" if month else ""
        exits = c.execute(
            "SELECT count(*) FROM visit_cases"
            + time_filter
            + (" AND" if month else " WHERE")
            + " kind='exit'",
            args,
        ).fetchone()[0]
        return dict(
            entries=totals.get("entry", 0),
            exits=exits,
            **visits.indicators(c, month),
            month=month,
            scope="month" if month else "all_history",
        )
