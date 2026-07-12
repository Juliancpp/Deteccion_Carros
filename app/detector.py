"""
Deteccion_Carros — Detector de Vehículos
=========================================
Clase principal que encapsula la lógica de detección
vehicular usando YOLO11n optimizado para CPU.
"""

import os
import time
from dataclasses import dataclass, field
from typing import List, Tuple

import cv2
import numpy as np
try:
    from ultralytics import YOLO
except ImportError:
    YOLO = None

try:
    from app.config import (
        MODEL_PATH,
        CONFIDENCE_THRESHOLD,
        CLASSES_ALLOWED,
        CLASS_NAMES,
        CLASS_COLORS,
        INFERENCE_SIZE,
    )
except ImportError:
    from config import (  # type: ignore
        MODEL_PATH,
        CONFIDENCE_THRESHOLD,
        CLASSES_ALLOWED,
        CLASS_NAMES,
        CLASS_COLORS,
        INFERENCE_SIZE,
    )


@dataclass
class Detection:
    """Representa una detección individual de vehículo."""
    bbox: Tuple[int, int, int, int]  # (x1, y1, x2, y2)
    class_id: int
    class_name: str
    confidence: float


class VehicleDetector:
    """
    Detector de vehículos basado en YOLO11n.

    Optimizado para ejecución en CPU:
    - Usa modelo nano (yolo11n).
    - Inferencia en tamaño reducido.
    - Filtra solo clases vehiculares.

    Uso:
        detector = VehicleDetector()
        detections = detector.detect(frame)
    """

    model_load_count = 0

    def __init__(self):
        """Inicializa el detector cargando el modelo YOLO."""
        self._load_model()
        self._inference_time = 0.0

    def _load_model(self) -> None:
        """
        Carga el modelo YOLO11n.
        Si no existe localmente, Ultralytics lo descarga automáticamente.
        """
        # Asegurar que el directorio models/ existe
        if YOLO is None:
            raise RuntimeError("ultralytics no esta instalado; instala requirements.txt para usar YOLO")

        models_dir = os.path.dirname(MODEL_PATH)
        os.makedirs(models_dir, exist_ok=True)

        # Si no hay modelo local, usar el nombre para auto-descarga
        if os.path.exists(MODEL_PATH):
            model_source = MODEL_PATH
            print(f"[✓] Modelo cargado desde: {MODEL_PATH}")
        else:
            model_source = "yolo11n.pt"
            print(f"[↓] Modelo no encontrado en {MODEL_PATH}")
            print(f"[↓] Descargando yolo11n.pt automáticamente...")

        self.model = YOLO(model_source)
        VehicleDetector.model_load_count += 1

        # Forzar uso de CPU
        self.model.to("cpu")
        print(f"[✓] Detector inicializado en CPU")
        print(f"[✓] Umbral de confianza: {CONFIDENCE_THRESHOLD}")
        print(f"[✓] Tamaño de inferencia: {INFERENCE_SIZE}px")
        print(f"[✓] Clases: {list(CLASS_NAMES.values())}")
        print("─" * 50)

    def detect(self, frame: np.ndarray) -> List[dict]:
        """
        Procesa un frame y devuelve las detecciones vehiculares básicas.

        Args:
            frame: Imagen BGR de OpenCV (numpy array).

        Returns:
            Lista de diccionarios con las detecciones encontradas:
            [{"class": "Auto", "class_id": 2, "confidence": 0.86, "bbox": [x1, y1, x2, y2]}]
        """
        start = time.perf_counter()

        # Ejecutar inferencia YOLO básica
        results = self.model(
            frame,
            imgsz=INFERENCE_SIZE,
            conf=CONFIDENCE_THRESHOLD,
            classes=CLASSES_ALLOWED,
            verbose=False,
            device="cpu",
        )

        self._inference_time = (time.perf_counter() - start) * 1000  # ms

        detections = []
        for result in results:
            if result.boxes is None or len(result.boxes) == 0:
                continue

            boxes = result.boxes
            for i in range(len(boxes)):
                x1, y1, x2, y2 = boxes.xyxy[i].cpu().numpy().astype(int)
                class_id = int(boxes.cls[i].cpu().numpy())
                confidence = float(boxes.conf[i].cpu().numpy())
                class_name = CLASS_NAMES.get(class_id, "Desconocido")

                detections.append({
                    "class": class_name,
                    "class_id": class_id,
                    "confidence": confidence,
                    "bbox": [int(x1), int(y1), int(x2), int(y2)]
                })

        return detections

    def track(self, frame: np.ndarray):
        """
        Ejecuta YOLO11n con seguimiento de objetos (ByteTrack).

        Args:
            frame: Imagen BGR de OpenCV (numpy array).

        Returns:
            Resultados crudos de YOLO con IDs de tracking incorporados.
        """
        start = time.perf_counter()

        # Ejecutar inferencia YOLO con tracking
        results = self.model.track(
            frame,
            persist=True,
            tracker="bytetrack.yaml",
            imgsz=INFERENCE_SIZE,
            conf=CONFIDENCE_THRESHOLD,
            classes=CLASSES_ALLOWED,
            verbose=False,
            device="cpu",
        )

        self._inference_time = (time.perf_counter() - start) * 1000  # ms
        return results

    def draw_detections(
        self, frame: np.ndarray, detections: List[dict]
    ) -> np.ndarray:
        """
        Dibuja bounding boxes y etiquetas (con ID si está disponible) sobre el frame.

        Args:
            frame: Imagen BGR original.
            detections: Lista de diccionarios de detecciones/vehículos.

        Returns:
            Frame con las anotaciones dibujadas.
        """
        annotated = frame.copy()

        for det in detections:
            x1, y1, x2, y2 = det["bbox"]
            class_id = det.get("class_id", 2)
            color = CLASS_COLORS.get(class_id, (255, 255, 255))

            # Bounding box con grosor proporcional
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)

            # Etiqueta con ID (si existe) y confianza
            track_id = det.get("id")
            if track_id is not None:
                label = f"{det['class'].upper()} ID: {track_id} {det['confidence']:.0%}"
            else:
                label = f"{det['class'].upper()} {det['confidence']:.0%}"

            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.5
            thickness = 1
            (text_w, text_h), baseline = cv2.getTextSize(
                label, font, font_scale, thickness
            )

            # Fondo de la etiqueta
            cv2.rectangle(
                annotated,
                (x1, y1 - text_h - 10),
                (x1 + text_w + 8, y1),
                color,
                -1,
            )

            # Texto
            cv2.putText(
                annotated,
                label,
                (x1 + 4, y1 - 6),
                font,
                font_scale,
                (0, 0, 0),
                thickness,
                cv2.LINE_AA,
            )

        return annotated

    def draw_hud(
        self, frame: np.ndarray, fps: float, count: int, active_ids: List[int] = None
    ) -> np.ndarray:
        """
        Dibuja el HUD (heads-up display) con información del sistema.

        Args:
            frame: Frame anotado.
            fps: FPS real de procesamiento/video.
            count: Número de vehículos detectados/activos.
            active_ids: Lista de IDs activos en el frame.

        Returns:
            Frame con HUD superpuesto.
        """
        h, w = frame.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX

        # ── Panel superior semitransparente ──
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (w, 85), (30, 30, 30), -1)
        cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)

        # ── Título ──
        cv2.putText(
            frame,
            "DETECCION Y SEGUIMIENTO VEHICULAR",
            (15, 28),
            font,
            0.65,
            (0, 255, 200),
            2,
            cv2.LINE_AA,
        )

        # ── FPS Video ──
        fps_color = (0, 255, 0) if fps >= 15 else (0, 200, 255) if fps >= 8 else (0, 0, 255)
        cv2.putText(
            frame,
            f"FPS Video: {fps:.1f}",
            (w - 180, 28),
            font,
            0.65,
            fps_color,
            2,
            cv2.LINE_AA,
        )

        # ── Conteo de vehículos ──
        cv2.putText(
            frame,
            f"Vehiculos actuales: {count}",
            (15, 52),
            font,
            0.55,
            (200, 200, 200),
            1,
            cv2.LINE_AA,
        )

        # ── Tiempo de inferencia ──
        cv2.putText(
            frame,
            f"Inferencia: {self._inference_time:.0f}ms",
            (w - 180, 52),
            font,
            0.55,
            (200, 200, 200),
            1,
            cv2.LINE_AA,
        )

        # ── IDs activos en pantalla ──
        if active_ids is not None:
            ids_str = ", ".join(map(str, active_ids)) if active_ids else "Ninguno"
            # Limitar longitud para evitar desbordar pantalla
            if len(ids_str) > 40:
                ids_str = ids_str[:37] + "..."
            cv2.putText(
                frame,
                f"IDs activos: {ids_str}",
                (15, 74),
                font,
                0.5,
                (0, 180, 255),
                1,
                cv2.LINE_AA,
            )

        # ── Instrucción de salida ──
        cv2.putText(
            frame,
            "Presiona Q para salir",
            (15, h - 15),
            font,
            0.5,
            (120, 120, 120),
            1,
            cv2.LINE_AA,
        )

        return frame

    @property
    def inference_time_ms(self) -> float:
        """Tiempo de la última inferencia en milisegundos."""
        return self._inference_time

    @classmethod
    def get_model_load_count(cls) -> int:
        return cls.model_load_count
