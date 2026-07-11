"""
Deteccion_Carros — Captura de Evidencia (Fase 4)
=================================================
Guarda imágenes de evidencia cuando se genera un evento vehicular.
"""

import os
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

from config import CAPTURE_DIR, CAPTURE_ENABLED, PADDING_CAPTURE


class EvidenceCapture:
    """Genera recortes anotados de vehículos asociados a eventos."""

    def __init__(
        self,
        enabled: bool = CAPTURE_ENABLED,
        capture_dir: str = CAPTURE_DIR,
        padding: int = PADDING_CAPTURE,
    ):
        self.enabled = enabled
        self.capture_dir = capture_dir
        self.padding = padding

    def capture(self, frame: np.ndarray, vehicle: dict, event: dict) -> Optional[str]:
        """
        Guarda una imagen de evidencia para el evento recibido.

        Args:
            frame: Frame BGR actual, en las mismas coordenadas del bbox.
            vehicle: Detección estandarizada con id y bbox.
            event: Evento generado por VirtualLineDetector.

        Returns:
            Ruta de la imagen guardada o None si la captura está desactivada.
        """
        if not self.enabled:
            return None

        crop, relative_bbox = self._crop_vehicle(frame, vehicle["bbox"])
        annotated = self._draw_evidence(crop, relative_bbox, vehicle, event)
        filepath = self._build_filepath(event)

        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        if not cv2.imwrite(filepath, annotated):
            raise RuntimeError(f"No se pudo guardar evidencia: {filepath}")

        return filepath

    def _crop_vehicle(self, frame: np.ndarray, bbox: list):
        """Recorta el vehículo con padding y devuelve bbox relativo al recorte."""
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = [int(value) for value in bbox]

        crop_x1 = max(0, x1 - self.padding)
        crop_y1 = max(0, y1 - self.padding)
        crop_x2 = min(width, x2 + self.padding)
        crop_y2 = min(height, y2 + self.padding)

        crop = frame[crop_y1:crop_y2, crop_x1:crop_x2].copy()
        relative_bbox = [
            x1 - crop_x1,
            y1 - crop_y1,
            x2 - crop_x1,
            y2 - crop_y1,
        ]
        return crop, relative_bbox

    def _draw_evidence(
        self,
        image: np.ndarray,
        bbox: list,
        vehicle: dict,
        event: dict,
    ) -> np.ndarray:
        """Dibuja bbox, ID, tipo de evento y fecha/hora sobre el recorte."""
        annotated = image.copy()
        x1, y1, x2, y2 = bbox
        event_type = event["type"].upper()
        vehicle_id = event["vehicle_id"]
        timestamp = self._display_timestamp(event)

        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 255), 2)

        lines = [
            f"{event_type} ID:{vehicle_id}",
            timestamp,
            f"{vehicle.get('class', 'Vehiculo')} {vehicle.get('confidence', 0):.0%}",
        ]
        self._draw_text_panel(annotated, lines)
        return annotated

    def _draw_text_panel(self, image: np.ndarray, lines: list) -> None:
        """Dibuja un panel legible con la información del evento."""
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.55
        thickness = 1
        line_height = 22
        panel_height = 12 + (line_height * len(lines))
        panel_width = min(image.shape[1], 360)

        overlay = image.copy()
        cv2.rectangle(overlay, (0, 0), (panel_width, panel_height), (20, 20, 20), -1)
        cv2.addWeighted(overlay, 0.75, image, 0.25, 0, image)

        for index, line in enumerate(lines):
            y = 24 + (index * line_height)
            cv2.putText(
                image,
                line,
                (10, y),
                font,
                font_scale,
                (0, 255, 255) if index == 0 else (235, 235, 235),
                thickness,
                cv2.LINE_AA,
            )

    def _build_filepath(self, event: dict) -> str:
        """Construye data/captures/YYYY-MM-DD/tipo_IDx_timestamp.jpg."""
        timestamp = self._event_datetime(event)
        date_dir = timestamp.strftime("%Y-%m-%d")
        filename_timestamp = timestamp.strftime("%Y-%m-%d_%H-%M-%S")
        event_type = event["type"].lower()
        vehicle_id = event["vehicle_id"]
        filename = f"{event_type}_ID{vehicle_id}_{filename_timestamp}.jpg"
        return os.path.join(self.capture_dir, date_dir, filename)

    def _display_timestamp(self, event: dict) -> str:
        """Devuelve fecha y hora para mostrar en la evidencia."""
        return self._event_datetime(event).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _event_datetime(event: dict) -> datetime:
        """Convierte el timestamp del evento a datetime."""
        timestamp = event.get("timestamp")
        if not timestamp:
            return datetime.now()

        try:
            return datetime.fromisoformat(timestamp)
        except ValueError:
            return datetime.now()
