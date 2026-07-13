from __future__ import annotations

import os
import queue
import time
import uuid
import logging
import threading
import sqlite3
from datetime import datetime, timezone, timedelta
import numpy as np

from app.media.clip_buffer import CameraClipBuffer
from app.media.clip_encoder import encode_clip, is_ffmpeg_available

logger = logging.getLogger(__name__)


class ClipManager:
    def __init__(
        self,
        db_path: str,
        clips_dir: str = "data/clips",
        sample_fps: int = 5,
        buffer_seconds: int = 15,
        pre_seconds: float = 6.0,
        post_seconds: float = 5.0,
        crf: int = 28,
        width: int = 960,
        height: int = 540,
        max_queue_size: int = 8
    ) -> None:
        self.db_path = db_path
        self.clips_dir = clips_dir
        self.sample_fps = sample_fps
        self.buffer_seconds = buffer_seconds
        self.pre_seconds = pre_seconds
        self.post_seconds = post_seconds
        self.crf = crf
        self.width = width
        self.height = height
        self.max_queue_size = max_queue_size

        self.buffers: dict[str, CameraClipBuffer] = {}
        self.buffers_lock = threading.Lock()

        # Thread-safe queue for encoding jobs
        self.queue: queue.Queue[dict] = queue.Queue(maxsize=max_queue_size)
        self.pending_jobs_count = 0
        self.stats_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False

    def get_buffer(self, camera_id: str) -> CameraClipBuffer:
        """Retrieves or instantiates a camera circular buffer thread-safely."""
        with self.buffers_lock:
            if camera_id not in self.buffers:
                self.buffers[camera_id] = CameraClipBuffer(
                    sample_fps=self.sample_fps,
                    buffer_seconds=self.buffer_seconds
                )
            return self.buffers[camera_id]

    def add_frame(self, camera_id: str, captured_at: datetime, sequence: int, frame: np.ndarray) -> bool:
        """Pushes a frame to the corresponding camera circular buffer."""
        buf = self.get_buffer(camera_id)
        return buf.add_frame(captured_at, sequence, frame)

    def start(self) -> None:
        """Starts the background worker thread."""
        with self.stats_lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._worker_loop, name="ClipManagerWorker", daemon=True)
            self._thread.start()
            logger.info("ClipManager worker thread started.")

    def stop(self, drain: bool = True) -> None:
        """Stops the background worker thread, optionally draining queue."""
        with self.stats_lock:
            if not self._running:
                return
            self._stop_event.set()

        if self._thread:
            # Wait for thread termination
            self._thread.join(timeout=15.0 if drain else 2.0)
            self._running = False
            logger.info("ClipManager worker thread stopped.")

    def request_clip(
        self,
        event_id: str,
        camera_id: str,
        anchor_at: datetime,
        event_type: str,
        plate: str | None,
        room_number: int | None,
        track_id: int
    ) -> bool:
        """
        Submits a clip compilation request.
        If the queue is full, fails immediately, updating SQLite state.
        """
        job = {
            "event_id": event_id,
            "camera_id": camera_id,
            "anchor_at": anchor_at,
            "event_type": event_type,
            "plate": plate,
            "room_number": room_number,
            "track_id": track_id,
            "requested_at": time.monotonic()
        }

        # Check if FFmpeg is available; if not, fail early
        if not is_ffmpeg_available():
            self._update_db_failed(event_id, "FFmpeg utility is missing from the system path.")
            return False

        try:
            self.queue.put(job, block=False)
            with self.stats_lock:
                self.pending_jobs_count += 1
            
            # Update event status in DB to PENDING
            self._update_db_status(event_id, "PENDING")
            return True
        except queue.Full:
            logger.warning("ClipManager queue full. Drop request for event_id: %s", event_id)
            self._update_db_failed(event_id, "ClipManager queue overflow error.")
            return False

    def get_pending_count(self) -> int:
        with self.stats_lock:
            return self.pending_jobs_count

    def is_alive(self) -> bool:
        with self.stats_lock:
            return self._running and self._thread is not None and self._thread.is_alive()

    def _worker_loop(self) -> None:
        """Background loop executing clip encoding tasks."""
        # Open dedicated connection for database updates
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        
        # Ensure schema is up to date
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
        except Exception:
            pass

        while True:
            # Check stop condition
            stop_requested = self._stop_event.is_set()
            if stop_requested and self.queue.empty():
                break

            try:
                job = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                event_id = job["event_id"]
                camera_id = job["camera_id"]
                anchor_at = job["anchor_at"]

                post_time_needed = anchor_at + timedelta(seconds=self.post_seconds)
                now_dt = datetime.now(timezone.utc) if anchor_at.tzinfo else datetime.now()
                wait_time = (post_time_needed - now_dt).total_seconds()
                if wait_time > 0:
                    time.sleep(min(wait_time, 8.0))

                buf = self.get_buffer(camera_id)
                selected_frames = buf.get_frames_in_range(anchor_at, self.pre_seconds, self.post_seconds)

                if not selected_frames:
                    self._update_db_failed_conn(conn, event_id, "No frames found in camera circular buffer for target time range.")
                    continue

                date_str = anchor_at.strftime("%Y-%m-%d")
                time_str = anchor_at.strftime("%Y-%m-%d_%H-%M-%S")
                evt = job["event_type"]
                pl = job["plate"] or "NO_LEGIBLE"
                uid = uuid.uuid4().hex[:6]
                filename = f"{camera_id}_{evt}_{pl}_{time_str}_{uid}.mp4"
                relative_clip_path = os.path.join(self.clips_dir, camera_id, date_str, filename)
                abs_clip_path = os.path.abspath(relative_clip_path)
                metadata = {
                    "camera_id": camera_id,
                    "event_type": evt,
                    "plate": job["plate"],
                    "room_number": job["room_number"],
                    "track_id": job["track_id"]
                }
                # Encode the clip
                encode_clip(
                    frames=selected_frames,
                    metadata=metadata,
                    output_path=abs_clip_path,
                    fps=self.sample_fps,
                    crf=self.crf,
                    width=self.width,
                    height=self.height
                )
                # Success
                self._update_db_success_conn(conn, event_id, relative_clip_path)
            except Exception as e:
                logger.error("Encoding error for event_id %s: %s", job.get("event_id"), e, exc_info=True)
                self._update_db_failed_conn(conn, job.get("event_id", ""), f"FFmpeg compilation error: {e}")
            finally:
                with self.stats_lock:
                    self.pending_jobs_count = max(0, self.pending_jobs_count - 1)
                self.queue.task_done()

        try:
            conn.close()
        except Exception:
            pass

    def _update_db_status(self, event_id: str, status: str) -> None:
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                conn.execute(
                    "UPDATE hotel_vehicle_events SET clip_status=? WHERE event_id=?",
                    (status, event_id)
                )
                conn.commit()
        except Exception as e:
            logger.warning("Failed to update status for event %s: %s", event_id, e)

    def _update_db_failed(self, event_id: str, error_msg: str) -> None:
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                conn.execute(
                    """
                    UPDATE hotel_vehicle_events 
                    SET clip_status='FAILED', clip_error=?, review_required=1,
                        notes = CASE WHEN notes = '' THEN 'clip fallido; ' || ?
                                     ELSE notes || '; clip fallido; ' || ? END
                    WHERE event_id=?
                    """,
                    (error_msg, error_msg, error_msg, event_id)
                )
                conn.commit()
        except Exception as e:
            logger.warning("Failed to update failed status for event %s: %s", event_id, e)

    def _update_db_failed_conn(self, conn: sqlite3.Connection, event_id: str, error_msg: str) -> None:
        try:
            conn.execute(
                """
                UPDATE hotel_vehicle_events 
                SET clip_status='FAILED', clip_error=?, review_required=1,
                    notes = CASE WHEN notes = '' THEN 'clip fallido; ' || ?
                                 ELSE notes || '; clip fallido; ' || ? END
                WHERE event_id=?
                """,
                (error_msg, error_msg, error_msg, event_id)
            )
            conn.commit()
        except Exception as e:
            logger.warning("Failed to update failed status with connection for event %s: %s", event_id, e)

    def _update_db_success_conn(self, conn: sqlite3.Connection, event_id: str, clip_path: str) -> None:
        try:
            conn.execute(
                "UPDATE hotel_vehicle_events SET clip_status='READY', clip_path=?, clip_error=NULL WHERE event_id=?",
                (clip_path, event_id)
            )
            conn.commit()
        except Exception as e:
            logger.warning("Failed to update success status for event %s: %s", event_id, e)
