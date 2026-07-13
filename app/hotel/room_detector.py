from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from app.hotel.gate_detector import bottom_center
from app.hotel.models import RoomDecision


ROOM_SCORE_WEIGHTS = {
    "evidence_count": 0.15,
    "depth": 0.25,
    "direction": 0.20,
    "final_position": 0.15,
    "dwell": 0.10,
    "disappearance": 0.15,
}


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
    points: list[tuple[float, float]] = field(default_factory=list)
    room_per_point: list[int | None] = field(default_factory=list)
    depth_by_room: dict[int, list[float]] = field(default_factory=dict)
    entered_room_at_index: dict[int, int] = field(default_factory=dict)
    exited_room: set[int] = field(default_factory=set)


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
        normalized = (point[0] / max(1, frame_width), point[1] / max(1, frame_height))
        state.points.append(normalized)

        current_room = None
        current_index = len(state.points) - 1
        for zone in self.zones:
            if point_in_polygon(normalized, zone.polygon):
                current_room = zone.room_number
                state.counts[zone.room_number] = state.counts.get(zone.room_number, 0) + 1
                state.depth_by_room.setdefault(zone.room_number, []).append(room_depth(normalized, zone.polygon))
                state.entered_room_at_index.setdefault(zone.room_number, current_index)
                if state.first_room is None:
                    state.first_room = zone.room_number
                state.last_room = zone.room_number
                break

        previous_room = state.room_per_point[-1] if state.room_per_point else None
        if previous_room is not None and current_room != previous_room:
            state.exited_room.add(previous_room)
        state.room_per_point.append(current_room)
        state.recent_rooms.append(current_room)
        state.recent_rooms = state.recent_rooms[-5:]
        return self.decision(track_id)

    def decision(self, track_id: int, movement: str = "UNKNOWN", finalized: bool = False) -> RoomDecision:
        state = self._tracks.get(track_id)
        if state is None or not state.counts:
            return _no_room("no_room_observations")

        scored = []
        for zone in self.zones:
            if zone.room_number not in state.counts:
                continue
            scored.append(self._score_room(state, zone.room_number, movement, finalized))
        if not scored:
            return _no_room("no_room_candidates")

        scored.sort(key=lambda item: item["score"], reverse=True)
        best = scored[0]
        runner = scored[1] if len(scored) > 1 else None
        runner_score = float(runner["score"]) if runner else 0.0
        margin = float(best["score"] - runner_score)
        room_number = int(best["room_number"])
        count = int(best["count"])
        passed_in_front = bool(best["passed_in_front_only"])
        review_reasons: list[str] = []

        if movement == "UNKNOWN":
            review_reasons.append("movement_unknown")
            if count >= self.probable_observations and not passed_in_front:
                return _decision(room_number, best, "PROBABLE", margin, runner, True, review_reasons)
            return _no_room("movement_unknown", breakdown=best["breakdown"], runner=runner, margin=margin, passed=passed_in_front)

        if runner is not None and margin < 0.12:
            review_reasons.append("ambiguous_room_candidates")

        if movement == "OUTWARD" and not best["started_in_room"]:
            return _no_room("outward_started_in_corridor", breakdown=best["breakdown"], runner=runner, margin=margin, passed=passed_in_front)

        if passed_in_front:
            review_reasons.append("passed_in_front_only")
            if best["score"] >= 0.55 and count >= self.probable_observations:
                return _decision(room_number, best, "PROBABLE", margin, runner, True, review_reasons)
            return _no_room("passed_in_front_only", breakdown=best["breakdown"], runner=runner, margin=margin, passed=True)

        can_confirm = (
            count >= self.confirmed_observations
            and best["depth_score"] >= 0.45
            and best["direction_score"] >= 0.70
            and margin >= 0.12
            and (best["final_position_score"] >= 0.70 or best["disappearance_score"] >= 0.70)
        )
        if movement == "OUTWARD":
            can_confirm = can_confirm and best["exited_room"]

        if can_confirm:
            return _decision(room_number, best, "CONFIRMADA", margin, runner, False, review_reasons)

        if count >= self.probable_observations and best["score"] >= 0.40 and margin >= 0.02:
            return _decision(room_number, best, "PROBABLE", margin, runner, True, review_reasons)

        return _no_room("insufficient_room_evidence", breakdown=best["breakdown"], runner=runner, margin=margin, passed=passed_in_front)

    def _score_room(self, state: _RoomTrackState, room_number: int, movement: str, finalized: bool) -> dict:
        count = state.counts.get(room_number, 0)
        total = max(1, len(state.points))
        depths = state.depth_by_room.get(room_number, [])
        max_depth = max(depths) if depths else 0.0
        recent_inside = any(room == room_number for room in state.room_per_point[-3:])
        final_inside = state.room_per_point[-1] == room_number if state.room_per_point else False
        initial_window = state.room_per_point[: min(2, len(state.room_per_point))]
        started_in_room = bool(initial_window) and room_number in initial_window
        exited_room = room_number in state.exited_room
        dwell_score = min(1.0, count / max(1, total))
        evidence_count_score = min(1.0, count / max(1, self.confirmed_observations))
        depth_score = min(1.0, max_depth)

        if movement == "INWARD":
            direction_score = 1.0 if not started_in_room else 0.55
            final_position_score = 1.0 if recent_inside else 0.0
            disappearance_score = 1.0 if finalized and recent_inside else 0.0
        elif movement == "OUTWARD":
            direction_score = 1.0 if started_in_room and exited_room else 0.35
            final_position_score = 1.0 if started_in_room and not final_inside else 0.0
            disappearance_score = 1.0 if finalized and not final_inside and started_in_room else 0.0
        else:
            direction_score = 0.0
            final_position_score = 0.5 if recent_inside else 0.0
            disappearance_score = 0.0

        first_index = state.entered_room_at_index.get(room_number, total)
        low_dwell = count <= max(1, self.probable_observations)
        passed_in_front = (
            count >= self.probable_observations
            and max_depth < 0.35
            and not recent_inside
            and not (finalized and final_inside)
        )
        if movement == "INWARD" and first_index < max(1, total - 4) and not recent_inside and max_depth < 0.45:
            passed_in_front = True
        if movement == "INWARD" and not final_inside and max_depth < 0.45:
            passed_in_front = True
        if movement == "OUTWARD" and not started_in_room:
            passed_in_front = True
        if low_dwell and max_depth < 0.30 and not finalized:
            passed_in_front = True

        ambiguity_penalty = 0.0
        corridor_overlap_penalty = 0.0
        score = (
            evidence_count_score * ROOM_SCORE_WEIGHTS["evidence_count"]
            + depth_score * ROOM_SCORE_WEIGHTS["depth"]
            + direction_score * ROOM_SCORE_WEIGHTS["direction"]
            + final_position_score * ROOM_SCORE_WEIGHTS["final_position"]
            + dwell_score * ROOM_SCORE_WEIGHTS["dwell"]
            + disappearance_score * ROOM_SCORE_WEIGHTS["disappearance"]
            - ambiguity_penalty
            - corridor_overlap_penalty
        )
        breakdown = {
            "evidence_count_score": evidence_count_score,
            "depth_score": depth_score,
            "direction_score": direction_score,
            "final_position_score": final_position_score,
            "dwell_score": dwell_score,
            "disappearance_score": disappearance_score,
            "ambiguity_penalty": ambiguity_penalty,
            "corridor_overlap_penalty": corridor_overlap_penalty,
            "room_score": max(0.0, min(1.0, score)),
        }
        return {
            "room_number": room_number,
            "count": count,
            "score": breakdown["room_score"],
            "breakdown": breakdown,
            "depth_score": depth_score,
            "direction_score": direction_score,
            "final_position_score": final_position_score,
            "disappearance_score": disappearance_score,
            "passed_in_front_only": passed_in_front,
            "started_in_room": started_in_room,
            "exited_room": exited_room,
        }

    def _cleanup(self, now: float) -> None:
        expired = [track_id for track_id, state in self._tracks.items() if now - state.last_seen > self.ttl_seconds]
        for track_id in expired:
            self._tracks.pop(track_id, None)


def room_depth(point: tuple[float, float], polygon: tuple[tuple[float, float], ...]) -> float:
    """
    Aproximacion conservadora: entrada = vertice del poligono mas cercano al
    centroide del primer borde visible; interior = centroide. Sin orientacion
    manual, la distancia al centroide se usa como profundidad relativa.
    """
    centroid = polygon_centroid(polygon)
    max_distance = max((_distance(vertex, centroid) for vertex in polygon), default=1e-6)
    distance_to_centroid = _distance(point, centroid)
    return max(0.0, min(1.0, 1.0 - distance_to_centroid / max(max_distance, 1e-6)))


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


def polygon_centroid(polygon: tuple[tuple[float, float], ...]) -> tuple[float, float]:
    if not polygon:
        return (0.0, 0.0)
    return (
        sum(point[0] for point in polygon) / len(polygon),
        sum(point[1] for point in polygon) / len(polygon),
    )


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _decision(room_number: int, scored: dict, status: str, margin: float, runner: dict | None, review_required: bool, reasons: list[str]) -> RoomDecision:
    return RoomDecision(
        room_number=room_number,
        room_confidence=float(scored["score"]),
        room_status=status,
        room_score_breakdown=dict(scored["breakdown"]),
        room_runner_up=int(runner["room_number"]) if runner else None,
        room_vote_margin=margin,
        passed_in_front_only=bool(scored["passed_in_front_only"]),
        review_required=review_required,
        review_reasons=tuple(reasons),
    )


def _no_room(
    reason: str,
    *,
    breakdown: dict[str, float] | None = None,
    runner: dict | None = None,
    margin: float = 0.0,
    passed: bool = False,
) -> RoomDecision:
    return RoomDecision(
        None,
        0.0,
        "NO_DETERMINADA",
        room_score_breakdown=breakdown or {},
        room_runner_up=int(runner["room_number"]) if runner else None,
        room_vote_margin=margin,
        passed_in_front_only=passed,
        review_required=True,
        review_reasons=(reason,),
    )
