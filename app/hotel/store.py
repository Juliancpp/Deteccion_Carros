from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime

from app.hotel.models import HotelVehicleEvent


class HotelStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS hotel_vehicle_events (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                camera_id TEXT NOT NULL,
                camera_name TEXT NOT NULL,
                track_id INTEGER NOT NULL,
                movement TEXT NOT NULL,
                event_type TEXT NOT NULL,
                plate TEXT,
                plate_confidence REAL NOT NULL,
                plate_status TEXT NOT NULL,
                plate_candidates TEXT NOT NULL,
                room_number INTEGER,
                room_confidence REAL NOT NULL,
                room_status TEXT NOT NULL,
                vehicle_class TEXT NOT NULL,
                vehicle_confidence REAL NOT NULL,
                evidence_path TEXT,
                review_required INTEGER NOT NULL,
                notes TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS hotel_visits (
                visit_id TEXT PRIMARY KEY,
                plate TEXT NOT NULL,
                entry_event_id TEXT,
                exit_event_id TEXT,
                entry_at TEXT,
                exit_at TEXT,
                entry_camera TEXT,
                exit_camera TEXT,
                room_number INTEGER,
                status TEXT NOT NULL,
                exit_type TEXT,
                review_required INTEGER NOT NULL
            );
            """
        )
        self.conn.commit()

    def save_event_and_update_visit(self, event: HotelVehicleEvent) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO hotel_vehicle_events (
                event_id, occurred_at, camera_id, camera_name, track_id, movement, event_type,
                plate, plate_confidence, plate_status, plate_candidates, room_number,
                room_confidence, room_status, vehicle_class, vehicle_confidence, evidence_path,
                review_required, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.occurred_at.isoformat(),
                event.camera_id,
                event.camera_name,
                event.track_id,
                event.movement,
                event.event_type,
                event.plate,
                event.plate_confidence,
                event.plate_status,
                json.dumps(event.plate_candidates, ensure_ascii=False),
                event.room_number,
                event.room_confidence,
                event.room_status,
                event.vehicle_class,
                event.vehicle_confidence,
                event.evidence_path,
                1 if event.review_required else 0,
                event.notes,
            ),
        )
        self._update_visit(event)
        self.conn.commit()

    def _update_visit(self, event: HotelVehicleEvent) -> None:
        if not event.plate or event.plate_status == "NO_LEGIBLE":
            return
        open_visit = self.get_open_visit(event.plate)
        if event.event_type in {"ENTRADA_NORMAL", "ENTRADA_POR_SALIDA"}:
            if open_visit is None:
                self.conn.execute(
                    """
                    INSERT INTO hotel_visits (
                        visit_id, plate, entry_event_id, entry_at, entry_camera, room_number,
                        status, review_required
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        event.plate,
                        event.event_id,
                        event.occurred_at.isoformat(),
                        event.camera_id,
                        event.room_number,
                        "ABIERTA" if not event.review_required else "REVISAR",
                        1 if event.review_required else 0,
                    ),
                )
            return
        if event.event_type in {"SALIDA_NORMAL", "SALIDA_POR_ENTRADA"} and open_visit is not None:
            self.conn.execute(
                """
                UPDATE hotel_visits
                SET exit_event_id=?, exit_at=?, exit_camera=?, exit_type=?, status=?, review_required=?
                WHERE visit_id=?
                """,
                (
                    event.event_id,
                    event.occurred_at.isoformat(),
                    event.camera_id,
                    event.event_type,
                    "CERRADA" if not event.review_required else "REVISAR",
                    1 if event.review_required else int(open_visit["review_required"]),
                    open_visit["visit_id"],
                ),
            )

    def get_open_visit(self, plate: str):
        return self.conn.execute(
            "SELECT * FROM hotel_visits WHERE plate=? AND status='ABIERTA' ORDER BY entry_at DESC LIMIT 1",
            (plate,),
        ).fetchone()

    def count_open_visits(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS total FROM hotel_visits WHERE status='ABIERTA'").fetchone()
        return int(row["total"])

    def list_events(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM hotel_vehicle_events ORDER BY occurred_at DESC"))

    def list_visits(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM hotel_visits ORDER BY COALESCE(entry_at, exit_at) DESC"))


def classify_event(camera_id: str, movement: str, plate: str | None, store: HotelStore | None) -> tuple[str, bool, str]:
    open_visit = store.get_open_visit(plate) if plate and store is not None else None
    if camera_id == "A1" and movement == "inward":
        return "ENTRADA_NORMAL", False, ""
    if camera_id == "A1" and movement == "outward":
        if open_visit is not None:
            return "SALIDA_POR_ENTRADA", False, ""
        return "SALIDA_SIN_ENTRADA", True, "salida por A1 sin visita abierta"
    if camera_id == "A5" and movement == "outward":
        if open_visit is not None:
            return "SALIDA_NORMAL", False, ""
        return "SALIDA_SIN_ENTRADA", True, "salida por A5 sin visita abierta"
    if camera_id == "A5" and movement == "inward":
        return "ENTRADA_POR_SALIDA", True, "entrada por camara de salida"
    return "EVENTO_NO_RESUELTO", True, "camara/movimiento no reconocido"


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)
