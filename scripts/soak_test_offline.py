#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import resource
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.evidence import EvidenceCapture
import app.multicamera.event_consumer as event_consumer_module
from app.multicamera.event_candidate import EventCandidate
from app.multicamera.event_consumer import MultiCameraEventConsumer


class OfflineCoordinator:
    def __init__(self) -> None:
        self._event_queue: queue.Queue[EventCandidate] = queue.Queue()

    def get_event(self, timeout: float | None = None):
        try:
            return self._event_queue.get(timeout=timeout)
        except queue.Empty:
            return None


class OfflinePlateReader:
    def __init__(self, inject_failures: bool) -> None:
        self.inject_failures = inject_failures
        self.calls = 0

    def read(self, _image):
        self.calls += 1
        if self.inject_failures and self.calls % 17 == 0:
            raise RuntimeError("offline OCR injected failure")
        return {"plate": f"PDA{self.calls % 10000:04d}", "confidence": 0.86}


class OfflineEvidence(EvidenceCapture):
    def __init__(self, capture_dir: str, inject_failures: bool) -> None:
        super().__init__(enabled=True, capture_dir=capture_dir, padding=6)
        self.inject_failures = inject_failures
        self.calls = 0

    def capture(self, frame, vehicle, event):
        self.calls += 1
        if self.inject_failures and self.calls % 23 == 0:
            raise RuntimeError("offline evidence injected failure")
        return super().capture(frame, vehicle, event)


class OfflineDB:
    def __init__(self, inject_failures: bool) -> None:
        self.inject_failures = inject_failures
        self.calls = 0
        self.saved = 0
        self.lock_burst_remaining = 0
        self.failures = 0

    def save_event(self, _event):
        self.calls += 1
        if self.inject_failures and self.calls % 29 == 0:
            self.lock_burst_remaining = 4
        if self.lock_burst_remaining > 0:
            self.lock_burst_remaining -= 1
            if self.lock_burst_remaining == 0:
                self.failures += 1
            raise RuntimeError("database is locked")
        self.saved += 1
        return self.saved


class OfflineClipManager:
    def __init__(self, inject_failures: bool) -> None:
        self.inject_failures = inject_failures
        self.calls = 0
        self.failures = 0

    def request_clip(self, **_kwargs):
        self.calls += 1
        if self.inject_failures and self.calls % 31 == 0:
            self.failures += 1
            raise BrokenPipeError("offline ffmpeg BrokenPipe injected")
        if self.inject_failures and self.calls % 37 == 0:
            self.failures += 1
            raise TimeoutError("offline ffmpeg timeout injected")
        return True

    def get_pending_count(self) -> int:
        return 0

    def is_alive(self) -> bool:
        return False


class OfflineHotelStore:
    def __init__(self, inject_failures: bool) -> None:
        self.inject_failures = inject_failures
        self.calls = 0
        self.saved = 0
        self.lock_burst_remaining = 0
        self.failures = 0

    def get_open_visit(self, _plate):
        return None

    def save_event_and_update_visit(self, _event):
        self.calls += 1
        if self.inject_failures and self.calls % 29 == 0:
            self.lock_burst_remaining = 4
        if self.lock_burst_remaining > 0:
            self.lock_burst_remaining -= 1
            if self.lock_burst_remaining == 0:
                self.failures += 1
            raise RuntimeError("database is locked")
        self.saved += 1

    def close(self):
        return None


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _candidate(index: int, frame, inject_failures: bool) -> EventCandidate:
    now = datetime.now(timezone.utc)
    bbox = (8, 8, 92, 72)
    if inject_failures and index % 41 == 0:
        bbox = (20, 20, 20, 40)
    if inject_failures and index % 53 == 0:
        frame = None
    return EventCandidate(
        event_id=f"offline-{index}",
        camera_id="A1" if index % 2 == 0 else "A5",
        camera_name="Offline",
        track_id=index + 1,
        direction="INWARD" if index % 2 == 0 else "OUTWARD",
        occurred_at=now,
        source_sequence=index,
        class_name="car",
        class_id=2,
        confidence=0.9,
        bbox=bbox,
        center=(50, 40),
        frame_width=120,
        frame_height=90,
        frame=frame,
        captured_at=now,
    )


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Offline stability soak test without DVR, YOLO or EasyOCR.")
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--inject-failures", action="store_true")
    args = parser.parse_args()

    mem_initial = _rss_mb()
    threads_initial = {t.name for t in threading.enumerate()}
    frame = np.zeros((90, 120, 3), dtype=np.uint8)
    frame[:, :] = (32, 72, 120)

    with tempfile.TemporaryDirectory(prefix="deteccion_soak_") as tmp:
        coordinator = OfflineCoordinator()
        db = OfflineDB(args.inject_failures)
        hotel_store = OfflineHotelStore(args.inject_failures)
        clip_manager = OfflineClipManager(args.inject_failures)
        consumer = MultiCameraEventConsumer(
            coordinator=coordinator,
            db_path=None,
            plate_reader=OfflinePlateReader(args.inject_failures),
            evidence=OfflineEvidence(os.path.join(tmp, "captures"), args.inject_failures),
            analyzers=None,
            frame_buffer=None,
            hotel_db_path=None,
            clip_manager=clip_manager,
        )
        consumer.db_manager = db
        original_vote = event_consumer_module.vote_plates_multiframe
        vote_calls = {"n": 0, "failures": 0}

        def injected_vote(candidates):
            vote_calls["n"] += 1
            if args.inject_failures and vote_calls["n"] % 19 == 0:
                vote_calls["failures"] += 1
                raise RuntimeError("offline plate voting injected failure")
            return original_vote(candidates)

        event_consumer_module.vote_plates_multiframe = injected_vote
        consumer.start()
        consumer.db_manager = db
        consumer.hotel_store = hotel_store
        for idx in range(args.events):
            coordinator._event_queue.put(_candidate(idx, frame, args.inject_failures))
        coordinator._event_queue.join()
        time.sleep(0.2)
        status = consumer.get_status()
        consumer.stop(drain=True)
        event_consumer_module.vote_plates_multiframe = original_vote

    threads_after = {t.name for t in threading.enumerate()}
    leaked_threads = sorted(name for name in threads_after - threads_initial if name not in {"MainThread"})
    summary = {
        "events_attempted": args.events,
        "events_saved": hotel_store.saved,
        "events_review_required": status.failed_events + status.evidence_failures + status.ocr_failures,
        "ocr_failures": status.ocr_failures,
        "evidence_failures": status.evidence_failures,
        "database_failures": status.database_failures + hotel_store.failures,
        "clip_failures": clip_manager.failures,
        "voting_failures": vote_calls["failures"],
        "consumer_alive": status.consumer_alive,
        "queue_size": status.queue_size,
        "threads_remaining": leaked_threads,
        "memory_initial_mb": round(mem_initial, 2),
        "memory_final_mb": round(_rss_mb(), 2),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary["queue_size"] != 0 or leaked_threads:
        return 1
    if args.events >= 2 and status.processed_events == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
