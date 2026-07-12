from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from collections import deque

from app.video.rtsp_stream import RTSPStream


@dataclass
class CameraContext:
    camera_id: str
    camera_name: str
    target_fps: float
    stream: RTSPStream
    tracker: Any
    line_detector: Any | None = None
    last_consumed_sequence: int = 0
    last_inference_at: float = 0.0
    last_result: Any | None = None
    last_stream_connected: bool = False
    last_reconnect_count: int = 0
    inference_count: int = 0
    inference_ms_last: float = 0.0
    inference_ms_total: float = 0.0
    processed_sequence: int = 0
    source_sequence: int = 0
    skipped_frames: int = 0
    detections_count: int = 0
    active_track_ids: list[int] = field(default_factory=list)
    inference_times: deque[float] = field(default_factory=deque)
    tracker_backend: str = "unknown"
    crossing_candidates: int = 0
    events_published: int = 0
    duplicate_events_suppressed: int = 0
    event_queue_dropped: int = 0
    last_event_direction: str | None = None
    last_event_track_id: int | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class CameraInferenceResult:
    camera_id: str
    camera_name: str
    source_sequence: int
    captured_at: datetime
    processed_at: datetime
    inference_ms: float
    detections_count: int
    tracked_vehicles: tuple[dict[str, Any], ...]
    annotated_frame: Any | None = None


@dataclass(frozen=True)
class CameraCoordinatorStatus:
    camera_id: str
    camera_name: str
    stream_connected: bool
    capture_fps: float
    target_inference_fps: float
    actual_inference_fps: float
    inference_count: int
    last_inference_at: float
    inference_ms_last: float
    inference_ms_average: float
    source_sequence: int
    processed_sequence: int
    dropped_or_skipped_frames: int
    detections_count: int
    active_track_ids: tuple[int, ...]
    tracker_backend: str
    crossing_candidates: int
    events_published: int
    duplicate_events_suppressed: int
    event_queue_dropped: int
    last_event_direction: str | None
    last_event_track_id: int | None
    last_error: str | None


@dataclass(frozen=True)
class MultiCameraStatus:
    model_load_count: int
    total_inference_count: int
    scheduler_running: bool
    scheduler_loop_rate: float
    pending_events: int
    event_queue_dropped: int
    cameras: tuple[CameraCoordinatorStatus, ...]
