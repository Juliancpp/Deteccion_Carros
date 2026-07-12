from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class GateEvent:
    camera_id: str
    track_id: int
    movement: str
    occurred_at: datetime
    source_sequence: int
    track_point: tuple[int, int]


@dataclass(frozen=True)
class RoomDecision:
    room_number: int | None
    room_confidence: float
    room_status: str


@dataclass(frozen=True)
class PlateDecision:
    plate: str | None
    confidence: float
    status: str
    candidates: tuple[dict[str, Any], ...] = ()


@dataclass
class HotelVehicleEvent:
    event_id: str
    occurred_at: datetime
    camera_id: str
    camera_name: str
    track_id: int
    movement: str
    event_type: str
    plate: str | None
    plate_confidence: float
    plate_status: str
    room_number: int | None
    room_confidence: float
    room_status: str
    vehicle_class: str
    vehicle_confidence: float
    evidence_path: str | None
    review_required: bool
    notes: str = ""
    plate_candidates: list[dict[str, Any]] = field(default_factory=list)
