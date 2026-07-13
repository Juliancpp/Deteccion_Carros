#!/usr/bin/env python3
from __future__ import annotations

import csv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
VALIDATION_DIR = BASE_DIR / "data" / "validation"

PLATE_FIELDS = ["sample_id", "image_path", "camera_id", "true_plate", "plate_visible", "expected_status", "notes"]
ROOM_FIELDS = ["sample_id", "media_path", "camera_id", "movement", "expected_room", "expected_room_status", "passed_in_front_only", "notes"]


def main() -> int:
    for subdir in ("plates", "rooms", "reports"):
        (VALIDATION_DIR / subdir).mkdir(parents=True, exist_ok=True)
    _ensure_manifest(VALIDATION_DIR / "plates" / "manifest.csv", PLATE_FIELDS)
    _ensure_manifest(VALIDATION_DIR / "rooms" / "manifest.csv", ROOM_FIELDS)
    print(f"[VALIDATION] Plantilla placas: {VALIDATION_DIR / 'plates' / 'manifest.csv'}")
    print(f"[VALIDATION] Plantilla habitaciones: {VALIDATION_DIR / 'rooms' / 'manifest.csv'}")
    print("[VALIDATION] Ground truth queda vacio para etiquetado manual; no se inventan valores.")
    return 0


def _ensure_manifest(path: Path, fields: list[str]) -> None:
    if path.exists():
        return
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()


if __name__ == "__main__":
    raise SystemExit(main())
