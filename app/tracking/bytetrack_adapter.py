"""
Adaptador estable para ByteTrack por camara.

Produccion usa `ultralytics.trackers.byte_tracker.BYTETracker`. El fallback IoU
solo se activa explicitamente para pruebas; nunca se usa silenciosamente.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from types import SimpleNamespace
from typing import Any

import numpy as np


class ByteTrackUnavailableError(RuntimeError):
    """ByteTrack real no esta disponible o no es compatible."""


class DetectionResults:
    """Objeto minimo compatible con BYTETracker.update() de Ultralytics 8.4.x."""

    def __init__(self, detections: list[dict[str, Any]], indices: np.ndarray | None = None):
        self._detections = detections
        self._indices = np.arange(len(detections)) if indices is None else indices
        self.xyxy = np.asarray([detections[i]["bbox"] for i in self._indices], dtype=np.float32)
        self.conf = np.asarray([detections[i].get("confidence", 0.0) for i in self._indices], dtype=np.float32)
        self.cls = np.asarray([detections[i].get("class_id", 0) for i in self._indices], dtype=np.float32)
        self.xywh = _xyxy_to_xywh(self.xyxy)

    def __len__(self) -> int:
        return len(self._indices)

    def __getitem__(self, item):
        return DetectionResults(self._detections, self._indices[item])


class ByteTrackAdapter:
    """Tracker independiente por camara con backend real de Ultralytics."""

    def __init__(self, camera_id: str, allow_iou_fallback: bool = False):
        self.camera_id = camera_id
        self.allow_iou_fallback = allow_iou_fallback
        self.active_vehicles: dict[int, dict[str, Any]] = {}
        self.reset_count = 0
        self.tracker_backend = "uninitialized"
        self._tracker = None
        self._fallback = None

    def ensure_ready(self) -> None:
        if self._tracker is not None or self._fallback is not None:
            return
        try:
            self._tracker = _create_ultralytics_bytetrack()
            self.tracker_backend = "ultralytics-bytetrack"
        except Exception as exc:  # noqa: BLE001 - se convierte en error estable.
            if not self.allow_iou_fallback:
                raise ByteTrackUnavailableError(
                    "ByteTrack no disponible en la version instalada de Ultralytics"
                ) from exc
            self._fallback = _IoUTracker(camera_id=self.camera_id)
            self.tracker_backend = "iou-test-fallback"

    def reset(self) -> None:
        self.ensure_ready()
        self.active_vehicles.clear()
        self.reset_count += 1
        if self._tracker is not None:
            self._tracker.reset()
        elif self._fallback is not None:
            self._fallback.reset()

    def update(
        self,
        detections: list[dict[str, Any]],
        frame_shape: tuple[int, ...] | None = None,
        timestamp: object | None = None,
        frame: Any | None = None,
    ) -> list[dict[str, Any]]:
        del timestamp
        self.ensure_ready()
        if self._fallback is not None:
            tracked = self._fallback.update(detections, frame_shape=frame_shape)
            self.active_vehicles = dict(self._fallback.active_vehicles)
            return tracked

        results = DetectionResults(detections)
        tracks = self._tracker.update(results, img=frame)
        tracked = [_track_row_to_vehicle(row, detections) for row in tracks]
        self.active_vehicles = {vehicle["id"]: vehicle for vehicle in tracked if vehicle["id"] is not None}
        return tracked

    def get_active_ids(self) -> list[int]:
        return sorted(self.active_vehicles.keys())


def _create_ultralytics_bytetrack():
    from ultralytics.trackers.byte_tracker import BYTETracker

    class CameraTracker(BYTETracker):
        """Ultralytics defaults to a process-global ID allocator, even on reset."""

        def reset_id(self):
            self._camera_ids = count(1)

        def init_track(self, *args, **kwargs):
            tracks = super().init_track(*args, **kwargs)
            for track in tracks:
                track.next_id = self._camera_ids.__next__
            return tracks

    args = SimpleNamespace(
        tracker_type="bytetrack",
        track_high_thresh=0.25,
        track_low_thresh=0.1,
        new_track_thresh=0.25,
        track_buffer=30,
        match_thresh=0.8,
        fuse_score=True,
    )
    return CameraTracker(args)


def _track_row_to_vehicle(row: np.ndarray, detections: list[dict[str, Any]]) -> dict[str, Any]:
    values = row.tolist()
    x1, y1, x2, y2 = [int(round(v)) for v in values[:4]]
    track_id = int(values[4])
    confidence = float(values[5])
    class_id = int(values[6])
    detection_index = int(values[7]) if len(values) > 7 else -1
    class_name = "Desconocido"
    if 0 <= detection_index < len(detections):
        source = detections[detection_index]
        class_name = str(source.get("class", class_name))
        confidence = float(source.get("confidence", confidence))
        class_id = int(source.get("class_id", class_id))
    return {
        "id": track_id,
        "class": class_name,
        "class_id": class_id,
        "confidence": confidence,
        "bbox": [x1, y1, x2, y2],
    }


def _xyxy_to_xywh(xyxy: np.ndarray) -> np.ndarray:
    if len(xyxy) == 0:
        return np.empty((0, 4), dtype=np.float32)
    xywh = xyxy.copy()
    xywh[:, 0] = (xyxy[:, 0] + xyxy[:, 2]) / 2
    xywh[:, 1] = (xyxy[:, 1] + xyxy[:, 3]) / 2
    xywh[:, 2] = xyxy[:, 2] - xyxy[:, 0]
    xywh[:, 3] = xyxy[:, 3] - xyxy[:, 1]
    return xywh


@dataclass
class _FallbackTrack:
    track_id: int
    bbox: list[int]
    class_id: int
    class_name: str
    confidence: float
    missed: int = 0


class _IoUTracker:
    def __init__(self, camera_id: str, iou_threshold: float = 0.3, max_missed: int = 8):
        self.camera_id = camera_id
        self.iou_threshold = iou_threshold
        self.max_missed = max_missed
        self._next_id = 1
        self._tracks: dict[int, _FallbackTrack] = {}
        self.active_vehicles: dict[int, dict[str, Any]] = {}

    def reset(self) -> None:
        self._next_id = 1
        self._tracks.clear()
        self.active_vehicles.clear()

    def update(self, detections: list[dict[str, Any]], frame_shape: tuple[int, ...] | None = None) -> list[dict[str, Any]]:
        del frame_shape
        assigned_tracks: set[int] = set()
        tracked: list[dict[str, Any]] = []
        for detection in detections:
            track = self._match_detection(detection, assigned_tracks)
            if track is None:
                track = self._create_track(detection)
            else:
                self._update_track(track, detection)
            assigned_tracks.add(track.track_id)
            tracked.append(self._to_vehicle(track))
        for track_id, track in list(self._tracks.items()):
            if track_id not in assigned_tracks:
                track.missed += 1
                if track.missed > self.max_missed:
                    self._tracks.pop(track_id, None)
        self.active_vehicles = {vehicle["id"]: vehicle for vehicle in tracked if vehicle["id"] is not None}
        return tracked

    def _match_detection(self, detection: dict[str, Any], assigned_tracks: set[int]) -> _FallbackTrack | None:
        best_track = None
        best_iou = 0.0
        for track in self._tracks.values():
            if track.track_id in assigned_tracks or track.class_id != detection.get("class_id"):
                continue
            iou = _bbox_iou(track.bbox, detection["bbox"])
            if iou > best_iou:
                best_iou = iou
                best_track = track
        return best_track if best_iou >= self.iou_threshold else None

    def _create_track(self, detection: dict[str, Any]) -> _FallbackTrack:
        track = _FallbackTrack(
            track_id=self._next_id,
            bbox=[int(value) for value in detection["bbox"]],
            class_id=int(detection["class_id"]),
            class_name=str(detection.get("class", "Desconocido")),
            confidence=float(detection.get("confidence", 0.0)),
        )
        self._tracks[track.track_id] = track
        self._next_id += 1
        return track

    @staticmethod
    def _update_track(track: _FallbackTrack, detection: dict[str, Any]) -> None:
        track.bbox = [int(value) for value in detection["bbox"]]
        track.class_id = int(detection["class_id"])
        track.class_name = str(detection.get("class", "Desconocido"))
        track.confidence = float(detection.get("confidence", 0.0))
        track.missed = 0

    @staticmethod
    def _to_vehicle(track: _FallbackTrack) -> dict[str, Any]:
        return {
            "id": track.track_id,
            "class": track.class_name,
            "class_id": track.class_id,
            "confidence": track.confidence,
            "bbox": list(track.bbox),
        }


def _bbox_iou(a: list[int], b: list[int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_w = max(0, inter_x2 - inter_x1)
    inter_h = max(0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h
    if inter_area <= 0:
        return 0.0
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter_area
    return inter_area / union if union > 0 else 0.0
