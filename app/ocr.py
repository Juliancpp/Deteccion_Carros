"""
Deteccion_Carros — OCR (Reconocimiento Óptico de Caracteres)
=============================================================
Lectura básica de placas vehiculares usando EasyOCR en CPU.
"""

import re
from typing import Optional

import cv2
import numpy as np

try:
    from app.config import OCR_ENABLED, OCR_LANGUAGES, OCR_MIN_CONFIDENCE
except ImportError:
    from config import OCR_ENABLED, OCR_LANGUAGES, OCR_MIN_CONFIDENCE  # type: ignore


class PlateReader:
    """Preprocesa recortes vehiculares y busca placas con formato Ecuador."""

    ECUADOR_PLATE_PATTERN = re.compile(r"^[A-Z]{3}[0-9]{4}$")

    def __init__(
        self,
        enabled: bool = OCR_ENABLED,
        languages: list = OCR_LANGUAGES,
        min_confidence: float = OCR_MIN_CONFIDENCE,
    ):
        self.enabled = enabled
        self.languages = languages
        self.min_confidence = min_confidence
        self.reader = None

        if self.enabled:
            self._load_reader()

    def _load_reader(self) -> None:
        """Inicializa EasyOCR en CPU."""
        try:
            import easyocr
        except ImportError:
            print("[OCR] EasyOCR no está instalado. OCR desactivado.")
            self.enabled = False
            return

        self.reader = easyocr.Reader(self.languages, gpu=False)
        print(f"[OCR] EasyOCR inicializado en CPU. Idiomas: {self.languages}")

    def read(self, vehicle_image: np.ndarray) -> Optional[dict]:
        """
        Intenta leer una placa desde la imagen recortada del vehículo.

        Returns:
            {"plate": "ABC1234", "confidence": 0.82} o None.
        """
        if not self.enabled or self.reader is None or vehicle_image is None:
            return None

        preprocessed = self.preprocess(vehicle_image)
        results = self.reader.readtext(preprocessed, detail=1, paragraph=False)
        return self._best_plate_result(results)

    def preprocess(self, image: np.ndarray) -> np.ndarray:
        """Aplica gris, contraste, resize y threshold básico."""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)

        height, width = enhanced.shape[:2]
        scale = 2 if width < 600 else 1
        resized = cv2.resize(
            enhanced,
            (width * scale, height * scale),
            interpolation=cv2.INTER_CUBIC,
        )

        blurred = cv2.GaussianBlur(resized, (3, 3), 0)
        _, threshold = cv2.threshold(
            blurred,
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        return threshold

    def _best_plate_result(self, results: list) -> Optional[dict]:
        """Selecciona la mejor placa válida encontrada por EasyOCR."""
        best = None

        for result in results:
            if len(result) < 3:
                continue

            _bbox, text, confidence = result
            confidence = float(confidence)
            if confidence < self.min_confidence:
                continue

            plate = self.clean_text(text)
            if not self.is_valid_ecuador_plate(plate):
                continue

            if best is None or confidence > best["confidence"]:
                best = {"plate": plate, "confidence": confidence}

        return best

    @staticmethod
    def clean_text(text: str) -> str:
        """Normaliza a mayúsculas y deja sólo caracteres alfanuméricos."""
        return re.sub(r"[^A-Z0-9]", "", text.upper())

    @classmethod
    def is_valid_ecuador_plate(cls, text: str) -> bool:
        """Valida formato básico Ecuador: AAA0000."""
        return bool(cls.ECUADOR_PLATE_PATTERN.match(text))
