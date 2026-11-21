from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import cv2
import numpy as np


QUALITY_WEIGHTS = {
    "area": 0.25,
    "sharpness": 0.30,
    "contrast": 0.15,
    "confidence": 0.15,
    "border": 0.15,
}


@dataclass
class FrameCandidate:
    frame: np.ndarray
    bbox: tuple[int, int, int, int]
    score: float
    captured_at: object
    vehicle: dict[str, Any]
    camera_id: str
    track_id: int
    logical_vehicle_id: int | None
    frame_id: str
    source_sequence: int | None
    vehicle_width: int
    vehicle_height: int
    bbox_area_ratio: float
    sharpness: float
    brightness: float
    contrast: float
    border_penalty: float
    detection_confidence: float
    quality_score: float


class TrackFrameBuffer:
    def __init__(self, max_candidates: int = 8, ttl_seconds: float = 20.0, time_fn=time.time) -> None:
        self.max_candidates = max_candidates
        self.ttl_seconds = ttl_seconds
        self._time_fn = time_fn
        self._buffers: dict[tuple[str, str, int], list[FrameCandidate]] = {}
        self._last_seen: dict[tuple[str, str, int], float] = {}
        self._lock = threading.Lock()
        self.candidates_stored = 0
        self.candidates_discarded = 0

    def add(self, camera_id: str, track_id: int, frame: np.ndarray, vehicle: dict, captured_at, *, session_id: str = "") -> None:
        with self._lock:
            key = (camera_id, session_id, int(track_id))
            bbox = tuple(int(value) for value in vehicle["bbox"])
            metrics = candidate_metrics(frame, bbox, float(vehicle.get("confidence", 0.0)))
            source_sequence = _optional_int(vehicle.get("source_sequence") or vehicle.get("sequence"))
            logical_vehicle_id = _optional_int(vehicle.get("logical_vehicle_id") or vehicle.get("logical_id"))
            frame_id = str(
                vehicle.get("frame_id")
                or f"{camera_id}:track{logical_vehicle_id or int(track_id)}:sequence{source_sequence if source_sequence is not None else _timestamp_key(captured_at)}"
            )
            candidate = FrameCandidate(
                frame=frame.copy(),
                bbox=bbox,
                score=metrics["quality_score"],
                captured_at=captured_at,
                vehicle=dict(vehicle),
                camera_id=camera_id,
                track_id=int(track_id),
                logical_vehicle_id=logical_vehicle_id,
                frame_id=frame_id,
                source_sequence=source_sequence,
                vehicle_width=metrics["vehicle_width"],
                vehicle_height=metrics["vehicle_height"],
                bbox_area_ratio=metrics["bbox_area_ratio"],
                sharpness=metrics["sharpness"],
                brightness=metrics["brightness"],
                contrast=metrics["contrast"],
                border_penalty=metrics["border_penalty"],
                detection_confidence=metrics["detection_confidence"],
                quality_score=metrics["quality_score"],
            )
            values = self._buffers.setdefault(key, [])
            values[:] = [existing for existing in values if existing.frame_id != frame_id]
            values.append(candidate)
            values.sort(key=lambda item: item.quality_score, reverse=True)
            discarded = max(0, len(values) - self.max_candidates)
            if discarded:
                self.candidates_discarded += discarded
                del values[self.max_candidates :]
            self.candidates_stored += 1
            self._last_seen[key] = self._time_fn()
            self._cleanup_nolock()

    def best(self, camera_id: str, track_id_or_ids: int | list[int] | set[int], limit: int = 5, *, session_id: str = "") -> list[FrameCandidate]:
        with self._lock:
            if isinstance(track_id_or_ids, (list, tuple, set)):
                merged: list[FrameCandidate] = []
                for track_id in track_id_or_ids:
                    merged.extend(self._buffers.get((camera_id, session_id, int(track_id)), []))
                merged.sort(key=lambda item: item.quality_score, reverse=True)
                return list(merged[:limit])
            return list(self._buffers.get((camera_id, session_id, int(track_id_or_ids)), []))[:limit]

    def remove(self, camera_id: str, track_id: int, *, session_id: str = "") -> None:
        with self._lock:
            key = (camera_id, session_id, int(track_id))
            self._buffers.pop(key, None)
            self._last_seen.pop(key, None)

    def cleanup(self) -> None:
        with self._lock:
            self._cleanup_nolock()

    def _cleanup_nolock(self) -> None:
        now = self._time_fn()
        expired = [key for key, last_seen in self._last_seen.items() if now - last_seen > self.ttl_seconds]
        for key in expired:
            self._buffers.pop(key, None)
            self._last_seen.pop(key, None)


def candidate_score(frame: np.ndarray, bbox: tuple[int, int, int, int], confidence: float) -> float:
    return candidate_metrics(frame, bbox, confidence)["quality_score"]


def candidate_metrics(frame: np.ndarray, bbox: tuple[int, int, int, int], confidence: float) -> dict[str, float | int]:
    x1, y1, x2, y2 = _clip_bbox(bbox, frame.shape[1], frame.shape[0])
    frame_height, frame_width = frame.shape[:2]
    vehicle_width = max(0, x2 - x1)
    vehicle_height = max(0, y2 - y1)
    area = vehicle_width * vehicle_height
    bbox_area_ratio = area / max(1, frame_width * frame_height)
    crop = frame[y1:y2, x1:x2]

    sharpness_raw = 0.0
    brightness = 0.0
    contrast = 0.0
    if crop.size:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        sharpness_raw = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        mean, std = cv2.meanStdDev(gray)
        brightness = float(mean[0][0]) / 255.0
        contrast = float(std[0][0]) / 128.0

    touches_border = x1 <= 2 or y1 <= 2 or x2 >= frame_width - 2 or y2 >= frame_height - 2
    border_penalty = 0.50 if touches_border else 0.0
    if brightness < 0.15 or brightness > 0.90:
        border_penalty += 0.15
    if contrast < 0.08:
        border_penalty += 0.15

    area_score = min(1.0, bbox_area_ratio / 0.18)
    sharpness_score = min(1.0, sharpness_raw / 400.0)
    contrast_score = min(1.0, contrast)
    confidence_score = min(1.0, max(0.0, confidence))
    border_score = max(0.0, 1.0 - border_penalty)
    quality_score = (
        area_score * QUALITY_WEIGHTS["area"]
        + sharpness_score * QUALITY_WEIGHTS["sharpness"]
        + contrast_score * QUALITY_WEIGHTS["contrast"]
        + confidence_score * QUALITY_WEIGHTS["confidence"]
        + border_score * QUALITY_WEIGHTS["border"]
    )
    return {
        "vehicle_width": vehicle_width,
        "vehicle_height": vehicle_height,
        "bbox_area_ratio": float(bbox_area_ratio),
        "sharpness": float(sharpness_raw),
        "brightness": float(brightness),
        "contrast": float(contrast),
        "border_penalty": float(border_penalty),
        "detection_confidence": float(confidence_score),
        "quality_score": float(max(0.0, min(1.0, quality_score))),
    }


def _clip_bbox(bbox: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    return max(0, x1), max(0, y1), min(width, x2), min(height, y2)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _timestamp_key(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
