from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.hotel.gate_detector import bottom_center
from app.hotel.models import RoomDecision


@dataclass(frozen=True)
class RoomZone:
    room_number: int
    polygon: tuple[tuple[float, float], ...]


@dataclass
class _RoomTrackState:
    counts: dict[int, int] = field(default_factory=dict)
    last_seen: float = 0.0
    last_room: int | None = None
    first_room: int | None = None
    recent_rooms: list[int | None] = field(default_factory=list)


class RoomDetector:
    def __init__(
        self,
        camera_id: str,
        zones: list[RoomZone] | tuple[RoomZone, ...],
        *,
        probable_observations: int = 2,
        confirmed_observations: int = 4,
        ttl_seconds: float = 30.0,
        time_fn=time.time,
    ) -> None:
        self.camera_id = camera_id
        self.zones = tuple(zones)
        self.probable_observations = probable_observations
        self.confirmed_observations = confirmed_observations
        self.ttl_seconds = ttl_seconds
        self._time_fn = time_fn
        self._tracks: dict[int, _RoomTrackState] = {}

    def reset(self) -> None:
        self._tracks.clear()

    def update(
        self,
        *,
        track_id: int,
        bbox: list[int] | tuple[int, int, int, int],
        frame_width: int,
        frame_height: int,
    ) -> RoomDecision:
        now = self._time_fn()
        self._cleanup(now)
        state = self._tracks.setdefault(track_id, _RoomTrackState())
        state.last_seen = now
        point = bottom_center(bbox)
        normalized = (point[0] / frame_width, point[1] / frame_height)
        for zone in self.zones:
            if point_in_polygon(normalized, zone.polygon):
                state.counts[zone.room_number] = state.counts.get(zone.room_number, 0) + 1
                if state.first_room is None:
                    state.first_room = zone.room_number
                state.last_room = zone.room_number
                state.recent_rooms.append(zone.room_number)
                state.recent_rooms = state.recent_rooms[-4:]
        return self.decision(track_id)

    def decision(self, track_id: int, movement: str = "UNKNOWN", finalized: bool = False) -> RoomDecision:
        state = self._tracks.get(track_id)
        if state is None or not state.counts:
            return RoomDecision(None, 0.0, "NO_DETERMINADA")
        if movement == "OUTWARD" and state.first_room is not None:
            room_number = state.first_room
            count = state.counts.get(room_number, 0)
        else:
            room_number, count = max(state.counts.items(), key=lambda item: item[1])
        total = max(1, sum(state.counts.values()))
        confidence = min(1.0, count / max(self.confirmed_observations, total))
        recent_same = state.recent_rooms and all(room == room_number for room in state.recent_rooms[-2:])
        if count >= self.confirmed_observations and (finalized or recent_same):
            status = "CONFIRMADA"
        elif count >= self.probable_observations:
            status = "PROBABLE"
        else:
            status = "NO_DETERMINADA"
            room_number = None
            confidence = 0.0
        return RoomDecision(room_number, confidence, status)

    def _cleanup(self, now: float) -> None:
        expired = [track_id for track_id, state in self._tracks.items() if now - state.last_seen > self.ttl_seconds]
        for track_id in expired:
            self._tracks.pop(track_id, None)


def point_in_polygon(point: tuple[float, float], polygon: tuple[tuple[float, float], ...]) -> bool:
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside
