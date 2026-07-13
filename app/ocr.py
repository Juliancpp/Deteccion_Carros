"""
Deteccion_Carros — OCR (Reconocimiento Óptico de Caracteres)
=============================================================
Lectura robusta de placas vehiculares usando EasyOCR en CPU con localización por niveles y preprocesamiento múltiple.
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

    ECUADOR_PLATE_PATTERN = re.compile(r"^[A-Z]{3}[0-9]{3,4}$")

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
        Intenta leer una placa desde la imagen recortada del vehículo (API clásica).

        Returns:
            {"plate": "ABC1234", "confidence": 0.82} o None.
        """
        candidates = self.read_all_candidates(vehicle_image)
        if not candidates:
            return None
        candidates.sort(key=lambda x: x["confidence"], reverse=True)
        return {
            "plate": candidates[0]["plate"],
            "confidence": candidates[0]["confidence"]
        }

    def read_all_candidates(self, vehicle_image: np.ndarray, frame_id=None) -> list[dict]:
        """
        Busca todas las posibles placas válidas aplicando localización multinivel
        y variantes de preprocesamiento.
        """
        if not self.enabled or self.reader is None or vehicle_image is None or vehicle_image.size == 0:
            return []

        h, w = vehicle_image.shape[:2]
        crops = []

        # Nivel 4: Imagen completa (fallback)
        crops.append(("full", vehicle_image))

        # Nivel 1: Mitad inferior
        if h > 40:
            crops.append(("bottom", vehicle_image[h // 2 :, :]))

        # Nivel 2: Mitad inferior central
        if h > 40 and w > 50:
            crops.append(("bottom_center", vehicle_image[h // 2 :, w // 5 : 4 * w // 5]))

        # Nivel 3: Recortes basados en contornos rectangulares
        contour_crops = self._get_contour_crops(vehicle_image)
        for i, cc in enumerate(contour_crops[:3]):  # Limitar a los mejores 3 contornos
            crops.append((f"contour_{i}", cc))

        found = []
        seen = set()

        for label, crop in crops:
            if crop.size == 0 or crop.shape[0] < 8 or crop.shape[1] < 15:
                continue

            variants = self._get_preprocessing_variants(crop)
            for v_name, prep_img in variants:
                try:
                    results = self.reader.readtext(prep_img, detail=1, paragraph=False)
                    for res in results:
                        if len(res) < 3:
                            continue
                        _bbox, text, confidence = res
                        confidence = float(confidence)
                        if confidence < self.min_confidence:
                            continue

                        plate = self.clean_text(text)
                        if self.is_valid_ecuador_plate(plate):
                            key = (plate, label, v_name)
                            if key not in seen:
                                seen.add(key)
                                found.append({
                                    "plate": plate,
                                    "confidence": confidence,
                                    "crop_label": label,
                                    "variant": v_name,
                                    "frame_id": frame_id
                                })
                except Exception:
                    pass
        return found

    def _get_contour_crops(self, image: np.ndarray) -> list[np.ndarray]:
        crops = []
        try:
            for candidate in locate_plate_candidates(image):
                if candidate["crop_type"].startswith("contour"):
                    x1, y1, x2, y2 = candidate["crop_bbox"]
                    crops.append(image[y1:y2, x1:x2].copy())
        except Exception:
            pass
        return crops

    def _get_preprocessing_variants(self, crop: np.ndarray) -> list[tuple[str, np.ndarray]]:
        variants = []
        try:
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape[:2]
            scale = 2 if w < 300 else 1
            if scale > 1:
                gray = cv2.resize(gray, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)

            # Variante 1: CLAHE básica
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            enhanced = clahe.apply(gray)
            variants.append(("clahe", enhanced))

            # Variante 2: Otsu threshold
            blurred = cv2.GaussianBlur(enhanced, (3, 3), 0)
            _, thresh_otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            variants.append(("otsu", thresh_otsu))

            # Variante 3: Adaptive threshold
            thresh_adapt = cv2.adaptiveThreshold(
                blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
            )
            variants.append(("adaptive", thresh_adapt))
        except Exception:
            pass
        return variants

    def preprocess(self, image: np.ndarray) -> np.ndarray:
        """Aplica gris, contraste, resize y threshold básico (API clásica/fallback)."""
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

    @staticmethod
    def clean_text(text: str) -> str:
        """Normaliza a mayúsculas y deja sólo caracteres alfanuméricos."""
        return re.sub(r"[^A-Z0-9]", "", text.upper())

    @classmethod
    def is_valid_ecuador_plate(cls, text: str) -> bool:
        """Valida formato básico Ecuador: AAA0000 o AAA000."""
        return bool(cls.ECUADOR_PLATE_PATTERN.match(text))


def locate_plate_candidates(vehicle_crop: np.ndarray, max_candidates: int = 3) -> list[dict]:
    """
    Localizador CPU ligero de regiones candidatas de placa dentro de un recorte vehicular.

    No ejecuta OCR. Devuelve metadatos y crops por coordenadas relativas al crop
    del vehiculo para que todos los derivados conserven el mismo frame_id externo.
    """
    if vehicle_crop is None or vehicle_crop.size == 0:
        return []
    height, width = vehicle_crop.shape[:2]
    candidates: list[dict] = []

    def add_candidate(crop_type: str, bbox: tuple[int, int, int, int], base_score: float) -> None:
        x1, y1, x2, y2 = _clip_bbox(bbox, width, height)
        if x2 <= x1 or y2 <= y1:
            return
        crop = vehicle_crop[y1:y2, x1:x2]
        if crop.shape[0] < 10 or crop.shape[1] < 25:
            return
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        _mean, std = cv2.meanStdDev(gray)
        contrast = float(std[0][0]) / 128.0
        quality = min(1.0, base_score + min(0.35, sharpness / 1200.0) + min(0.25, contrast * 0.25))
        candidates.append(
            {
                "crop_type": crop_type,
                "crop_bbox": (x1, y1, x2, y2),
                "width": x2 - x1,
                "height": y2 - y1,
                "sharpness": sharpness,
                "crop_quality": quality,
            }
        )

    if height >= 40:
        add_candidate("lower_half", (0, height // 2, width, height), 0.35)
    if height >= 40 and width >= 50:
        add_candidate("lower_center", (width // 5, height // 2, 4 * width // 5, height), 0.45)

    try:
        gray = cv2.cvtColor(vehicle_crop, cv2.COLOR_BGR2GRAY)
        lower = gray[height // 3 :, :]
        y_offset = height // 3
        grad_x = cv2.Sobel(lower, cv2.CV_8U, 1, 0, ksize=3)
        _, thresh = cv2.threshold(grad_x, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (17, 3))
        closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contour_candidates = []
        for contour in contours:
            x, y, cw, ch = cv2.boundingRect(contour)
            aspect = cw / float(ch or 1)
            area_ratio = (cw * ch) / max(1, width * height)
            if 2.0 <= aspect <= 5.5 and cw >= 35 and ch >= 10 and 0.001 <= area_ratio <= 0.12:
                contour_candidates.append((cw * ch, (x - 5, y + y_offset - 5, x + cw + 5, y + y_offset + ch + 5)))
        contour_candidates.sort(reverse=True)
        for index, (_area, bbox) in enumerate(contour_candidates[:max_candidates]):
            add_candidate(f"contour_{index}", bbox, 0.55)
    except Exception:
        pass

    add_candidate("full_vehicle", (0, 0, width, height), 0.15)
    candidates.sort(key=lambda item: item["crop_quality"], reverse=True)
    return candidates[:max_candidates]


def _clip_bbox(bbox: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    return max(0, x1), max(0, y1), min(width, x2), min(height, y2)
