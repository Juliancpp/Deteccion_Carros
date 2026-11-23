from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import time

from openpyxl import load_workbook
from playwright.sync_api import expect

from app import administration, config
from test_manual_administration import client, login
from test_dashboard_regression import browser_page, browser_login, runtime_fixture


CASES = "/api/hotel/visit-cases"
REPORT = "/api/hotel/reports/visits"
SETTINGS = "/api/hotel/display-settings"


def test_history_and_workbook_share_rows(client):
    login(client)
    filters = dict(
        month="2026-09",
        date_from="2026-09-01",
        date_to="2026-09-01",
        state="ACTIVA",
        plate="bc12",
        q="ent",
        camera="A1",
        order="asc",
    )
    data = client.get(CASES, params=filters).json()
    assert [r["id"] for r in data["visits"]] == ["entry"]
    report = client.post(REPORT, json=filters)
    assert report.headers["X-Visit-Count"] == "1"
    rows = list(load_workbook(BytesIO(report.content), read_only=True).active.values)
    assert [r[0] for r in rows[1:]] == ["entry"]
    for overrides in [dict(camera="A5"), dict(room=1), dict(month="2026-08"), dict(plate="%")]:
        assert client.get(CASES, params={**filters, **overrides}).json()["total"] == 0
    assert client.get(CASES, params=dict(date_from="2026-10-01", date_to="2026-09-01")).status_code == 422
    assert client.post(REPORT, json=dict(date_to="yesterday")).status_code == 422
    assert client.get(CASES, params=dict(page=2, page_size=1)).json()["visits"][0]["id"] == "similar"


def test_settings_roles_history_and_csrf(client):
    assert client.get(SETTINGS).status_code == 401
    login(client)
    payload = dict(page_size=2, revision=0)
    assert client.patch(SETTINGS, json=payload).status_code == 403
    administration.create_user("admin-c3", "local-admin-password", "admin")
    res = client.post("/api/auth/login", json=dict(username="admin-c3", password="local-admin-password"))
    assert client.patch(SETTINGS, json=payload).status_code == 403
    client.headers["X-CSRF-Token"] = res.json()["csrf_token"]
    assert client.patch(SETTINGS, json=dict(payload, page_size=101)).status_code == 422
    assert client.patch(SETTINGS, json=dict(payload, rtsp="invalid")).status_code == 422
    assert client.patch(SETTINGS, json=payload).json()["page_size"] == 2
    assert client.patch(SETTINGS, json=payload).status_code == 409
    assert client.get(SETTINGS).json()["history"][0]["page_size"] == 2
    with administration.connection() as c:
        assert (
            c.execute("SELECT count(*) FROM audit_log WHERE action='DISPLAY_SETTINGS_CHANGED'").fetchone()[0]
            == 1
        )


def test_monitor_stale_snapshot_and_heartbeat(client, runtime_fixture, tmp_path):
    login(client)
    folder = tmp_path / "data/runtime"
    old = time.time() - 100
    for filename in ("A1_latest.jpg", "hotel_status.json"):
        os.utime(folder / filename, (old, old))
    status = client.get("/api/hotel/monitor").json()
    assert not status["running"] and not status["cameras"]["A1"]["connected"]
    assert status["cameras"]["A1"]["snapshot"]["stale"]
    assert "last_plate" not in status["cameras"]["A1"]
    photo = client.get("/api/hotel/cameras/A1/snapshot?allow_stale=true")
    assert photo.headers["X-Snapshot-Stale"] == "true" and photo.content.startswith(b"\xff\xd8")


def test_browser_history_report_navigation_mobile(browser_page, tmp_path):
    page, base = browser_page
    browser_login(page, base)
    cards = page.locator(".cards button")
    expect(cards).to_have_count(4)
    for key in ("visits_completed", "entries_unpaired", "exits_unpaired", "review_pending"):
        page.get_by_role("link", name="Control", exact=True).click()
        card = page.locator(f'[data-category="{key}"]')
        expect(card.locator("strong")).to_have_text(re.compile(r"^\d+$"))
        value = int(card.locator("strong").inner_text())
        card.click()
        expect(page.locator("#eventsMeta")).to_have_text(f"{value} registro" + ("" if value == 1 else "s"))
    page.locator("#clearFilters").click()
    history = page.locator("#filterForm")
    history.get_by_label("Buscar").fill("entry2")
    history.get_by_role("button", name="Filtrar").click()
    expect(page.locator("#eventsMeta")).to_have_text("1 registro")
    history.get_by_label("Mes", exact=True).fill("2025-06")
    history.get_by_role("button", name="Filtrar").click()
    expect(page.get_by_text("Sin resultados con estos filtros.")).to_be_visible()
    page.locator("#clearFilters").click()
    page.get_by_role("link", name="Reportes").click()
    report = page.locator("#reportForm")
    report.get_by_label("Estado").select_option("ACTIVA")
    report.get_by_role("button", name="Generar Excel").click()
    expect(page.locator("#reportStatus")).to_have_text("2 registros exportados.")
    with page.expect_download() as dl:
        page.locator("#download").click()
    assert len(list(load_workbook(dl.value.path().open("rb"), read_only=True).active.values)) == 3
    report.get_by_label("Desde").fill("2026-09-02")
    expect(page.locator("#download")).to_be_hidden()
    page.route(
        "**/api/hotel/reports/visits",
        lambda r: r.fulfill(status=503, json={"detail": "Generación interrumpida"}),
    )
    report.get_by_role("button", name="Generar Excel").click()
    expect(page.locator("#reportStatus")).to_have_text("Generación interrumpida")
    page.get_by_role("link", name="Configuración").click()
    expect(page.locator("#pageSize")).to_be_disabled()
    page.get_by_role("link", name="Cámaras").click()
    expect(page.locator("#camA1_img")).to_be_visible()
    expect(page.locator("#infoA5")).to_contain_text("3 FPS")
    page.get_by_role("link", name="Control", exact=True).click()
    page.locator("#monthPicker").fill("2026-09")
    expect(page.locator("#eventsMeta")).to_have_text("6 registros")
    page.locator("#monthPicker").blur()
    page.set_viewport_size({"width": 1440, "height": 950})
    page.screenshot(path=tmp_path / "dashboard-escritorio.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=tmp_path / "dashboard-movil.png", full_page=True)
    page.locator("button[data-detail=entry]").click()
    expect(page.locator("#correctionModal")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#correctionModal")).to_be_hidden()


def test_browser_media_room_and_review(browser_page, tmp_path, monkeypatch):
    page, base = browser_page
    captures = tmp_path / "captures"
    captures.mkdir()
    monkeypatch.setattr(config, "CAPTURE_DIR", str(captures))
    photo = captures / "private-name.jpg"
    shutil.copy(tmp_path / "data/runtime/A1_latest.jpg", photo)
    clips = tmp_path / "data/clips"
    clips.mkdir()
    clip = clips / "private-name.mp4"
    shutil.copy(Path(__file__).parent / "fixtures/visit-synthetic.mp4", clip)
    with administration.connection() as c, c:
        c.execute(
            "UPDATE hotel_vehicle_events SET evidence_path=?,clip_path=? WHERE event_id='entry'",
            (str(photo), str(clip)),
        )
    browser_login(page, base)
    page.locator("button[data-detail=entry]").click()
    panel = page.locator("#correctionModal")
    page.wait_for_function("document.querySelector('dialog article img')?.naturalWidth > 0")
    page.wait_for_function("document.querySelector('dialog video')?.videoWidth > 0")
    assert "private-name" not in panel.locator("img").get_attribute("src")
    assert page.evaluate("document.querySelector('video').duration") > 0
    panel.get_by_role("button", name="Corregir habitación").click()
    page.locator("#modalPlate").fill("4")
    page.locator("#btnSaveCorrection").click()
    page.reload()
    page.locator("button[data-detail=entry]").click()
    expect(panel).to_contain_text("Habitación · 4")
    page.on("dialog", lambda d: d.accept())
    panel.get_by_role("button", name="Marcar revisión").click()
    panel.get_by_role("button", name="Resolver revisión").click()
    expect(panel.get_by_role("button", name="Marcar revisión")).to_be_visible()
    expect(panel).to_contain_text("REVISION_CONFIRMADA")
    expect(panel.get_by_role("button", name="Cierre excepcional")).to_have_count(0)


def test_browser_admin_rollback_pagination_and_close(browser_page):
    page, base = browser_page
    administration.create_user("admin-c3", "local-admin-password", "admin")
    page.goto(base + "/login")
    page.locator("#loginUsername").fill("admin-c3")
    page.locator("#loginPassword").fill("local-admin-password")
    page.locator("#loginSubmit").click()
    page.on("dialog", lambda d: d.accept("Validación local") if d.type == "prompt" else d.accept())
    page.get_by_role("link", name="Configuración").click()
    page.locator("#pageSize").fill("1")
    page.get_by_role("button", name="Guardar cambios").click()
    expect(page.locator("#settingStatus")).to_have_text("Preferencia guardada.")
    page.get_by_role("link", name="Historial").click()
    expect(page.locator("#pageIndicator")).to_have_text("1 / 6")
    page.get_by_role("button", name="Página siguiente").click()
    expect(page.locator("#pageIndicator")).to_have_text("2 / 6")
    page.get_by_role("button", name="Página anterior").click()
    expect(page.locator("#pageIndicator")).to_have_text("1 / 6")
    page.get_by_role("link", name="Configuración").click()
    page.get_by_role("button", name="Restaurar valor inicial (20)").click()
    expect(page.locator("#pageSize")).to_have_value("20")
    page.get_by_role("link", name="Historial").click()
    page.locator("button[data-detail=entry]").click()
    page.get_by_role("button", name="Cierre excepcional").click()
    expect(page.locator("#modalAuditLogs")).to_contain_text("CIERRE_EXCEPCIONAL")


def test_browser_unassigned_room_and_expired_session(browser_page):
    from app.hotel import visits
    from app.hotel.geometry import GeometryEvent
    from datetime import datetime

    page, base = browser_page
    with administration.connection() as c, c:
        visits.geometry(
            c, GeometryEvent("late", "A1", 44, 15, datetime(2026, 9, 1, 12, 15), "hab-3", "ROOM_ENTRY")
        )
    browser_login(page, base)
    page.locator('[data-category="review_pending"]').click()
    row = page.locator("#eventsTable tr").filter(has_text="Habitación sin asignar")
    row.get_by_role("button", name="Ver detalle").click()
    page.on("dialog", lambda d: d.accept("entry") if d.type == "prompt" else d.accept())
    page.get_by_role("button", name="Asignar a entrada").click()
    expect(page.locator("#modalAuditLogs")).to_contain_text("Habitación · 3")
    page.locator("#btnCloseModal").click()
    with administration.connection() as c, c:
        c.execute("UPDATE auth_sessions SET expires_at=0")
    page.locator("#clearFilters").click()
    page.wait_for_url(base + "/login")


def test_browser_more_exits_and_missing_media(browser_page, tmp_path, monkeypatch):
    from test_canonical_visits import sample
    from app.hotel.store import HotelStore

    page, base = browser_page
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    try:
        for number in range(28):
            store.save_event_and_update_visit(sample("orphan-" + str(number), number, departure=True))
    finally:
        store.close()
    monkeypatch.setattr(config, "CAPTURE_DIR", str(tmp_path))
    with administration.connection() as c, c:
        c.execute(
            "UPDATE hotel_vehicle_events SET evidence_path=?,ocr_diagnostics_json='incorrecto' WHERE event_id='entry'",
            (str(tmp_path / "missing.jpg"),),
        )
    browser_login(page, base)
    page.get_by_role("link", name="Abrir historial").click()
    page.locator("#filterForm").get_by_label("Buscar").fill("entry")
    page.locator("#filterForm").get_by_role("button", name="Filtrar").click()
    row = page.locator("#eventsTable tr").filter(has_text="ABC1234")
    row.get_by_role("button", name="Ver detalle").click()
    expect(page.locator("#modalAuditLogs")).to_contain_text("Archivo no disponible")
    page.locator("#btnCloseModal").click()
    row.get_by_role("button", name="Asociar salida").click()
    page.get_by_role("button", name="Más salidas").click()
    expect(page.locator("input[name=exit_event_id]")).to_have_count(31)
    page.get_by_role("button", name="Cancelar").click()


def test_login_controls_and_combined_history_filters(browser_page):
    page, base = browser_page
    with administration.connection() as c, c:
        c.execute("UPDATE hotel_vehicle_events SET source='legacy_eventos' WHERE event_id='entry'")
    page.goto(base + "/login")
    toggle = page.locator("#togglePassword")
    toggle.click()
    expect(page.locator("#loginPassword")).to_have_attribute("type", "text")
    toggle.click()
    page.locator("#loginUsername").fill("operator")
    page.locator("#loginPassword").fill("incorrecta")
    page.locator("#loginSubmit").click()
    expect(page.locator("#loginError")).not_to_be_empty()
    page.locator("#loginPassword").fill("a-long-test-password")
    page.locator("#loginSubmit").click()
    page.get_by_role("link", name="Abrir historial").click()
    form = page.locator("#filterForm")
    form.get_by_label("Desde").fill("2026-09-01")
    form.get_by_label("Hasta").fill("2026-09-01")
    form.get_by_label("Estado").select_option("ACTIVA")
    form.get_by_label("Cámara").select_option("A1")
    form.get_by_label("Placa", exact=True).fill("BC12")
    form.get_by_label("Orden").select_option("asc")
    form.get_by_role("button", name="Filtrar").click()
    expect(page.locator("#eventsMeta")).to_have_text("1 registro")
    expect(page.locator("#eventsTable")).to_contain_text("Histórico importado")
    form.get_by_role("spinbutton",name="Habitación",exact=True).fill("9")
    form.get_by_role("button", name="Filtrar").click()
    expect(page.locator("#eventsMeta")).to_have_text("0 registros")


def test_rendered_buttons_have_click_or_form_handler(browser_page):
    page, base = browser_page
    browser_login(page, base)
    expect(page.locator("#eventsMeta")).to_have_text("6 registros")
    devtools = page.context.new_cdp_session(page)
    result = devtools.send(
        "Runtime.evaluate",
        {
            "expression": """[...document.querySelectorAll('button')].filter(b =>
                !getEventListeners(b).click?.length &&
                !(b.type === 'submit' && b.form && getEventListeners(b.form).submit?.length)
                ).map(b => b.textContent.trim())""",
            "includeCommandLineAPI": True,
            "returnByValue": True,
        },
    )
    assert result["result"]["value"] == []
