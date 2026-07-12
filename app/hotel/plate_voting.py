from __future__ import annotations

import re
from collections import defaultdict

PLATE_PATTERN = re.compile(r"^[A-Z]{3}[0-9]{4}$")


def normalize_plate(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def is_plausible_plate(text: str) -> bool:
    return bool(PLATE_PATTERN.match(normalize_plate(text)))


def vote_plate(candidates: list[dict], min_confidence: float = 0.55) -> dict:
    scores: dict[str, float] = defaultdict(float)
    normalized_candidates = []
    for candidate in candidates:
        plate = normalize_plate(str(candidate.get("plate", "")))
        confidence = float(candidate.get("confidence", 0.0))
        if not is_plausible_plate(plate):
            continue
        normalized_candidates.append({"plate": plate, "confidence": confidence})
        scores[plate] += confidence
    if not scores:
        return {"plate": None, "confidence": 0.0, "status": "NO_LEGIBLE", "candidates": normalized_candidates}
    plate, score = max(scores.items(), key=lambda item: item[1])
    count = sum(1 for item in normalized_candidates if item["plate"] == plate)
    confidence = min(1.0, score / max(1, count))
    if confidence >= min_confidence and count >= 1:
        status = "LEIDA" if count >= 2 or confidence >= 0.75 else "PROBABLE"
        return {"plate": plate, "confidence": confidence, "status": status, "candidates": normalized_candidates}
    return {"plate": None, "confidence": confidence, "status": "NO_LEGIBLE", "candidates": normalized_candidates}
