from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

PLATE_PATTERN = re.compile(r"^[A-Z]{3}[0-9]{3,4}$")
LETTER_CORRECTIONS = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B", "6": "G"})
NUMBER_CORRECTIONS = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "G": "6", "Z": "2"})


def normalize_plate(text: str) -> str:
    """Conserva solo A-Z/0-9 en mayusculas, sin insertar ni borrar para forzar formato."""
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def correct_plate_characters(text: str) -> str:
    cleaned = normalize_plate(text)
    if len(cleaned) < 6 or len(cleaned) > 7:
        return cleaned
    letters = cleaned[:3].translate(LETTER_CORRECTIONS)
    numbers = cleaned[3:].translate(NUMBER_CORRECTIONS)
    return letters + numbers


def is_plausible_plate(text: str) -> bool:
    return bool(PLATE_PATTERN.match(correct_plate_characters(text)))


def vote_plate(candidates: list[dict], min_confidence: float = 0.55) -> dict:
    result = vote_plates_multiframe(candidates)
    if result["confidence"] < min_confidence and result["status"] == "PROBABLE":
        result = dict(result)
        result["plate"] = None
        result["confidence"] = 0.0
        result["status"] = "NO_LEGIBLE"
        result["review_required"] = True
        result["review_reasons"] = tuple([*result.get("review_reasons", ()), "below_min_confidence"])
    return result


def vote_plates_multiframe(candidates: list[dict[str, Any]], *, observed_only=False) -> dict[str, Any]:
    """
    Votacion estricta por frames fisicos.

    Solo se permite una contribucion principal por (placa, frame_id). Crops y
    variantes del mismo frame pueden mejorar la confianza de ese frame, pero no
    cuentan como consenso temporal independiente.
    """
    normalized = [_normalize_candidate(candidate, index) for index, candidate in enumerate(candidates)]
    if observed_only:
        for source, item in zip(candidates, normalized):
            if source.get("frame_id") is None:
                item["frame_id"] = "unknown_frame"
            observed = item["sanitized_text"]
            item.update(plate=observed, normalized_text=observed,
                        valid_format=bool(PLATE_PATTERN.fullmatch(observed)) and
                        bool(re.fullmatch(r"[A-Za-z0-9\s-]+", item["raw_text"])))
    valid = [candidate for candidate in normalized if candidate["valid_format"]]

    if observed_only and not valid and normalized:
        useful = [c for c in normalized if c["sanitized_text"]]
        if useful:
            best = max(useful, key=_candidate_weight)
            return _result(partial_text=best["raw_text"], plate=None, confidence=_candidate_weight(best), status="PARCIAL",
                           candidates=normalized, supporting_frame_ids=(best["frame_id"],),
                           review_reasons=("incomplete_observation",))
    if not valid:
        return _result(
            plate=None,
            confidence=0.0,
            status="NO_LEGIBLE",
            candidates=normalized or candidates,
            review_required=True,
            review_reasons=("no_valid_plate_format",),
        )

    cameras = {candidate["camera_id"] for candidate in valid if candidate.get("camera_id")}
    if len(cameras) > 1:
        return _result(
            plate=None,
            confidence=0.0,
            status="NO_LEGIBLE",
            candidates=normalized,
            review_required=True,
            review_reasons=("mixed_cameras",),
        )

    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    crop_ids_by_plate: dict[str, set[str]] = defaultdict(set)
    for candidate in valid:
        plate = candidate["normalized_text"]
        frame_id = candidate["frame_id"]
        crop_ids_by_plate[plate].add(candidate["crop_id"])
        weighted = _candidate_weight(candidate)
        current = grouped[plate].get(frame_id)
        if current is None or weighted > current["_weight"]:
            enriched = dict(candidate)
            enriched["_weight"] = weighted
            grouped[plate][frame_id] = enriched

    scored = []
    for plate, by_frame in grouped.items():
        frame_votes = list(by_frame.values())
        weights = [vote["_weight"] for vote in frame_votes]
        combined = sum(weights) / len(weights)
        if observed_only:
            # Discount single-frame hypotheses when competing with repeated observations.
            combined *= 1.0 if len(weights) >= 2 else 0.90
        scored.append(
            {
                "plate": plate,
                "score": combined,
                "unique_frame_ids": tuple(sorted(by_frame)),
                "unique_crop_ids": tuple(sorted(crop_ids_by_plate[plate])),
                "frame_votes": frame_votes,
            }
        )
    scored.sort(key=lambda item: item["score"], reverse=True)

    winner = scored[0]
    runner = scored[1] if len(scored) > 1 else None
    runner_score = float(runner["score"]) if runner else 0.0
    vote_margin = float(winner["score"] - runner_score)
    unique_frames = len(winner["unique_frame_ids"])
    contradiction = runner is not None and runner_score >= 0.40 and vote_margin < 0.15
    review_reasons: list[str] = []

    strong_frames = sum(v["_weight"] >= 0.55 for v in winner["frame_votes"])
    if unique_frames >= 2 and (not observed_only or strong_frames >= 2) and winner["score"] >= 0.65 and vote_margin >= 0.15 and not contradiction:
        status = "LEIDA"
        review_required = False
        review_reasons.append("independent_exact_agreement")
    elif winner["score"] >= 0.40:
        status = "PROBABLE"
        review_required = True
        if unique_frames < 2:
            review_reasons.append("single_physical_frame")
        if vote_margin < 0.15 and runner is not None:
            review_reasons.append("weak_vote_margin")
        if contradiction:
            review_reasons.append("contradictory_candidates")
    elif observed_only:
        status = "PROBABLE"
        review_required = True
        review_reasons.append("low_confidence")
    else:
        status = "NO_LEGIBLE"
        review_required = True
        review_reasons.append("low_confidence")

    plate = winner["plate"] if status != "NO_LEGIBLE" else None
    return _result(
        plate=plate,
        confidence=float(winner["score"] if plate else 0.0),
        status=status,
        candidates=normalized,
        runner_up_plate=runner["plate"] if runner else None,
        runner_up_confidence=runner_score,
        vote_margin=vote_margin,
        supporting_frame_ids=winner["unique_frame_ids"] if plate else (),
        supporting_crop_ids=winner["unique_crop_ids"] if plate else (),
        unique_frames_supporting=unique_frames if plate else 0,
        review_required=review_required,
        review_reasons=tuple(review_reasons),
    )


def _normalize_candidate(candidate: dict[str, Any], index: int) -> dict[str, Any]:
    raw = str(candidate.get("raw_text") or candidate.get("plate") or candidate.get("text") or "")
    sanitized = normalize_plate(raw)
    normalized = correct_plate_characters(sanitized)
    frame_id = str(candidate.get("frame_id") or f"legacy_frame_{index}")
    crop_type = str(candidate.get("crop_type") or candidate.get("crop_label") or "unknown")
    variant_type = str(candidate.get("variant_type") or candidate.get("variant") or "original")
    crop_id = str(candidate.get("crop_id") or f"{frame_id}:{crop_type}")
    confidence = _bounded_float(candidate.get("ocr_confidence", candidate.get("confidence", 0.0)))
    frame_quality = _bounded_float(candidate.get("frame_quality", candidate.get("frame_score", 1.0)))
    crop_quality = _bounded_float(candidate.get("crop_quality", 1.0))
    return {
        **candidate,
        "raw_text": raw,
        "sanitized_text": sanitized,
        "normalized_text": normalized,
        "plate": normalized,
        "confidence": confidence,
        "ocr_confidence": confidence,
        "frame_quality": frame_quality,
        "crop_quality": crop_quality,
        "frame_id": frame_id,
        "crop_id": crop_id,
        "crop_type": crop_type,
        "variant_type": variant_type,
        "camera_id": candidate.get("camera_id"),
        "track_id": candidate.get("track_id"),
        "logical_vehicle_id": candidate.get("logical_vehicle_id"),
        "source_sequence": candidate.get("source_sequence"),
        "captured_at": candidate.get("captured_at"),
        "valid_format": bool(PLATE_PATTERN.match(normalized)),
    }


def _candidate_weight(candidate: dict[str, Any]) -> float:
    confidence = candidate["ocr_confidence"]
    frame_quality = min(1.5, max(0.0, candidate["frame_quality"]))
    crop_quality = min(1.5, max(0.0, candidate["crop_quality"]))
    return confidence * (0.70 + 0.20 * min(frame_quality, 1.0) + 0.10 * min(crop_quality, 1.0))


def _bounded_float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _result(
    *,
    plate: str | None,
    confidence: float,
    status: str,
    candidates: list[dict[str, Any]],
    partial_text: str | None = None,
    runner_up_plate: str | None = None,
    runner_up_confidence: float = 0.0,
    vote_margin: float = 0.0,
    supporting_frame_ids: tuple[str, ...] = (),
    supporting_crop_ids: tuple[str, ...] = (),
    unique_frames_supporting: int = 0,
    review_required: bool = True,
    review_reasons: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "plate": plate,
        "plate_text": plate,
        "partial_text": partial_text,
        "confidence": confidence,
        "plate_confidence": confidence,
        "status": status,
        "plate_status": status,
        "candidates": candidates,
        "runner_up_plate": runner_up_plate,
        "runner_up_confidence": runner_up_confidence,
        "vote_margin": vote_margin,
        "supporting_frame_ids": supporting_frame_ids,
        "supporting_crop_ids": supporting_crop_ids,
        "unique_frames_supporting": unique_frames_supporting,
        "review_required": review_required,
        "review_reasons": review_reasons,
    }
