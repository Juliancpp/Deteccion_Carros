#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import config


def parse_args():
    parser = argparse.ArgumentParser(description="Genera paquete de revision para placa/habitacion de un evento")
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--db-path", default=config.HOTEL_DATABASE_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"[REVIEW] DB no existe: {db_path}")
        return 1
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM hotel_vehicle_events WHERE event_id=?", (args.event_id,)).fetchone()
        if row is None:
            print(f"[REVIEW] Evento no encontrado: {args.event_id}")
            return 1
        output_dir = PROJECT_ROOT / "data" / "validation" / "reports" / "events" / args.event_id
        output_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(row)
        (output_dir / "event.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        for column in ("plate_candidates", "plate_raw_candidates_json", "room_score_breakdown_json", "review_reasons_json"):
            value = payload.get(column)
            if value:
                try:
                    parsed = json.loads(value)
                except Exception:
                    parsed = value
                (output_dir / f"{column}.json").write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[REVIEW] Reporte generado: {output_dir}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
