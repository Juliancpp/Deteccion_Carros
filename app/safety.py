from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class SafeCropResult:
    crop: np.ndarray | None
    bbox: tuple[int, int, int, int] | None
    reason: str | None = None


def safe_crop(frame: np.ndarray | None, bbox: Sequence[object], padding: int = 0) -> SafeCropResult:
    if frame is None:
        return SafeCropResult(None, None, "FRAME_NONE")
    if not isinstance(frame, np.ndarray) or frame.size == 0 or not hasattr(frame, "shape") or len(frame.shape) < 2:
        return SafeCropResult(None, None, "FRAME_EMPTY")
    height, width = frame.shape[:2]
    if height <= 0 or width <= 0:
        return SafeCropResult(None, None, "FRAME_BAD_SHAPE")
    try:
        x1, y1, x2, y2 = [int(round(float(value))) for value in bbox]
    except (TypeError, ValueError):
        return SafeCropResult(None, None, "BBOX_INVALID")
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    x1 -= padding
    y1 -= padding
    x2 += padding
    y2 += padding
    x1 = max(0, min(width, x1))
    y1 = max(0, min(height, y1))
    x2 = max(0, min(width, x2))
    y2 = max(0, min(height, y2))
    if x2 <= x1 or y2 <= y1:
        return SafeCropResult(None, None, "BBOX_ZERO_AREA")
    crop = frame[y1:y2, x1:x2].copy()
    if crop.size == 0:
        return SafeCropResult(None, None, "CROP_EMPTY")
    return SafeCropResult(crop, (x1, y1, x2, y2), None)
