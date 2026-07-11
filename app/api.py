"""
Deteccion_Carros — API REST (FastAPI)
======================================
Servidor web para consultar eventos, estadísticas y evidencias.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import BASE_DIR, CAPTURE_DIR
from database import DatabaseManager


app = FastAPI(title="Deteccion_Carros Dashboard", version="1.0.0")
db = DatabaseManager()

WEB_DIR = Path(BASE_DIR) / "web"

app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
app.mount("/captures", StaticFiles(directory=CAPTURE_DIR), name="captures")


@app.get("/")
def dashboard():
    """Sirve el dashboard web."""
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/events")
def get_events():
    """Devuelve los últimos eventos."""
    return {"events": _events_with_image_urls(db.get_events(limit=50))}


@app.get("/api/events/{limit}")
def get_events_limit(limit: int):
    """Devuelve los últimos N eventos."""
    safe_limit = max(1, min(limit, 500))
    return {"events": _events_with_image_urls(db.get_events(limit=safe_limit))}


@app.get("/api/stats")
def get_stats():
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
    """Agrega URL pública para evidencias guardadas bajo data/captures."""
    return [{**event, "imagen_url": _image_url(event.get("imagen", ""))} for event in events]


def _image_url(image_path: str) -> str:
    if not image_path:
        return ""

    abs_image = os.path.abspath(image_path)
    abs_capture_dir = os.path.abspath(CAPTURE_DIR)

    if not abs_image.startswith(abs_capture_dir):
        return ""

    relative = os.path.relpath(abs_image, abs_capture_dir).replace(os.sep, "/")
    return f"/captures/{relative}"
