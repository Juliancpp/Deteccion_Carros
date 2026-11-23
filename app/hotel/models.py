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
    room_score_breakdown: dict[str, float] = field(default_factory=dict)
    room_runner_up: int | None = None
    room_vote_margin: float = 0.0
    passed_in_front_only: bool = False
    review_required: bool = False
    review_reasons: tuple[str, ...] = ()


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
    movement_confidence: float
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
    logical_vehicle_id: int | None = None
    original_track_ids_json: str | None = None
    plate_raw_candidates_json: str | None = None
    plate_runner_up: str | None = None
    plate_runner_up_confidence: float = 0.0
    plate_vote_margin: float = 0.0
    plate_supporting_frames_json: str | None = None
    room_score_breakdown_json: str | None = None
    room_runner_up: int | None = None
    room_vote_margin: float = 0.0
    review_reasons_json: str | None = None
    source: str = "mvp_hotel"
    ocr_diagnostics_json: str | None = None
