#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
MANIFEST = BASE_DIR / "data" / "validation" / "rooms" / "manifest.csv"


def parse_args():
    parser = argparse.ArgumentParser(description="Evalua habitaciones contra ground truth manual")
    parser.add_argument("--manifest", default=str(MANIFEST))
    parser.add_argument("--predictions", required=False, help="CSV con sample_id,room_number,room_status,movement")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = Path(args.manifest)
    if not manifest.exists():
        print(f"[ROOM_EVAL] No existe manifest: {manifest}")
        return 1
    rows = list(csv.DictReader(manifest.open(encoding="utf-8")))
    labeled = [row for row in rows if str(row.get("expected_room", "")).strip()]
    if not labeled:
        print("[ROOM_EVAL] No hay ground truth etiquetado. No se evaluan datos dummy.")
        return 1
    predictions = _load_predictions(Path(args.predictions)) if args.predictions else {}
    stats = {
        "total": 0,
        "exact_room_match": 0,
        "CONFIRMADA_correcta": 0,
        "CONFIRMADA_incorrecta": 0,
        "PROBABLE_correcta": 0,
        "PROBABLE_incorrecta": 0,
        "NO_DETERMINADA": 0,
        "false_confirmed_room_rate": 0,
        "front_pass_false_confirmed": 0,
    }
    for row in labeled:
        pred = predictions.get(row["sample_id"], {})
        expected_room = str(row.get("expected_room") or "NO_DETERMINADA")
        pred_room = str(pred.get("room_number") or "NO_DETERMINADA")
        status = pred.get("room_status", "NO_DETERMINADA")
        passed_front = str(row.get("passed_in_front_only", "")).lower() in {"true", "1", "yes"}
        stats["total"] += 1
        exact = pred_room == expected_room
        if exact:
            stats["exact_room_match"] += 1
        if status == "CONFIRMADA" and exact:
            stats["CONFIRMADA_correcta"] += 1
        elif status == "CONFIRMADA" and not exact:
            stats["CONFIRMADA_incorrecta"] += 1
            stats["false_confirmed_room_rate"] += 1
        elif status == "PROBABLE" and exact:
            stats["PROBABLE_correcta"] += 1
        elif status == "PROBABLE" and not exact:
            stats["PROBABLE_incorrecta"] += 1
        elif status == "NO_DETERMINADA":
            stats["NO_DETERMINADA"] += 1
        if passed_front and status == "CONFIRMADA":
            stats["front_pass_false_confirmed"] += 1
    stats["false_confirmed_room_rate"] = stats["false_confirmed_room_rate"] / stats["total"] if stats["total"] else 0.0
    for key, value in stats.items():
        print(f"{key}: {value}")
    return 0


def _load_predictions(path: Path) -> dict:
    if not path.exists():
        return {}
    return {row["sample_id"]: row for row in csv.DictReader(path.open(encoding="utf-8"))}


if __name__ == "__main__":
    raise SystemExit(main())
