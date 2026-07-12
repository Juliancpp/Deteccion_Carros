from __future__ import annotations

import json
import os
import queue
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime

import cv2
import torch

from app import config
from app.hotel.frame_buffer import FrameCandidate
from app.hotel.models import GateEvent, HotelVehicleEvent, PlateDecision, RoomDecision
from app.hotel.plate_voting import vote_plate
from app.hotel.store import HotelStore, classify_event


@dataclass
class HotelEventJob:
    gate_event: GateEvent
    camera_name: str
    vehicle: dict
    room: RoomDecision
    candidates: list[FrameCandidate]


class HotelEventWorker:
    def __init__(
        self,
        store: HotelStore,
        *,
        queue_maxsize: int = config.HOTEL_OCR_QUEUE_MAXSIZE,
        evidence_dir: str = config.HOTEL_EVIDENCE_DIR,
        plate_reader_factory=None,
        exporter=None,
    ) -> None:
        self.store = store
        self.evidence_dir = evidence_dir
        self.plate_reader_factory = plate_reader_factory
        self.exporter = exporter
        self._queue: queue.Queue[HotelEventJob] = queue.Queue(maxsize=queue_maxsize)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._reader = None
        self.processed_events = 0
        self.dropped_jobs = 0
        self.last_event: HotelVehicleEvent | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="HotelEventWorker", daemon=False)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> bool:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        return thread is None or not thread.is_alive()

    def submit(self, job: HotelEventJob) -> bool:
        try:
            self._queue.put_nowait(job)
            return True
        except queue.Full:
            self.dropped_jobs += 1
            return False

    def pending_count(self) -> int:
        return self._queue.qsize()

    def _run(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                job = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                event = self._process_job(job)
                self.store.save_event_and_update_visit(event)
                self.processed_events += 1
                self.last_event = event
                if self.exporter is not None:
                    self.exporter.export()
            finally:
                self._queue.task_done()

    def _process_job(self, job: HotelEventJob) -> HotelVehicleEvent:
        plate = self._read_plate(job.candidates)
        event_type, review_by_type, notes = classify_event(
            job.gate_event.camera_id,
            job.gate_event.movement,
            plate.plate,
            self.store,
        )
        review_required = (
            review_by_type
            or plate.status != "LEIDA"
            or job.room.room_status == "NO_DETERMINADA"
        )
        evidence_path = self._save_evidence(job, plate, event_type)
        return HotelVehicleEvent(
            event_id=str(uuid.uuid4()),
            occurred_at=job.gate_event.occurred_at,
            camera_id=job.gate_event.camera_id,
            camera_name=job.camera_name,
            track_id=job.gate_event.track_id,
            movement=job.gate_event.movement,
            event_type=event_type,
            plate=plate.plate,
            plate_confidence=plate.confidence,
            plate_status=plate.status,
            room_number=job.room.room_number,
            room_confidence=job.room.room_confidence,
            room_status=job.room.room_status,
            vehicle_class=str(job.vehicle.get("class", "Vehiculo")),
            vehicle_confidence=float(job.vehicle.get("confidence", 0.0)),
            evidence_path=evidence_path,
            review_required=review_required,
            notes=notes,
            plate_candidates=list(plate.candidates),
        )

    def _reader_instance(self):
        if self._reader is None:
            torch.backends.quantized.engine = "qnnpack"
            torch.backends.mkldnn.enabled = False
            if self.plate_reader_factory is not None:
                self._reader = self.plate_reader_factory()
            else:
                from app.ocr import PlateReader

                self._reader = PlateReader()
        return self._reader

    def _read_plate(self, candidates: list[FrameCandidate]) -> PlateDecision:
        reader = self._reader_instance()
        raw_candidates = []
        for candidate in candidates[:5]:
            for crop in _crops(candidate.frame, candidate.bbox):
                result = reader.read(crop)
                if result:
                    raw_candidates.append(result)
        voted = vote_plate(raw_candidates, min_confidence=config.HOTEL_PLATE_MIN_CONFIDENCE)
        return PlateDecision(
            plate=voted["plate"],
            confidence=float(voted["confidence"]),
            status=str(voted["status"]),
            candidates=tuple(voted["candidates"]),
        )

    def _save_evidence(self, job: HotelEventJob, plate: PlateDecision, event_type: str) -> str | None:
        if not job.candidates:
            return None
        frame = job.candidates[0].frame.copy()
        x1, y1, x2, y2 = [int(v) for v in job.vehicle["bbox"]]
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 128), 2)
        plate_text = plate.plate or "SIN_PLACA"
        room = job.room.room_number if job.room.room_number is not None else "NO_DET"
        lines = [
            f"{job.gate_event.camera_id} {event_type} {job.gate_event.movement}",
            f"Track {job.gate_event.track_id} {job.vehicle.get('class', 'Vehiculo')} {float(job.vehicle.get('confidence', 0.0)):.2f}",
            f"Placa {plate_text} {plate.confidence:.2f} {plate.status}",
            f"Habitacion {room} {job.room.room_confidence:.2f} {job.room.room_status}",
            job.gate_event.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
        ]
        _draw_panel(frame, lines)
        date_dir = job.gate_event.occurred_at.strftime("%Y-%m-%d")
        timestamp = job.gate_event.occurred_at.strftime("%Y-%m-%d_%H-%M-%S")
        movement = event_type.lower()
        filename = f"{job.gate_event.camera_id}_{movement}_{plate_text}_{timestamp}.jpg"
        path = os.path.join(self.evidence_dir, date_dir, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not cv2.imwrite(path, frame):
            return None
        audit_path = path + ".json"
        with open(audit_path, "w", encoding="utf-8") as fh:
            json.dump({"plate_candidates": list(plate.candidates)}, fh, ensure_ascii=False, indent=2)
        return path


def _crops(frame, bbox: tuple[int, int, int, int]):
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(width, x2), min(height, y2)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return []
    h, w = crop.shape[:2]
    return [
        crop.copy(),
        crop[h // 2 : h, :].copy(),
        crop[h // 2 : h, w // 4 : (w * 3) // 4].copy(),
    ]


def _draw_panel(frame, lines: list[str]) -> None:
    font = cv2.FONT_HERSHEY_SIMPLEX
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (min(frame.shape[1], 680), 28 + 24 * len(lines)), (10, 10, 10), -1)
    cv2.addWeighted(overlay, 0.72, frame, 0.28, 0, frame)
    for index, line in enumerate(lines):
        cv2.putText(frame, line, (12, 28 + index * 24), font, 0.62, (0, 255, 255), 2, cv2.LINE_AA)
