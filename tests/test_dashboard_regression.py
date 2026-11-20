"""HTML + real JS + authenticated FastAPI integration (isolated databases)."""
import hashlib
import ast
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import socket
import threading
import time

import cv2
import numpy as np
import pytest
import uvicorn

from app import api, config, runtime
from test_manual_administration import client, login  # reusable isolated SQLite fixture

ROOT = Path(__file__).resolve().parents[1]


class DashboardHTML(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.ids = []
        self.scripts = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        if tag == 'script' and 'src' in attrs:
            self.scripts.append(attrs['src'])


def test_dom_contract():
    html = DashboardHTML((ROOT / 'web/index.html').read_text())
    js = (ROOT / 'web/app.js').read_text()
    used = set(re.findall(r'''getElementById\(["']([^"']+)["']\)''', js))
    for array in re.findall(r'requireElements\([^,]+,\s*(\[[\s\S]*?\])\)', js):
        used.update(ast.literal_eval(array))
    assert not used - set(html.ids), f'Missing DOM IDs: {used - set(html.ids)}'
    assert len(html.ids) == len(set(html.ids)), 'Duplicate HTML IDs'
    assert any(src.split('?')[0] == '/static/app.js' for src in html.scripts)


@pytest.fixture
def runtime_fixture(tmp_path, monkeypatch, client):
    monkeypatch.setattr(config, 'BASE_DIR', str(tmp_path))
    directory = tmp_path / 'data/runtime'
    directory.mkdir(parents=True)
    monkeypatch.setattr(runtime, 'RUNTIME_DIR', directory)
    status = {'running': True, 'consumer_alive': True, 'cameras': {
        cam: {'connected':True, 'capture_fps':25, 'inference_fps':3, 'active_tracks':1}
        for cam in ('A1','A5')}}
    (directory / 'hotel_status.json').write_text(json.dumps(status))
    for cam in ('A1','A5'):
        cv2.imwrite(str(directory / f'{cam}_latest.jpg'), np.zeros((40,60,3), dtype=np.uint8))
    return status


def test_authenticated_dashboard_contract(client, runtime_fixture):
    assert client.get('/api/hotel/cameras/A1/snapshot').status_code == 401
    login(client)
    html = client.get('/')
    assert html.status_code == 200
    parsed = DashboardHTML(html.text)
    script = next(s for s in parsed.scripts if s.split('?')[0] == '/static/app.js')
    static = client.get(script)
    assert static.status_code == 200
    assert '?v=' in script
    assert 'must-revalidate' in static.headers['cache-control']
    assert DashboardHTML(client.get('/').text).scripts == parsed.scripts
    assert hashlib.sha256(static.content).digest() == hashlib.sha256((ROOT/'web/app.js').read_bytes()).digest()
    assert client.get('/api/auth/me').status_code == 200
    assert client.get('/api/hotel/status').json() == runtime_fixture
    data = client.get('/api/hotel/events').json()
    assert data['total'] == 6
    assert client.get('/api/hotel/events?camera=&event_type=&plate_status=').json()['total'] == 6
    assert all(e['visit_id'] is None and e['plate_manual'] is None for e in data['events'])
    assert client.get('/api/hotel/events?page=2&page_size=2').json()['total'] == 6
    summary = client.get('/api/hotel/summary').json()
    assert (summary['entries'],summary['exits'],summary['visits_completed']) == (2,4,0)
    for cam in ('A1','A5'):
        response = client.get(f'/api/hotel/cameras/{cam}/snapshot')
        assert response.status_code == 200 and response.headers['content-type'] == 'image/jpeg'


@pytest.fixture
def browser_page(client, runtime_fixture):
    from playwright.sync_api import sync_playwright
    # Real browser -> real HTTP -> FastAPI, with the temporary fixture DB only.
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api.app, host='127.0.0.1', port=port, lifespan='off', log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(8000)
            yield page, f'http://127.0.0.1:{port}'
            browser.close()
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


def browser_login(page, base):
    page.goto(base + '/login')
    page.locator('#loginUsername').fill('operator')
    page.locator('#loginPassword').fill('a-long-test-password')
    page.locator('#loginSubmit').click()
    page.wait_for_url(base + '/')


def test_browser_complete_flow(browser_page):
    from playwright.sync_api import expect
    page, base = browser_page
    errors = []
    page.on('pageerror', lambda err: errors.append(str(err)))
    browser_login(page, base)
    expect(page.locator('#detectorStatus')).to_have_text('● Detector Online')
    for cam in ('A1','A5'):
        expect(page.locator(f'#cam{cam}_state')).to_have_text('Conectado')
        page.wait_for_function(f"document.getElementById('cam{cam}_img').naturalWidth > 0")
    expect(page.locator('#eventsTable tr')).to_have_count(6)
    expect(page.locator('#statEntradas')).to_have_text('2')
    row = page.locator('#eventsTable tr').filter(has=page.get_by_role('button',name='Asociar salida')).filter(has_text='ABC1234')
    row.get_by_role('button',name='Editar placa').click()
    expect(page.locator('#modalPlateOriginal')).to_have_text('ABC1234')
    page.locator('#modalPlate').fill('PBC1234')
    page.locator('#btnSaveCorrection').click()
    expect(page.locator('#correctionMessage')).to_have_text('✓ Corrección guardada')
    row = page.locator('#eventsTable tr').filter(has_text='PBC1234')
    row.get_by_role('button',name='Asociar salida').click()
    page.locator('input[name=exit_event_id][value=exit]').check()
    page.locator('#confirmAssociation').click()
    expect(page.locator('#statVisitasActivas')).to_have_text('1')
    page.on('dialog', lambda dialog: dialog.accept())
    row.get_by_role('button',name='Desasociar').click()
    expect(page.locator('#statVisitasActivas')).to_have_text('0')
    expect(row.get_by_role('button',name='Asociar salida')).to_be_visible()
    page.locator('#logoutBtn').click()
    page.wait_for_url(base + '/login')
    page.goto(base + '/')
    expect(page).to_have_url(base + '/login')
    assert not errors


def test_browser_missing_feature_and_snapshot_failure_are_isolated(browser_page):
    from playwright.sync_api import expect
    page, base = browser_page
    errors, logs = [], []
    page.on('pageerror', lambda err: errors.append(str(err)))
    page.on('console', lambda msg: logs.append(msg.text) if msg.type == 'error' else None)
    def incomplete_html(route):
        response = route.fetch()
        route.fulfill(response=response, body=response.text().replace('id="associationModal"', 'id="missingAssociation"'))
    page.route(base+'/', incomplete_html)
    page.route('**/snapshot?*', lambda route: route.fulfill(status=503, json={'detail':'Snapshot no disponible'}))
    browser_login(page,base)
    expect(page.locator('#detectorStatus')).to_have_text('● Detector Online')
    expect(page.locator('#eventsTable tr')).to_have_count(6)
    expect(page.locator('#statEntradas')).to_have_text('2')
    expect(page.locator('#camA1_overlay')).to_have_text('SIN IMAGEN')
    page.evaluate('initializeDashboard(); initializeDashboard(); startPolling(); startPolling();')
    assert page.evaluate('pollingTimers.length') == 4
    assert any('Asociación: faltan elementos DOM: associationModal' in msg for msg in logs)
    page.locator('#logoutBtn').click()
    expect(page).to_have_url(base+'/login')
    assert not errors


def test_browser_invalid_session_never_starts_polling(browser_page):
    from playwright.sync_api import expect
    page, base = browser_page
    requests = []
    page.on('request', lambda req: requests.append(req.url))
    page.route('**/api/auth/me', lambda route: route.fulfill(status=401, json={'detail':'Sesión requerida.'}))
    # Bypass only the HTML gate for this test; the JS still receives a real failed session lookup.
    page.route(base+'/', lambda route: route.fulfill(status=200, content_type='text/html', body=api.web_page('index.html').body))
    page.goto(base+'/')
    expect(page).to_have_url(base+'/login')
    assert not any('/api/hotel/' in url for url in requests)


def test_browser_bypasses_obsolete_cached_script(browser_page):
    from playwright.sync_api import expect
    page, base = browser_page
    obsolete_requests = []
    def cached_script(route):
        if route.request.url.endswith('/static/app.js'):
            obsolete_requests.append(route.request.url)
            route.fulfill(content_type='text/javascript', body='document.getElementById("btnRebuild").addEventListener("click", () => {});')
        else:
            route.continue_()
    page.route('**/static/app.js*', cached_script)
    browser_login(page,base)
    expect(page.locator('#detectorStatus')).to_have_text('● Detector Online')
    assert not obsolete_requests


def test_database_path_is_independent_of_cwd(client, tmp_path, monkeypatch):
    from app import administration
    monkeypatch.chdir(tmp_path)
    with administration.connection() as conn:
        actual = conn.execute('PRAGMA database_list').fetchone()[2]
        assert actual == config.HOTEL_DATABASE_PATH
        assert Path(actual).is_absolute()
