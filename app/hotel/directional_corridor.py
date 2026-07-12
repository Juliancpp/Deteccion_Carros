from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class CorridorObservation:
    inside: bool
    progress: float
    lateral_distance: float


@dataclass(frozen=True)
class DirectionResult:
    camera_id: str
    track_id: int
    movement: str
    initial_progress: float
    current_progress: float
    progress_delta: float
    observations: int
    confidence: float
    observed_at: datetime


@dataclass
class _TrackDirectionState:
    first_seen_at: datetime
    last_seen_at: datetime
    progresses: list[float] = field(default_factory=list)
    inside_count: int = 0
    last_update_monotonic: float = 0.0


class DirectionalCorridor:
    """Corredor direccional normalizado: progreso creciente=INWARD."""

    def __init__(
        self,
        camera_id: str,
        points: tuple[tuple[float, float], ...],
        *,
        half_width: float,
        invert_direction: bool = False,
        min_observations: int = 3,
        min_progress_delta: float = 0.06,
        history_size: int = 8,
        ttl_seconds: float = 8.0,
        enabled: bool = True,
        time_fn=time.time,
    ) -> None:
        if len(points) < 2:
            raise ValueError("DirectionalCorridor requiere al menos dos puntos")
        for x, y in points:
            if x < 0.0 or x > 1.0 or y < 0.0 or y > 1.0:
                raise ValueError("Los puntos del corredor deben estar entre 0.0 y 1.0")
        if half_width <= 0.0 or half_width > 1.0:
            raise ValueError("half_width debe estar entre 0.0 y 1.0")
        self.camera_id = camera_id
        self.points = tuple(points)
        self.half_width = half_width
        self.invert_direction = invert_direction
        self.min_observations = min_observations
        self.min_progress_delta = min_progress_delta
        self.history_size = history_size
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled
        self._time_fn = time_fn
        self._states: dict[int, _TrackDirectionState] = {}

    def reset(self) -> None:
        self._states.clear()

    def update(
        self,
        *,
        track_id: int,
        point: tuple[int, int],
        frame_width: int,
        frame_height: int,
        observed_at: datetime | None = None,
    ) -> DirectionResult:
        observed_at = observed_at or datetime.now(timezone.utc)
        now = self._time_fn()
        self.cleanup(now)
        observation = self.observe(point, frame_width, frame_height)
        state = self._states.get(track_id)
        if state is None:
            state = _TrackDirectionState(observed_at, observed_at, last_update_monotonic=now)
            self._states[track_id] = state
        state.last_seen_at = observed_at
        state.last_update_monotonic = now
        if observation.inside and self.enabled:
            state.progresses.append(observation.progress)
            state.progresses = state.progresses[-self.history_size :]
            state.inside_count += 1
        return self.result(track_id, observed_at=observed_at)

    def result(self, track_id: int, *, observed_at: datetime | None = None) -> DirectionResult:
        observed_at = observed_at or datetime.now(timezone.utc)
        state = self._states.get(track_id)
        if state is None or not state.progresses:
            return DirectionResult(self.camera_id, track_id, "UNKNOWN", 0.0, 0.0, 0.0, 0, 0.0, observed_at)
        initial = state.progresses[0]
        current = state.progresses[-1]
        delta = current - initial
        observations = len(state.progresses)
        movement = "UNKNOWN"
        if observations >= self.min_observations and abs(delta) >= self.min_progress_delta:
            movement = "INWARD" if delta > 0 else "OUTWARD"
            if self.invert_direction:
                movement = "OUTWARD" if movement == "INWARD" else "INWARD"
        confidence = self._confidence(state.progresses, movement)
        return DirectionResult(
            camera_id=self.camera_id,
            track_id=track_id,
            movement=movement,
            initial_progress=initial,
            current_progress=current,
            progress_delta=delta,
            observations=observations,
            confidence=confidence,
            observed_at=observed_at,
        )

    def observe(self, point: tuple[int, int], frame_width: int, frame_height: int) -> CorridorObservation:
        normalized = (point[0] / frame_width, point[1] / frame_height)
        progress, lateral_distance = self.project_normalized_point(normalized)
        return CorridorObservation(
            inside=lateral_distance <= self.half_width,
            progress=progress,
            lateral_distance=lateral_distance,
        )

    def project_normalized_point(self, point: tuple[float, float]) -> tuple[float, float]:
        lengths = [math.dist(start, end) for start, end in zip(self.points, self.points[1:])]
        total_length = sum(lengths)
        cumulative = 0.0
        best_distance = float("inf")
        best_progress_len = 0.0
        for index, (start, end) in enumerate(zip(self.points, self.points[1:])):
            _projection, distance, t = project_to_segment(point, start, end)
            if distance < best_distance:
                best_distance = distance
                best_progress_len = cumulative + lengths[index] * t
            cumulative += lengths[index]
        progress = best_progress_len / total_length if total_length > 0 else 0.0
        return max(0.0, min(1.0, progress)), best_distance

    def points_for_frame(self, frame_width: int, frame_height: int) -> tuple[tuple[int, int], ...]:
        return tuple((round(x * frame_width), round(y * frame_height)) for x, y in self.points)

    def cleanup(self, now: float | None = None) -> list[int]:
        now = self._time_fn() if now is None else now
        expired = [
            track_id
            for track_id, state in self._states.items()
            if now - state.last_update_monotonic > self.ttl_seconds
        ]
        for track_id in expired:
            self._states.pop(track_id, None)
        return expired

    def _confidence(self, progresses: list[float], movement: str) -> float:
        if movement == "UNKNOWN" or len(progresses) < self.min_observations:
            return 0.0
        deltas = [b - a for a, b in zip(progresses, progresses[1:])]
        sign = 1 if progresses[-1] - progresses[0] > 0 else -1
        consistent = sum(1 for delta in deltas if delta == 0 or delta * sign > 0)
        consistency = consistent / max(1, len(deltas))
        magnitude = min(1.0, abs(progresses[-1] - progresses[0]) / max(self.min_progress_delta, 1e-6))
        observation_score = min(1.0, len(progresses) / max(self.min_observations, 1))
        return round((0.45 * consistency) + (0.35 * magnitude) + (0.20 * observation_score), 3)


def project_to_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[tuple[float, float], float, float]:
    px, py = point
    ax, ay = start
    bx, by = end
    dx = bx - ax
    dy = by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return start, math.dist(point, start), 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    projection = (ax + dx * t, ay + dy * t)
    return projection, math.dist(point, projection), t


def bottom_center(bbox: list[int] | tuple[int, int, int, int]) -> tuple[int, int]:
    x1, _y1, x2, y2 = [int(value) for value in bbox]
    return (round((x1 + x2) / 2), y2)
