from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class HotelTrackResult:
    result_id: str
    camera_id: str
    camera_name: str
    track_id: int
    first_seen_at: datetime
    last_seen_at: datetime
    movement: str
    movement_confidence: float
    room_number: int | None
    room_confidence: float
    room_status: str
    vehicle_class: str
    vehicle_confidence: float
    last_bbox: tuple[int, int, int, int]
    evidence_frame: Any | None
    reason: str
    finalized: bool
    logical_id: int | None = None
    original_track_ids: tuple[int, ...] = ()
    room_score_breakdown: dict[str, float] | None = None
    room_runner_up: int | None = None
    room_vote_margin: float = 0.0
    passed_in_front_only: bool = False
    review_reasons: tuple[str, ...] = ()



def new_result_id() -> str:
    return str(uuid.uuid4())
