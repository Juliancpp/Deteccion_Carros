from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable


@dataclass(frozen=True)
class PassageObservation:
    track_id: int
    inside_corridor: bool
    progress: float
    lateral_distance: float
    progress_delta: float
    direction: str | None
    confirmed: bool
    track_point: tuple[int, int]
    observed_at: datetime


@dataclass(frozen=True)
class PassageConfirmation:
    track_id: int
    direction: str
    track_point: tuple[int, int]
    progress: float
    lateral_distance: float
    confirmed_at: datetime


@dataclass
class _TrackState:
    first_seen_at: float
    last_seen_at: float
    progress_values: list[float] = field(default_factory=list)
    points: list[tuple[int, int]] = field(default_factory=list)
    observations: int = 0
    event_emitted: bool = False
    last_event_at: float | None = None


class PassageCorridorDetector:
    """Detecta pasos direccionales dentro de un corredor definido por polilínea."""

    def __init__(
        self,
        *,
        points: Iterable[tuple[float, float]],
        half_width: float,
        event_direction: str,
        expected_direction: str = "forward",
        trigger_progress: float = 0.15,
        min_observations: int = 3,
        min_progress_delta: float = 0.08,
        max_lateral_distance: float | None = None,
        track_point: str = "bottom_center",
        track_ttl_seconds: float = 10.0,
        duplicate_cooldown_seconds: float = 10.0,
        enabled: bool = True,
        time_fn=time.time,
    ) -> None:
        self.points = tuple((float(x), float(y)) for x, y in points)
        if len(self.points) < 2:
            raise ValueError("El corredor requiere al menos dos puntos")
        for x, y in self.points:
            if x < 0.0 or x > 1.0 or y < 0.0 or y > 1.0:
                raise ValueError("Las coordenadas del corredor deben estar entre 0.0 y 1.0")
        if half_width <= 0.0 or half_width > 1.0:
            raise ValueError("half_width debe estar entre 0.0 y 1.0")
        if event_direction not in {"ENTRADA", "SALIDA"}:
            raise ValueError("event_direction debe ser ENTRADA o SALIDA")
        if expected_direction not in {"forward", "backward"}:
            raise ValueError("expected_direction debe ser forward o backward")
        if trigger_progress < 0.0 or trigger_progress > 1.0:
            raise ValueError("trigger_progress debe estar entre 0.0 y 1.0")
        self.half_width = half_width
        self.event_direction = event_direction
        self.expected_direction = expected_direction
        self.trigger_progress = trigger_progress
        self.min_observations = min_observations
        self.min_progress_delta = min_progress_delta
        self.max_lateral_distance = max_lateral_distance if max_lateral_distance is not None else half_width
        self.track_point = track_point
        self.track_ttl_seconds = track_ttl_seconds
        self.duplicate_cooldown_seconds = duplicate_cooldown_seconds
        self.enabled = enabled
        self._time_fn = time_fn
        self._states: dict[int, _TrackState] = {}
        self.confirmed_count = 0
        self.duplicate_suppressed_count = 0

    def reset(self) -> None:
        self._states.clear()
        self.confirmed_count = 0
        self.duplicate_suppressed_count = 0

    def update(
        self,
        track_id: int,
        bbox: tuple[int, int, int, int] | list[int],
        frame_width: int,
        frame_height: int,
        captured_at: datetime | None = None,
    ) -> tuple[PassageObservation, PassageConfirmation | None]:
        observed_at = captured_at or datetime.now(timezone.utc)
        track_id = int(track_id)
        point = self.track_point_from_bbox(bbox)
        progress, lateral_distance = self.project_point(point, frame_width, frame_height)
        inside = self.enabled and lateral_distance <= self.max_lateral_distance
        now = self._time_fn()
        self._cleanup(now)

        state = self._states.get(track_id)
        if state is None:
            state = _TrackState(first_seen_at=now, last_seen_at=now)
            self._states[track_id] = state
        state.last_seen_at = now

        direction = None
        confirmation = None
        progress_delta = 0.0
        if inside:
            state.observations += 1
            state.progress_values.append(progress)
            state.points.append(point)
            if len(state.progress_values) > 8:
                state.progress_values = state.progress_values[-8:]
                state.points = state.points[-8:]
            progress_delta = self._directional_delta(state.progress_values)
            direction = self._dominant_direction(progress_delta)
            if self._should_confirm(state, progress, progress_delta, direction, now):
                state.event_emitted = True
                state.last_event_at = now
                self.confirmed_count += 1
                confirmation = PassageConfirmation(
                    track_id=track_id,
                    direction=self.event_direction,
                    track_point=point,
                    progress=progress,
                    lateral_distance=lateral_distance,
                    confirmed_at=observed_at,
                )
        observation = PassageObservation(
            track_id=track_id,
            inside_corridor=inside,
            progress=progress,
            lateral_distance=lateral_distance,
            progress_delta=progress_delta,
            direction=direction,
            confirmed=confirmation is not None,
            track_point=point,
            observed_at=observed_at,
        )
        return observation, confirmation

    def track_point_from_bbox(self, bbox: tuple[int, int, int, int] | list[int]) -> tuple[int, int]:
        x1, y1, x2, y2 = [int(value) for value in bbox]
        if self.track_point == "center":
            return (round((x1 + x2) / 2), round((y1 + y2) / 2))
        return (round((x1 + x2) / 2), y2)

    def project_point(self, point: tuple[int, int], frame_width: int, frame_height: int) -> tuple[float, float]:
        normalized = (point[0] / frame_width, point[1] / frame_height)
        return self.project_normalized_point(normalized)

    def project_normalized_point(self, point: tuple[float, float]) -> tuple[float, float]:
        lengths = self._segment_lengths()
        total_length = sum(lengths)
        best_distance = float("inf")
        best_progress_length = 0.0
        cumulative = 0.0
        for index, (start, end) in enumerate(zip(self.points, self.points[1:])):
            projection, distance, t = _project_to_segment(point, start, end)
            if distance < best_distance:
                best_distance = distance
                best_progress_length = cumulative + lengths[index] * t
            cumulative += lengths[index]
        progress = best_progress_length / total_length if total_length > 0 else 0.0
        return max(0.0, min(1.0, progress)), best_distance

    def points_for_frame(self, frame_width: int, frame_height: int) -> tuple[tuple[int, int], ...]:
        return tuple((round(x * frame_width), round(y * frame_height)) for x, y in self.points)

    def trigger_point_for_frame(self, frame_width: int, frame_height: int) -> tuple[int, int]:
        point = self.point_at_progress(self.trigger_progress)
        return (round(point[0] * frame_width), round(point[1] * frame_height))

    def point_at_progress(self, progress: float) -> tuple[float, float]:
        lengths = self._segment_lengths()
        total_length = sum(lengths)
        target = max(0.0, min(1.0, progress)) * total_length
        cumulative = 0.0
        for length, start, end in zip(lengths, self.points, self.points[1:]):
            if cumulative + length >= target:
                t = 0.0 if length == 0 else (target - cumulative) / length
                return (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
            cumulative += length
        return self.points[-1]

    def _should_confirm(
        self,
        state: _TrackState,
        progress: float,
        progress_delta: float,
        direction: str | None,
        now: float,
    ) -> bool:
        if state.event_emitted:
            return False
        if state.last_event_at is not None and now - state.last_event_at < self.duplicate_cooldown_seconds:
            self.duplicate_suppressed_count += 1
            return False
        if state.observations < self.min_observations:
            return False
        if abs(progress_delta) < self.min_progress_delta:
            return False
        if direction != self.expected_direction:
            return False
        if self.expected_direction == "forward" and progress < self.trigger_progress:
            return False
        if self.expected_direction == "backward" and progress > self.trigger_progress:
            return False
        return True

    def _dominant_direction(self, progress_delta: float) -> str | None:
        if abs(progress_delta) < self.min_progress_delta:
            return None
        return "forward" if progress_delta > 0 else "backward"

    def _directional_delta(self, values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        return values[-1] - values[0]

    def _cleanup(self, now: float) -> None:
        expired = [
            track_id
            for track_id, state in self._states.items()
            if now - state.last_seen_at > self.track_ttl_seconds
        ]
        for track_id in expired:
            self._states.pop(track_id, None)

    def _segment_lengths(self) -> list[float]:
        return [
            math.dist(start, end)
            for start, end in zip(self.points, self.points[1:])
        ]


def _project_to_segment(
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
        projection = start
        return projection, math.dist(point, projection), 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    projection = (ax + dx * t, ay + dy * t)
    return projection, math.dist(point, projection), t
