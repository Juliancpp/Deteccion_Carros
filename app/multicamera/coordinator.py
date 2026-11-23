from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from typing import Any, Callable

from app import config
from app.hotel.geometry import GeometryDetector, GeometryEvent
from app.hotel.gate_detector import bottom_center
from app.hotel.geometry_overlay import draw_features
from app.detector import VehicleDetector
from app.line_detector import VirtualLineDetector, LineCrossing
from app.multicamera.camera_context import (
    CameraContext,
    CameraCoordinatorStatus,
    CameraInferenceResult,
    MultiCameraStatus,
)
from app.multicamera.event_candidate import EventCandidate
from app.passage.corridor_detector import PassageCorridorDetector, PassageConfirmation
from app.settings import CameraSettings, DVRSettings
from app.tracker import VehicleTracker
from app.video.rtsp_stream import RTSPStream


@dataclass(frozen=True)
class NormalizedCrossing:
    track_id: int
    direction: str
    track_point: tuple[int, int]
    occurred_at: datetime
    corridor_progress: float | None = None
    lateral_distance: float | None = None


def _normalize_crossing(crossing: Any) -> NormalizedCrossing:
    if isinstance(crossing, PassageConfirmation):
        return NormalizedCrossing(
            track_id=crossing.track_id,
            direction=crossing.direction,
            track_point=crossing.track_point,
            occurred_at=crossing.confirmed_at,
            corridor_progress=crossing.progress,
            lateral_distance=crossing.lateral_distance,
        )
    if isinstance(crossing, LineCrossing):
        return NormalizedCrossing(
            track_id=crossing.track_id,
            direction=crossing.direction,
            track_point=crossing.center,
            occurred_at=crossing.crossed_at,
            corridor_progress=None,
            lateral_distance=None,
        )
    # Generic duck typing for mocks in tests
    track_id = int(getattr(crossing, "track_id"))
    direction = str(getattr(crossing, "direction"))
    occurred_at = getattr(crossing, "confirmed_at", None) or getattr(crossing, "crossed_at", None)
    if occurred_at is None:
        occurred_at = datetime.now(timezone.utc)
    track_point = getattr(crossing, "track_point", None) or getattr(crossing, "center", (0, 0))
    progress = getattr(crossing, "progress", None)
    lateral_distance = getattr(crossing, "lateral_distance", None)
    return NormalizedCrossing(
        track_id=track_id,
        direction=direction,
        track_point=track_point,
        occurred_at=occurred_at,
        corridor_progress=progress,
        lateral_distance=lateral_distance,
    )


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


DetectorFactory = Callable[[], Any]
StreamFactory = Callable[[DVRSettings, CameraSettings], RTSPStream]
TrackerFactory = Callable[[str], Any]
LineDetectorFactory = Callable[[CameraSettings], Any]


class MultiCameraCoordinator:
    """Un unico hilo de inferencia YOLO con trackers aislados por camara."""

    def __init__(
        self,
        dvr_settings: DVRSettings,
        camera_settings: list[CameraSettings],
        *,
        geometry_sink: Callable[[list[GeometryEvent]], None] | None = None,
        detector: Any | None = None,
        detector_factory: DetectorFactory = VehicleDetector,
        stream_factory: StreamFactory | None = None,
        tracker_factory: TrackerFactory | None = None,
        line_detector_factory: LineDetectorFactory | None = None,
        copy_result_frame: bool = config.INFERENCE_RESULT_COPY_FRAME,
        idle_sleep_seconds: float = config.INFERENCE_SCHEDULER_IDLE_SLEEP_SECONDS,
        status_window_seconds: float = config.INFERENCE_STATUS_WINDOW_SECONDS,
    ) -> None:
        self.geometry_sink = geometry_sink
        self.dvr_settings = dvr_settings
        self.camera_settings = [camera for camera in camera_settings if camera.enabled]
        self.detector = detector if detector is not None else detector_factory()
        self.model_load_count = 1
        self.copy_result_frame = copy_result_frame
        self.idle_sleep_seconds = idle_sleep_seconds
        self.status_window_seconds = status_window_seconds
        self._stream_factory = stream_factory or (lambda dvr, camera: RTSPStream(dvr, camera))
        self._tracker_factory = tracker_factory or (lambda camera_id: VehicleTracker(camera_id=camera_id))
        self._line_detector_factory = line_detector_factory or _passage_detector_from_camera
        self._event_queue: queue.Queue[EventCandidate] = queue.Queue(maxsize=config.EVENT_QUEUE_MAXSIZE)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._stopped_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._total_inference_count = 0
        self._event_queue_dropped = 0
        self._loop_times: deque[float] = deque()
        self._inference_times: deque[float] = deque()
        self._rr_index = 0

        self.contexts: list[CameraContext] = []
        for camera in self.camera_settings:
            stream = self._stream_factory(dvr_settings, camera)
            tracker = self._tracker_factory(camera.camera_id)
            if hasattr(tracker, "ensure_ready"):
                tracker.ensure_ready()
            tracker_backend = getattr(tracker, "tracker_backend", "unknown")
            use_geometry = camera.geometry and camera.geometry.use_for_visits
            line_detector = None if use_geometry else self._line_detector_factory(camera)
            self.contexts.append(
                CameraContext(
                    camera_id=camera.camera_id,
                    camera_name=camera.name,
                    target_fps=camera.target_fps,
                    stream=stream,
                    tracker=tracker,
                    line_detector=line_detector,
                    tracker_backend=tracker_backend,
                    geometry_detector=GeometryDetector(camera.geometry) if camera.geometry else None,
                )
            )

    def start(self, start_streams: bool = True) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._stopped_event.clear()
            self._running = True
            if start_streams:
                for context in self.contexts:
                    context.stream.start()
            self._thread = threading.Thread(
                target=self._scheduler_loop,
                name="MultiCameraInferenceScheduler",
                daemon=False,
            )
            self._thread.start()

    def stop(self, stop_streams: bool = True) -> bool:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(1.0, config.RTSP_STOP_JOIN_TIMEOUT_SECONDS))

        scheduler_stopped = thread is None or not thread.is_alive()
        streams_stopped = True
        if stop_streams:
            for context in self.contexts:
                streams_stopped = context.stream.stop() and streams_stopped

        if scheduler_stopped:
            with self._lock:
                self._running = False
            self._stopped_event.set()
        return scheduler_stopped and streams_stopped

    def is_running(self) -> bool:
        with self._lock:
            return self._running and self._thread is not None and self._thread.is_alive()

    def wait_until_stopped(self, timeout: float) -> bool:
        return self._stopped_event.wait(timeout)

    def get_latest_result(
        self,
        camera_id: str,
        after_sequence: int | None = None,
        *,
        copy_frame: bool = True,
        after_session: str | None = None,
    ) -> CameraInferenceResult | None:
        with self._lock:
            context = self._context_by_id(camera_id)
            if context is None or context.last_result is None:
                return None
            result = context.last_result
            if after_session in (None, result.session_id) and after_sequence is not None and result.source_sequence <= after_sequence:
                return None
            if copy_frame and result.annotated_frame is not None:
                return CameraInferenceResult(
                    camera_id=result.camera_id,
                    camera_name=result.camera_name,
                    session_id=result.session_id,
                    source_sequence=result.source_sequence,
                    captured_at=result.captured_at,
                    processed_at=result.processed_at,
                    inference_ms=result.inference_ms,
                    detections_count=result.detections_count,
                    tracked_vehicles=result.tracked_vehicles,
                    annotated_frame=result.annotated_frame.copy(),
                    raw_frame=result.raw_frame.copy() if result.raw_frame is not None else None,
                )
            return result

    def get_status(self) -> MultiCameraStatus:
        now = time.monotonic()
        with self._lock:
            camera_statuses = tuple(self._camera_status(context, now) for context in self.contexts)
            return MultiCameraStatus(
                model_load_count=self.model_load_count,
                total_inference_count=self._total_inference_count,
                scheduler_running=self._running and self._thread is not None and self._thread.is_alive(),
                scheduler_loop_rate=self._loop_rate(now),
                pending_events=self._event_queue.qsize(),
                event_queue_dropped=self._event_queue_dropped,
                cameras=camera_statuses,
            )

    def get_event(self, timeout: float | None = None) -> EventCandidate | None:
        try:
            return self._event_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_pending_event_count(self) -> int:
        return self._event_queue.qsize()

    def _scheduler_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                loop_now = time.monotonic()
                self._record_loop(loop_now)
                context = self._next_context()
                did_work = False
                if context is not None and self._camera_due(context, loop_now):
                    did_work = self._process_camera(context, loop_now)
                if not did_work:
                    self._stop_event.wait(self.idle_sleep_seconds)
        finally:
            with self._lock:
                self._running = False
            self._stopped_event.set()

    def _next_context(self) -> CameraContext | None:
        if not self.contexts:
            return None
        context = self.contexts[self._rr_index % len(self.contexts)]
        self._rr_index = (self._rr_index + 1) % len(self.contexts)
        return context

    @staticmethod
    def _camera_due(context: CameraContext, now: float) -> bool:
        interval = 1.0 / max(context.target_fps, 0.001)
        return now - context.last_inference_at >= interval

    def _process_camera(self, context: CameraContext, now: float) -> bool:
        stream_status = context.stream.get_status()
        self._handle_reconnect_if_needed(context, stream_status)
        latest = context.stream.get_latest_frame(after_sequence=context.last_consumed_sequence)
        if latest is None:
            context.source_sequence = stream_status.frame_sequence
            return False

        generation = getattr(latest, "generation", None)
        if generation is not None and generation != (context.last_reconnect_count, context.last_loop_count):
            # Capture crossed a reconnect/rewind between the two snapshot reads.
            # Leave this frame unconsumed until status and frame agree.
            return False
        skipped = max(0, latest.sequence - context.last_consumed_sequence - 1)
        context.source_sequence = latest.sequence
        frame = latest.frame.copy()

        start = time.perf_counter()
        try:
            detections = self.detector.detect(frame)
        except Exception as exc:  # noqa: BLE001
            context.last_error = f"inferencia: {type(exc).__name__}"
            logger.exception("camera=%s event=inference_error", context.camera_id)
            context.last_consumed_sequence = latest.sequence
            context.skipped_frames += skipped
            context.last_inference_at = now
            return True
        inference_ms = (time.perf_counter() - start) * 1000

        try:
            tracked = context.tracker.update(
                detections=detections,
                frame_shape=frame.shape,
                timestamp=latest.captured_at,
                frame=frame,
            )
        except Exception as exc:  # noqa: BLE001
            context.last_error = f"tracker: {type(exc).__name__}"
            tracked = []
            logger.exception("camera=%s event=tracker_error", context.camera_id)

        self._process_crossings(context, tracked, frame, latest, now)
        annotated = self._annotate_frame(frame, context, tracked, inference_ms)
        result = CameraInferenceResult(
            session_id=context.session_id,
            camera_id=context.camera_id,
            camera_name=context.camera_name,
            source_sequence=latest.sequence,
            captured_at=latest.captured_at,
            processed_at=datetime.now(timezone.utc),
            inference_ms=inference_ms,
            detections_count=len(detections),
            tracked_vehicles=tuple(dict(vehicle) for vehicle in tracked),
            annotated_frame=annotated.copy() if self.copy_result_frame else None,
            raw_frame=frame.copy() if self.copy_result_frame else None,
        )

        with self._lock:
            context.last_consumed_sequence = latest.sequence
            context.last_inference_at = time.monotonic()
            context.last_result = result
            context.inference_count += 1
            context.inference_ms_last = inference_ms
            context.inference_ms_total += inference_ms
            context.processed_sequence = latest.sequence
            context.skipped_frames += skipped
            context.detections_count = len(detections)
            context.active_track_ids = _active_ids(context.tracker, tracked)
            if context.geometry_detector and context.geometry_detector.layout.use_for_visits:
                context.crossing_candidates = context.events_published + context.event_queue_dropped
            else:
                context.crossing_candidates = _line_metric(context.line_detector, "confirmed_count")
            context.duplicate_events_suppressed = _line_metric(context.line_detector, "duplicate_suppressed_count")
            context.inference_times.append(context.last_inference_at)
            self._prune_window(context.inference_times, context.last_inference_at)
            context.last_error = None
            self._total_inference_count += 1
            self._inference_times.append(time.monotonic())
            self._prune_window(self._inference_times, time.monotonic())
        return True

    def _process_crossings(self, context, tracked, frame, latest, now):
        geometry = context.geometry_detector
        if geometry is not None:
            height, width = frame.shape[:2]
            events = geometry.update(tracked, width, height, latest.captured_at, latest.sequence, now)
            if events and self.geometry_sink is not None:
                batch = []
                for event in events:
                    if event.event_type == "ROOM_ENTRY":
                        vehicle = _vehicle_by_track_id(tracked, event.track_id)
                        if vehicle:
                            from app.safety import safe_crop
                            event = replace(event, evidence_image=safe_crop(frame,vehicle["bbox"],padding=12).crop)
                    batch.append(event)
                self.geometry_sink(batch)
            if geometry.layout.use_for_visits:
                for event in events:
                    if event.event_type not in {"HOTEL_ENTRY", "HOTEL_EXIT"}:
                        continue
                    vehicle = _vehicle_by_track_id(tracked, event.track_id)
                    direction = "INWARD" if event.event_type == "HOTEL_ENTRY" else "OUTWARD"
                    crossing = NormalizedCrossing(
                        event.track_id, direction, bottom_center(vehicle["bbox"]), event.occurred_at
                    )
                    candidate = self._event_from_confirmation(
                        context, crossing, vehicle, frame, latest.sequence, width, height
                    )
                    self._publish_event(context, candidate)
                return
        self._process_passage_confirmations(context, tracked, frame, latest)

    def _process_passage_confirmations(self, context: CameraContext, tracked: list[dict], frame, latest) -> list[NormalizedCrossing]:
        if context.line_detector is None:
            return []
        frame_height, frame_width = frame.shape[:2]
        normalized_crossings: list[NormalizedCrossing] = []
        try:
            detector_cls_name = type(context.line_detector).__name__
            if detector_cls_name == "PassageCorridorDetector" or hasattr(context.line_detector, "points_for_frame"):
                for vehicle in tracked:
                    if vehicle.get("id") is None:
                        continue
                    _observation, confirmation = context.line_detector.update(
                        track_id=int(vehicle["id"]),
                        bbox=vehicle["bbox"],
                        frame_width=frame_width,
                        frame_height=frame_height,
                        captured_at=latest.captured_at,
                    )
                    if confirmation is not None:
                        normalized_crossings.append(_normalize_crossing(confirmation))
            else:
                crossings = context.line_detector.update(
                    tracked_objects=tracked,
                    frame_width=frame_width,
                    frame_height=frame_height,
                    captured_at=latest.captured_at,
                    frame_sequence=latest.sequence,
                )
                for crossing in crossings:
                    normalized_crossings.append(_normalize_crossing(crossing))
        except Exception as exc:  # noqa: BLE001
            context.last_error = f"detector: {type(exc).__name__}"
            logger.exception("camera=%s event=detector_error", context.camera_id)
            return []

        for normalized in normalized_crossings:
            vehicle = _vehicle_by_track_id(tracked, normalized.track_id)
            if vehicle is None:
                continue
            event = self._event_from_confirmation(
                context,
                normalized,
                vehicle,
                frame,
                latest.sequence,
                frame_width,
                frame_height,
            )
            self._publish_event(context, event)
        return normalized_crossings

    def _event_from_confirmation(
        self,
        context: CameraContext,
        confirmation: NormalizedCrossing,
        vehicle: dict,
        frame,
        source_sequence: int,
        frame_width: int,
        frame_height: int,
    ) -> EventCandidate:
        frame_copy = frame.copy() if config.EVENT_FRAME_COPY_ENABLED else None
        x1, y1, x2, y2 = [int(value) for value in vehicle["bbox"]]
        return EventCandidate(
            event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, repr((context.camera_id, confirmation.occurred_at.isoformat(),
                    confirmation.direction, (x1,y1,x2,y2), tuple(confirmation.track_point))))),
            camera_id=context.camera_id,
            camera_name=context.camera_name,
            track_id=confirmation.track_id,
            direction=confirmation.direction,
            occurred_at=confirmation.occurred_at,
            source_sequence=int(source_sequence),
            class_name=str(vehicle.get("class", "Desconocido")),
            class_id=int(vehicle.get("class_id", -1)),
            confidence=float(vehicle.get("confidence", 0.0)),
            bbox=(x1, y1, x2, y2),
            center=confirmation.track_point,
            frame_width=frame_width,
            frame_height=frame_height,
            frame=frame_copy,
            track_point=confirmation.track_point,
            corridor_progress=confirmation.corridor_progress,
            session_id=context.session_id,
            geometry_gate=bool(context.geometry_detector and context.geometry_detector.layout.use_for_visits),
        )

    def _publish_event(self, context: CameraContext, event: EventCandidate) -> None:
        try:
            self._event_queue.put_nowait(event)
        except queue.Full:
            with self._lock:
                self._event_queue_dropped += 1
                context.event_queue_dropped += 1
                context.last_error = "cola de eventos llena"
            logger.warning("camera=%s event=event_queue_full", context.camera_id)
            return
        with self._lock:
            context.events_published += 1
            context.last_event_direction = event.direction
            context.last_event_track_id = event.track_id

    def _handle_reconnect_if_needed(self, context: CameraContext, stream_status) -> None:
        reconnected = (
            stream_status.connected
            and (not context.last_stream_connected or stream_status.reconnect_count != context.last_reconnect_count)
        )
        sequence_reset = stream_status.frame_sequence < context.last_consumed_sequence
        
        loop_reset = False
        loop_count = getattr(stream_status, "loop_count", 0)
        if not hasattr(context, "last_loop_count"):
            context.last_loop_count = 0
        if loop_count != context.last_loop_count:
            loop_reset = True
            context.last_loop_count = loop_count

        if reconnected or sequence_reset or loop_reset:
            context.tracker.reset()
            if context.geometry_detector is not None:
                context.geometry_detector.reset()
                context.session_id = context.geometry_detector.session_id
            else:
                context.session_id = uuid.uuid4().hex
            if context.line_detector is not None:
                context.line_detector.reset()
            context.last_consumed_sequence = 0
            context.active_track_ids.clear()
            context.skipped_frames = 0
            context.processed_sequence = 0
            context.source_sequence = 0

        context.last_stream_connected = stream_status.connected
        context.last_reconnect_count = stream_status.reconnect_count

    def _annotate_frame(self, frame, context: CameraContext, tracked: list[dict], inference_ms: float):
        try:
            import cv2
        except ImportError:
            return frame
        annotated = self.detector.draw_detections(frame, tracked)
        fps = self._actual_fps(context, time.monotonic())
        self._draw_passage(annotated, context)
        if context.geometry_detector is not None:
            draw_features(annotated, context.geometry_detector.features)
        self._draw_centers(annotated, tracked)
        lines = [
            f"{context.camera_id} {context.camera_name}",
            f"Inferencia: {inference_ms:.0f} ms",
            f"Procesamiento: {fps:.1f} FPS",
            f"Eventos: {context.events_published}",
        ]
        if context.last_event_direction is not None:
            lines.append(f"Ultimo evento: {context.last_event_direction} ID {context.last_event_track_id}")
        y = 26
        try:
            for line in lines:
                cv2.putText(annotated, line, (16, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                y += 28
        except Exception:
            return annotated
        return annotated

    def _draw_passage(self, frame, context: CameraContext) -> None:
        if context.line_detector is None:
            return
        try:
            import cv2

            height, width = frame.shape[:2]
            if hasattr(context.line_detector, "points_for_frame"):
                points = context.line_detector.points_for_frame(width, height)
                for start, end in zip(points, points[1:]):
                    cv2.line(frame, start, end, (0, 255, 255), 2)
                    cv2.arrowedLine(frame, start, end, (0, 220, 255), 2, tipLength=0.18)
                trigger = context.line_detector.trigger_point_for_frame(width, height)
                cv2.circle(frame, trigger, 7, (0, 180, 255), -1)
                cv2.putText(
                    frame,
                    context.line_detector.event_direction,
                    trigger,
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (0, 220, 255),
                    2,
                )
            else:
                point_a, point_b = context.line_detector.line_points_for_frame(width, height)
                cv2.line(frame, point_a, point_b, (0, 255, 255), 2)
        except Exception:
            return

    @staticmethod
    def _draw_centers(frame, tracked: list[dict]) -> None:
        try:
            import cv2

            for vehicle in tracked:
                x1, y1, x2, y2 = vehicle["bbox"]
                center = (round((x1 + x2) / 2), round((y1 + y2) / 2))
                cv2.circle(frame, center, 4, (255, 255, 0), -1)
        except Exception:
            return

    def _camera_status(self, context: CameraContext, now: float) -> CameraCoordinatorStatus:
        stream_status = context.stream.get_status()
        avg = context.inference_ms_total / context.inference_count if context.inference_count else 0.0
        return CameraCoordinatorStatus(
            camera_id=context.camera_id,
            camera_name=context.camera_name,
            stream_connected=stream_status.connected,
            capture_fps=stream_status.capture_fps,
            target_inference_fps=context.target_fps,
            actual_inference_fps=self._actual_fps(context, now),
            inference_count=context.inference_count,
            last_inference_at=context.last_inference_at,
            inference_ms_last=context.inference_ms_last,
            inference_ms_average=avg,
            source_sequence=stream_status.frame_sequence,
            processed_sequence=context.processed_sequence,
            dropped_or_skipped_frames=context.skipped_frames,
            detections_count=context.detections_count,
            active_track_ids=tuple(context.active_track_ids),
            tracker_backend=context.tracker_backend,
            crossing_candidates=context.crossing_candidates,
            events_published=context.events_published,
            duplicate_events_suppressed=context.duplicate_events_suppressed,
            event_queue_dropped=context.event_queue_dropped,
            last_event_direction=context.last_event_direction,
            last_event_track_id=context.last_event_track_id,
            last_error=context.last_error or stream_status.last_error,
        )

    def _actual_fps(self, context: CameraContext, now: float) -> float:
        if context.inference_count <= 0:
            return 0.0
        self._prune_window(context.inference_times, now)
        if len(context.inference_times) < 2:
            return 0.0
        elapsed = context.inference_times[-1] - context.inference_times[0]
        return (len(context.inference_times) - 1) / elapsed if elapsed > 0 else 0.0

    def _record_loop(self, now: float) -> None:
        self._loop_times.append(now)
        self._prune_window(self._loop_times, now)

    def _loop_rate(self, now: float) -> float:
        self._prune_window(self._loop_times, now)
        if len(self._loop_times) < 2:
            return 0.0
        elapsed = self._loop_times[-1] - self._loop_times[0]
        return (len(self._loop_times) - 1) / elapsed if elapsed > 0 else 0.0

    def _prune_window(self, values: deque[float], now: float) -> None:
        cutoff = now - self.status_window_seconds
        while values and values[0] < cutoff:
            values.popleft()

    def _context_by_id(self, camera_id: str) -> CameraContext | None:
        for context in self.contexts:
            if context.camera_id == camera_id:
                return context
        return None


def _active_ids(tracker: Any, tracked: list[dict]) -> list[int]:
    if hasattr(tracker, "get_active_ids"):
        return list(tracker.get_active_ids())
    return sorted(vehicle["id"] for vehicle in tracked if vehicle.get("id") is not None)


def _passage_detector_from_camera(camera: CameraSettings) -> Any:
    # Si la línea virtual está habilitada, usar VirtualLineDetector
    if getattr(camera, "line", None) is not None and camera.line.enabled:
        return VirtualLineDetector(
            normalized_start=(camera.line.start_x, camera.line.start_y),
            normalized_end=(camera.line.end_x, camera.line.end_y),
            event_directions=camera.line.event_directions,
            line_enabled=camera.line.enabled,
            invert_direction=camera.line.invert_direction,
        )

    passage = camera.passage
    if passage is None:
        points = ((0.27, 0.98), (0.43, 0.83), (0.59, 0.69), (0.70, 0.58))
        event = "ENTRADA"
        trigger = 0.15
        enabled = True
        direction = "forward"
        half_width = config.PASSAGE_MAX_LATERAL_DISTANCE_NORMALIZED
    else:
        points = passage.points
        event = passage.event
        trigger = passage.trigger_progress
        enabled = passage.enabled
        direction = passage.direction
        half_width = passage.half_width
    return PassageCorridorDetector(
        points=points,
        half_width=half_width,
        event_direction=event,
        expected_direction=direction,
        trigger_progress=trigger,
        min_observations=config.PASSAGE_MIN_OBSERVATIONS,
        min_progress_delta=config.PASSAGE_MIN_PROGRESS_DELTA,
        max_lateral_distance=config.PASSAGE_MAX_LATERAL_DISTANCE_NORMALIZED,
        track_point=config.PASSAGE_TRACK_POINT,
        track_ttl_seconds=config.PASSAGE_TRACK_TTL_SECONDS,
        duplicate_cooldown_seconds=config.PASSAGE_DUPLICATE_COOLDOWN_SECONDS,
        enabled=enabled,
    )


def _vehicle_by_track_id(tracked: list[dict], track_id: int) -> dict | None:
    for vehicle in tracked:
        if vehicle.get("id") == track_id:
            return vehicle
    return None


def _line_metric(line_detector: Any, metric: str) -> int:
    if line_detector is None:
        return 0
    if metric == "confirmed_count" and hasattr(line_detector, "crossing_candidates_count"):
        return int(getattr(line_detector, "crossing_candidates_count", 0))
    return int(getattr(line_detector, metric, 0))
