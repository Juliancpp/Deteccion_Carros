import json
import logging
import os
import queue
import threading
import time
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import cv2
import numpy as np

import app.config as config
from app.database import DatabaseManager
from app.evidence import EvidenceCapture
from app.multicamera.coordinator import MultiCameraCoordinator
from app.multicamera.event_candidate import EventCandidate
from app.ocr import PlateReader
from app.hotel.models import HotelVehicleEvent
from app.hotel.store import HotelStore, classify_event
from app.hotel.plate_voting import vote_plates_multiframe
from app.hotel.event_source import validate_hotel_event_source
from app.safety import safe_crop

logger = logging.getLogger(__name__)


@dataclass
class ConsumerStatus:
    running: bool
    queue_size: int
    processed_events: int
    failed_events: int
    invalid_events: int
    ocr_failures: int
    evidence_failures: int
    database_failures: int
    last_event_at: Optional[float]
    last_error: Optional[str]
    average_processing_ms: float
    total_processing_ms: float
    consumer_alive: bool = False
    last_heartbeat_at: Optional[float] = None
    last_success_at: Optional[float] = None
    last_error_at: Optional[float] = None
    consecutive_failures: int = 0
    total_failures: int = 0
    oldest_queue_item_age: Optional[float] = None
    ocr_counts: dict = field(default_factory=dict)


class MultiCameraEvidenceCapture(EvidenceCapture):
    """Subclase de EvidenceCapture con separación por cámara y nombres sin colisiones."""

    def __init__(self, capture_dir: str, padding: int = 20):
        super().__init__(enabled=True, capture_dir=capture_dir, padding=padding)

    def _build_filepath(self, event: dict) -> str:
        camera_id = str(event.get("camera_id", "default"))
        timestamp = self._event_datetime(event)
        date_dir = timestamp.strftime("%Y-%m-%d")
        filename_timestamp = timestamp.strftime("%Y-%m-%d_%H-%M-%S")
        event_type = event.get("type", "event").lower()
        vehicle_id = event.get("vehicle_id", 0)

        # Generar nombre resistente a colisiones
        unique_id = uuid.uuid4().hex[:6]
        filename = f"{camera_id}_{event_type}_ID{vehicle_id}_{filename_timestamp}_{unique_id}.jpg"

        # Ruta: data/evidence/camera_id/YYYY-MM-DD/filename
        return os.path.join(self.capture_dir, camera_id, date_dir, filename)


class HotelEvidenceCapture(EvidenceCapture):
    """Evidence capture specifically designed for Hotel MVP requirements."""

    def __init__(self, capture_dir: str, padding: int = 20):
        super().__init__(enabled=True, capture_dir=capture_dir, padding=padding)

    def _build_filepath(self, event: dict) -> str:
        camera_id = str(event.get("camera_id", "default"))
        timestamp = self._event_datetime(event)
        date_dir = timestamp.strftime("%Y-%m-%d")
        return os.path.join(self.capture_dir, camera_id, date_dir, uuid.uuid4().hex + ".jpg")

    def save_ocr_frame(self, image, camera_id, captured_at):
        path = self._build_filepath({"camera_id": camera_id, "timestamp": captured_at.isoformat()})
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not cv2.imwrite(path, image):
            raise OSError("ocr_evidence_write_failed")
        return path

    def _draw_evidence(
        self,
        image: np.ndarray,
        bbox: list,
        vehicle: dict,
        event: dict,
    ) -> np.ndarray:
        annotated = image.copy()
        try:
            x1, y1, x2, y2 = [int(round(float(c))) for c in bbox]
        except (ValueError, TypeError):
            x1, y1, x2, y2 = 0, 0, image.shape[1], image.shape[0]

        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 255), 2)

        # Extraer metadatos
        camera_id = event.get("camera_id", "Unknown")
        track_id = event.get("vehicle_id", 0)
        movement = event.get("movement", "UNKNOWN")
        event_type = event.get("type", "EVENTO_NO_RESUELTO")
        plate = event.get("plate") or "NO_LEGIBLE"
        plate_confidence = event.get("plate_confidence", 0.0)
        room_number = event.get("room_number")
        room_status = event.get("room_status", "NO_DETERMINADA")

        timestamp = self._display_timestamp(event)

        lines = [
            f"CAM: {camera_id} | ID: {track_id}",
            f"TIPO: {event_type} | MOV: {movement}",
            f"PLACA: {plate} ({plate_confidence:.0%})",
            f"HAB: {room_number or 'NO_DET'} ({room_status})",
            timestamp,
        ]
        self._draw_text_panel(annotated, lines)
        return annotated


class MultiCameraEventConsumer:
    """Consumidor multihilo encargado de procesar eventos, OCR, evidencias y guardarlos en DB."""

    def __init__(
        self,
        coordinator: MultiCameraCoordinator,
        db_path: Optional[str] = None,
        plate_reader: Optional[PlateReader] = None,
        evidence: Optional[EvidenceCapture] = None,
        padding: int = 20,
        analyzers: Optional[dict[str, Any]] = None,
        frame_buffer: Optional[Any] = None,
        hotel_db_path: Optional[str] = None,
        room_timeout: float = 8.0,
        clip_manager: Optional[Any] = None,
    ):
        self.coordinator = coordinator
        self._db_path = db_path
        self.plate_reader = plate_reader or PlateReader()
        self.evidence = evidence or MultiCameraEvidenceCapture(
            capture_dir=config.CAPTURE_DIR, padding=padding
        )
        self._padding = padding

        # Extensiones de Hotel MVP
        self.analyzers = analyzers
        self.frame_buffer = frame_buffer
        self.hotel_db_path = hotel_db_path
        self.room_timeout = room_timeout
        self.clip_manager = clip_manager
        self.hotel_store: Optional[HotelStore] = None
        self._pending_events = []
        validate_hotel_event_source({"phase1_analyzer"} if analyzers is not None else {config.HOTEL_EVENT_SOURCE})

        self.db_manager: Optional[DatabaseManager] = None

        # Sincronización y control
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._drain = True

        # Métricas
        self._processed_events = 0
        self._failed_events = 0
        self._invalid_events = 0
        self._ocr_counts = Counter()
        self._ocr_failures = 0
        self._evidence_failures = 0
        self._database_failures = 0
        self._last_event_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._average_processing_ms = 0.0
        self._total_processing_ms = 0.0
        self._last_heartbeat_at: Optional[float] = None
        self._last_success_at: Optional[float] = None
        self._last_error_at: Optional[float] = None
        self._consecutive_failures = 0
        self._total_failures = 0
        self._max_consecutive_failures = 25

    def start(self) -> None:
        """Inicia el hilo de ejecución del consumidor."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop, name="MultiCameraEventConsumer", daemon=True
            )
            self._thread.start()
            logger.info("MultiCameraEventConsumer iniciado.")

    def stop(self, drain: bool = True) -> None:
        """Detiene el consumidor, esperando opcionalmente a vaciar la cola actual."""
        with self._lock:
            if not self._running:
                return
            self._drain = drain
            self._stop_event.set()

        if self._thread:
            # Join con un timeout prudente para evitar bloqueos eternos
            self._thread.join(timeout=10.0 if drain else 2.0)
            self._running = False
            logger.info("MultiCameraEventConsumer detenido.")

    @property
    def is_running(self) -> bool:
        """Devuelve True si el consumidor está activo."""
        with self._lock:
            return self._running

    def get_status(self) -> ConsumerStatus:
        """Devuelve un objeto ConsumerStatus con las métricas actuales de procesamiento."""
        with self._lock:
            return ConsumerStatus(
                running=self._running,
                queue_size=self.coordinator._event_queue.qsize(),
                processed_events=self._processed_events,
                failed_events=self._failed_events,
                invalid_events=self._invalid_events,
                ocr_failures=self._ocr_failures,
                ocr_counts=dict(self._ocr_counts),
                evidence_failures=self._evidence_failures,
                database_failures=self._database_failures,
                last_event_at=self._last_event_at,
                last_error=self._last_error,
                average_processing_ms=self._average_processing_ms,
                total_processing_ms=self._total_processing_ms,
                consumer_alive=self._thread is not None and self._thread.is_alive(),
                last_heartbeat_at=self._last_heartbeat_at,
                last_success_at=self._last_success_at,
                last_error_at=self._last_error_at,
                consecutive_failures=self._consecutive_failures,
                total_failures=self._total_failures,
                oldest_queue_item_age=self._oldest_pending_age_locked(),
            )

    def _run_loop(self) -> None:
        """Ciclo de procesamiento principal ejecutado en su propio hilo."""
        # Inicializar DatabaseManager en el hilo propio del consumidor
        try:
            self.db_manager = DatabaseManager(
                db_path=self._db_path or config.DATABASE_PATH
            )
        except Exception as e:
            with self._lock:
                self._last_error = f"Failed to initialize DatabaseManager: {e}"
                self._database_failures += 1
            logger.error("Error al inicializar base de datos en consumidor: %s", e)

        # Inicializar HotelStore si corresponde
        if self.hotel_db_path:
            try:
                self.hotel_store = HotelStore(db_path=self.hotel_db_path)
            except Exception as e:
                with self._lock:
                    self._last_error = f"Failed to initialize HotelStore: {e}"
                logger.error("Error al inicializar hotel store en consumidor: %s", e)

        while True:
            with self._lock:
                self._last_heartbeat_at = time.time()
            # Validar parada y ver si terminamos
            stop_set = self._stop_event.is_set()
            if stop_set:
                if not self._drain:
                    break
                # Si es drain, sólo continuamos si hay elementos en cola o pendientes
                if self.coordinator._event_queue.empty() and not self._pending_events:
                    break

            # 1. Obtener candidatos nuevos de la cola del coordinador
            try:
                event_candidate = self.coordinator.get_event(timeout=0.05)
                if event_candidate is not None:
                    self._pending_events.append({
                        "candidate": event_candidate,
                        "first_seen": time.monotonic()
                    })
            except Exception as e:
                logger.warning("Error fetching candidate: %s", e)

            # Si no hay analizadores asociados, procesamos inmediatamente todo lo que llegue
            if not self.analyzers:
                resolved = list(self._pending_events)
                for p in resolved:
                    self._process_candidate_safely(p["candidate"], None)
                    self._mark_queue_task_done()
                    self._pending_events.remove(p)
                time.sleep(0.01)
                continue

            # 2. Evaluar la resolución diferida para eventos pendientes
            resolved_list = []
            for pending in list(self._pending_events):
                cand = pending["candidate"]
                first_seen = pending["first_seen"]

                analyzer = self._candidate_analyzer(cand)
                track_result = None
                if analyzer:
                    track_result = analyzer._latest_results.get(cand.track_id)

                elapsed = time.monotonic() - first_seen
                is_finalized = track_result.finalized if track_result else False
                room_status = track_result.room_status if track_result else "NO_DETERMINADA"

                # Resolver si:
                # - El track está finalizado por el analizador
                # - La decisión de habitación ya es firme (CONFIRMADA o PROBABLE)
                # - Ha expirado el tiempo de espera (room_timeout)
                if is_finalized or room_status in ("CONFIRMADA", "PROBABLE") or elapsed >= self.room_timeout:
                    resolved_list.append(pending)
                    self._process_candidate_safely(cand, track_result)
                    self._mark_queue_task_done()

            for p in resolved_list:
                self._pending_events.remove(p)

            time.sleep(0.01)

        # Cerrar conexión de HotelStore al salir del hilo
        if self.hotel_store:
            try:
                self.hotel_store.close()
            except Exception:
                pass

    def _mark_queue_task_done(self) -> None:
        queue_obj = getattr(self.coordinator, "_event_queue", None)
        if queue_obj is None or not hasattr(queue_obj, "task_done"):
            return
        try:
            queue_obj.task_done()
        except ValueError:
            logger.debug("Queue task_done omitido: contador ya estaba en cero.")

    def _oldest_pending_age_locked(self) -> Optional[float]:
        if not self._pending_events:
            return None
        return max(0.0, time.monotonic() - min(item["first_seen"] for item in self._pending_events))

    def _candidate_analyzer(self, candidate):
        analyzer = self.analyzers.get(candidate.camera_id) if self.analyzers else None
        if candidate.session_id and getattr(analyzer, "session_id", "") != candidate.session_id:
            return None
        return analyzer

    def _process_candidate_safely(self, candidate: EventCandidate, track_result: Optional[Any]) -> None:
        try:
            self._process_resolved_candidate(candidate, track_result)
        except Exception as e:
            self._record_stage_failure("consumer_wrapper", e)
            logger.error(
                "Excepción no controlada procesando candidato %s/%s:\n%s",
                getattr(candidate, "camera_id", "?"),
                getattr(candidate, "track_id", "?"),
                traceback.format_exc(limit=20),
            )

    def _process_candidate(self, candidate: EventCandidate) -> None:
        """Alias de compatibilidad con pruebas unitarias."""
        self._process_candidate_safely(candidate, None)

    def _record_stage_failure(self, stage: str, exc: Exception) -> None:
        message = f"{stage}: {type(exc).__name__}: {exc}"
        with self._lock:
            self._failed_events += 1
            self._total_failures += 1
            self._consecutive_failures += 1
            self._last_error_at = time.time()
            self._last_error = message[:500]
        logger.warning("Fallo controlado en etapa %s: %s", stage, exc, exc_info=True)

    @staticmethod
    def _append_note(notes: str, reason: str) -> str:
        return f"{reason}; {notes}" if notes and reason not in notes else (notes or reason)

    def _process_resolved_candidate(self, candidate: EventCandidate, track_result: Optional[Any]) -> None:
        """Valida, ejecuta OCR (con votación multiframe), guarda evidencia y persiste los resultados."""
        existing = self.hotel_store.get_event(candidate.event_id) if self.hotel_store else None
        if existing is not None:
            if self.db_manager:
                self.db_manager.save_event(dict(source_event_id=candidate.event_id,timestamp=existing["occurred_at"],
                    plate=existing["plate"],confidence=existing["plate_confidence"],type=existing["movement"],
                    vehicle_id=existing["track_id"],image_path=existing["evidence_path"],camera=existing["camera_id"]))
            return
        start_time = time.perf_counter()
        success = False
        is_invalid = False
        persistence_failed = False
        err_msg = None

        try:
            # 1. Validar frame y bbox
            frame = candidate.frame
            crop_result = safe_crop(frame, candidate.bbox)
            if crop_result.crop is None or crop_result.bbox is None:
                is_invalid = True
                raise ValueError(f"Frame/bbox inválido en EventCandidate: {crop_result.reason}")
            cx1, cy1, cx2, cy2 = crop_result.bbox

            # 3. Extraer detalles de movimiento y habitación
            if track_result is not None:
                movement = track_result.movement
                movement_confidence = track_result.movement_confidence
                room_number = track_result.room_number
                room_confidence = track_result.room_confidence
                room_status = track_result.room_status
            else:
                movement = candidate.direction
                movement_confidence = 1.0
                room_number = None
                room_confidence = 0.0
                room_status = "NO_DETERMINADA"

            if candidate.geometry_gate:
                movement = candidate.direction
                movement_confidence = 1.0

            # Get logical track ID and linked track IDs from analyzer's linker
            logical_id = candidate.track_id
            linked_track_ids = {candidate.track_id}
            analyzer = self._candidate_analyzer(candidate)
            if analyzer and hasattr(analyzer, "linker"):
                logical_id = analyzer.linker.get_logical_id(candidate.camera_id, candidate.track_id)
                linked_track_ids = analyzer.linker.get_linked_track_ids(candidate.camera_id, logical_id)

            select_start = time.perf_counter()
            # 4. OCR Multiframe
            ocr_candidates = []
            if self.frame_buffer and candidate.track_id is not None:
                # Obtener mejores frames del buffer de track
                candidates_frames = self.frame_buffer.best(
                    candidate.camera_id, linked_track_ids, limit=5, session_id=candidate.session_id
                )
                for cf in candidates_frames:
                    ocr_candidates.append({
                        "frame": cf.frame,
                        "bbox": cf.bbox,
                        "frame_score": cf.score,
                        "source_sequence": cf.source_sequence,
                        "captured_at": cf.captured_at
                    })

            # Si no hay frames adicionales buffered, usar el frame del evento como fallback
            if not ocr_candidates:
                ocr_candidates.append({
                    "frame": frame,
                    "bbox": (cx1, cy1, cx2, cy2),
                    "frame_score": 1.0,
                    "source_sequence": candidate.source_sequence,
                    "captured_at": candidate.captured_at
                })

            select_ms = (time.perf_counter() - select_start) * 1000
            voted_res, diagnostics = self._inspect_frames(candidate, logical_id, ocr_candidates)
            diagnostics["metrics"]["select_ms"] = select_ms
            plate_text = voted_res["plate"]
            plate_confidence = voted_res["confidence"]
            plate_status = voted_res["status"]
            plate_candidates_list = voted_res["candidates"]

            # 5. Clasificar Evento Hotel e identificar necesidades de revisión
            event_type = "EVENTO_NO_RESUELTO"
            review_required = True
            notes = ""
            if self.hotel_store:
                event_type, review_required, notes = classify_event(
                    candidate.camera_id, movement, plate_text if plate_status == "LEIDA" else None, self.hotel_store
                )

            # Reglas adicionales de revisión
            if plate_status != "LEIDA" or diagnostics["engine_status"] != "OK":
                review_required = True
            if plate_status == "NO_LEGIBLE":
                review_required = True
                if "placa no legible" not in notes:
                    notes = f"placa no legible; {notes}" if notes else "placa no legible"

            if candidate.geometry_gate and plate_status == "PROBABLE":
                review_required = True
                notes = self._append_note(notes, "placa probable sin consenso suficiente")

            if candidate.geometry_gate and room_status == "PROBABLE":
                review_required = True
                notes = self._append_note(notes, "habitación probable pendiente de revisión")

            if room_status == "NO_DETERMINADA" or room_number is None:
                review_required = True
                if "habitacion no determinada" not in notes:
                    notes = f"habitacion no determinada; {notes}" if notes else "habitacion no determinada"

            # 6. Guardar evidencia física
            evidence_path = diagnostics["selected_evidence"]
            if self.evidence and not evidence_path:
                try:
                    vehicle_dict = {
                        "bbox": (cx1, cy1, cx2, cy2),
                        "class": candidate.vehicle_class,
                        "confidence": candidate.detection_confidence,
                    }
                    event_dict = {
                        "camera_id": candidate.camera_id,
                        "camera_name": candidate.camera_name,
                        "vehicle_id": logical_id,
                        "movement": movement,
                        "type": event_type,
                        "plate": plate_text,
                        "plate_confidence": plate_confidence,
                        "room_number": room_number,
                        "room_status": room_status,
                        "timestamp": candidate.captured_at.isoformat(),
                    }
                    evidence_path = self.evidence.capture(frame, vehicle_dict, event_dict)
                except Exception as e:
                    with self._lock:
                        self._evidence_failures += 1
                        self._last_error = f"Error al guardar evidencia: {e}"
                    logger.warning("Fallo no crítico guardando evidencia: %s", e, exc_info=True)

            if any(r.get("evidence_error") for r in diagnostics["frames"]):
                review_required = True
                notes = self._append_note(notes, "evidencia OCR incompleta")
            if not evidence_path:
                review_required = True
                if "evidencia faltante" not in notes:
                    notes = f"evidencia faltante; {notes}" if notes else "evidencia faltante"

            db_start = time.perf_counter()
            # 7. Persistencia base de datos tradicional (eventos.db)
            if self.db_manager:
                try:
                    db_event = {
                        "timestamp": candidate.captured_at,
                        "source_event_id": candidate.event_id,
                        "plate": plate_text,
                        "confidence": plate_confidence,
                        "type": movement,
                        "vehicle_id": logical_id,
                        "image_path": evidence_path,
                        "camera": candidate.camera_id,
                    }
                    self.db_manager.save_event(db_event)
                except Exception as e:
                    with self._lock:
                        self._database_failures += 1
                        self._last_error = f"Fallo al persistir en eventos.db: {type(e).__name__}"
                    review_required = True
                    notes = self._append_note(notes, "sqlite eventos.db falló")
                    persistence_failed = True
                    logger.error("db_write_failed db=eventos error=%s", type(e).__name__)

            # 8. Persistencia base de datos Hotel MVP (hotel_mvp.db)
            if self.hotel_store:
                try:
                    hotel_event = HotelVehicleEvent(
                        event_id=candidate.event_id,
                        occurred_at=candidate.captured_at,
                        camera_id=candidate.camera_id,
                        camera_name=candidate.camera_name,
                        track_id=logical_id,
                        movement=movement,
                        movement_confidence=movement_confidence,
                        event_type=event_type,
                        plate=plate_text,
                        plate_confidence=plate_confidence,
                        plate_status=plate_status,
                        room_number=room_number,
                        room_confidence=room_confidence,
                        room_status=room_status,
                        vehicle_class=candidate.vehicle_class,
                        vehicle_confidence=candidate.detection_confidence,
                        evidence_path=evidence_path,
                        review_required=review_required,
                        notes=notes,
                        source="geometry_gate" if candidate.geometry_gate else "mvp_hotel",
                        plate_supporting_frames_json=json.dumps(voted_res.get("supporting_frame_ids", ())),
                        capture_session=candidate.session_id,
                        physical_track_id=candidate.track_id,
                        logical_vehicle_id=logical_id,
                        original_track_ids_json=json.dumps(sorted(linked_track_ids)),
                        review_reasons_json=json.dumps(voted_res["review_reasons"]),
                        ocr_diagnostics_json=json.dumps(diagnostics),
                        plate_runner_up=voted_res.get("runner_up_plate"),
                        plate_runner_up_confidence=voted_res.get("runner_up_confidence", 0.0),
                        plate_vote_margin=float(voted_res.get("vote_margin", 0.0)),
                        plate_candidates=plate_candidates_list
                    )
                    self._save_hotel_event_with_retry(hotel_event)
                    
                    if self.clip_manager is not None:
                        try:
                            self.clip_manager.request_clip(
                                event_id=hotel_event.event_id,
                                camera_id=candidate.camera_id,
                                anchor_at=candidate.captured_at,
                                event_type=event_type,
                                plate=plate_text,
                                room_number=room_number,
                                track_id=logical_id
                            )
                        except Exception as clip_exc:
                            with self._lock:
                                self._last_error = f"Fallo al solicitar clip: {clip_exc}"
                            logger.warning("Fallo no crítico al solicitar clip: %s", clip_exc, exc_info=True)
                except Exception as e:
                    with self._lock:
                        self._database_failures += 1
                        self._last_error = f"Fallo al persistir en hotel_mvp.db: {type(e).__name__}"
                    persistence_failed = True
                    logger.error("db_write_failed db=hotel error=%s", type(e).__name__)

            logger.info("event_persistence camera=%s elapsed_ms=%.2f failed=%s",
                        candidate.camera_id, (time.perf_counter() - db_start) * 1000, persistence_failed)
            success = not persistence_failed

        except Exception as e:
            err_msg = str(e)
            logger.warning("Excepción procesando EventCandidate: %s", e)

        # Registro de métricas e incremento de contadores bajo candado
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        with self._lock:
            self._last_event_at = time.time()
            if is_invalid:
                self._invalid_events += 1
                self._failed_events += 1
                self._last_error = err_msg or "Evento inválido"
            elif not success:
                self._failed_events += 1
                self._last_error = err_msg or "Fallo general de guardado"
            else:
                self._processed_events += 1
                self._consecutive_failures = 0
                self._last_success_at = time.time()
                self._total_processing_ms += elapsed_ms
                self._average_processing_ms = (
                    self._total_processing_ms / self._processed_events
                )

    def _inspect_frames(self, event, logical_id, frames):
        candidates = []
        reports = []
        seen = set()
        for item in frames:
            timestamp = item["captured_at"]
            sequence = item.get("source_sequence")
            frame_id = f"{event.camera_id}:{event.session_id}:{sequence if sequence is not None else timestamp}"
            if frame_id in seen:
                continue
            seen.add(frame_id)
            crop = safe_crop(item["frame"], item["bbox"], padding=self._padding)
            report = dict(frame_id=frame_id, captured_at=str(timestamp), source_sequence=sequence,
                          vehicle_crop_bbox=crop.bbox, evidence_path=None, engine_status="SIN_CANDIDATO")
            reports.append(report)
            if crop.crop is None:
                continue
            try:
                if hasattr(self.plate_reader, "inspect_frame"):
                    readings, metrics = self.plate_reader.inspect_frame(crop.crop, frame_id)
                    report.update(metrics)
                elif hasattr(self.plate_reader, "read_all_candidates"):
                    readings = self.plate_reader.read_all_candidates(crop.crop, frame_id=frame_id)
                    report["engine_status"] = "OK"
                else:
                    result = self.plate_reader.read(crop.crop)
                    readings = [result] if result else []
                    report["engine_status"] = "OK"
            except Exception:
                readings = []
                report.update(engine_status="ERROR_OCR", errors=1)
            if report["engine_status"] == "MOTOR_NO_DISPONIBLE":
                report["errors"] = 1
            if report.get("errors"):
                with self._lock:
                    self._ocr_failures += report["errors"]
                    self._last_error = "ERROR_OCR"
            for reading in readings:
                candidates.append(dict(reading, camera_id=event.camera_id, track_id=event.track_id,
                                       logical_vehicle_id=logical_id, source_sequence=sequence,
                                       frame_id=frame_id, captured_at=str(timestamp), frame_score=item["frame_score"]))
            if readings and hasattr(self.evidence, "save_ocr_frame"):
                started = time.perf_counter()
                try:
                    report["evidence_path"] = self.evidence.save_ocr_frame(crop.crop, event.camera_id, timestamp)
                except (OSError, cv2.error):
                    with self._lock:
                        self._evidence_failures += 1
                    report["evidence_error"] = True
                report["evidence_ms"] = (time.perf_counter() - started) * 1000

        start = time.perf_counter()
        decision = vote_plates_multiframe(candidates, observed_only=True)
        states = {r["engine_status"] for r in reports}
        engine = next((state for state in ("ERROR_OCR", "MOTOR_NO_DISPONIBLE", "OK") if state in states),
                      "SIN_CANDIDATO")
        supporting = set(decision["supporting_frame_ids"])
        selected = next((r for r in reports if r["frame_id"] in supporting and r["evidence_path"]), None)
        totals = {key: sum(r.get(key, 0) for r in reports)
                  for key in ("engine_calls", "crops", "errors", "locate_ms", "preprocess_ms", "engine_ms", "evidence_ms")}
        totals["vote_ms"] = (time.perf_counter() - start) * 1000
        with self._lock:
            self._ocr_counts.update(events=1, frames=len(reports), crops=totals["crops"],
                                    engine_calls=totals["engine_calls"], technical_errors=totals["errors"])
            self._ocr_counts[decision["status"]] += 1
        logger.info("ocr_event %s", json.dumps(dict(totals, frames=len(reports), status=decision["status"],
                                                    camera=event.camera_id, engine_status=engine)))
        return decision, dict(engine_status=engine, frames=reports, metrics=totals,
                              partial_text=decision.get("partial_text"),
                              selected_frame_id=selected["frame_id"] if selected else None,
                              selected_evidence=selected["evidence_path"] if selected else None)

    def _save_hotel_event_with_retry(self, hotel_event: HotelVehicleEvent) -> None:
        if self.hotel_store is None:
            return
        delays = (0.2, 0.5, 1.0)
        for attempt, delay in enumerate((0.0, *delays), start=1):
            if delay:
                time.sleep(delay)
            try:
                self.hotel_store.save_event_and_update_visit(hotel_event)
                return
            except Exception as exc:
                text = str(exc).lower()
                if "database is locked" not in text and "database is busy" not in text:
                    raise
                if attempt > len(delays):
                    raise
                logger.warning("SQLite ocupado guardando evento %s; reintento %s", hotel_event.event_id, attempt)
