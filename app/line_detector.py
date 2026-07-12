"""
Deteccion_Carros — Línea Virtual (Fase 3)
=========================================
Detecta cruces de una línea virtual usando el centro del bounding box
y el ID persistente entregado por ByteTrack.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class LineCrossing:
    track_id: int
    direction: str
    previous_side: int
    current_side: int
    center: tuple[int, int]
    crossed_at: datetime


class VirtualLineDetector:
    """
    Detecta eventos de entrada/salida cuando un vehículo cruza una línea.

    La detección usa el centro del bbox, compara la posición anterior contra
    la actual y evita eventos duplicados para el mismo ID.
    """

    def __init__(
        self,
        point_a: Tuple[int, int] | None = None,
        point_b: Tuple[int, int] | None = None,
        direction: str = "horizontal",
        invert_direction: bool = False,
        processed_id_ttl_minutes: int = 10,
        *,
        line_enabled: bool = True,
        normalized_start: tuple[float, float] | None = None,
        normalized_end: tuple[float, float] | None = None,
        event_directions: str = "both",
        track_ttl_seconds: float | None = None,
        duplicate_cooldown_seconds: float = 5.0,
        time_fn=time.time,
    ):
        self.point_a = point_a or (0, 0)
        self.point_b = point_b or (1, 0)
        self.direction = direction.lower()
        self.invert_direction = invert_direction
        self.processed_id_ttl_seconds = processed_id_ttl_minutes * 60
        self.line_enabled = line_enabled
        self.normalized_start = normalized_start
        self.normalized_end = normalized_end
        self.event_directions = event_directions.lower()
        self.track_ttl_seconds = track_ttl_seconds if track_ttl_seconds is not None else self.processed_id_ttl_seconds
        self.duplicate_cooldown_seconds = duplicate_cooldown_seconds
        self._time_fn = time_fn
        self.previous_centers: Dict[int, Tuple[float, float]] = {}
        self.previous_sides: Dict[int, int] = {}
        self.last_seen: Dict[int, float] = {}
        self.cooldowns: Dict[tuple[int, str], float] = {}
        self.processed_ids: Dict[int, Dict[str, float]] = {}
        self.events = []
        self.crossing_candidates_count = 0
        self.duplicate_suppressed_count = 0

    def reset(self) -> None:
        """Limpia el estado temporal de cruces y eventos."""
        self.previous_centers.clear()
        self.previous_sides.clear()
        self.last_seen.clear()
        self.cooldowns.clear()
        self.processed_ids.clear()
        self.events.clear()
        self.crossing_candidates_count = 0
        self.duplicate_suppressed_count = 0

    @staticmethod
    def get_center(vehicle: dict) -> Tuple[float, float]:
        """Calcula el centro del bounding box del vehículo."""
        x1, y1, x2, y2 = vehicle["bbox"]
        center_x = (x1 + x2) / 2
        center_y = (y1 + y2) / 2
        return center_x, center_y

    def update(
        self,
        vehicle: dict | None = None,
        *,
        tracked_objects: list[dict] | None = None,
        frame_width: int | None = None,
        frame_height: int | None = None,
        captured_at: datetime | None = None,
        frame_sequence: int | None = None,
    ) -> Optional[dict] | list[LineCrossing]:
        """
        Actualiza la posición de un vehículo y genera evento si cruza la línea.

        Args:
            vehicle: Diccionario con al menos {"id": int, "bbox": [...]}.

        Returns:
            Evento generado o None si no hubo cruce.
        """
        if tracked_objects is not None:
            if frame_width is None or frame_height is None:
                raise ValueError("frame_width y frame_height son requeridos para tracked_objects")
            return self.update_tracked_objects(
                tracked_objects,
                frame_width=frame_width,
                frame_height=frame_height,
                captured_at=captured_at,
                frame_sequence=frame_sequence,
            )
        if vehicle is None:
            return None

        vehicle_id = vehicle.get("id")
        if vehicle_id is None:
            return None

        self._cleanup_processed_ids()

        current_center = self.get_center(vehicle)
        previous_center = self.previous_centers.get(vehicle_id)
        self.previous_centers[vehicle_id] = current_center

        if previous_center is None or vehicle_id in self.processed_ids:
            return None

        if not self._crossed_line(previous_center, current_center):
            return None

        event_type = self._movement_direction(previous_center, current_center)
        event = {
            "vehicle_id": vehicle_id,
            "type": event_type.lower(),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        self.processed_ids[vehicle_id] = {"time": time.time()}
        self.events.append(event)
        return event

    def update_tracked_objects(
        self,
        tracked_objects: list[dict],
        *,
        frame_width: int,
        frame_height: int,
        captured_at: datetime | None = None,
        frame_sequence: int | None = None,
    ) -> list[LineCrossing]:
        """Actualiza todos los tracks de una cámara y devuelve cruces nuevos."""
        del frame_sequence
        if not self.line_enabled:
            return []

        self._cleanup_multicamera_state()
        crossings: list[LineCrossing] = []
        crossed_at = captured_at or datetime.now(timezone.utc)
        for vehicle in tracked_objects:
            crossing = self._update_tracked_vehicle(
                vehicle,
                frame_width=frame_width,
                frame_height=frame_height,
                crossed_at=crossed_at,
            )
            if crossing is not None:
                crossings.append(crossing)
        return crossings

    def line_points_for_frame(self, frame_width: int, frame_height: int) -> tuple[tuple[int, int], tuple[int, int]]:
        """Convierte la línea configurada a coordenadas de píxel para este frame."""
        if self.normalized_start is None or self.normalized_end is None:
            return self.point_a, self.point_b
        start = (
            round(self.normalized_start[0] * frame_width),
            round(self.normalized_start[1] * frame_height),
        )
        end = (
            round(self.normalized_end[0] * frame_width),
            round(self.normalized_end[1] * frame_height),
        )
        return start, end

    def _update_tracked_vehicle(
        self,
        vehicle: dict,
        *,
        frame_width: int,
        frame_height: int,
        crossed_at: datetime,
    ) -> LineCrossing | None:
        track_id = vehicle.get("id")
        if track_id is None:
            return None

        track_id = int(track_id)
        now = self._time_fn()
        center_float = self.get_center(vehicle)
        center = (round(center_float[0]), round(center_float[1]))
        side = self._side_for_frame(center_float, frame_width, frame_height)
        self.last_seen[track_id] = now

        if side == 0:
            return None

        previous_side = self.previous_sides.get(track_id)
        self.previous_centers[track_id] = center_float
        self.previous_sides[track_id] = side

        if previous_side is None or previous_side == side:
            return None

        direction = self._direction_from_side_change(previous_side, side)
        self.crossing_candidates_count += 1
        if not self._direction_allowed(direction):
            return None
        if self._is_duplicate(track_id, direction, now):
            self.duplicate_suppressed_count += 1
            return None

        self.cooldowns[(track_id, direction)] = now
        return LineCrossing(
            track_id=track_id,
            direction=direction,
            previous_side=previous_side,
            current_side=side,
            center=center,
            crossed_at=crossed_at,
        )

    def _side_for_frame(self, point: Tuple[float, float], frame_width: int, frame_height: int) -> int:
        point_a, point_b = self.line_points_for_frame(frame_width, frame_height)
        value = self._signed_side_for_points(point, point_a, point_b)
        if value > 0:
            return 1
        if value < 0:
            return -1
        return 0

    @staticmethod
    def _signed_side_for_points(
        point: Tuple[float, float],
        point_a: tuple[int, int],
        point_b: tuple[int, int],
    ) -> float:
        ax, ay = point_a
        bx, by = point_b
        px, py = point
        return (bx - ax) * (py - ay) - (by - ay) * (px - ax)

    def _direction_from_side_change(self, previous_side: int, current_side: int) -> str:
        direction = "ENTRADA" if previous_side < current_side else "SALIDA"
        if self.invert_direction:
            direction = "SALIDA" if direction == "ENTRADA" else "ENTRADA"
        return direction

    def _direction_allowed(self, direction: str) -> bool:
        if self.event_directions == "both":
            return True
        return self.event_directions == direction.lower()

    def _is_duplicate(self, track_id: int, direction: str, now: float) -> bool:
        last = self.cooldowns.get((track_id, direction))
        return last is not None and now - last < self.duplicate_cooldown_seconds

    def _cleanup_multicamera_state(self) -> None:
        now = self._time_fn()
        expired_tracks = [
            track_id
            for track_id, last_seen in self.last_seen.items()
            if now - last_seen > self.track_ttl_seconds
        ]
        for track_id in expired_tracks:
            self.last_seen.pop(track_id, None)
            self.previous_sides.pop(track_id, None)
            self.previous_centers.pop(track_id, None)

        expired_cooldowns = [
            key
            for key, last_time in self.cooldowns.items()
            if now - last_time > self.duplicate_cooldown_seconds
        ]
        for key in expired_cooldowns:
            self.cooldowns.pop(key, None)

    def _cleanup_processed_ids(self) -> None:
        """Elimina IDs procesados que superaron el tiempo de retención."""
        if self.processed_id_ttl_seconds <= 0:
            return

        now = time.time()
        expired_ids = [
            vehicle_id
            for vehicle_id, data in self.processed_ids.items()
            if now - data["time"] > self.processed_id_ttl_seconds
        ]

        for vehicle_id in expired_ids:
            self.processed_ids.pop(vehicle_id, None)

    def _crossed_line(
        self,
        previous_center: Tuple[float, float],
        current_center: Tuple[float, float],
    ) -> bool:
        """Determina si el centro cruzó la línea virtual entre dos frames."""
        prev_side = self._signed_side(previous_center)
        current_side = self._signed_side(current_center)

        if prev_side == 0 or current_side == 0:
            crossed = prev_side != current_side
        else:
            crossed = (prev_side < 0 < current_side) or (prev_side > 0 > current_side)

        return crossed and self._within_line_span(previous_center, current_center)

    def _signed_side(self, point: Tuple[float, float]) -> float:
        """Devuelve el lado firmado de un punto respecto a la línea A-B."""
        return self._signed_side_for_points(point, self.point_a, self.point_b)

    def _within_line_span(
        self,
        previous_center: Tuple[float, float],
        current_center: Tuple[float, float],
    ) -> bool:
        """Valida que el cruce ocurra dentro del segmento configurado."""
        ax, ay = self.point_a
        bx, by = self.point_b
        px = (previous_center[0] + current_center[0]) / 2
        py = (previous_center[1] + current_center[1]) / 2

        if self.direction == "horizontal":
            return min(ax, bx) <= px <= max(ax, bx)

        if self.direction == "vertical":
            return min(ay, by) <= py <= max(ay, by)

        dx = bx - ax
        dy = by - ay
        length_sq = dx * dx + dy * dy
        if length_sq == 0:
            return False

        projection = ((px - ax) * dx + (py - ay) * dy) / length_sq
        return 0 <= projection <= 1

    def _movement_direction(
        self,
        previous_center: Tuple[float, float],
        current_center: Tuple[float, float],
    ) -> str:
        """Clasifica el movimiento como ENTRADA o SALIDA."""
        if self.direction == "vertical":
            moving_positive = current_center[0] > previous_center[0]
        else:
            moving_positive = current_center[1] > previous_center[1]

        event_type = "ENTRADA" if moving_positive else "SALIDA"
        if self.invert_direction:
            event_type = "SALIDA" if event_type == "ENTRADA" else "ENTRADA"
        return event_type
