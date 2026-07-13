"""
Deteccion_Carros — API REST (FastAPI)
======================================
Servidor web para consultar eventos, estadísticas y evidencias del Hotel MVP.
"""

import os
import sys
import hmac
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, status, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.config as config
from app.database import DatabaseManager
from app.hotel.store import HotelStore
from app.export.excel_exporter import HotelExcelExporter

app = FastAPI(title="Deteccion_Carros Dashboard", version="1.0.0")

# Security schema for optional Basic Auth
security = HTTPBasic()


def check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> bool:
    """Verifies credentials if DASHBOARD_AUTH_ENABLED is set to true."""
    auth_enabled = os.getenv("DASHBOARD_AUTH_ENABLED", "false").lower() == "true"
    if not auth_enabled:
        return True

    expected_username = os.getenv("DASHBOARD_USERNAME", "admin")
    expected_password = os.getenv("DASHBOARD_PASSWORD", "valor_local")

    is_username_correct = hmac.compare_digest(credentials.username.encode("utf-8"), expected_username.encode("utf-8"))
    is_password_correct = hmac.compare_digest(credentials.password.encode("utf-8"), expected_password.encode("utf-8"))

    if not (is_username_correct and is_password_correct):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


# Initialize DB managers
db = DatabaseManager()

# Static mounts
WEB_DIR = Path(config.BASE_DIR) / "web"
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def dashboard(authenticated: bool = Depends(check_auth)):
    """Sirve el dashboard web."""
    return FileResponse(WEB_DIR / "index.html")


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
def get_hotel_summary(authenticated: bool = Depends(check_auth)):
    """Métricas del hotel hoy (entradas, salidas, visitas abiertas, etc.)."""
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    try:
        return store.get_summary_stats()
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


@app.get("/api/hotel/events")
def get_hotel_events(
    page: int = 1,
    page_size: int = 25,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    camera: Optional[str] = None,
    event_type: Optional[str] = None,
    plate: Optional[str] = None,
    room: Optional[int] = None,
    review_required: Optional[bool] = None,
    plate_status: Optional[str] = None,
    authenticated: bool = Depends(check_auth)
):
    """Devuelve listado paginado y filtrado de eventos del hotel."""
    filters = {
        "date_from": date_from,
        "date_to": date_to,
        "camera": camera,
        "event_type": event_type,
        "plate": plate,
        "room": room,
        "review_required": review_required,
        "plate_status": plate_status
    }
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    try:
        res = store.get_events_filtered(filters, page=page, page_size=page_size)
        
        # Enriquecer evidencias y clips con URLs relativas seguras
        for ev in res["events"]:
            ev["evidence_url"] = _relative_evidence_url(ev.get("evidence_path"))
            ev["clip_url"] = _relative_clip_url(ev.get("clip_path"))
        return res
    finally:
        store.close()


@app.get("/api/hotel/events/{event_id}")
def get_hotel_event_detail(event_id: str, authenticated: bool = Depends(check_auth)):
    """Detalle de un evento específico."""
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    try:
        row = store.get_event(event_id)
        if not row:
            raise HTTPException(status_code=404, detail="Event not found")
        ev = dict(row)
        ev["evidence_url"] = _relative_evidence_url(ev.get("evidence_path"))
        ev["clip_url"] = _relative_clip_url(ev.get("clip_path"))
        
        # Obtener logs de auditoría para este evento
        cursor = store.conn.cursor()
        cursor.execute("SELECT * FROM hotel_event_audit WHERE event_id=? ORDER BY changed_at DESC", (event_id,))
        ev["audit_logs"] = [dict(r) for r in cursor.fetchall()]
        
        return ev
    finally:
        store.close()


@app.patch("/api/hotel/events/{event_id}")
def patch_hotel_event(event_id: str, payload: dict, authenticated: bool = Depends(check_auth)):
    """Modifica manualmente los datos de un evento y regenera las visitas."""
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    try:
        success = store.update_event_manually(event_id, payload, changed_by="admin")
        if not success:
            raise HTTPException(status_code=404, detail="Event not found")
        return {"success": True, "message": "Event modified and visits rebuilt."}
    finally:
        store.close()


@app.post("/api/hotel/events/{event_id}/review")
def review_hotel_event(payload: Optional[dict] = None, event_id: str = "", authenticated: bool = Depends(check_auth)):
    """Marca un evento como revisado de forma manual."""
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    try:
        notes = (payload or {}).get("notes", "Revisión completada manualmente.")
        updates = {"review_required": 0, "notes": notes}
        success = store.update_event_manually(event_id, updates, changed_by="admin")
        if not success:
            raise HTTPException(status_code=404, detail="Event not found")
        return {"success": True, "message": "Event marked as reviewed."}
    finally:
        store.close()


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


@app.get("/api/hotel/visits")
def get_hotel_visits(page: int = 1, page_size: int = 25, authenticated: bool = Depends(check_auth)):
    """Retorna listado paginado de visitas vehiculares."""
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    try:
        return store.get_visits_filtered(page=page, page_size=page_size)
    finally:
        store.close()


@app.get("/api/hotel/visits/{visit_id}")
def get_hotel_visit_detail(visit_id: str, authenticated: bool = Depends(check_auth)):
    """Retorna los detalles de una visita específica."""
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    try:
        row = store.get_visit(visit_id)
        if not row:
            raise HTTPException(status_code=404, detail="Visit not found")
        return dict(row)
    finally:
        store.close()


@app.post("/api/hotel/rebuild-visits")
def post_rebuild_visits(authenticated: bool = Depends(check_auth)):
    """Reconstruye transaccionalmente la tabla de visitas en base al histórico de eventos."""
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    try:
        store.rebuild_hotel_visits()
        return {"success": True, "message": "Visits history successfully rebuilt."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rebuild failed: {e}")
    finally:
        store.close()


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
    base = Path(config.BASE_DIR).resolve() / "data" / "runtime"
    target = (base / f"{camera_id}_latest.jpg").resolve()
    _verify_path_safe(base, target)
    if not target.is_file():
        # Fallback a placeholder o error
        fallback = WEB_DIR / "style.css"  # dummy fallback check or return 404
        raise HTTPException(status_code=404, detail="Snapshot not available.")
    return FileResponse(target, media_type="image/jpeg")


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
