from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import administration as admin, config
from app.api import app, _login_failures
from app.hotel.models import HotelVehicleEvent
from app.hotel.store import HotelStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HOTEL_DATABASE_PATH', str(tmp_path / 'hotel.db'))
    monkeypatch.setenv('DASHBOARD_COOKIE_SECURE', 'false')
    monkeypatch.delenv('DASHBOARD_ENV', raising=False)
    _login_failures.clear()
    with TestClient(app) as client:
        admin.create_user('operator', 'a-long-test-password', 'operator')
        store = HotelStore(config.HOTEL_DATABASE_PATH)
        for id, kind, at, plate in [
            ('entry', 'ENTRADA_NORMAL', '2026-09-01T12:00:00', 'ABC1234'),
            ('entry2', 'ENTRADA_POR_SALIDA', '2026-09-01T11:00:00', None),
            ('exit', 'SALIDA_SIN_ENTRADA', '2026-09-01T14:00:00', 'ABC1234'),
            ('similar', 'SALIDA_NORMAL', '2026-09-01T13:00:00', 'ABC123A'),
            ('unknown', 'SALIDA_POR_ENTRADA', '2026-09-01T12:30:00', None),
            ('early', 'SALIDA_NORMAL', '2026-09-01T10:00:00', 'ABC1234'),
        ]:
            store.save_event_and_update_visit(HotelVehicleEvent(
                event_id=id, occurred_at=datetime.fromisoformat(at), camera_id='A1',
                camera_name='Test', track_id=1, movement='INWARD' if 'ENTRADA' == kind[:7] else 'OUTWARD',
                movement_confidence=.9, event_type=kind, plate=plate, plate_confidence=.9,
                plate_status='LEIDA' if plate else 'NO_LEGIBLE', plate_candidates=[],
                room_number=None, room_confidence=0, room_status='NO_DETERMINADA',
                vehicle_class='car', vehicle_confidence=.9, evidence_path=None, review_required=False, notes=''))
        store.close()
        yield client


def login(client):
    response = client.post('/api/auth/login', json={'username': 'operator', 'password': 'a-long-test-password'})
    assert response.status_code == 200
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return response


def test_sessions_and_backend_protection(client, monkeypatch):
    monkeypatch.setenv('DASHBOARD_AUTH_ENABLED', 'false')
    assert client.get('/', follow_redirects=False).status_code == 303
    assert client.get('/static/index.html', follow_redirects=False).status_code == 303
    for method, url in [('get', '/api/hotel/events'), ('patch', '/api/hotel/events/entry'),
                        ('post', '/api/hotel/visits/associate'), ('delete', '/api/hotel/visits/1/association')]:
        assert getattr(client, method)(url).status_code == 401
    assert client.post('/api/auth/login', json={'username':'operator','password':'wrong'}).status_code == 401
    res = login(client)
    assert 'httponly' in res.headers['set-cookie'].lower()
    assert 'samesite=lax' in res.headers['set-cookie'].lower()
    me = client.get('/api/auth/me').json()
    assert me['user'] == {'id': 1, 'username': 'operator', 'role': 'operator'}
    assert 'password_hash' not in str(me)
    with admin.connection() as c:
        row = c.execute('SELECT * FROM users').fetchone()
        assert row['password_hash'].startswith('$argon2id$') and row['last_login']
        assert c.execute('SELECT token_hash FROM auth_sessions').fetchone()[0] != client.cookies.get('hotel_relax_session')
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.patch('/api/hotel/events/entry', json={'plate':'NEW123'}).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    token = client.cookies.get('hotel_relax_session')
    assert client.post('/api/auth/logout').status_code == 200
    client.cookies.set('hotel_relax_session', token)
    assert client.get('/api/auth/me').status_code == 401
    login(client)
    with admin.connection() as c, c:
        c.execute('UPDATE users SET is_active=0')
    assert client.get('/api/auth/me').status_code == 401
    assert client.post('/api/auth/login', json={'username':'operator','password':'a-long-test-password'}).status_code == 401


def test_manual_lifecycle_and_evidence_preservation(client):
    login(client)
    with admin.connection() as c:
        originals = [tuple(r) for r in c.execute('SELECT * FROM hotel_vehicle_events ORDER BY event_id')]
        automatic_visits = [tuple(r) for r in c.execute('SELECT * FROM hotel_visits')]
    for invalid in ['', '   ', '---', None]:
        assert client.patch('/api/hotel/events/entry', json={'plate':invalid}).status_code == 422
    response = client.patch('/api/hotel/events/entry', json={'plate':' ab-c 1234 '})
    assert response.status_code == 200
    event = response.json()['event']
    assert event['plate'] == 'ABC1234' and event['plate_original'] == 'ABC1234'
    assert event['plate_corrected_by_user_id'] == 1 and event['plate_corrected_at']
    assert client.patch('/api/hotel/events/entry', json={'plate':'PBC1234'}).status_code == 200
    assert len(client.get('/api/hotel/events?plate=PBC').json()['events']) == 1
    assert client.get('/api/hotel/events/entry').json()['plate_original'] == 'ABC1234'
    client.patch('/api/hotel/events/entry', json={'plate':'ABC1234'})
    exits = client.get('/api/hotel/events/exits/unpaired?entry_event_id=entry').json()['events']
    assert [e['event_id'] for e in exits] == ['exit', 'similar', 'unknown']
    for entry, exit, status in [('missing','exit',404), ('exit','entry',422), ('entry','early',422), ('entry','entry',422)]:
        assert client.post('/api/hotel/visits/associate', json={'entry_event_id':entry,'exit_event_id':exit}).status_code == status
    res = client.post('/api/hotel/visits/associate', json={'entry_event_id':'entry','exit_event_id':'exit'})
    assert res.status_code == 200
    visit = res.json()['visit']
    assert visit['status'] == 'COMPLETED'
    for entry, exit in [('entry2', 'exit'), ('entry','similar')]:
        assert client.post('/api/hotel/visits/associate', json={'entry_event_id':entry,'exit_event_id':exit}).status_code == 409
    stats = client.get('/api/hotel/summary').json()
    assert (stats['entries'],stats['exits'],stats['visits_completed'],stats['entries_unpaired'],stats['exits_unpaired']) == (2,4,1,1,3)
    assert client.get('/api/hotel/visits').json()['total'] == 1
    assert client.post('/api/hotel/rebuild-visits').status_code == 409
    assert client.delete(f"/api/hotel/visits/{visit['id']}/association").status_code == 200
    assert client.get('/api/hotel/summary').json()['visits_completed'] == 0
    linked_again = client.post('/api/hotel/visits/associate', json={'entry_event_id':'entry','exit_event_id':'exit'}).json()['visit']
    assert linked_again['id'] > visit['id']
    assert client.delete(f"/api/hotel/visits/{linked_again['id']}/association").status_code == 200
    assert len(client.get('/api/hotel/events/entries/unpaired').json()['events']) == 2
    with admin.connection() as c:
        assert originals == [tuple(r) for r in c.execute('SELECT * FROM hotel_vehicle_events ORDER BY event_id')]
        assert automatic_visits == [tuple(r) for r in c.execute('SELECT * FROM hotel_visits')]
        actions = [r[0] for r in c.execute('SELECT action FROM audit_log')]
        assert 'PLATE_CHANGED' in actions and 'VISIT_LINKED' in actions and 'VISIT_UNLINKED' in actions


def test_concurrent_association(client):
    user = {'id':1}
    def link(entry):
        try:
            return admin.associate(entry, 'exit', user)['status']
        except FileExistsError:
            return 'CONFLICT'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(link, ['entry','entry2'])) == ['COMPLETED','CONFLICT']


def test_migration_idempotency_expiration_and_production_cookie(client, monkeypatch):
    admin.migrate()
    admin.migrate()
    with pytest.raises(sqlite3.IntegrityError):
        admin.create_user('operator','another-long-password','admin')
    login(client)
    with admin.connection() as c, c:
        c.execute('UPDATE auth_sessions SET expires_at=0')
    assert client.get('/api/auth/me').status_code == 401
    monkeypatch.setenv('DASHBOARD_ENV','production')
    response = login(client)
    assert all('Secure' in cookie for cookie in response.headers.get_list('set-cookie'))
