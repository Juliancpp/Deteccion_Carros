#!/usr/bin/env python3
"""Diagnostico seguro de autenticacion RTSP sin ejecutar el pipeline de IA."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.settings import (  # noqa: E402
    SettingsError,
    build_rtsp_url,
    load_settings,
    sanitize_rtsp_url,
    sanitize_sensitive_text,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnostico seguro de autenticacion RTSP")
    parser.add_argument("--camera", choices=["A1", "A5", "all"], default="all")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        settings = load_settings(override_env=True)
    except SettingsError as exc:
        print(f"[CONFIG] Configuracion incompleta o invalida: {exc}", file=sys.stderr)
        return 2

    selected = [
        camera
        for camera in settings.cameras
        if camera.enabled and (args.camera == "all" or camera.camera_id == args.camera)
    ]
    if not selected:
        print(f"[CONFIG] No hay camaras habilitadas para: {args.camera}", file=sys.stderr)
        return 2

    print("[CONFIG]")
    print(f"Host: {settings.dvr.host}")
    print(f"Puerto: {settings.dvr.rtsp_port}")
    print(f"Usuario: {settings.dvr.username}")
    print(f"Contrasena presente: {'si' if bool(settings.dvr.password) else 'no'}")
    print(f"Longitud de contrasena: {len(settings.dvr.password)}")
    print()

    exit_code = 0
    for camera in selected:
        if not _diagnose_camera(settings.dvr, camera):
            exit_code = 1
    return exit_code


def _diagnose_camera(dvr_settings, camera_settings) -> bool:
    print(f"[{camera_settings.camera_id}]")
    print(f"Camara: {camera_settings.name}")
    print(f"Canal: {camera_settings.channel}")
    print(f"URL: {sanitize_rtsp_url(build_rtsp_url(dvr_settings, camera_settings))}")

    try:
        import cv2
    except ImportError as exc:
        safe_error = sanitize_sensitive_text(exc, dvr_settings, camera_settings=camera_settings)
        print("Conexion: fallida")
        print("Primer frame: no recibido")
        print("Error: OpenCV no esta instalado")
        print(f"Detalle: {safe_error}")
        print()
        return False

    url = build_rtsp_url(dvr_settings, camera_settings)
    capture = None
    try:
        capture = cv2.VideoCapture(url, getattr(cv2, "CAP_FFMPEG", 0))
        if not capture.isOpened():
            capture.release()
            capture = cv2.VideoCapture(url)
        if not capture.isOpened():
            print("Conexion: fallida")
            print("Primer frame: no recibido")
            print("Error: apertura fallida")
            print()
            return False

        print("Conexion: correcta")
        ok, frame = capture.read()
        if not ok or frame is None:
            print("Primer frame: no recibido")
            print("Error: stream abierto sin frames")
            print()
            return False

        height, width = frame.shape[:2]
        print("Primer frame: recibido")
        print(f"Resolucion: {width}x{height}")
        print()
        return True
    except Exception as exc:  # noqa: BLE001 - se sanitiza antes de mostrar.
        safe_error = sanitize_sensitive_text(exc, dvr_settings, camera_settings=camera_settings)
        print("Conexion: fallida")
        print("Primer frame: no recibido")
        print(f"Error: {_classify_error(safe_error)}")
        print(f"Detalle: {safe_error}")
        print()
        return False
    finally:
        if capture is not None:
            capture.release()


def _classify_error(error: str) -> str:
    lowered = error.lower()
    if "401" in lowered or "unauthorized" in lowered:
        return "401 Unauthorized"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    return "error de apertura o lectura"


if __name__ == "__main__":
    raise SystemExit(main())
