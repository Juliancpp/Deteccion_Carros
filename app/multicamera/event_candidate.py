from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class EventCandidate:
    event_id: str
    camera_id: str
    camera_name: str
    track_id: int
    direction: str
    occurred_at: datetime
    source_sequence: int
    class_name: str
    class_id: int
    confidence: float
    bbox: tuple[int, int, int, int]
    center: tuple[int, int]
    frame_width: int
    frame_height: int
    frame: Any | None
    track_point: tuple[int, int] | None = None
    corridor_progress: float | None = None
