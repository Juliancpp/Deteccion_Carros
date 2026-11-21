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
    
    # Nuevos campos requeridos para inmutabilidad y consistencia
    captured_at: datetime = None
    vehicle_class: str = ""
    detection_confidence: float = 0.0
    logical_id: int | None = None
    geometry_gate: bool = False
    session_id: str = ""


    def __post_init__(self):
        if self.captured_at is None:
            object.__setattr__(self, "captured_at", self.occurred_at)
        if not self.vehicle_class:
            object.__setattr__(self, "vehicle_class", self.class_name)
        if not self.detection_confidence:
            object.__setattr__(self, "detection_confidence", self.confidence)
        if self.frame is not None and hasattr(self.frame, "copy"):
            object.__setattr__(self, "frame", self.frame.copy())

