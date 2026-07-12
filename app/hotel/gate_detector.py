from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from app.hotel.models import GateEvent


@dataclass
class _GateTrackState:
    sides: list[int] = field(default_factory=list)
    points: list[tuple[int, int]] = field(default_factory=list)
    last_seen: float = 0.0
    emitted_at: float | None = None


class DirectionalGateDetector:
    """Puerta virtual corta con límites de segmento y confirmación anti-jitter."""

    def __init__(
        self,
        camera_id: str,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        invert_direction: bool = False,
        min_observations: int = 3,
        min_delta: float = 0.06,
        duplicate_cooldown_seconds: float = 10.0,
        ttl_seconds: float = 20.0,
        time_fn=time.time,
    ) -> None:
        self.camera_id = camera_id
        self.start = start
        self.end = end
        self.invert_direction = invert_direction
        self.min_observations = min_observations
        self.min_delta = min_delta
        self.duplicate_cooldown_seconds = duplicate_cooldown_seconds
        self.ttl_seconds = ttl_seconds
        self._time_fn = time_fn
        self._tracks: dict[int, _GateTrackState] = {}

    def reset(self) -> None:
        self._tracks.clear()

    def update(
        self,
        *,
        track_id: int,
        bbox: list[int] | tuple[int, int, int, int],
        frame_width: int,
        frame_height: int,
        captured_at: datetime,
        source_sequence: int,
    ) -> GateEvent | None:
        now = self._time_fn()
        self._cleanup(now)
        point = bottom_center(bbox)
        side_value = self._signed_side(point, frame_width, frame_height)
        side = 1 if side_value > 0 else -1 if side_value < 0 else 0
        if side == 0:
            return None
        if not self._within_gate_span(point, frame_width, frame_height):
            return None
        state = self._tracks.setdefault(track_id, _GateTrackState())
        state.last_seen = now
        state.sides.append(side)
        state.points.append(point)
        state.sides = state.sides[-6:]
        state.points = state.points[-6:]
        if state.emitted_at is not None and now - state.emitted_at < self.duplicate_cooldown_seconds:
            return None
        if len(state.sides) < self.min_observations:
            return None
        first_side = state.sides[0]
        last_side = state.sides[-1]
        if first_side == last_side:
            return None
        movement = "inward" if first_side < last_side else "outward"
        if self.invert_direction:
            movement = "outward" if movement == "inward" else "inward"
        if self._movement_magnitude(state.points, frame_width, frame_height) < self.min_delta:
            return None
        state.emitted_at = now
        return GateEvent(
            camera_id=self.camera_id,
            track_id=track_id,
            movement=movement,
            occurred_at=captured_at,
            source_sequence=source_sequence,
            track_point=point,
        )

    def _signed_side(self, point: tuple[int, int], frame_width: int, frame_height: int) -> float:
        ax, ay = self.start[0] * frame_width, self.start[1] * frame_height
        bx, by = self.end[0] * frame_width, self.end[1] * frame_height
        px, py = point
        return (bx - ax) * (py - ay) - (by - ay) * (px - ax)

    def _within_gate_span(self, point: tuple[int, int], frame_width: int, frame_height: int) -> bool:
        ax, ay = self.start[0] * frame_width, self.start[1] * frame_height
        bx, by = self.end[0] * frame_width, self.end[1] * frame_height
        px, py = point
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq == 0:
            return False
        t = ((px - ax) * dx + (py - ay) * dy) / length_sq
        return -0.25 <= t <= 1.25

    @staticmethod
    def _movement_magnitude(points: list[tuple[int, int]], frame_width: int, frame_height: int) -> float:
        if len(points) < 2:
            return 0.0
        first = points[0]
        last = points[-1]
        dx = (last[0] - first[0]) / frame_width
        dy = (last[1] - first[1]) / frame_height
        return (dx * dx + dy * dy) ** 0.5

    def _cleanup(self, now: float) -> None:
        expired = [track_id for track_id, state in self._tracks.items() if now - state.last_seen > self.ttl_seconds]
        for track_id in expired:
            self._tracks.pop(track_id, None)


def bottom_center(bbox: list[int] | tuple[int, int, int, int]) -> tuple[int, int]:
    x1, _y1, x2, y2 = [int(value) for value in bbox]
    return (round((x1 + x2) / 2), y2)
