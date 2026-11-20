"""Import legacy eventos.db rows into hotel_mvp.db, preserving provenance.

The command is additive and idempotent. It creates timestamped SQLite backups before
writing; it never deletes either source row or existing MVP data.
"""
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config
from app.hotel.store import HotelStore


def backup(source: Path, destination: Path) -> None:
    source_conn = sqlite3.connect(str(source), uri=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_conn = sqlite3.connect(str(destination))
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def classify(camera, movement):
    camera, movement = (camera or "").upper(), (movement or "").upper()
    if movement == "INWARD":
        return "ENTRADA_NORMAL" if camera == "A1" else "ENTRADA_POR_SALIDA"
    if movement == "OUTWARD":
        return "SALIDA_NORMAL" if camera == "A5" else "SALIDA_POR_ENTRADA"
    return "EVENTO_NO_RESUELTO"


def migrate(legacy_path=None, hotel_path=None, backup_dir=None):
    legacy = Path(legacy_path or config.DATABASE_PATH).resolve()
    hotel = Path(hotel_path or config.HOTEL_DATABASE_PATH).resolve()
    if legacy == hotel:
        raise ValueError("Las bases legacy y MVP deben ser distintas.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = Path(backup_dir or ROOT / "backups")
    destination.mkdir(parents=True, exist_ok=True)
    def unique(name):
        path = destination / name
        index = 1
        while path.exists():
            path = destination / f"{path.stem}_{index}{path.suffix}"
            index += 1
        return path
    backup(legacy, unique(f"eventos_pre_legacy_{stamp}.db"))
    backup(hotel, unique(f"hotel_mvp_pre_legacy_{stamp}.db"))

    # Store migration schema is additive and creates the provenance unique index.
    store = HotelStore(str(hotel))
    store.close()
    source = sqlite3.connect(str(legacy))
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(str(hotel), timeout=30)
    target.row_factory = sqlite3.Row
    inserted = 0
    duplicates = 0
    try:
        target.execute("PRAGMA foreign_keys=ON")
        target.execute("BEGIN IMMEDIATE")
        existing = [dict(r) for r in target.execute("SELECT occurred_at,camera_id,movement,plate FROM hotel_vehicle_events")]
        for row in source.execute("SELECT * FROM eventos ORDER BY fecha_hora, id"):
            source_id = str(row["id"])
            exists = target.execute("SELECT 1 FROM hotel_vehicle_events WHERE source='legacy_eventos' AND source_event_id=?", (source_id,)).fetchone()
            if exists:
                duplicates += 1
                continue
            # The detector writes the same crossing to both databases. Use the
            # complete observed identity, including a two-second timestamp window,
            # only to identify those already-present MVP rows; provenance remains
            # the durable idempotency key for imported rows.
            row_time = datetime.fromisoformat(row["fecha_hora"]).replace(tzinfo=None)
            plate = row["placa"] or None
            duplicate = False
            for item in existing:
                item_time = datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00")).replace(tzinfo=None)
                if (item["camera_id"] == row["camara"] and item["movement"] == row["tipo_evento"]
                        and (item["plate"] or None) == plate
                        and abs((item_time - row_time).total_seconds()) <= 2):
                    duplicate = True
                    break
            if duplicate:
                duplicates += 1
                continue
            event_id = f"legacy-{source_id}"
            status = "LEIDA" if plate else "NO_LEGIBLE"
            event_type = classify(row["camara"], row["tipo_evento"])
            note = "Importado desde eventos.db"
            if event_type == "EVENTO_NO_RESUELTO":
                note += "; movimiento legacy UNKNOWN"
            target.execute("""INSERT INTO hotel_vehicle_events(
                event_id, occurred_at, camera_id, camera_name, track_id, movement,
                movement_confidence, event_type, plate, plate_confidence, plate_status,
                plate_candidates, room_number, room_confidence, room_status, vehicle_class,
                vehicle_confidence, evidence_path, review_required, notes, clip_status,
                source, source_event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, row["fecha_hora"].replace(" ", "T"), row["camara"], row["camara"],
                 int(row["vehicle_id"]), row["tipo_evento"], float(row["confianza_ocr"] or 0),
                 event_type, plate, float(row["confianza_ocr"] or 0), status, "[]", None,
                 0.0, "NO_DETERMINADA", "legacy", 0.0, row["imagen"], 1 if not plate or event_type == "EVENTO_NO_RESUELTO" else 0,
                 note, "DISABLED", "legacy_eventos", source_id))
            inserted += 1
        target.commit()
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()
    return {"legacy_records": inserted + duplicates, "inserted": inserted, "duplicates": duplicates,
            "total_unique": sqlite3.connect(str(hotel)).execute("SELECT count(*) FROM hotel_vehicle_events").fetchone()[0]}


if __name__ == "__main__":
    result = migrate()
    print(result)
