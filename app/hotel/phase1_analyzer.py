from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from app import config
from app.hotel.directional_corridor import DirectionalCorridor, bottom_center
from app.hotel.phase1_event import HotelTrackResult, new_result_id
from app.hotel.room_detector import RoomDetector, RoomZone
from app.settings import CameraSettings


@dataclass
class _TrackPhase1State:
    first_seen_at: datetime
    last_seen_at: datetime
    last_seen_monotonic: float
    last_bbox: tuple[int, int, int, int]
    vehicle_class: str
    vehicle_confidence: float
    last_room_status: str = "NO_DETERMINADA"
    finalized: bool = False


class HotelPhase1Analyzer:
    def __init__(self, camera: CameraSettings, *, time_fn=time.time) -> None:
        self.camera = camera
        self._time_fn = time_fn
        corridor = camera.corridor
        self.corridor = DirectionalCorridor(
            camera.camera_id,
            corridor.points,
            half_width=corridor.half_width,
            invert_direction=corridor.invert_direction,
            min_observations=config.DIRECTION_MIN_OBSERVATIONS,
            min_progress_delta=config.DIRECTION_MIN_PROGRESS_DELTA,
            history_size=config.DIRECTION_HISTORY_SIZE,
            ttl_seconds=config.DIRECTION_TRACK_TTL_SECONDS,
            enabled=corridor.enabled,
            time_fn=time_fn,
        )
        zones = [RoomZone(room.room_number, room.polygon) for room in camera.rooms]
        self.rooms = RoomDetector(
            camera.camera_id,
            zones,
            probable_observations=config.ROOM_MIN_PROBABLE_OBSERVATIONS,
            confirmed_observations=config.ROOM_MIN_CONFIRMED_OBSERVATIONS,
            ttl_seconds=config.ROOM_TRACK_TTL_SECONDS,
            time_fn=time_fn,
        )
        self._tracks: dict[int, _TrackPhase1State] = {}
        self._latest_results: dict[int, HotelTrackResult] = {}
        self.finalized_results: list[HotelTrackResult] = []

    def update(
        self,
        *,
        tracked_vehicles: tuple[dict, ...] | list[dict],
        frame,
        captured_at: datetime,
        frame_width: int,
        frame_height: int,
    ) -> list[HotelTrackResult]:
        now = self._time_fn()
        active_ids: set[int] = set()
        emitted: list[HotelTrackResult] = []
        for vehicle in tracked_vehicles:
            if vehicle.get("id") is None:
                continue
            track_id = int(vehicle["id"])
            active_ids.add(track_id)
            bbox = tuple(int(value) for value in vehicle["bbox"])
            state = self._tracks.get(track_id)
            if state is None:
                state = _TrackPhase1State(
                    first_seen_at=captured_at,
                    last_seen_at=captured_at,
                    last_seen_monotonic=now,
                    last_bbox=bbox,
                    vehicle_class=str(vehicle.get("class", "Vehiculo")),
                    vehicle_confidence=float(vehicle.get("confidence", 0.0)),
                )
                self._tracks[track_id] = state
            state.last_seen_at = captured_at
            state.last_seen_monotonic = now
            state.last_bbox = bbox
            state.vehicle_class = str(vehicle.get("class", state.vehicle_class))
            state.vehicle_confidence = float(vehicle.get("confidence", state.vehicle_confidence))
            direction = self.corridor.update(
                track_id=track_id,
                point=bottom_center(bbox),
                frame_width=frame_width,
                frame_height=frame_height,
                observed_at=captured_at,
            )
            self.rooms.update(track_id=track_id, bbox=bbox, frame_width=frame_width, frame_height=frame_height)
            room = self.rooms.decision(track_id, movement=direction.movement, finalized=False)
            reason = _reason(room.room_status, direction.movement, finalized=False)
            copy_frame = room.room_status != state.last_room_status and room.room_status in {"PROBABLE", "CONFIRMADA"}
            state.last_room_status = room.room_status
            result = self._build_result(track_id, state, direction, room, frame.copy() if copy_frame else None, reason, False)
            self._latest_results[track_id] = result
            emitted.append(result)
        for track_id, state in list(self._tracks.items()):
            if track_id in active_ids or state.finalized:
                continue
            if now - state.last_seen_monotonic < config.ROOM_TRACK_MISSING_GRACE_SECONDS:
                continue
            direction = self.corridor.result(track_id, observed_at=state.last_seen_at)
            room = self.rooms.decision(track_id, movement=direction.movement, finalized=True)
            reason = _reason(room.room_status, direction.movement, finalized=True)
            result = self._build_result(track_id, state, direction, room, frame.copy(), reason, True)
            state.finalized = True
            self._latest_results[track_id] = result
            self.finalized_results.append(result)
            emitted.append(result)
        return emitted

    def latest_results(self) -> list[HotelTrackResult]:
        return list(self._latest_results.values())

    def reset(self) -> None:
        self.corridor.reset()
        self.rooms.reset()
        self._tracks.clear()
        self._latest_results.clear()
        self.finalized_results.clear()

    def _build_result(self, track_id, state, direction, room, evidence_frame, reason, finalized) -> HotelTrackResult:
        return HotelTrackResult(
            result_id=new_result_id(),
            camera_id=self.camera.camera_id,
            camera_name=self.camera.name,
            track_id=track_id,
            first_seen_at=state.first_seen_at,
            last_seen_at=state.last_seen_at,
            movement=direction.movement,
            movement_confidence=direction.confidence,
            room_number=room.room_number,
            room_confidence=room.room_confidence,
            room_status=room.room_status,
            vehicle_class=state.vehicle_class,
            vehicle_confidence=state.vehicle_confidence,
            last_bbox=state.last_bbox,
            evidence_frame=evidence_frame,
            reason=reason,
            finalized=finalized,
        )


def _reason(room_status: str, movement: str, *, finalized: bool) -> str:
    if room_status == "CONFIRMADA" and finalized:
        return "TRACK_DISAPPEARED_IN_ROOM"
    if room_status in {"PROBABLE", "CONFIRMADA"} and movement == "INWARD":
        return "ENTERED_ROOM_ZONE"
    if room_status in {"PROBABLE", "CONFIRMADA"} and movement == "OUTWARD":
        return "TRACK_STARTED_IN_ROOM"
    if finalized:
        return "NO_ROOM_MATCH"
    return "TRACK_ACTIVE"
