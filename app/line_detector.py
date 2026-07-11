"""
Deteccion_Carros — Línea Virtual (Fase 3)
=========================================
Detecta cruces de una línea virtual usando el centro del bounding box
y el ID persistente entregado por ByteTrack.
"""

from datetime import datetime
import time
from typing import Dict, Optional, Tuple


class VirtualLineDetector:
    """
    Detecta eventos de entrada/salida cuando un vehículo cruza una línea.

    La detección usa el centro del bbox, compara la posición anterior contra
    la actual y evita eventos duplicados para el mismo ID.
    """

    def __init__(
        self,
        point_a: Tuple[int, int],
        point_b: Tuple[int, int],
        direction: str = "horizontal",
        invert_direction: bool = False,
        processed_id_ttl_minutes: int = 10,
    ):
        self.point_a = point_a
        self.point_b = point_b
        self.direction = direction.lower()
        self.invert_direction = invert_direction
        self.processed_id_ttl_seconds = processed_id_ttl_minutes * 60
        self.previous_centers: Dict[int, Tuple[float, float]] = {}
        self.processed_ids: Dict[int, Dict[str, float]] = {}
        self.events = []

    def reset(self) -> None:
        """Limpia el estado temporal de cruces y eventos."""
        self.previous_centers.clear()
        self.processed_ids.clear()
        self.events.clear()

    @staticmethod
    def get_center(vehicle: dict) -> Tuple[float, float]:
        """Calcula el centro del bounding box del vehículo."""
        x1, y1, x2, y2 = vehicle["bbox"]
        center_x = (x1 + x2) / 2
        center_y = (y1 + y2) / 2
        return center_x, center_y

    def update(self, vehicle: dict) -> Optional[dict]:
        """
        Actualiza la posición de un vehículo y genera evento si cruza la línea.

        Args:
            vehicle: Diccionario con al menos {"id": int, "bbox": [...]}.

        Returns:
            Evento generado o None si no hubo cruce.
        """
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
        ax, ay = self.point_a
        bx, by = self.point_b
        px, py = point
        return (bx - ax) * (py - ay) - (by - ay) * (px - ax)

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
