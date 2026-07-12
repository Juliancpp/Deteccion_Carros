#!/usr/bin/env python3
"""Valida la configuracion local de camaras sin conectar al DVR."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"
sys.path.insert(0, str(APP_DIR))

from settings import SettingsError, build_rtsp_url, load_settings, sanitize_rtsp_url


def main() -> int:
    try:
        settings = load_settings(override_env=True)
    except SettingsError as exc:
        print(f"[CONFIG] Error: {exc}", file=sys.stderr)
        return 1

    print(f"[CONFIG] DVR configurado: {settings.dvr.host}:{settings.dvr.rtsp_port}")
    for camera in settings.cameras:
        if not camera.enabled:
            continue

        safe_url = sanitize_rtsp_url(build_rtsp_url(settings.dvr, camera))
        print(f"[CONFIG] Camara {camera.camera_id} habilitada")
        print(f"         Nombre: {camera.name}")
        print(f"         Canal: {camera.channel}")
        print(f"         FPS objetivo: {camera.target_fps:g}")
        if camera.line is not None:
            print(f"         Linea habilitada: {str(camera.line.enabled).lower()}")
            print(f"         Linea inicio: ({camera.line.start_x:.3f}, {camera.line.start_y:.3f})")
            print(f"         Linea fin: ({camera.line.end_x:.3f}, {camera.line.end_y:.3f})")
            print(f"         Invertir direccion: {str(camera.line.invert_direction).lower()}")
            print(f"         Direcciones: {camera.line.event_directions}")
        if camera.passage is not None:
            points = ";".join(f"{x:.3f},{y:.3f}" for x, y in camera.passage.points)
            print(f"         Corredor habilitado: {str(camera.passage.enabled).lower()}")
            print(f"         Corredor puntos: {points}")
            print(f"         Corredor ancho medio: {camera.passage.half_width:.3f}")
            print(f"         Evento corredor: {camera.passage.event}")
            print(f"         Direccion corredor: {camera.passage.direction}")
            print(f"         Progreso disparo: {camera.passage.trigger_progress:.3f}")
        print(f"         URL: {safe_url}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
