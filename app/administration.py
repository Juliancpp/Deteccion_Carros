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
    LEFT JOIN manual_visits v ON v.entry_event_id=e.event_id OR v.exit_event_id=e.event_id'''


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


def correct_plate(event_id, plate, user):
    plate = normalize_plate(plate)
    with connection() as c, c:
        c.execute('BEGIN IMMEDIATE')
        original = event(c, event_id)
        c.execute('''INSERT INTO event_plate_corrections VALUES(?,?,?,?) ON CONFLICT(event_id)
            DO UPDATE SET plate_manual=excluded.plate_manual,corrected_by_user_id=excluded.corrected_by_user_id,
            corrected_at=excluded.corrected_at''', (event_id, plate, user['id'], now()))
        audit(c, user['id'], 'PLATE_CHANGED', 'event', event_id, original['plate'], plate)
        return event(c, event_id)


def timestamp(value):
    dt = datetime.fromisoformat(value)
    # Existing naive timestamps use server local time; aware timestamps retain their offset.
    return dt.timestamp()


def unpaired(kind, entry_id=None):
    with connection() as c:
        candidates = [e for e in events(c) if not e['visit_id'] and e['event_type'] in (ENTRY_TYPES if kind == 'entries' else EXIT_TYPES)]
        if entry_id:
            entry = event(c, entry_id)
            if entry['event_type'] not in ENTRY_TYPES or entry['visit_id']:
                raise ValueError('La entrada no está disponible.')
            candidates = [e for e in candidates if timestamp(e['occurred_at']) >= timestamp(entry['occurred_at'])]
            def rank(e):
                a, b = entry['plate'] or '', e['plate'] or ''
                a, b = a.replace('-', '').replace(' ', '').upper(), b.replace('-', '').replace(' ', '').upper()
                similarity = SequenceMatcher(None, a, b).ratio() if a and b else 0
                return (0 if a and a == b else 1 if similarity >= .7 else 2,
                        -similarity if similarity >= .7 else 0, timestamp(e['occurred_at']), e['event_id'])
            candidates.sort(key=rank)
        return candidates


def associate(entry_id, exit_id, user):
    with connection() as c, c:
        c.execute('BEGIN IMMEDIATE')
        entry, departure = event(c, entry_id), event(c, exit_id)
        if entry['event_type'] not in ENTRY_TYPES or departure['event_type'] not in EXIT_TYPES:
            raise ValueError('Selecciona una entrada y una salida reales.')
        if entry['visit_id'] or departure['visit_id']:
            raise FileExistsError('Uno de los eventos ya está asociado. Desasocia primero.')
        if timestamp(departure['occurred_at']) < timestamp(entry['occurred_at']):
            raise ValueError('La salida no puede ser anterior a la entrada.')
        date = now()
        cur = c.execute('''INSERT INTO manual_visits(entry_event_id,exit_event_id,linked_by_user_id,linked_at,created_at,updated_at)
                           VALUES(?,?,?,?,?,?)''', (entry_id, exit_id, user['id'], date, date, date))
        visit = dict(c.execute('SELECT * FROM manual_visits WHERE id=?', (cur.lastrowid,)).fetchone())
        audit(c, user['id'], 'VISIT_LINKED', 'visit', visit['id'], None, visit)
        return visit


def unlink(visit_id, user):
    with connection() as c, c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM manual_visits WHERE id=?', (visit_id,)).fetchone()
        if not row:
            raise LookupError('Visita no encontrada.')
        audit(c, user['id'], 'VISIT_UNLINKED', 'visit', visit_id, dict(row), None)
        c.execute('DELETE FROM manual_visits WHERE id=?', (visit_id,))


def summary(month=None, rows=None):
    with connection() as c:
        all_events = rows if rows is not None else events(c)
        if month:
            start = f"{month}-01"
            year, mon = map(int, month.split('-'))
            end = f"{year + (mon == 12):04d}-{1 if mon == 12 else mon + 1:02d}-01"
            all_events = [e for e in all_events if start <= (e.get('occurred_at') or '') < end]
        entries = [e for e in all_events if e['event_type'] in ENTRY_TYPES]
        exits = [e for e in all_events if e['event_type'] in EXIT_TYPES]
        visit_count = c.execute('''SELECT count(*) FROM manual_visits v JOIN hotel_vehicle_events e
                                   ON e.event_id=v.entry_event_id WHERE ? IS NULL OR (e.occurred_at>=? AND e.occurred_at<?)''',
                                (month, f"{month}-01" if month else '', end if month else '')).fetchone()[0]
        return dict(entries=len(entries), exits=len(exits),
                    visits_completed=visit_count,
                    entries_unpaired=sum(not e['visit_id'] for e in entries),
                    exits_unpaired=sum(not e['visit_id'] for e in exits),
                    review_pending=sum(bool(e.get('review_required')) for e in all_events),
                    month=month, scope='month' if month else 'all_history')
