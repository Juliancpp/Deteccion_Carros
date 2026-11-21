"""
Captura RTSP robusta con buffer de un solo frame.

No integra YOLO ni persiste eventos. Cada instancia representa una camara y
mantiene solo el frame mas reciente para evitar latencia acumulada.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

try:
    import cv2
except ImportError:  # Las pruebas unitarias usan capture_factory y no requieren OpenCV real.
    cv2 = None  # type: ignore

try:
    from app import config
    from app.settings import (
        CameraSettings,
        DVRSettings,
        build_rtsp_url,
        sanitize_rtsp_url,
        sanitize_sensitive_text,
    )
except ImportError:  # Permite ejecutar scripts con app/ en sys.path.
    import config  # type: ignore
    from settings import (  # type: ignore
        CameraSettings,
        DVRSettings,
        build_rtsp_url,
        sanitize_rtsp_url,
        sanitize_sensitive_text,
    )


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

CaptureFactory = Callable[[str], Any]


@dataclass(frozen=True)
class LatestFrame:
    frame: Any
    captured_at: datetime
    sequence: int
    generation: tuple[int, int] | None = None


@dataclass(frozen=True)
class StreamStatus:
    camera_id: str
    camera_name: str
    running: bool
    connected: bool
    frame_sequence: int
    last_frame_at: datetime | None
    frame_age_seconds: float | None
    capture_fps: float
    width: int | None
    height: int | None
    source_fps: float | None
    connection_attempts: int
    reconnect_count: int
    read_error_count: int
    consecutive_read_failures: int
    last_error: str | None
    loop_count: int = 0



class RTSPStream:
    """Capturador RTSP de una camara con reconexion automatica."""

    def __init__(
        self,
        dvr_settings: DVRSettings,
        camera_settings: CameraSettings,
        *,
        capture_factory: CaptureFactory | None = None,
        rtsp_transport: str = config.RTSP_TRANSPORT,
        open_timeout_ms: int = config.RTSP_OPEN_TIMEOUT_MS,
        read_timeout_ms: int = config.RTSP_READ_TIMEOUT_MS,
        stale_timeout_seconds: float = config.RTSP_STALE_TIMEOUT_SECONDS,
        reconnect_initial_seconds: float = config.RTSP_RECONNECT_INITIAL_SECONDS,
        reconnect_max_seconds: float = config.RTSP_RECONNECT_MAX_SECONDS,
        max_consecutive_read_failures: int = config.RTSP_MAX_CONSECUTIVE_READ_FAILURES,
        stop_join_timeout_seconds: float = config.RTSP_STOP_JOIN_TIMEOUT_SECONDS,
    ) -> None:
        self.dvr_settings = dvr_settings
        self.camera_settings = camera_settings
        
        self.video_path = getattr(camera_settings, "video_path", None)
        self._is_local_file = False
        self._is_webcam = False
        self._loop_count = 0

        if self.video_path is not None:
            if str(self.video_path).isdigit():
                self._url = int(self.video_path)
                self._is_webcam = True
            else:
                self._url = self.video_path
                self._is_local_file = True
            self._safe_url = str(self._url)
        else:
            self._url = build_rtsp_url(dvr_settings, camera_settings)
            self._safe_url = sanitize_rtsp_url(self._url)
            
        self._capture_factory = capture_factory or self._default_capture_factory
        self._rtsp_transport = rtsp_transport
        self._open_timeout_ms = int(open_timeout_ms)
        self._read_timeout_ms = int(read_timeout_ms)
        self._stale_timeout_seconds = float(stale_timeout_seconds)
        self._reconnect_initial_seconds = float(reconnect_initial_seconds)
        self._reconnect_max_seconds = float(reconnect_max_seconds)
        self._max_consecutive_read_failures = int(max_consecutive_read_failures)
        self._stop_join_timeout_seconds = float(stop_join_timeout_seconds)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._started_event = threading.Event()
        self._stopped_event = threading.Event()
        self._connected_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture: Any = None
        self._latest_frame: LatestFrame | None = None
        self._running = False
        self._connected = False
        self._frame_sequence = 0
        self._last_frame_at: datetime | None = None
        self._capture_fps = 0.0
        self._width: int | None = None
        self._height: int | None = None
        self._source_fps: float | None = None
        self._connection_attempts = 0
        self._reconnect_count = 0
        self._read_error_count = 0
        self._consecutive_read_failures = 0
        self._last_error: str | None = None
        self._fps_times: deque[float] = deque()

    def __repr__(self) -> str:
        return (
            f"RTSPStream(camera_id={self.camera_settings.camera_id!r}, "
            f"camera_name={self.camera_settings.name!r}, "
            f"host={self.dvr_settings.host!r}, channel={self.camera_settings.channel!r})"
        )

    def start(self) -> None:
        """Inicia el hilo de captura si no esta activo."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._started_event.clear()
            self._stopped_event.clear()
            self._connected_event.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._run,
                name=f"RTSPStream-{self.camera_settings.camera_id}",
                daemon=False,
            )
            self._thread.start()

    def stop(self) -> bool:
        """Solicita detener el capturador y espera al worker. Es idempotente."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._stop_join_timeout_seconds)

        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                self._running = False
                self._connected = False
            self._connected_event.clear()
            self._stopped_event.set()
            logger.info("camera=%s event=stopped", self.camera_settings.camera_id)
        else:
            logger.error("camera=%s event=stop_timeout", self.camera_settings.camera_id)
        return stopped

    def is_running(self) -> bool:
        with self._lock:
            return self._running and self._thread is not None and self._thread.is_alive()

    def is_worker_alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def wait_until_stopped(self, timeout: float) -> bool:
        return self._stopped_event.wait(timeout)

    def get_latest_frame(
        self,
        after_sequence: int | None = None,
        *,
        copy_frame: bool = True,
    ) -> LatestFrame | None:
        """Devuelve el frame mas reciente o None si no hay frame nuevo."""
        with self._lock:
            latest = self._latest_frame
            if latest is None:
                return None
            if after_sequence is not None and latest.sequence <= after_sequence:
                return None
            frame = latest.frame.copy() if copy_frame else latest.frame
            return LatestFrame(frame=frame, captured_at=latest.captured_at, sequence=latest.sequence, generation=latest.generation)

    def get_status(self) -> StreamStatus:
        with self._lock:
            now = _utc_now()
            frame_age = None
            if self._last_frame_at is not None:
                frame_age = max(0.0, (now - self._last_frame_at).total_seconds())

            return StreamStatus(
                camera_id=self.camera_settings.camera_id,
                camera_name=self.camera_settings.name,
                running=self._running and self._thread is not None and self._thread.is_alive(),
                connected=self._connected,
                frame_sequence=self._frame_sequence,
                last_frame_at=self._last_frame_at,
                frame_age_seconds=frame_age,
                capture_fps=self._capture_fps,
                width=self._width,
                height=self._height,
                source_fps=self._source_fps,
                connection_attempts=self._connection_attempts,
                reconnect_count=self._reconnect_count,
                read_error_count=self._read_error_count,
                consecutive_read_failures=self._consecutive_read_failures,
                last_error=self._last_error,
                loop_count=self._loop_count,
            )

    def wait_until_connected(self, timeout: float) -> bool:
        return self._connected_event.wait(timeout)

    def _run(self) -> None:
        delay = self._reconnect_initial_seconds
        self._started_event.set()
        try:
            while not self._stop_event.is_set():
                capture = None
                failure_reason = "Captura detenida"
                try:
                    self._mark_connection_attempt()
                    logger.info("camera=%s event=connecting", self.camera_settings.camera_id)
                    capture = self._open_capture()
                    self._set_capture_reference(capture)

                    if capture is None or not self._is_opened(capture):
                        failure_reason = "No se pudo abrir el stream RTSP"
                        self._mark_disconnected(failure_reason)
                        delay = self._wait_before_reconnect(delay)
                        continue

                    self._mark_connected(capture)
                    delay = self._reconnect_initial_seconds
                    failure_reason = self._read_loop(capture)
                finally:
                    self._release_owned_capture(capture)
                    capture = None
                    self._clear_capture_reference()

                if self._stop_event.is_set():
                    break

                self._mark_disconnected(failure_reason)
                delay = self._wait_before_reconnect(delay)
        finally:
            self._mark_worker_stopped()

    def _read_loop(self, capture: Any) -> str:
        last_success = time.monotonic()
        
        frame_delay = 0.0
        if self._is_local_file:
            fps = 0.0
            if cv2 is not None:
                try:
                    fps = capture.get(cv2.CAP_PROP_FPS)
                except Exception:
                    pass
            if not fps or fps <= 0:
                fps = getattr(self.camera_settings, "target_fps", 25.0) or 25.0
            frame_delay = 1.0 / fps

        while not self._stop_event.is_set():
            start_time = time.monotonic()
            try:
                ok, frame = capture.read()
            except Exception as exc:  # noqa: BLE001 - se sanitiza antes de estado/log.
                self._mark_read_failure(exc)
                return self._sanitize_error(exc)

            now_monotonic = time.monotonic()
            if ok and frame is not None:
                self._store_frame(frame)
                last_success = now_monotonic
                
                # Apply rate-limiting for local files
                if self._is_local_file:
                    elapsed = time.monotonic() - start_time
                    sleep_time = max(0.0, frame_delay - elapsed)
                    if sleep_time > 0:
                        self._stop_event.wait(sleep_time)
                continue

            # Si es un video local y terminó, revisar si configuramos bucle
            if self._is_local_file:
                video_loop = getattr(self.camera_settings, "video_loop", True)
                if video_loop:
                    try:
                        if cv2 is not None:
                            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        with self._lock:
                            self._latest_frame = None
                            self._loop_count += 1
                            self._frame_sequence = 0  # Trigger de reseteo para el coordinador
                        logger.info("camera=%s event=video_loop_reset loop=%d", self.camera_settings.camera_id, self._loop_count)
                        continue
                    except Exception as exc:
                        logger.warning("camera=%s event=video_loop_failed error=%s", self.camera_settings.camera_id, self._sanitize_error(exc))
                else:
                    return "Fin del video (loop desactivado)"

            self._mark_read_failure("read() sin frame")
            if self._consecutive_failures_exceeded():
                return "Fallos consecutivos leyendo el stream"
            if now_monotonic - last_success > self._stale_timeout_seconds:
                return "Stream RTSP sin frames recientes"

        return "Captura detenida"

    def _open_capture(self) -> Any | None:
        try:
            return self._capture_factory(self._url)
        except Exception as exc:  # noqa: BLE001 - se sanitiza antes de estado/log.
            self._set_last_error(exc)
            logger.warning(
                "camera=%s event=open_failure error=%s",
                self.camera_settings.camera_id,
                self._sanitize_error(exc),
            )
            return None

    def _set_capture_reference(self, capture: Any | None) -> None:
        with self._lock:
            self._capture = capture

    def _clear_capture_reference(self) -> None:
        with self._lock:
            self._capture = None

    def _release_owned_capture(self, capture: Any | None) -> None:
        if capture is None:
            return
        try:
            capture.release()
        except Exception as exc:  # noqa: BLE001
            self._set_last_error(exc)

    def _default_capture_factory(self, url: str | int) -> cv2.VideoCapture:
        if cv2 is None:
            raise RuntimeError("OpenCV no esta instalado; instala opencv-python para usar RTSP")
        if isinstance(url, int) or self._is_local_file or self._is_webcam:
            capture = cv2.VideoCapture(url)
            return capture

        self._prepare_ffmpeg_options()
        backend = getattr(cv2, "CAP_FFMPEG", 0)

        try:
            capture = cv2.VideoCapture(url, backend)
        except Exception:
            capture = cv2.VideoCapture(url)
        if not self._is_opened(capture):
            try:
                capture.release()
            except Exception:
                pass
            capture = cv2.VideoCapture(url)
        self._apply_capture_properties(capture)
        return capture

    def _prepare_ffmpeg_options(self) -> None:
        if self._rtsp_transport and "OPENCV_FFMPEG_CAPTURE_OPTIONS" not in os.environ:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{self._rtsp_transport}"

    def _apply_capture_properties(self, capture: Any) -> None:
        if cv2 is None:
            return
        for prop_name, value in (
            ("CAP_PROP_OPEN_TIMEOUT_MSEC", self._open_timeout_ms),
            ("CAP_PROP_READ_TIMEOUT_MSEC", self._read_timeout_ms),
            ("CAP_PROP_BUFFERSIZE", 1),
        ):
            prop = getattr(cv2, prop_name, None)
            if prop is None:
                continue
            try:
                capture.set(prop, int(value))
            except Exception:
                logger.debug(
                    "camera=%s event=capture_property_ignored property=%s",
                    self.camera_settings.camera_id,
                    prop_name,
                )

    def _mark_connection_attempt(self) -> None:
        with self._lock:
            self._connection_attempts += 1

    def _mark_connected(self, capture: Any) -> None:
        width, height, source_fps = self._read_capture_metadata(capture)
        with self._lock:
            self._connected = True
            self._last_error = None
            self._consecutive_read_failures = 0
            self._width = width
            self._height = height
            self._source_fps = source_fps
        self._connected_event.set()
        logger.info(
            "camera=%s event=connected resolution=%sx%s source_fps=%s",
            self.camera_settings.camera_id,
            width,
            height,
            source_fps,
        )

    def _mark_disconnected(self, reason: Any) -> None:
        safe_reason = self._sanitize_error(reason)
        with self._lock:
            self._reconnect_count += 1
            self._connected = False
            self._latest_frame = None
            self._last_frame_at = None
            self._capture_fps = 0.0
            self._fps_times.clear()
            self._last_error = safe_reason
        self._connected_event.clear()
        logger.warning(
            "camera=%s event=disconnected error=%s",
            self.camera_settings.camera_id,
            safe_reason,
        )

    def _mark_read_failure(self, error: Any) -> None:
        safe_error = self._sanitize_error(error)
        with self._lock:
            self._read_error_count += 1
            self._consecutive_read_failures += 1
            self._last_error = safe_error
            count = self._consecutive_read_failures
        logger.warning(
            "camera=%s event=read_failure count=%s",
            self.camera_settings.camera_id,
            count,
        )

    def _store_frame(self, frame: Any) -> None:
        captured_at = _utc_now()
        frame_time = time.monotonic()
        with self._lock:
            self._frame_sequence += 1
            self._latest_frame = LatestFrame(
                frame=frame.copy(),
                captured_at=captured_at,
                sequence=self._frame_sequence,
                generation=(self._reconnect_count, self._loop_count),
            )
            self._last_frame_at = captured_at
            self._connected = True
            self._consecutive_read_failures = 0
            self._last_error = None
            self._update_capture_fps(frame_time)
        self._connected_event.set()

    def _update_capture_fps(self, frame_time: float) -> None:
        self._fps_times.append(frame_time)
        cutoff = frame_time - 1.0
        while self._fps_times and self._fps_times[0] < cutoff:
            self._fps_times.popleft()
        if len(self._fps_times) >= 2:
            elapsed = self._fps_times[-1] - self._fps_times[0]
            self._capture_fps = (len(self._fps_times) - 1) / elapsed if elapsed > 0 else 0.0
        else:
            self._capture_fps = float(len(self._fps_times))

    def _wait_before_reconnect(self, delay: float) -> float:
        capped_delay = min(delay, self._reconnect_max_seconds)
        logger.info(
            "camera=%s event=reconnecting delay_seconds=%s",
            self.camera_settings.camera_id,
            capped_delay,
        )
        self._stop_event.wait(capped_delay)
        return min(max(delay * 2, self._reconnect_initial_seconds), self._reconnect_max_seconds)

    def _set_last_error(self, error: Any) -> None:
        safe_error = self._sanitize_error(error)
        with self._lock:
            self._last_error = safe_error

    def _mark_worker_stopped(self) -> None:
        with self._lock:
            self._running = False
            self._connected = False
            self._capture = None
        self._connected_event.clear()
        self._stopped_event.set()

    def _sanitize_error(self, error: Any) -> str:
        return sanitize_sensitive_text(
            error,
            self.dvr_settings,
            camera_settings=self.camera_settings,
            hide_username=False,
        )

    @staticmethod
    def _is_opened(capture: Any) -> bool:
        try:
            return bool(capture.isOpened())
        except Exception:
            return False

    @staticmethod
    def _read_capture_metadata(capture: Any) -> tuple[int | None, int | None, float | None]:
        def read_prop(prop_name: str) -> float | None:
            if cv2 is None:
                return None
            prop = getattr(cv2, prop_name, None)
            if prop is None:
                return None
            try:
                value = float(capture.get(prop))
            except Exception:
                return None
            return value if value > 0 else None

        width_value = read_prop("CAP_PROP_FRAME_WIDTH")
        height_value = read_prop("CAP_PROP_FRAME_HEIGHT")
        source_fps = read_prop("CAP_PROP_FPS")
        width = int(width_value) if width_value else None
        height = int(height_value) if height_value else None
        return width, height, source_fps

    def _consecutive_failures_exceeded(self) -> bool:
        with self._lock:
            return self._consecutive_read_failures >= self._max_consecutive_read_failures


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
