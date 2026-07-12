#!/usr/bin/env python3
"""Valida eventos multicámara por línea virtual sin OCR ni SQLite."""

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
from app.multicamera.event_candidate import EventCandidate
from app.settings import SettingsError, load_settings


_shutdown_requested = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prueba de eventos multicamara por linea virtual")
    parser.add_argument("--camera", choices=["A1", "A5", "all"], default="all")
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--save-event-snapshots", action="store_true")
    parser.add_argument("--snapshot-dir", default=str(PROJECT_ROOT / "data" / "multicamera_events_test"))
    parser.add_argument("--status-interval", type=float, default=1.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    shutdown_event = _install_signal_handlers()
    started_at = time.monotonic()
    cpu_started = time.process_time()
    ram_started = _rss_mb()
    events: list[EventCandidate] = []

    try:
        settings = load_settings(override_env=True)
    except SettingsError as exc:
        print(f"[EVENTS] Configuracion invalida: {exc}", file=sys.stderr)
        return 2

    selected_cameras = [camera for camera in settings.cameras if args.camera == "all" or camera.camera_id == args.camera]
    coordinator = MultiCameraCoordinator(settings.dvr, selected_cameras)
    exit_code = 0

    try:
        coordinator.start(start_streams=True)
        end_at = time.monotonic() + args.duration
        next_status_at = 0.0
        while time.monotonic() < end_at and not shutdown_event.is_set():
            event = coordinator.get_event(timeout=0.05)
            if event is not None:
                events.append(event)
                _print_event(event)
                if args.save_event_snapshots:
                    _save_event_snapshot(event, Path(args.snapshot_dir))
            now = time.monotonic()
            if now >= next_status_at:
                _print_status(coordinator)
                next_status_at = now + args.status_interval
    finally:
        scheduler_stopped = coordinator.stop(stop_streams=False)
        streams_stopped = _stop_streams(coordinator)
        if not scheduler_stopped or not streams_stopped:
            exit_code = 1

    status = coordinator.get_status()
    for camera_status in status.cameras:
        if camera_status.source_sequence <= 0 or camera_status.inference_count <= 0:
            print(f"[EVENTS] ERROR: {camera_status.camera_id} no recibio frames o inferencias", file=sys.stderr)
            exit_code = 1

    elapsed = max(time.monotonic() - started_at, 0.001)
    cpu_percent = (time.process_time() - cpu_started) / elapsed * 100.0
    print(f"[EVENTS] Eventos detectados: {len(events)}")
    print(f"[EVENTS] CPU proceso aproximado: {cpu_percent:.1f}%")
    print(f"[EVENTS] RAM inicial/final: {ram_started:.1f}MB/{_rss_mb():.1f}MB")
    print("[EVENTS] Cierre limpio" if exit_code == 0 else "[EVENTS] Cierre con errores")
    coordinator.contexts.clear()
    return exit_code


def _install_signal_handlers():
    import threading

    shutdown_event = threading.Event()

    def handle_signal(_signum, _frame):
        global _shutdown_requested
        if not _shutdown_requested:
            _shutdown_requested = True
            print("[EVENTS] Interrupcion solicitada; iniciando cierre seguro...")
        else:
            print("[EVENTS] Cierre ya en progreso...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    return shutdown_event


def _print_status(coordinator: MultiCameraCoordinator) -> None:
    status = coordinator.get_status()
    print(
        f"GLOBAL model_loads={status.model_load_count} total_inferences={status.total_inference_count} "
        f"pending_events={status.pending_events} dropped_events={status.event_queue_dropped} "
        f"scheduler={str(status.scheduler_running).lower()}"
    )
    for camera in status.cameras:
        print(
            f"{camera.camera_id} stream={str(camera.stream_connected).lower()} "
            f"capture={camera.capture_fps:.1f}fps actual={camera.actual_inference_fps:.1f}fps "
            f"events={camera.events_published} crossings={camera.crossing_candidates} "
            f"duplicates={camera.duplicate_events_suppressed} dropped={camera.event_queue_dropped}"
        )
        print(
            f"   seq={camera.source_sequence} processed={camera.processed_sequence} "
            f"last_event={camera.last_event_direction} track={camera.last_event_track_id}"
        )
    print()


def _print_event(event: EventCandidate) -> None:
    print("[EVENT]")
    print(f"camera={event.camera_id}")
    print(f"track_id={event.track_id}")
    print(f"direction={event.direction}")
    print(f"class={event.class_name}")
    print(f"confidence={event.confidence:.2f}")
    if event.corridor_progress is not None:
        print(f"corridor_progress={event.corridor_progress:.3f}")
    print(f"time={event.occurred_at.isoformat()}")
    print()


def _save_event_snapshot(event: EventCandidate, snapshot_dir: Path) -> None:
    if event.frame is None:
        return
    import cv2

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    frame = event.frame.copy()
    x1, y1, x2, y2 = event.bbox
    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 128), 2)
    track_point = event.track_point or event.center
    cv2.circle(frame, track_point, 5, (255, 255, 0), -1)
    label = f"{event.camera_id} {event.direction} ID {event.track_id}"
    if event.corridor_progress is not None:
        label += f" p={event.corridor_progress:.2f}"
    cv2.putText(frame, label, (16, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    direction = event.direction.lower()
    filepath = snapshot_dir / f"{event.camera_id}_{direction}_track{event.track_id}_{timestamp}.jpg"
    if cv2.imwrite(str(filepath), frame):
        print(f"[EVENTS] Snapshot evento: {filepath}")
    else:
        print(f"[EVENTS] No se pudo guardar snapshot: {filepath}", file=sys.stderr)


def _stop_streams(coordinator: MultiCameraCoordinator) -> bool:
    all_stopped = True
    for context in coordinator.contexts:
        stopped = context.stream.stop()
        if stopped:
            print(f"[EVENTS] {context.camera_id} stream detenido correctamente")
        else:
            print(f"[EVENTS] ERROR: {context.camera_id} stream no termino", file=sys.stderr)
            all_stopped = False
    if all_stopped:
        print("[EVENTS] Todos los streams finalizaron")
    return all_stopped


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


if __name__ == "__main__":
    raise SystemExit(main())
