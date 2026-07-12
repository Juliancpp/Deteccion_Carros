#!/usr/bin/env python3
"""Prueba real de Fase 9A: YOLO compartido y tracking aislado multicamara."""

from __future__ import annotations

import argparse
import resource
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.multicamera.coordinator import MultiCameraCoordinator
from app.settings import SettingsError, load_settings


_shutdown_requested = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prueba multicamara con YOLO compartido")
    parser.add_argument("--camera", choices=["A1", "A5", "all"], default="all")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--save-snapshots", action="store_true")
    parser.add_argument("--snapshot-dir", default=str(PROJECT_ROOT / "data" / "multicamera_test"))
    parser.add_argument("--status-interval", type=float, default=1.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    shutdown_event = _install_signal_handlers()
    started_at = time.monotonic()
    cpu_started = time.process_time()
    ram_started = _rss_mb()

    try:
        settings = load_settings(override_env=True)
    except SettingsError as exc:
        print(f"[MULTI] Configuracion invalida: {exc}", file=sys.stderr)
        return 2

    try:
        selected_cameras = [
            camera for camera in settings.cameras if args.camera == "all" or camera.camera_id == args.camera
        ]
        coordinator = MultiCameraCoordinator(settings.dvr, selected_cameras)
        invalid_backends = [
            context
            for context in coordinator.contexts
            if context.tracker_backend != "ultralytics-bytetrack"
        ]
        if invalid_backends:
            backends = ", ".join(
                f"{context.camera_id}={context.tracker_backend}" for context in invalid_backends
            )
            print(f"[MULTI] Backend de tracking invalido para validacion real: {backends}", file=sys.stderr)
            return 2
    except RuntimeError as exc:
        print(f"[MULTI] No se pudo inicializar inferencia: {exc}", file=sys.stderr)
        return 2
    exit_code = 0
    snapshots = []

    try:
        coordinator.start(start_streams=True)
        end_at = time.monotonic() + args.duration
        next_status_at = 0.0
        while time.monotonic() < end_at and not shutdown_event.is_set():
            now = time.monotonic()
            if now >= next_status_at:
                _print_status(coordinator)
                next_status_at = now + args.status_interval
            shutdown_event.wait(0.05)
    finally:
        coordinator.stop(stop_streams=False)
        if args.save_snapshots:
            snapshots = _copy_result_snapshots(coordinator)
        streams_stopped = _stop_streams(coordinator)
        if not streams_stopped:
            exit_code = 1

    if args.save_snapshots:
        _save_snapshots(snapshots, Path(args.snapshot_dir))

    status = coordinator.get_status()
    for camera_status in status.cameras:
        if camera_status.source_sequence <= 0 or camera_status.inference_count <= 0:
            exit_code = 1
            print(
                f"[MULTI] ERROR: {camera_status.camera_id} no recibio frames o inferencias",
                file=sys.stderr,
            )

    elapsed = max(time.monotonic() - started_at, 0.001)
    cpu_percent = (time.process_time() - cpu_started) / elapsed * 100.0
    print(f"[MULTI] CPU proceso aproximado: {cpu_percent:.1f}%")
    print(f"[MULTI] RAM inicial/final: {ram_started:.1f}MB/{_rss_mb():.1f}MB")
    print("[MULTI] Cierre limpio" if exit_code == 0 else "[MULTI] Cierre con errores")
    coordinator.contexts.clear()
    return exit_code


def _install_signal_handlers():
    import threading

    shutdown_event = threading.Event()

    def handle_signal(_signum, _frame):
        global _shutdown_requested
        if not _shutdown_requested:
            _shutdown_requested = True
            print("[MULTI] Interrupcion solicitada; iniciando cierre seguro...")
        else:
            print("[MULTI] Cierre ya en progreso...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    return shutdown_event


def _print_status(coordinator: MultiCameraCoordinator) -> None:
    status = coordinator.get_status()
    print(
            f"GLOBAL model_loads={status.model_load_count} "
            f"total_inferences={status.total_inference_count} "
            f"scheduler={str(status.scheduler_running).lower()} "
            f"loop_rate={status.scheduler_loop_rate:.1f} device=cpu"
    )
    for camera in status.cameras:
        print(
            f"{camera.camera_id} stream={str(camera.stream_connected).lower()} "
            f"capture={camera.capture_fps:.1f}fps target={camera.target_inference_fps:.1f}fps "
            f"actual={camera.actual_inference_fps:.1f}fps tracker_backend={camera.tracker_backend}"
        )
        print(
            f"   seq={camera.source_sequence} processed={camera.processed_sequence} "
            f"skipped={camera.dropped_or_skipped_frames} inference={camera.inference_ms_last:.0f}ms"
        )
        ids = ", ".join(str(track_id) for track_id in camera.active_track_ids)
        print(f"   vehicles={camera.detections_count} ids=[{ids}]")
    print()


def _copy_result_snapshots(coordinator: MultiCameraCoordinator) -> list[tuple[str, object]]:
    snapshots = []
    for context in coordinator.contexts:
        result = coordinator.get_latest_result(context.camera_id)
        if result is None or result.annotated_frame is None:
            print(f"[MULTI] Sin snapshot anotado para {context.camera_id}")
            continue
        snapshots.append((context.camera_id, result.annotated_frame.copy()))
    return snapshots


def _stop_streams(coordinator: MultiCameraCoordinator) -> bool:
    all_stopped = True
    for context in coordinator.contexts:
        stopped = context.stream.stop()
        if stopped:
            print(f"[MULTI] {context.camera_id} stream detenido correctamente")
        else:
            print(f"[MULTI] ERROR: {context.camera_id} stream no termino", file=sys.stderr)
            all_stopped = False
    if all_stopped:
        print("[MULTI] Todos los streams finalizaron")
    return all_stopped


def _save_snapshots(snapshots: list[tuple[str, object]], snapshot_dir: Path) -> None:
    try:
        import cv2
    except ImportError:
        print("[MULTI] OpenCV no esta instalado; no se guardan snapshots", file=sys.stderr)
        return

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    for camera_id, frame in snapshots:
        filepath = snapshot_dir / f"{camera_id}_annotated_{timestamp}.jpg"
        if cv2.imwrite(str(filepath), frame):
            print(f"[MULTI] Snapshot guardado: {filepath}")
        else:
            print(f"[MULTI] No se pudo guardar snapshot: {filepath}", file=sys.stderr)


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


if __name__ == "__main__":
    raise SystemExit(main())
