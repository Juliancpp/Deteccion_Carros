#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
MANIFEST = BASE_DIR / "data" / "validation" / "plates" / "manifest.csv"


def parse_args():
    parser = argparse.ArgumentParser(description="Evalua resultados de placas contra ground truth manual")
    parser.add_argument("--manifest", default=str(MANIFEST))
    parser.add_argument("--predictions", required=False, help="CSV con sample_id,plate_text,plate_status")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = Path(args.manifest)
    if not manifest.exists():
        print(f"[PLATE_EVAL] No existe manifest: {manifest}")
        return 1
    rows = list(csv.DictReader(manifest.open(encoding="utf-8")))
    labeled = [row for row in rows if _clean(row.get("true_plate")) or row.get("expected_status")]
    if not labeled:
        print("[PLATE_EVAL] No hay ground truth etiquetado. No se evaluan datos dummy.")
        return 1
    predictions = _load_predictions(Path(args.predictions)) if args.predictions else {}
    stats = {
        "total": 0,
        "exact_match": 0,
        "LEIDA_correcta": 0,
        "LEIDA_incorrecta": 0,
        "PROBABLE_correcta": 0,
        "PROBABLE_incorrecta": 0,
        "NO_LEGIBLE_correcta": 0,
        "false_plate_rate": 0,
    }
    char_total = 0
    char_ok = 0
    for row in labeled:
        sample_id = row["sample_id"]
        pred = predictions.get(sample_id, {})
        true_plate = _clean(row.get("true_plate"))
        pred_plate = _clean(pred.get("plate_text"))
        status = pred.get("plate_status", "NO_LEGIBLE")
        stats["total"] += 1
        exact = bool(true_plate and pred_plate == true_plate)
        if exact:
            stats["exact_match"] += 1
        if status == "LEIDA" and exact:
            stats["LEIDA_correcta"] += 1
        elif status == "LEIDA" and not exact:
            stats["LEIDA_incorrecta"] += 1
        elif status == "PROBABLE" and exact:
            stats["PROBABLE_correcta"] += 1
        elif status == "PROBABLE" and not exact:
            stats["PROBABLE_incorrecta"] += 1
        elif status == "NO_LEGIBLE" and row.get("expected_status") == "NO_LEGIBLE":
            stats["NO_LEGIBLE_correcta"] += 1
        if status == "LEIDA" and not exact:
            stats["false_plate_rate"] += 1
        for expected, got in zip(true_plate, pred_plate):
            char_total += 1
            char_ok += int(expected == got)
    stats["character_accuracy"] = char_ok / char_total if char_total else 0.0
    stats["false_plate_rate"] = stats["false_plate_rate"] / stats["total"] if stats["total"] else 0.0
    for key, value in stats.items():
        print(f"{key}: {value}")
    return 0


def _load_predictions(path: Path) -> dict:
    if not path.exists():
        return {}
    return {row["sample_id"]: row for row in csv.DictReader(path.open(encoding="utf-8"))}


def _clean(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


if __name__ == "__main__":
    raise SystemExit(main())
