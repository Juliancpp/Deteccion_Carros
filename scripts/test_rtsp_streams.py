#!/usr/bin/env python3
"""Prueba independiente de capturadores RTSP sin ejecutar IA."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.settings import SettingsError, load_settings
from app.video.rtsp_stream import RTSPStream

_shutdown_event = None
_shutdown_message_printed = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prueba segura de streams RTSP A1/A5")
    parser.add_argument("--camera", choices=["A1", "A5", "all"], default="all")
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--snapshot-dir", default=str(PROJECT_ROOT / "data" / "rtsp_test"))
    parser.add_argument("--save-snapshots", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    shutdown_event = _install_signal_handlers()
    try:
        settings = load_settings(override_env=True)
    except SettingsError as exc:
        print(f"[RTSP] Configuracion invalida: {exc}", file=sys.stderr)
        return 2

    selected = [
        camera
        for camera in settings.cameras
        if camera.enabled and (args.camera == "all" or camera.camera_id == args.camera)
    ]
    if not selected:
        print(f"[RTSP] No hay camaras habilitadas para: {args.camera}", file=sys.stderr)
        return 2

    streams = [RTSPStream(settings.dvr, camera) for camera in selected]
    exit_code = 0
    snapshots = []
    cleanup_done = False

    try:
        for stream in streams:
            stream.start()

        end_at = time.monotonic() + args.duration
        while time.monotonic() < end_at and not shutdown_event.is_set():
            _print_statuses(streams)
            shutdown_event.wait(1.0)
    except KeyboardInterrupt:
        shutdown_event.set()
    finally:
        if not cleanup_done:
            cleanup_done = True
            snapshots = _copy_snapshots(streams) if args.save_snapshots else []
            if not _stop_streams(streams):
                exit_code = 1

    if args.save_snapshots:
        _save_snapshot_copies(snapshots, Path(args.snapshot_dir))

    for stream in streams:
        status = stream.get_status()
        if status.frame_sequence <= 0:
            print(
                f"[RTSP] Camara {status.camera_id} no recibio frames durante la prueba",
                file=sys.stderr,
            )
            exit_code = 1

    streams.clear()

    return exit_code


def _install_signal_handlers():
    import threading

    global _shutdown_event
    _shutdown_event = threading.Event()

    def handle_signal(_signum, _frame):
        global _shutdown_message_printed
        if not _shutdown_message_printed:
            _shutdown_message_printed = True
            print("[RTSP] Interrupcion solicitada; iniciando cierre seguro...")
        else:
            print("[RTSP] Cierre ya en progreso...")
        _shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    return _shutdown_event


def _stop_streams(streams: list[RTSPStream]) -> bool:
    all_stopped = True
    for stream in streams:
        stopped = stream.stop()
        status = stream.get_status()
        if stopped:
            print(f"[RTSP] {status.camera_id} detenido correctamente")
        else:
            print(f"[RTSP] ERROR: {status.camera_id} no termino dentro del timeout", file=sys.stderr)
            all_stopped = False

    alive = [stream.get_status().camera_id for stream in streams if stream.is_worker_alive()]
    if alive:
        for camera_id in alive:
            print(f"[RTSP] ERROR: {camera_id} continua activo", file=sys.stderr)
        all_stopped = False
    elif all_stopped:
        print("[RTSP] Todos los capturadores finalizaron")

    return all_stopped


def _copy_snapshots(streams: list[RTSPStream]) -> list[tuple[str, object]]:
    snapshots = []
    for stream in streams:
        status = stream.get_status()
        latest = stream.get_latest_frame()
        if latest is None:
            print(f"[RTSP] Sin snapshot para {status.camera_id}: no hay frame")
            continue
        snapshots.append((status.camera_id, latest.frame.copy()))
    return snapshots


def _save_snapshot_copies(snapshots: list[tuple[str, object]], snapshot_dir: Path) -> None:
    try:
        import cv2
    except ImportError:
        print("[RTSP] No se pueden guardar snapshots: OpenCV no esta instalado", file=sys.stderr)
        return

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    for camera_id, frame in snapshots:
        filepath = snapshot_dir / f"{camera_id}_{timestamp}.jpg"
        if cv2.imwrite(str(filepath), frame):
            print(f"[RTSP] Snapshot guardado: {filepath}")
        else:
            print(f"[RTSP] No se pudo guardar snapshot: {filepath}", file=sys.stderr)

def _print_statuses(streams: list[RTSPStream]) -> None:
    for stream in streams:
        status = stream.get_status()
        age = "None" if status.frame_age_seconds is None else f"{status.frame_age_seconds:.2f}s"
        print(
            f"{status.camera_id} connected={str(status.connected).lower()} "
            f"frames={status.frame_sequence} capture_fps={status.capture_fps:.1f} "
            f"age={age} reconnects={status.reconnect_count} errors={status.read_error_count}"
        )
    print()

if __name__ == "__main__":
    raise SystemExit(main())
