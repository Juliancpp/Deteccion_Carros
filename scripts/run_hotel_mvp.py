#!/usr/bin/env python3
from __future__ import annotations

import argparse
import resource
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import config
from app.export.excel_exporter import HotelExcelExporter
from app.hotel.event_worker import HotelEventJob, HotelEventWorker
from app.hotel.frame_buffer import TrackFrameBuffer
from app.hotel.gate_detector import DirectionalGateDetector
from app.hotel.room_detector import RoomDetector, RoomZone
from app.hotel.store import HotelStore
from app.multicamera.coordinator import MultiCameraCoordinator
from app.settings import load_settings


_shutdown_requested = False


def parse_args():
    parser = argparse.ArgumentParser(description="Runner MVP hotel vehiculos/placas/habitaciones/excel")
    parser.add_argument("--camera", choices=["A1", "A5", "all"], default="all")
    parser.add_argument("--duration", type=float, default=0.0, help="0 = correr hasta Ctrl+C")
    parser.add_argument("--save-debug-snapshots", action="store_true")
    parser.add_argument("--export-interval", type=float, default=60.0)
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
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    exporter = HotelExcelExporter(store, config.HOTEL_REPORT_PATH)
    worker = HotelEventWorker(store, exporter=exporter)
    coordinator = MultiCameraCoordinator(settings.dvr, cameras)
    buffers = TrackFrameBuffer(config.HOTEL_FRAME_BUFFER_MAX_CANDIDATES, config.HOTEL_FRAME_BUFFER_TTL_SECONDS)
    gates = {camera.camera_id: _gate_for_camera(camera) for camera in cameras}
    rooms = {camera.camera_id: _room_detector_for_camera(camera) for camera in cameras}
    last_sequences = {camera.camera_id: 0 for camera in cameras}
    submitted_keys: set[tuple[str, int, str]] = set()
    exit_code = 0

    try:
        worker.start()
        coordinator.start(start_streams=True)
        end_at = time.monotonic() + args.duration if args.duration > 0 else None
        next_status = 0.0
        next_export = time.monotonic() + args.export_interval
        while not shutdown_event.is_set() and (end_at is None or time.monotonic() < end_at):
            for camera in cameras:
                result = coordinator.get_latest_result(camera.camera_id, after_sequence=last_sequences[camera.camera_id])
                if result is None:
                    continue
                last_sequences[camera.camera_id] = result.source_sequence
                frame = result.annotated_frame
                if frame is None:
                    continue
                for vehicle in result.tracked_vehicles:
                    if vehicle.get("id") is None:
                        continue
                    track_id = int(vehicle["id"])
                    buffers.add(camera.camera_id, track_id, frame, vehicle, result.captured_at)
                    room = rooms[camera.camera_id].update(
                        track_id=track_id,
                        bbox=vehicle["bbox"],
                        frame_width=frame.shape[1],
                        frame_height=frame.shape[0],
                    )
                    gate_event = gates[camera.camera_id].update(
                        track_id=track_id,
                        bbox=vehicle["bbox"],
                        frame_width=frame.shape[1],
                        frame_height=frame.shape[0],
                        captured_at=result.captured_at,
                        source_sequence=result.source_sequence,
                    )
                    if gate_event is None:
                        continue
                    key = (camera.camera_id, track_id, gate_event.movement)
                    if key in submitted_keys:
                        continue
                    submitted_keys.add(key)
                    job = HotelEventJob(
                        gate_event=gate_event,
                        camera_name=camera.name,
                        vehicle=dict(vehicle),
                        room=room,
                        candidates=buffers.best(camera.camera_id, track_id, limit=5),
                    )
                    if not worker.submit(job):
                        print(f"[HOTEL] WARNING cola OCR llena camera={camera.camera_id} track={track_id}")
                buffers.cleanup()
            now = time.monotonic()
            if now >= next_status:
                _print_status(coordinator, worker, store)
                next_status = now + args.status_interval
            if now >= next_export:
                exporter.export()
                next_export = now + args.export_interval
            shutdown_event.wait(0.02)
    finally:
        scheduler_ok = coordinator.stop(stop_streams=False)
        streams_ok = _stop_streams(coordinator)
        worker_ok = worker.stop(timeout=60)
        exporter.export()
        store.close()
        coordinator.contexts.clear()
        if not scheduler_ok or not streams_ok or not worker_ok:
            exit_code = 1

    elapsed = max(time.monotonic() - started, 0.001)
    cpu = (time.process_time() - cpu_started) / elapsed * 100.0
    print(f"[HOTEL] CPU proceso aproximado: {cpu:.1f}%")
    print(f"[HOTEL] RAM inicial/final: {ram_started:.1f}MB/{_rss_mb():.1f}MB")
    print(f"[HOTEL] Excel: {config.HOTEL_REPORT_PATH}")
    print("[HOTEL] Cierre limpio" if exit_code == 0 else "[HOTEL] Cierre con errores")
    return exit_code


def _gate_for_camera(camera):
    return DirectionalGateDetector(
        camera_id=camera.camera_id,
        start=camera.gate.start,
        end=camera.gate.end,
        invert_direction=camera.gate.invert_direction,
        min_observations=config.HOTEL_GATE_MIN_OBSERVATIONS,
        min_delta=config.HOTEL_GATE_MIN_DELTA,
        duplicate_cooldown_seconds=config.HOTEL_GATE_DUPLICATE_COOLDOWN_SECONDS,
    )


def _room_detector_for_camera(camera):
    zones = [RoomZone(room.room_number, room.polygon) for room in camera.rooms]
    return RoomDetector(
        camera.camera_id,
        zones,
        probable_observations=config.HOTEL_ROOM_MIN_PROBABLE_OBSERVATIONS,
        confirmed_observations=config.HOTEL_ROOM_MIN_CONFIRMED_OBSERVATIONS,
    )


def _print_status(coordinator, worker: HotelEventWorker, store: HotelStore) -> None:
    status = coordinator.get_status()
    last = worker.last_event
    print("GLOBAL:")
    print(f"model_loads={status.model_load_count} ocr_queue={worker.pending_count()} events={worker.processed_events} visits_open={store.count_open_visits()}")
    for camera in status.cameras:
        print(f"{camera.camera_id}: stream={str(camera.stream_connected).lower()} inference_fps={camera.actual_inference_fps:.1f} tracks={len(camera.active_track_ids)}")
        if last and last.camera_id == camera.camera_id:
            print(f"   last_event={last.event_type} plate={last.plate or 'SIN_PLACA'} room={last.room_number or 'NO_DET'} {last.room_status}")
    print()


def _stop_streams(coordinator) -> bool:
    ok = True
    for context in coordinator.contexts:
        stopped = context.stream.stop()
        print(f"[HOTEL] {context.camera_id} stream detenido correctamente" if stopped else f"[HOTEL] ERROR {context.camera_id} no termino")
        ok = ok and stopped
    return ok


def _install_signal_handlers():
    import threading

    shutdown_event = threading.Event()

    def handle_signal(_signum, _frame):
        global _shutdown_requested
        if not _shutdown_requested:
            _shutdown_requested = True
            print("[HOTEL] Interrupcion solicitada; cierre seguro...")
        else:
            print("[HOTEL] Cierre ya en progreso...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    return shutdown_event


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


if __name__ == "__main__":
    raise SystemExit(main())
