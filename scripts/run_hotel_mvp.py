#!/usr/bin/env python3
from __future__ import annotations

import logging
import uuid
from dataclasses import replace
import cv2
import argparse
import os
import resource
import signal
import sys
import time
import json
from collections import deque
from datetime import datetime
from pathlib import Path
import threading

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Legacy CPU: disable unsupported NNPACK backend
import torch

if hasattr(torch.backends, "nnpack") and hasattr(torch.backends.nnpack, "set_flags"):
    torch.backends.nnpack.set_flags(False)

from app.hotel.phase1_analyzer import HotelPhase1Analyzer
from app.hotel.frame_buffer import TrackFrameBuffer
from app.multicamera.coordinator import MultiCameraCoordinator
from app.multicamera.event_consumer import MultiCameraEventConsumer, HotelEvidenceCapture
from app.settings import load_settings
from app.ocr import PlateReader
from app.export.excel_exporter import HotelExcelExporter
from app.hotel.store import HotelStore
from app.runtime import atomic_write_jpeg, atomic_write_json, runtime_snapshot_path, snapshot_info
import app.config as config

_shutdown_requested = False


def parse_args():
    parser = argparse.ArgumentParser(description="Corredor MVP de Hotel con Tracker, OCR y Persistencia Excel")
    parser.add_argument("--camera", choices=["A1", "A5", "all"], default="all")
    parser.add_argument("--duration", type=float, default=180.0, help="Duracion en segundos de la ejecucion")
    parser.add_argument("--status-interval", type=float, default=2.0, help="Intervalo en segundos para mostrar reporte de status")
    parser.add_argument("--excel-output", default="data/reportes/control_vehiculos.xlsx", help="Ruta para exportar el reporte final de excel")
    parser.add_argument("--continuous", action="store_true", help="Ejecutar de manera continua e indefinida")
    parser.add_argument("--status-file", default="data/runtime/hotel_status.json", help="Ruta para persistir el estado JSON en tiempo real")
    parser.add_argument("--latest-snapshots", action="store_true", help="Guardar snapshots de camaras en tiempo real")
    parser.add_argument("--enable-clips", action="store_true", help="Habilitar generador de clips de video")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO)
    shutdown_event = _install_signal_handlers()
    started = time.monotonic()
    started_wall = datetime.now()
    cpu_started = time.process_time()
    ram_started = _rss_mb()

    # Cargar configuraciones
    settings = load_settings(override_env=True)
    cameras = [camera for camera in settings.cameras if args.camera == "all" or camera.camera_id == args.camera]

    print(f"[RUNNER] Iniciando MVP de Hotel para camaras: {[c.camera_id for c in cameras]}")

    # Inicializar componentes
    geometry_store = HotelStore(config.HOTEL_DATABASE_PATH) if any(c.enabled and c.geometry for c in cameras) else None
    pending_geometry = deque()
    coordinator = MultiCameraCoordinator(
        settings.dvr, cameras,
        geometry_sink=pending_geometry.append if geometry_store else None,
    )
    frame_buffer = TrackFrameBuffer(max_candidates=6, ttl_seconds=20.0)
    analyzers = {camera.camera_id: HotelPhase1Analyzer(camera) for camera in cameras}

    # Inicializar Consumidor de Eventos MVP
    plate_reader = PlateReader()
    evidence_capture = HotelEvidenceCapture(capture_dir=config.CAPTURE_DIR)
    
    # Initialize clip manager if enabled
    clip_manager = None
    if args.enable_clips:
        from app.media.clip_manager import ClipManager
        clip_manager = ClipManager(
            db_path=config.HOTEL_DATABASE_PATH,
            clips_dir=os.path.join(config.BASE_DIR, "data", "clips"),
            sample_fps=5,
            buffer_seconds=15,
            pre_seconds=6.0,
            post_seconds=5.0,
            crf=28,
            width=960,
            height=540,
            max_queue_size=8
        )
        clip_manager.start()

    consumer = MultiCameraEventConsumer(
        coordinator=coordinator,
        db_path=config.DATABASE_PATH,
        plate_reader=plate_reader,
        evidence=evidence_capture,
        padding=config.PADDING_CAPTURE,
        analyzers=analyzers,
        frame_buffer=frame_buffer,
        hotel_db_path=config.HOTEL_DATABASE_PATH,
        room_timeout=8.0,
        clip_manager=clip_manager,
    )

    last_sequences = {camera.camera_id: 0 for camera in cameras}
    last_snapshot_write = {camera.camera_id: 0.0 for camera in cameras}
    exit_code = 0

    try:
        coordinator.start(start_streams=True)
        consumer.start()

        if args.continuous:
            end_at = float("inf")
        else:
            end_at = time.monotonic() + args.duration
            
        next_status = 0.0

        while time.monotonic() < end_at and not shutdown_event.is_set():
            if geometry_store:
                _flush_geometry(geometry_store, pending_geometry)
            if not coordinator.is_running():
                raise RuntimeError("El coordinador de inferencia se detuvo")
            for camera in cameras:
                try:
                    result = coordinator.get_latest_result(camera.camera_id, after_sequence=last_sequences[camera.camera_id],
                                                           after_session=analyzers[camera.camera_id].session_id)
                    if result is None:
                        continue

                    if analyzers[camera.camera_id].session_id != result.session_id:
                        analyzers[camera.camera_id] = HotelPhase1Analyzer(camera, session_id=result.session_id)
                    last_sequences[camera.camera_id] = result.source_sequence

                    # Feed clip buffers
                    if clip_manager is not None and result.raw_frame is not None:
                        try:
                            clip_manager.add_frame(
                                camera_id=camera.camera_id,
                                captured_at=result.captured_at,
                                sequence=result.source_sequence,
                                frame=result.raw_frame
                            )
                        except Exception as exc:
                            print(f"[RUNNER] Clip buffer fallo en {camera.camera_id}: {exc}")

                    # Save snapshots in real time atomically, max 1/s per camera.
                    now_mono = time.monotonic()
                    if args.latest_snapshots and result.raw_frame is not None and now_mono - last_snapshot_write[camera.camera_id] >= 1.0:
                        try:
                            preview = result.annotated_frame if camera.geometry and result.annotated_frame is not None else result.raw_frame
                            if atomic_write_jpeg(runtime_snapshot_path(camera.camera_id), preview):
                                last_snapshot_write[camera.camera_id] = now_mono
                        except Exception as exc:
                            print(f"[RUNNER] Snapshot fallo en {camera.camera_id}: {exc}")

                    # 1. Alimentar TrackFrameBuffer con el frame limpio (raw_frame o annotated)
                    frame_to_buffer = result.raw_frame if result.raw_frame is not None else result.annotated_frame
                    if frame_to_buffer is not None:
                        for vehicle in result.tracked_vehicles:
                            try:
                                frame_buffer.add(
                                    camera_id=camera.camera_id,
                                    track_id=vehicle["id"],
                                    session_id=result.session_id,
                                    frame=frame_to_buffer,
                                    vehicle=dict(vehicle, source_sequence=result.source_sequence),
                                    captured_at=result.captured_at
                                )
                            except Exception as exc:
                                print(f"[RUNNER] Frame buffer fallo en {camera.camera_id}: {exc}")

                    # 2. Actualizar el HotelPhase1Analyzer
                    if result.annotated_frame is not None:
                        analyzers[camera.camera_id].update(
                            tracked_vehicles=result.tracked_vehicles,
                            frame=result.annotated_frame,
                            captured_at=result.captured_at,
                            frame_width=result.annotated_frame.shape[1],
                            frame_height=result.annotated_frame.shape[0],
                        )
                except Exception as exc:
                    print(f"[RUNNER] Resultado inválido aislado en {camera.camera_id}: {exc}")
                    continue

            now = time.monotonic()
            if now >= next_status:
                _print_status(coordinator, analyzers, consumer)
                if args.status_file:
                    _write_status_json(args.status_file, coordinator, analyzers, consumer, clip_manager, started, started_wall)
                next_status = now + args.status_interval

            shutdown_event.wait(0.02)

    except KeyboardInterrupt:
        print("[RUNNER] Interrupción manual detectada.")
    except Exception as e:
        print(f"[RUNNER] Excepción grave durante la ejecucion: {e}")
        exit_code = 1
    finally:
        print("[RUNNER] Deteniendo hilos de procesamiento de forma segura...")
        if clip_manager is not None:
            clip_manager.stop(drain=True)
            
        scheduler_ok = coordinator.stop(stop_streams=False)
        consumer.stop(drain=True)
        if geometry_store and scheduler_ok:
            try:
                _flush_geometry(geometry_store, pending_geometry)
            except Exception as exc:
                print(f"[RUNNER] Falló persistencia de geometría: {exc}")
                exit_code = 1
            finally:
                geometry_store.close()
        streams_ok = _stop_streams(coordinator)
        coordinator.contexts.clear()
        
        if not scheduler_ok or not streams_ok:
            exit_code = 1

        # Write stopped status JSON
        if args.status_file:
            _write_stopped_status_json(args.status_file)

        # Exportar automáticamente a Excel en cada apagado limpio
        if os.path.exists(config.HOTEL_DATABASE_PATH):
            print(f"[RUNNER] Generando reporte excel automatizado en: {args.excel_output} ...")
            store = HotelStore(config.HOTEL_DATABASE_PATH)
            try:
                exporter = HotelExcelExporter(store, args.excel_output)
                exporter.export()
                print("[RUNNER] Reporte Excel exportado exitosamente.")
            except Exception as e:
                print(f"[RUNNER] ERROR al exportar reporte Excel: {e}")
            finally:
                store.close()

    elapsed = max(time.monotonic() - started, 0.001)
    cpu = (time.process_time() - cpu_started) / elapsed * 100.0
    print(f"[RUNNER] CPU proceso aproximado: {cpu:.1f}%")
    print(f"[RUNNER] RAM inicial/final: {ram_started:.1f}MB/{_rss_mb():.1f}MB")
    print("[RUNNER] Cierre y guardado completados limpiamente." if exit_code == 0 else "[RUNNER] Cierre con advertencias/errores")
    return exit_code


def _flush_geometry(store, pending):
    # Only the scheduler appends and only the runner removes committed batches.
    for _ in range(len(pending)):
        batch = []
        for event in pending[0]:
            if getattr(event, "evidence_image", None) is not None:
                identity = repr((event.camera_id,event.occurred_at.isoformat(),event.track_id,event.feature_id))
                name = uuid.uuid5(uuid.NAMESPACE_URL,identity).hex + ".jpg"
                path = os.path.join(config.CAPTURE_DIR,event.camera_id,event.occurred_at.date().isoformat(),name)
                try:
                    if atomic_write_jpeg(path,event.evidence_image):
                        event = replace(event,evidence_path=path)
                except (OSError, cv2.error):
                    logging.getLogger(__name__).warning("room_evidence_failed camera=%s",event.camera_id)
            batch.append(event)
        store.save_geometry_events(batch)
        pending.popleft()


def _print_status(coordinator, analyzers, consumer):
    coord_status = coordinator.get_status()
    cons_status = consumer.get_status()
    print("================== STATUS MULTICAMERA MVP ==================")
    print(f"Model Loads: {coord_status.model_load_count} | Scheduler: {'RUNNING' if coord_status.scheduler_running else 'STOPPED'}")
    print(f"Consumer: {'RUNNING' if cons_status.running else 'STOPPED'} | Queue Size: {cons_status.queue_size}")
    print(f"Events Processed: {cons_status.processed_events} | Failed: {cons_status.failed_events} | DB Failures: {cons_status.database_failures}")
    if cons_status.last_error:
        print(f"Last Consumer Error: {cons_status.last_error}")

    for camera in coord_status.cameras:
        analyzer = analyzers.get(camera.camera_id)
        latest = analyzer.latest_results() if analyzer else []
        print(f"--- Camara {camera.camera_id} ---")
        print(f"  Stream: {'CONNECTED' if camera.stream_connected else 'DISCONNECTED'} | Capture FPS: {camera.capture_fps:.1f} | Inference FPS: {camera.actual_inference_fps:.1f}")
        print(f"  Active Tracks: {list(camera.active_track_ids)}")
        for result in latest[-2:]:
            print(
                f"  Track={result.track_id} | Movement={result.movement} ({result.movement_confidence:.2%}) "
                f"| Room={result.room_number or 'NO_DET'} | Room Status={result.room_status} ({result.room_confidence:.2%})"
            )
    print("============================================================")
    print()


def _write_status_json(status_file: str, coordinator, analyzers, consumer, clip_manager, started_mono: float, started_wall: datetime):
    coord_status = coordinator.get_status()
    cons_status = consumer.get_status()

    events_saved = 0
    visits_open = 0
    review_pending = 0
    ocr_completed = 0

    try:
        store = HotelStore(config.HOTEL_DATABASE_PATH)
        stats = store.get_summary_stats()
        events_saved = stats["total_vehiculos"]
        visits_open = stats["visits_open"]
        review_pending = stats["review_pending"]
        ocr_completed = stats["ocr_completed"]
        store.close()
    except Exception as exc:
        print(f"[RUNNER] Status DB stats fallo: {exc}")

    clip_pending = clip_manager.get_pending_count() if clip_manager else 0
    camera_data = {}
    now = time.monotonic()

    for cam in coord_status.cameras:
        analyzer = analyzers.get(cam.camera_id)
        latest_runs = analyzer.latest_results() if analyzer else []
        
        last_event_type = None
        last_plate = None
        last_room = None
        
        if latest_runs:
            last_run = latest_runs[-1]
            last_event_type = last_run.movement
            last_plate = getattr(last_run, "plate", None)
            last_room = last_run.room_number

        geometry = getattr(getattr(analyzer, "camera", None), "geometry", None)
        camera_data[cam.camera_id] = {
            "connected": bool(cam.stream_connected),
            "geometry_configured": bool(geometry) if analyzer else None,
            "geometry_features": [f.id for f in geometry.features if f.enabled] if geometry else [],
            "capture_fps": float(round(cam.capture_fps, 2)),
            "inference_fps": float(round(cam.actual_inference_fps, 2)),
            "frame_age_seconds": float(round(now - cam.last_inference_at, 3)) if cam.last_inference_at > 0 else None,
            "vehicle_detections_last_frame": cam.detections_count,
            "crossings": cam.crossing_candidates,
            "events_published": cam.events_published,
            "events_dropped": cam.event_queue_dropped,
            "inference_ms": cam.inference_ms_last,
            "active_tracks": int(len(cam.active_track_ids)),
            "last_event_type": last_event_type,
            "last_plate": last_plate,
            "last_room": last_room,
            "last_error": cam.last_error
        }

    status_dict = {
        "updated_at": datetime.now().isoformat(),
        "process_started_at": started_wall.isoformat(),
        "process_uptime_seconds": round(time.monotonic() - started_mono, 3),
        "main_loop_heartbeat": datetime.now().isoformat(),
        "running": True,
        "model_loads": int(coord_status.model_load_count),
        "consumer_alive": bool(cons_status.consumer_alive),
        "clip_manager_alive": bool(clip_manager and getattr(clip_manager, "is_alive", lambda: False)()),
        "last_successful_event_at": cons_status.last_success_at,
        "last_event_error": cons_status.last_error,
        "consecutive_event_failures": cons_status.consecutive_failures,
        "snapshot_age_seconds": {
            "A1": snapshot_info("A1").age_seconds,
            "A5": snapshot_info("A5").age_seconds,
        },
        "events_saved": int(events_saved),
        "visits_open": int(visits_open),
        "review_pending": int(review_pending),
        "ocr_completed": int(ocr_completed),
        "ocr_session_counts": cons_status.ocr_counts,
        "clip_jobs_pending": int(clip_pending),
        "cameras": camera_data
    }

    try:
        atomic_write_json(status_file, status_dict)
    except Exception as exc:
        print(f"[RUNNER] Escritura status JSON fallo: {exc}")


def _write_stopped_status_json(status_file: str):
    try:
        status_dict = {
            "updated_at": datetime.now().isoformat(),
            "running": False,
            "model_loads": 0,
            "events_saved": 0,
            "visits_open": 0,
            "review_pending": 0,
            "ocr_completed": 0,
            "clip_jobs_pending": 0,
            "cameras": {}
        }
        atomic_write_json(status_file, status_dict)
    except Exception as exc:
        print(f"[RUNNER] Escritura stopped status fallo: {exc}")


def _stop_streams(coordinator) -> bool:
    ok = True
    for context in coordinator.contexts:
        stopped = context.stream.stop()
        print(f"[RUNNER] {context.camera_id} stream detenido correctamente" if stopped else f"[RUNNER] ERROR {context.camera_id} no terminó")
        ok = ok and stopped
    return ok


def _install_signal_handlers():
    shutdown_event = threading.Event()

    def handle_signal(_signum, _frame):
        global _shutdown_requested
        if not _shutdown_requested:
            _shutdown_requested = True
            print("[RUNNER] Interrupcion solicitada; deteniendo de forma controlada...")
        else:
            print("[RUNNER] Cierre ya en progreso...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    return shutdown_event


def _rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


if __name__ == "__main__":
    raise SystemExit(main())
