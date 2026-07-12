#!/usr/bin/env python3
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

from app.hotel.phase1_analyzer import HotelPhase1Analyzer
from app.multicamera.coordinator import MultiCameraCoordinator
from app.settings import load_settings

_shutdown_requested = False


def parse_args():
    parser = argparse.ArgumentParser(description="Prueba Fase 1 hotel: direccion fisica + habitacion")
    parser.add_argument("--camera", choices=["A1", "A5", "all"], default="all")
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--save-snapshots", action="store_true")
    parser.add_argument("--snapshot-dir", default=str(PROJECT_ROOT / "data" / "hotel_phase1_test"))
    parser.add_argument("--status-interval", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    shutdown_event = _install_signal_handlers()
    started = time.monotonic()
    cpu_started = time.process_time()
    ram_started = _rss_mb()
    settings = load_settings(override_env=True)
    cameras = [camera for camera in settings.cameras if args.camera == "all" or camera.camera_id == args.camera]
    coordinator = MultiCameraCoordinator(settings.dvr, cameras)
    analyzers = {camera.camera_id: HotelPhase1Analyzer(camera) for camera in cameras}
    last_sequences = {camera.camera_id: 0 for camera in cameras}
    saved_results: set[str] = set()
    exit_code = 0
    try:
        coordinator.start(start_streams=True)
        end_at = time.monotonic() + args.duration
        next_status = 0.0
        while time.monotonic() < end_at and not shutdown_event.is_set():
            for camera in cameras:
                result = coordinator.get_latest_result(camera.camera_id, after_sequence=last_sequences[camera.camera_id])
                if result is None or result.annotated_frame is None:
                    continue
                last_sequences[camera.camera_id] = result.source_sequence
                analyzer = analyzers[camera.camera_id]
                phase_results = analyzer.update(
                    tracked_vehicles=result.tracked_vehicles,
                    frame=result.annotated_frame,
                    captured_at=result.captured_at,
                    frame_width=result.annotated_frame.shape[1],
                    frame_height=result.annotated_frame.shape[0],
                )
                for phase_result in phase_results:
                    if phase_result.finalized:
                        _print_result(phase_result)
                    if args.save_snapshots and phase_result.evidence_frame is not None and phase_result.result_id not in saved_results:
                        _save_snapshot(phase_result, Path(args.snapshot_dir))
                        saved_results.add(phase_result.result_id)
            now = time.monotonic()
            if now >= next_status:
                _print_status(coordinator, analyzers)
                next_status = now + args.status_interval
            shutdown_event.wait(0.02)
    finally:
        scheduler_ok = coordinator.stop(stop_streams=False)
        streams_ok = _stop_streams(coordinator)
        coordinator.contexts.clear()
        if not scheduler_ok or not streams_ok:
            exit_code = 1
    elapsed = max(time.monotonic() - started, 0.001)
    cpu = (time.process_time() - cpu_started) / elapsed * 100.0
    print(f"[PHASE1] CPU proceso aproximado: {cpu:.1f}%")
    print(f"[PHASE1] RAM inicial/final: {ram_started:.1f}MB/{_rss_mb():.1f}MB")
    print("[PHASE1] Cierre limpio" if exit_code == 0 else "[PHASE1] Cierre con errores")
    return exit_code


def _print_status(coordinator, analyzers):
    status = coordinator.get_status()
    print("GLOBAL")
    print(f"model_loads={status.model_load_count}")
    print(f"scheduler={str(status.scheduler_running).lower()}")
    for camera in status.cameras:
        latest = analyzers[camera.camera_id].latest_results()
        print()
        print(camera.camera_id)
        print(f"stream={str(camera.stream_connected).lower()}")
        print(f"capture_fps={camera.capture_fps:.1f}")
        print(f"inference_fps={camera.actual_inference_fps:.1f}")
        print(f"active_tracks={list(camera.active_track_ids)}")
        for result in latest[-3:]:
            print(
                f"track={result.track_id} movement={result.movement} movement_conf={result.movement_confidence:.2f} "
                f"room={result.room_number or 'NO_DET'} room_status={result.room_status} room_conf={result.room_confidence:.2f}"
            )
    print()


def _print_result(result):
    print("[PHASE1_RESULT]")
    print(f"camera={result.camera_id}")
    print(f"track_id={result.track_id}")
    print(f"movement={result.movement}")
    print(f"room={result.room_number or 'NO_DET'}")
    print(f"room_status={result.room_status}")
    print(f"movement_confidence={result.movement_confidence:.2f}")
    print(f"room_confidence={result.room_confidence:.2f}")
    print(f"first_seen={result.first_seen_at.isoformat()}")
    print(f"last_seen={result.last_seen_at.isoformat()}")
    print(f"reason={result.reason}")
    print()


def _save_snapshot(result, snapshot_dir: Path):
    import cv2

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    frame = result.evidence_frame.copy()
    x1, y1, x2, y2 = result.last_bbox
    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 128), 2)
    label = f"{result.camera_id} ID {result.track_id} {result.movement} room={result.room_number or 'NO_DET'} {result.room_status}"
    cv2.putText(frame, label, (16, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 255), 2)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    room = result.room_number or "NO_DET"
    path = snapshot_dir / f"{result.camera_id}_track{result.track_id}_{result.movement.lower()}_room{room}_{result.room_status.lower()}_{ts}.jpg"
    cv2.imwrite(str(path), frame)
    print(f"[PHASE1] Snapshot: {path}")


def _stop_streams(coordinator) -> bool:
    ok = True
    for context in coordinator.contexts:
        stopped = context.stream.stop()
        print(f"[PHASE1] {context.camera_id} stream detenido correctamente" if stopped else f"[PHASE1] ERROR {context.camera_id} no termino")
        ok = ok and stopped
    return ok


def _install_signal_handlers():
    import threading

    shutdown_event = threading.Event()

    def handle_signal(_signum, _frame):
        global _shutdown_requested
        if not _shutdown_requested:
            _shutdown_requested = True
            print("[PHASE1] Interrupcion solicitada; cierre seguro...")
        else:
            print("[PHASE1] Cierre ya en progreso...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    return shutdown_event


def _rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


if __name__ == "__main__":
    raise SystemExit(main())
