from __future__ import annotations

import time
import math
import threading
import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class TrackHistory:
    camera_id: str
    track_id: int
    logical_id: int
    vehicle_class: str
    last_bbox: tuple[int, int, int, int]
    last_seen_at: datetime
    last_seen_mono: float
    movement: str


class TrackFragmentLinker:
    def __init__(self, max_gap_seconds: float = 2.0, max_distance: float = 0.15) -> None:
        self.max_gap_seconds = max_gap_seconds
        self.max_distance = max_distance
        
        self._lock = threading.Lock()
        self._logical_map: dict[tuple[str, int], int] = {}  # (camera_id, track_id) -> logical_id
        self._reverse_map: dict[tuple[str, int], set[int]] = {}  # (camera_id, logical_id) -> {track_ids}
        self._inactive_tracks: list[TrackHistory] = []
        self._next_logical_id = 1000  # Offset to distinguish from raw track IDs

    def get_logical_id(self, camera_id: str, track_id: int) -> int:
        with self._lock:
            key = (camera_id, int(track_id))
            return self._logical_map.get(key, int(track_id))

    def get_linked_track_ids(self, camera_id: str, logical_id: int) -> set[int]:
        with self._lock:
            key = (camera_id, int(logical_id))
            return self._reverse_map.get(key, {int(logical_id)})

    def register_track_active(self, camera_id: str, track_id: int, vehicle_class: str, bbox: tuple[int, int, int, int], movement: str, frame_width: int = 960, frame_height: int = 540) -> int:
        """
        Registers an active track and checks if it can be linked to an expired track.
        Returns the logical vehicle ID assigned.
        """
        with self._lock:
            key = (camera_id, int(track_id))
            if key in self._logical_map:
                return self._logical_map[key]

            # Try to link to an inactive track
            now_mono = time.monotonic()
            now_dt = datetime.now()
            
            cx, cy = self._get_normalized_center(bbox, frame_width, frame_height)
            best_link: TrackHistory | None = None
            best_dist = float("inf")

            for hist in list(self._inactive_tracks):
                # Check age
                gap = now_mono - hist.last_seen_mono
                if gap > self.max_gap_seconds:
                    self._inactive_tracks.remove(hist)
                    continue

                if hist.camera_id != camera_id:
                    continue

                if hist.vehicle_class != vehicle_class:
                    continue

                # Compatibility of movement (e.g. both INWARD or both OUTWARD or one UNKNOWN)
                if hist.movement != "UNKNOWN" and movement != "UNKNOWN" and hist.movement != movement:
                    continue

                # Check spatial distance
                hcx, hcy = self._get_normalized_center(hist.last_bbox, frame_width, frame_height)
                dist = math.sqrt((cx - hcx)**2 + (cy - hcy)**2)
                
                if dist < self.max_distance and dist < best_dist:
                    best_dist = dist
                    best_link = hist

            if best_link is not None:
                logical_id = best_link.logical_id
                logger.info(
                    "[LINKER] Linked camera %s track %d to existing logical ID %d (previous track %d, gap=%.2fs, dist=%.3f)",
                    camera_id, track_id, logical_id, best_link.track_id, 
                    now_mono - best_link.last_seen_mono, best_dist
                )
                # Remove from inactive since it's now continued
                if best_link in self._inactive_tracks:
                    self._inactive_tracks.remove(best_link)
            else:
                logical_id = int(track_id)

            # Map the key to the logical ID
            self._logical_map[key] = logical_id
            
            # Update reverse map
            rev_key = (camera_id, logical_id)
            self._reverse_map.setdefault(rev_key, set()).add(int(track_id))
            
            return logical_id

    def register_track_inactive(self, camera_id: str, track_id: int, vehicle_class: str, last_bbox: tuple[int, int, int, int], movement: str) -> None:
        """Called when a track disappears or is finalized by the analyzer."""
        with self._lock:
            key = (camera_id, int(track_id))
            logical_id = self._logical_map.get(key, int(track_id))
            
            hist = TrackHistory(
                camera_id=camera_id,
                track_id=int(track_id),
                logical_id=logical_id,
                vehicle_class=vehicle_class,
                last_bbox=last_bbox,
                last_seen_at=datetime.now(),
                last_seen_mono=time.monotonic(),
                movement=movement
            )
            # Avoid duplicates
            self._inactive_tracks = [h for h in self._inactive_tracks if h.camera_id != camera_id or h.track_id != int(track_id)]
            self._inactive_tracks.append(hist)

    def _get_normalized_center(self, bbox: tuple[int, int, int, int], w: int, h: int) -> tuple[float, float]:
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        # If coordinates are already normalized (between 0.0 and 1.0)
        if x2 <= 1.0 and y2 <= 1.0:
            return cx, cy
        # Else convert to normalized
        return cx / max(1, w), cy / max(1, h)
