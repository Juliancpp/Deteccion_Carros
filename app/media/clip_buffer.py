from __future__ import annotations

import collections
import threading
import time
from datetime import datetime, timezone, timedelta
import numpy as np


class CameraClipBuffer:
    """
    Circular deque buffer to temporarily keep raw frames for a single camera.
    Locks operations to ensure safety when accessed by the main coordinator
    thread and the background clip manager thread.
    """

    def __init__(self, sample_fps: int = 5, buffer_seconds: int = 15) -> None:
        self.sample_fps = sample_fps
        self.buffer_seconds = buffer_seconds
        self.max_len = int(sample_fps * buffer_seconds)
        self.frames: collections.deque[dict] = collections.deque(maxlen=self.max_len)
        self.lock = threading.Lock()
        self.last_inserted_mono_time = 0.0

    def add_frame(self, captured_at: datetime, sequence: int, frame: np.ndarray) -> bool:
        """
        Adds a frame copy to the circular queue if rate limiting permits.
        Rate-limited to 5 FPS (i.e. at least 1/5 seconds = 0.2s interval).
        """
        now = time.monotonic()
        interval = 1.0 / self.sample_fps
        
        # Muestrear a 5 FPS máximo
        if now - self.last_inserted_mono_time < (interval - 0.01):
            return False
            
        self.last_inserted_mono_time = now
        
        # Copia defensiva del frame crudo
        frame_copy = frame.copy()
        
        with self.lock:
            self.frames.append({
                "mono_time": now,
                "captured_at": captured_at,
                "sequence": sequence,
                "frame": frame_copy
            })
        return True

    def get_frames_in_range(self, anchor_at: datetime, pre_seconds: float, post_seconds: float) -> list[dict]:
        """
        Extracts frames within [anchor_at - pre_seconds, anchor_at + post_seconds].
        Handles timezone differences dynamically.
        """
        t_start = anchor_at - timedelta(seconds=pre_seconds)
        t_end = anchor_at + timedelta(seconds=post_seconds)

        selected = []
        with self.lock:
            for item in self.frames:
                item_at = item["captured_at"]
                
                # Make naive/aware timestamps compatible
                if item_at.tzinfo is not None and t_start.tzinfo is None:
                    item_at = item_at.replace(tzinfo=None)
                elif item_at.tzinfo is None and t_start.tzinfo is not None:
                    item_at = item_at.replace(tzinfo=timezone.utc)
                
                if t_start <= item_at <= t_end:
                    selected.append(item)
        return selected

    def clear(self) -> None:
        with self.lock:
            self.frames.clear()
