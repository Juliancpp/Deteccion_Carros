"""
Deteccion_Carros — API REST (FastAPI)
======================================
Servidor web para consultar eventos, estadísticas y evidencias del Hotel MVP.
"""

import os
import sys
import hmac
import json
import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Query, FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / '.env', override=False)

import app.config as config
from app.database import DatabaseManager
from app.hotel.store import HotelStore
from app.export.excel_exporter import HotelExcelExporter
from app.runtime import is_valid_jpeg, runtime_snapshot_path, snapshot_info, validate_runtime_camera
from app import administration as admin
from contextlib import asynccontextmanager
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field


@asynccontextmanager
async def lifespan(app):
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    store.close()
    admin.migrate()
    yield


app = FastAPI(title="Deteccion_Carros Dashboard", version="1.1.0", lifespan=lifespan)
SESSION_COOKIE = "hotel_relax_session"
CSRF_COOKIE = "hotel_relax_csrf"
_login_failures: dict[str, tuple[int, float]] = {}


def _cookie_secure(request: Request | None = None) -> bool:
    if os.getenv("DASHBOARD_ENV", "").lower() == "production":
        return True
    mode = os.getenv("DASHBOARD_COOKIE_SECURE", "auto").lower()
    if mode in {"true", "false"}:
        return mode == "true"
    return bool(request and request.url.scheme == "https")


def _set_session_cookie(response: Response, user_id: int, request: Request | None = None) -> str:
    max_age = max(300, min(int(float(os.getenv("DASHBOARD_SESSION_HOURS", "8")) * 3600), 86400))
    token, csrf = admin.new_session(user_id, max_age)
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True,
                        secure=_cookie_secure(request), samesite="lax", path="/")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False,
                        secure=_cookie_secure(request), samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"
    return csrf


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "local"


def check_auth(request: Request) -> dict:
    user = admin.session(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="Sesión requerida.")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        token = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(token.encode(), user["csrf_token"].encode()):
            raise HTTPException(status_code=403, detail="CSRF token inválido.")
    return {k: user[k] for k in ("id", "username", "role")}


@app.middleware("http")
async def private_responses(request, call_next):
    if request.url.path == '/static/index.html':
        return RedirectResponse('/', status_code=303)
    response = await call_next(request)
    if request.url.path.startswith(("/api/", "/media/", "/downloads/")) or request.url.path in {"/", "/login"}:
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-cache, max-age=0, must-revalidate'
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


# Initialize DB managers
db = DatabaseManager()

# Static mounts
WEB_DIR = Path(config.BASE_DIR) / "web"
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


def web_page(filename: str):
    """Bind each HTML response to the exact asset contents, including through proxies."""
    html = (WEB_DIR / filename).read_text(encoding='utf-8')
    for asset in ('app.js', 'style.css'):
        version = hashlib.sha256((WEB_DIR / asset).read_bytes()).hexdigest()[:16]
        html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={version}"')
    return HTMLResponse(html)


@app.get("/")
def dashboard(request: Request):
    """Sirve el dashboard web."""
    if not admin.session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse('/login', status_code=303)
    return web_page('index.html')


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return web_page('login.html')


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


@app.post("/api/auth/login")
def login(request: Request, response: Response, payload: LoginPayload):
    key = _client_key(request)
    attempts, locked_until = _login_failures.get(key, (0, 0.0))
    current = time.time()
    if locked_until > current:
        raise HTTPException(429, "Bloqueo temporal activo.")
    if locked_until:
        attempts = 0
    user = admin.authenticate(payload.username.strip(), payload.password)
    if not user:
        attempts += 1
        limit = int(os.getenv("DASHBOARD_LOGIN_MAX_ATTEMPTS", "5"))
        seconds = int(os.getenv("DASHBOARD_LOGIN_LOCK_SECONDS", "300"))
        _login_failures[key] = (attempts, current + seconds if attempts >= limit else 0)
        raise HTTPException(401, "Credenciales incorrectas.")
    _login_failures.pop(key, None)
    admin.revoke(request.cookies.get(SESSION_COOKIE))
    csrf = _set_session_cookie(response, user["id"], request)
    return {"ok": True, "authenticated": True, "username": user["username"], "user": user, "csrf_token": csrf}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, authenticated: dict = Depends(check_auth)):
    admin.revoke(request.cookies.get(SESSION_COOKIE))
    _clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request, authenticated: dict = Depends(check_auth)):
    user = admin.session(request.cookies.get(SESSION_COOKIE))
    return {"authenticated": True, "user": authenticated, "username": authenticated["username"], "csrf_token": user["csrf_token"]}


@app.get("/api/health")
def health():
    status_path = Path(config.BASE_DIR) / "data" / "runtime" / "hotel_status.json"
    detector_heartbeat = False
    if status_path.is_file():
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
            detector_heartbeat = bool(data.get("running")) and bool(data.get("main_loop_heartbeat") or data.get("updated_at"))
        except Exception:
            detector_heartbeat = False
    database = Path(config.HOTEL_DATABASE_PATH).exists()
    snapshots = {cam: snapshot_info(cam).available for cam in ("A1", "A5")}
    overall = "ok" if detector_heartbeat and database else "degraded"
    return {"status": overall, "dashboard": True, "detector_heartbeat": detector_heartbeat, "database": database, "snapshots": snapshots}


# ─── Legacy endpoints for backwards compatibility ───────────────────
@app.get("/api/events")
def get_events(authenticated: bool = Depends(check_auth)):
    """Devuelve los últimos eventos de la base tradicional."""
    return {"events": _events_with_image_urls(db.get_events(limit=50))}


@app.get("/api/events/{limit}")
def get_events_limit(limit: int, authenticated: bool = Depends(check_auth)):
    """Devuelve los últimos N eventos de la base tradicional."""
    safe_limit = max(1, min(limit, 500))
    return {"events": _events_with_image_urls(db.get_events(limit=safe_limit))}


@app.get("/api/stats")
def get_stats(authenticated: bool = Depends(check_auth)):
    """Devuelve métricas del día y distribución por hora."""
    events = db.get_events(limit=5000)
    today = datetime.now().date()
    today_events = [
        event for event in events if datetime.fromisoformat(event["fecha_hora"]).date() == today
    ]

    entradas = sum(1 for event in today_events if event["tipo_evento"].lower() == "entrada")
    salidas = sum(1 for event in today_events if event["tipo_evento"].lower() == "salida")
    vehicles_by_hour = [{"hour": f"{hour:02d}:00", "count": 0} for hour in range(24)]

    for event in today_events:
        hour = datetime.fromisoformat(event["fecha_hora"]).hour
        vehicles_by_hour[hour]["count"] += 1

    return {
        "entradas_hoy": entradas,
        "salidas_hoy": salidas,
        "total_vehiculos": len(today_events),
        "vehiculos_por_hora": vehicles_by_hour,
    }


def _events_with_image_urls(events: list) -> list:
    return [{**event, "imagen_url": _image_url(event.get("imagen", ""))} for event in events]


def _image_url(image_path: str) -> str:
    if not image_path:
        return ""
    abs_image = os.path.abspath(image_path)
    abs_capture_dir = os.path.abspath(config.CAPTURE_DIR)
    if not abs_image.startswith(abs_capture_dir):
        return ""
    relative = os.path.relpath(abs_image, abs_capture_dir).replace(os.sep, "/")
    return f"/media/captures/{relative}"


# ─── Hotel MVP Endpoints ─────────────────────────────────────────────

@app.get("/api/hotel/summary")
def get_hotel_summary(month: Optional[str] = None, authenticated: dict = Depends(check_auth)):
    """Métricas del hotel hoy (entradas, salidas, visitas abiertas, etc.)."""
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    try:
        if month and (len(month) != 7 or month[4] != '-' or not month[:4].isdigit() or not month[5:].isdigit()):
            raise HTTPException(422, "month debe tener formato YYYY-MM")
        return {**store.get_summary_stats(), **admin.summary(month=month)}
    finally:
        store.close()


@app.get("/api/hotel/status")
def get_hotel_status(authenticated: bool = Depends(check_auth)):
    """Lee el estado en tiempo real del runtime generado por el detector."""
    status_path = Path(config.BASE_DIR) / "data" / "runtime" / "hotel_status.json"
    if not status_path.is_file():
        return {"running": False, "message": "Detector status file missing."}
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"running": False, "error": f"Failed to read status: {e}"}


@app.get("/api/hotel/cameras")
def get_hotel_cameras(authenticated: bool = Depends(check_auth)):
    """Obtiene la lista de cámaras y su estado desde el archivo runtime."""
    status_path = Path(config.BASE_DIR) / "data" / "runtime" / "hotel_status.json"
    if not status_path.is_file():
        return {"cameras": {}}
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {"cameras": data.get("cameras", {})}
    except Exception:
        return {"cameras": {}}


def enrich_event(ev):
    ev["evidence_url"] = _relative_evidence_url(ev.get("evidence_path"))
    ev["clip_url"] = _relative_clip_url(ev.get("clip_path"))
    return ev


def manual_operation(function, *args):
    try:
        return function(*args)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/hotel/geometry-events")
def geometry_events(limit: int = Query(100, ge=1, le=500), authenticated: dict = Depends(check_auth)):
    with admin.connection() as conn:
        rows = conn.execute("SELECT * FROM geometry_events ORDER BY occurred_at DESC, rowid DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]


@app.get("/api/hotel/events")
def get_hotel_events(
    page: int = 1, page_size: int = 25, date_from: Optional[str] = None,
    date_to: Optional[str] = None, camera: Optional[str] = None,
    event_type: Optional[str] = None, plate: Optional[str] = None,
    room: Optional[int] = None, review_required: Optional[bool] = None,
    plate_status: Optional[str] = None, month: Optional[str] = None,
    movement: Optional[str] = None, status: Optional[str] = None,
    authenticated: dict = Depends(check_auth)
):
    page, page_size = max(1, page), max(1, min(100, page_size))
    with admin.connection() as c:
        rows = admin.events(c)
    if month:
        if len(month) != 7 or month[4] != '-' or not month[:4].isdigit() or not month[5:].isdigit():
            raise HTTPException(422, "month debe tener formato YYYY-MM")
        year, mon = map(int, month.split('-'))
        if not 1 <= mon <= 12: raise HTTPException(422, "month inválido")
        end = f"{year + (mon == 12):04d}-{1 if mon == 12 else mon + 1:02d}-01"
        # Events are stored as ISO strings, sometimes naive and sometimes offset-aware;
        # matching the YYYY-MM prefix avoids lexical/timezone edge cases while keeping
        # the selected calendar month deterministic.
        rows = [e for e in rows if (e.get("occurred_at") or '').startswith(month)]
    if date_from:
        rows = [e for e in rows if e["occurred_at"] >= date_from]
    if date_to:
        end = date_to + "T23:59:59.999999" if len(date_to) == 10 else date_to
        rows = [e for e in rows if e["occurred_at"] <= end]
    for key, value in (("camera_id", camera), ("event_type", event_type), ("room_number", room),
                       ("review_required", review_required), ("plate_status", plate_status)):
        if value is not None and value != '':
            rows = [e for e in rows if e[key] == value]
    if movement:
        rows = [e for e in rows if e.get("movement") == movement.upper()]
    if status:
        wanted = status.upper()
        rows = [e for e in rows if ("COMPLETED" if e.get("visit_id") else "REQUIRES_REVIEW" if e.get("review_required") else "UNPAIRED") == wanted]
    if plate:
        term = ''.join(plate.upper().split()).replace('-', '')
        rows = [e for e in rows if term in (e["plate"] or "").upper().replace('-', '').replace(' ', '')]
    total = len(rows)
    return {"events": [enrich_event(e) for e in rows[(page-1)*page_size:page*page_size]],
            "total": total, "page": page, "page_size": page_size, "pages": (total+page_size-1)//page_size}


@app.get("/api/hotel/events/entries/unpaired")
def unpaired_entries(authenticated: dict = Depends(check_auth)):
    return {"events": [enrich_event(e) for e in admin.unpaired("entries")]}


@app.get("/api/hotel/events/exits/unpaired")
def unpaired_exits(entry_event_id: Optional[str] = None, authenticated: dict = Depends(check_auth)):
    rows = manual_operation(admin.unpaired, "exits", entry_event_id)
    return {"events": [enrich_event(e) for e in rows]}


@app.get("/api/hotel/events/{event_id}")
def get_hotel_event_detail(event_id: str, authenticated: dict = Depends(check_auth)):
    with admin.connection() as c:
        ev = manual_operation(admin.event, c, event_id)
        ev["audit_logs"] = [dict(r) for r in c.execute(
            """SELECT a.*,a.created_at AS changed_at,u.username AS changed_by,
                      'plate' AS field_name FROM audit_log a JOIN users u ON u.id=a.user_id
               WHERE a.entity_type='event' AND a.entity_id=? ORDER BY a.id DESC""", (event_id,))]
        ev["legacy_audit_logs"] = [dict(r) for r in c.execute(
            "SELECT * FROM hotel_event_audit WHERE event_id=? ORDER BY changed_at DESC", (event_id,))]
    diagnostics = json.loads(ev.get("ocr_diagnostics_json") or "{}")
    for frame in diagnostics.get("frames", []):
        frame["evidence_url"] = _relative_evidence_url(frame.pop("evidence_path", None))
    ev["ocr_diagnostics"] = diagnostics
    return enrich_event(ev)


class PlatePayload(BaseModel):
    model_config = {"extra": "forbid"}
    plate: str = Field(min_length=1, max_length=128)


@app.patch("/api/hotel/events/{event_id}")
def patch_hotel_event(event_id: str, payload: PlatePayload, authenticated: dict = Depends(check_auth)):
    ev = manual_operation(admin.correct_plate, event_id, payload.plate, authenticated)
    return {"success": True, "event": enrich_event(ev)}


@app.post("/api/hotel/events/{event_id}/review")
def review_hotel_event(event_id: str, authenticated: dict = Depends(check_auth)):
    with admin.connection() as c, c:
        c.execute("BEGIN IMMEDIATE")
        original = manual_operation(admin.event, c, event_id)
        c.execute("UPDATE hotel_vehicle_events SET review_required=0,reviewed_at=?,reviewed_by=? WHERE event_id=?",
                  (admin.now(), authenticated["username"], event_id))
        admin.audit(c, authenticated["id"], "EVENT_REVIEWED", "event", event_id, original["review_required"], 0)
    return {"success": True}


@app.post("/api/hotel/export")
def post_hotel_export(authenticated: bool = Depends(check_auth)):
    """Fuerza la generación física del archivo Excel."""
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    try:
        excel_path = os.path.join(config.BASE_DIR, "data", "reportes", "control_vehiculos.xlsx")
        exporter = HotelExcelExporter(store, excel_path)
        exporter.export()
        return {"success": True, "path": "/downloads/control_vehiculos.xlsx"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")
    finally:
        store.close()


class AssociationPayload(BaseModel):
    model_config = {"extra": "forbid"}
    entry_event_id: str = Field(min_length=1, max_length=128)
    exit_event_id: str = Field(min_length=1, max_length=128)


@app.post("/api/hotel/visits/associate")
def associate_visit(payload: AssociationPayload, authenticated: dict = Depends(check_auth)):
    visit = manual_operation(admin.associate, payload.entry_event_id, payload.exit_event_id, authenticated)
    return {"success": True, "visit": visit}


@app.get("/api/hotel/visits")
def get_hotel_visits(page: int = 1, page_size: int = 25, authenticated: dict = Depends(check_auth)):
    page, page_size = max(1, page), max(1, min(page_size, 100))
    with admin.connection() as c:
        total = c.execute("SELECT count(*) FROM manual_visits").fetchone()[0]
        visits = [dict(r) for r in c.execute("SELECT * FROM manual_visits ORDER BY id DESC LIMIT ? OFFSET ?",
                                           (page_size, (page-1)*page_size))]
        for v in visits:
            v["entry"] = enrich_event(admin.event(c, v["entry_event_id"]))
            v["exit"] = enrich_event(admin.event(c, v["exit_event_id"]))
    return {"visits": visits, "total": total, "page": page, "pages": (total+page_size-1)//page_size}


@app.get("/api/hotel/visits/{visit_id}")
def get_hotel_visit_detail(visit_id: int, authenticated: dict = Depends(check_auth)):
    with admin.connection() as c:
        row = c.execute("SELECT * FROM manual_visits WHERE id=?", (visit_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Visita no encontrada.")
        return {**dict(row), "entry": enrich_event(admin.event(c, row["entry_event_id"])),
                "exit": enrich_event(admin.event(c, row["exit_event_id"]))}


@app.delete("/api/hotel/visits/{visit_id}/association")
def unlink_visit(visit_id: int, authenticated: dict = Depends(check_auth)):
    manual_operation(admin.unlink, visit_id, authenticated)
    return {"success": True}


@app.post("/api/hotel/rebuild-visits")
def post_rebuild_visits(authenticated: dict = Depends(check_auth)):
    raise HTTPException(409, "Las visitas se confirman manualmente. Usa Asociar salida o Desasociar.")


# ─── Servicios Seguros de Archivos y Snapshots ───────────────────────

@app.get("/media/captures/{camera_id}/{date_dir}/{filename}")
def get_capture(camera_id: str, date_dir: str, filename: str, authenticated: bool = Depends(check_auth)):
    """Transmite una foto de evidencia con protección de path traversal."""
    base = Path(config.CAPTURE_DIR).resolve()
    target = (base / camera_id / date_dir / filename).resolve()
    _verify_path_safe(base, target)
    return FileResponse(target, media_type="image/jpeg")


@app.get("/media/clips/{camera_id}/{date_dir}/{filename}")
def get_clip(camera_id: str, date_dir: str, filename: str, authenticated: bool = Depends(check_auth)):
    """Transmite un clip de video MP4 con protección de path traversal."""
    base = Path(config.BASE_DIR).resolve() / "data" / "clips"
    target = (base / camera_id / date_dir / filename).resolve()
    _verify_path_safe(base, target)
    return FileResponse(target, media_type="video/mp4")


@app.get("/downloads/control_vehiculos.xlsx")
def download_excel(authenticated: bool = Depends(check_auth)):
    """Permite la descarga segura del reporte administrativo de excel."""
    base = Path(config.BASE_DIR).resolve() / "data" / "reportes"
    target = (base / "control_vehiculos.xlsx").resolve()
    _verify_path_safe(base, target)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Report Excel not generated yet.")
    return FileResponse(
        target,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="control_vehiculos.xlsx"
    )


@app.get("/api/hotel/cameras/{camera_id}/snapshot")
def get_camera_snapshot(camera_id: str, authenticated: bool = Depends(check_auth)):
    """Servicio para retornar la imagen en tiempo real capturada por el detector."""
    try:
        validate_runtime_camera(camera_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Cámara inválida.")
    target = runtime_snapshot_path(camera_id).resolve()
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Snapshot todavía no disponible.")
    info = snapshot_info(camera_id)
    if not info.available or not is_valid_jpeg(target):
        raise HTTPException(status_code=404, detail="Snapshot inválido.")
    if info.stale:
        raise HTTPException(status_code=503, detail="Snapshot desactualizado.")
    return FileResponse(
        target,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Last-Modified": datetime.fromtimestamp(target.stat().st_mtime).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        },
    )


@app.get("/api/hotel/cameras/{camera_id}/snapshot/status")
def get_camera_snapshot_status(camera_id: str, authenticated: bool = Depends(check_auth)):
    try:
        info = snapshot_info(camera_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Cámara inválida.")
    return info.__dict__


# ─── Auxiliares de Seguridad y URLs ──────────────────────────────────

def _verify_path_safe(base: Path, target: Path) -> None:
    """Valida que el archivo solicitado resida dentro del directorio raíz permitido."""
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path traversal access denied.")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Requested media file not found.")


def _relative_evidence_url(evidence_path: Optional[str]) -> str:
    """Construye URL de API segura a partir de la ruta del archivo."""
    if not evidence_path:
        return ""
    abs_path = os.path.abspath(evidence_path)
    abs_capture_dir = os.path.abspath(config.CAPTURE_DIR)
    if not abs_path.startswith(abs_capture_dir):
        return ""
    relative = os.path.relpath(abs_path, abs_capture_dir).replace(os.sep, "/")
    return f"/media/captures/{relative}"


def _relative_clip_url(clip_path: Optional[str]) -> str:
    """Construye URL de API segura a partir de la ruta del archivo."""
    if not clip_path:
        return ""
    abs_path = os.path.abspath(clip_path)
    abs_clips_dir = os.path.abspath(os.path.join(config.BASE_DIR, "data", "clips"))
    if not abs_path.startswith(abs_clips_dir):
        return ""
    relative = os.path.relpath(abs_path, abs_clips_dir).replace(os.sep, "/")
    return f"/media/clips/{relative}"
