from __future__ import annotations

import time
from dataclasses import dataclass

import cv2


@dataclass
class FrameCandidate:
    frame: object
    bbox: tuple[int, int, int, int]
    score: float
    captured_at: object
    vehicle: dict


class TrackFrameBuffer:
    def __init__(self, max_candidates: int = 6, ttl_seconds: float = 20.0, time_fn=time.time) -> None:
        self.max_candidates = max_candidates
        self.ttl_seconds = ttl_seconds
        self._time_fn = time_fn
        self._buffers: dict[tuple[str, int], list[FrameCandidate]] = {}
        self._last_seen: dict[tuple[str, int], float] = {}

    def add(self, camera_id: str, track_id: int, frame, vehicle: dict, captured_at) -> None:
        key = (camera_id, int(track_id))
        bbox = tuple(int(value) for value in vehicle["bbox"])
        score = candidate_score(frame, bbox, float(vehicle.get("confidence", 0.0)))
        candidate = FrameCandidate(frame.copy(), bbox, score, captured_at, dict(vehicle))
        values = self._buffers.setdefault(key, [])
        values.append(candidate)
        values.sort(key=lambda item: item.score, reverse=True)
        del values[self.max_candidates :]
        self._last_seen[key] = self._time_fn()
        self.cleanup()

    def best(self, camera_id: str, track_id: int, limit: int = 5) -> list[FrameCandidate]:
        return list(self._buffers.get((camera_id, int(track_id)), []))[:limit]

    def remove(self, camera_id: str, track_id: int) -> None:
        key = (camera_id, int(track_id))
        self._buffers.pop(key, None)
        self._last_seen.pop(key, None)

    def cleanup(self) -> None:
        now = self._time_fn()
        expired = [key for key, last_seen in self._last_seen.items() if now - last_seen > self.ttl_seconds]
        for key in expired:
            self._buffers.pop(key, None)
            self._last_seen.pop(key, None)


def candidate_score(frame, bbox: tuple[int, int, int, int], confidence: float) -> float:
    x1, y1, x2, y2 = bbox
    height, width = frame.shape[:2]
    area = max(0, x2 - x1) * max(0, y2 - y1)
    area_score = area / max(1, width * height)
    border_penalty = 0.25 if x1 <= 2 or y1 <= 2 or x2 >= width - 2 or y2 >= height - 2 else 0.0
    crop = frame[max(0, y1):min(height, y2), max(0, x1):min(width, x2)]
    sharpness = 0.0
    if crop.size:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        sharpness = min(1.0, cv2.Laplacian(gray, cv2.CV_64F).var() / 500.0)
    return confidence + area_score + sharpness - border_penalty
