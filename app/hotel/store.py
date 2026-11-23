from __future__ import annotations

import json
import os
import sqlite3
import uuid
import math
from datetime import datetime

from app.hotel.models import HotelVehicleEvent


class HotelStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def save_geometry_events(self, events):
        with self.conn:
            self.conn.executemany(
                """INSERT INTO geometry_events
                   (session_id,camera_id,track_id,source_sequence,occurred_at,feature_id,event_type)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                [(e.session_id, e.camera_id, e.track_id, e.source_sequence, e.occurred_at.isoformat(),
                  e.feature_id, e.event_type) for e in events],
            )

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS geometry_events (
                session_id TEXT NOT NULL, camera_id TEXT NOT NULL, track_id INTEGER NOT NULL,
                source_sequence INTEGER NOT NULL, occurred_at TEXT NOT NULL,
                feature_id TEXT NOT NULL, event_type TEXT NOT NULL,
                PRIMARY KEY(session_id, camera_id, track_id, source_sequence, feature_id)
            );
            CREATE INDEX IF NOT EXISTS geometry_time ON geometry_events(occurred_at DESC);
            CREATE TABLE IF NOT EXISTS hotel_vehicle_events (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                camera_id TEXT NOT NULL,
                camera_name TEXT NOT NULL,
                track_id INTEGER NOT NULL,
                movement TEXT NOT NULL,
                movement_confidence REAL NOT NULL DEFAULT 0.0,
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
                notes TEXT NOT NULL,
                clip_path TEXT,
                clip_status TEXT NOT NULL DEFAULT 'DISABLED',
                clip_error TEXT,
                reviewed_at TEXT,
                reviewed_by TEXT,
                corrected_at TEXT,
                correction_notes TEXT,
                source TEXT NOT NULL DEFAULT 'mvp_hotel',
                source_event_id TEXT
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

            CREATE TABLE IF NOT EXISTS hotel_event_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                changed_by TEXT NOT NULL,
                field_name TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                notes TEXT
            );
            """
        )
        # Triggers also protect local draft databases created before validation.
        for operation in ("INSERT", "UPDATE"):
            self.conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS geometry_validate_{operation}
                BEFORE {operation} ON geometry_events
                WHEN NEW.event_type NOT IN ('HOTEL_ENTRY','HOTEL_EXIT','ROOM_ENTRY','ROOM_EXIT','ROOM_APPROACH')
                  OR trim(NEW.camera_id) = '' OR trim(NEW.feature_id) = '' OR trim(NEW.session_id) = ''
                  OR typeof(NEW.track_id) != 'integer' OR NEW.track_id < 0
                  OR typeof(NEW.source_sequence) != 'integer' OR NEW.source_sequence < 0
                BEGIN SELECT RAISE(ABORT, 'invalid geometry event'); END
            """)
        self.conn.commit()

        # Add missing columns dynamically if they do not exist
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cursor = self.conn.cursor()
            cursor.execute("PRAGMA table_info(hotel_vehicle_events)")
            columns = [row["name"] for row in cursor.fetchall()]
            
            cols_to_add = {
                "ocr_diagnostics_json": "TEXT",
                "movement_confidence": "REAL NOT NULL DEFAULT 0.0",
                "clip_path": "TEXT",
                "clip_status": "TEXT NOT NULL DEFAULT 'DISABLED'",
                "clip_error": "TEXT",
                "reviewed_at": "TEXT",
                "reviewed_by": "TEXT",
                "corrected_at": "TEXT",
                "correction_notes": "TEXT",
                "logical_vehicle_id": "INTEGER",
                "original_track_ids_json": "TEXT",
                "plate_raw_candidates_json": "TEXT",
                "plate_runner_up": "TEXT",
                "plate_runner_up_confidence": "REAL NOT NULL DEFAULT 0.0",
                "plate_vote_margin": "REAL NOT NULL DEFAULT 0.0",
                "plate_supporting_frames_json": "TEXT",
                "room_score_breakdown_json": "TEXT",
                "room_runner_up": "INTEGER",
                "room_vote_margin": "REAL NOT NULL DEFAULT 0.0",
                "review_reasons_json": "TEXT"
            }
            
            for col_name, col_def in cols_to_add.items():
                if col_name not in columns:
                    self.conn.execute(f"ALTER TABLE hotel_vehicle_events ADD COLUMN {col_name} {col_def}")
            current = [row["name"] for row in self.conn.execute("PRAGMA table_info(hotel_vehicle_events)").fetchall()]
            for col_name, col_def in {"source": "TEXT NOT NULL DEFAULT 'mvp_hotel'", "source_event_id": "TEXT"}.items():
                if col_name not in current:
                    self.conn.execute(f"ALTER TABLE hotel_vehicle_events ADD COLUMN {col_name} {col_def}")
            self.conn.execute("UPDATE hotel_vehicle_events SET source='mvp_hotel', source_event_id=event_id WHERE source_event_id IS NULL")
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_hotel_event_source ON hotel_vehicle_events(source, source_event_id)")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def save_event_and_update_visit(self, event: HotelVehicleEvent) -> None:
        clip_path = getattr(event, "clip_path", None)
        clip_status = getattr(event, "clip_status", "DISABLED")
        clip_error = getattr(event, "clip_error", None)
        reviewed_at = getattr(event, "reviewed_at", None)
        reviewed_by = getattr(event, "reviewed_by", None)
        corrected_at = getattr(event, "corrected_at", None)
        correction_notes = getattr(event, "correction_notes", None)
        logical_vehicle_id = getattr(event, "logical_vehicle_id", None)
        original_track_ids_json = getattr(event, "original_track_ids_json", None)
        plate_raw_candidates_json = getattr(event, "plate_raw_candidates_json", None)
        plate_runner_up = getattr(event, "plate_runner_up", None)
        plate_runner_up_confidence = getattr(event, "plate_runner_up_confidence", 0.0)
        plate_vote_margin = getattr(event, "plate_vote_margin", 0.0)
        plate_supporting_frames_json = getattr(event, "plate_supporting_frames_json", None)
        room_score_breakdown_json = getattr(event, "room_score_breakdown_json", None)
        room_runner_up = getattr(event, "room_runner_up", None)
        room_vote_margin = getattr(event, "room_vote_margin", 0.0)
        review_reasons_json = getattr(event, "review_reasons_json", None)

        insert = "INSERT" if event.source == "geometry_gate" else "INSERT OR REPLACE"
        conflict = "ON CONFLICT(event_id) DO NOTHING" if event.source == "geometry_gate" else ""
        with self.conn:
            cursor = self.conn.execute(
                f"""
                {insert} INTO hotel_vehicle_events (
                    event_id, occurred_at, camera_id, camera_name, track_id, movement, movement_confidence, event_type,
                    plate, plate_confidence, plate_status, plate_candidates, room_number,
                    room_confidence, room_status, vehicle_class, vehicle_confidence, evidence_path,
                    review_required, notes, clip_path, clip_status, clip_error, reviewed_at, reviewed_by,
                    corrected_at, correction_notes, logical_vehicle_id, original_track_ids_json,
                    plate_raw_candidates_json, plate_runner_up, plate_runner_up_confidence,
                    plate_vote_margin, plate_supporting_frames_json, room_score_breakdown_json,
                    room_runner_up, room_vote_margin, review_reasons_json, source, source_event_id, ocr_diagnostics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                {conflict}
                """,
                (
                    event.event_id,
                    event.occurred_at.isoformat() if isinstance(event.occurred_at, datetime) else str(event.occurred_at),
                    event.camera_id,
                    event.camera_name,
                    event.track_id,
                    event.movement,
                    event.movement_confidence,
                    event.event_type,
                    event.plate,
                    event.plate_confidence,
                    event.plate_status,
                    json.dumps(event.plate_candidates, ensure_ascii=False) if isinstance(event.plate_candidates, list) else str(event.plate_candidates),
                    event.room_number,
                    event.room_confidence,
                    event.room_status,
                    event.vehicle_class,
                    event.vehicle_confidence,
                    event.evidence_path,
                    1 if event.review_required else 0,
                    event.notes,
                    clip_path,
                    clip_status,
                    clip_error,
                    reviewed_at,
                    reviewed_by,
                    corrected_at,
                    correction_notes,
                    logical_vehicle_id,
                    original_track_ids_json,
                    plate_raw_candidates_json,
                    plate_runner_up,
                    plate_runner_up_confidence,
                    plate_vote_margin,
                    plate_supporting_frames_json,
                    room_score_breakdown_json,
                    room_runner_up,
                    room_vote_margin,
                    review_reasons_json,
                    event.source,
                    event.event_id,
                    event.ocr_diagnostics_json,
                ),
            )
            if cursor.rowcount:
                self._update_visit(event)


    def _update_visit(self, event: HotelVehicleEvent) -> None:
        if not _eligible_for_visit(event):
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
                        event.occurred_at.isoformat() if isinstance(event.occurred_at, datetime) else str(event.occurred_at),
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
                    event.occurred_at.isoformat() if isinstance(event.occurred_at, datetime) else str(event.occurred_at),
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

    def get_event(self, event_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM hotel_vehicle_events WHERE event_id=?",
            (event_id,)
        ).fetchone()

    def get_summary_stats(self) -> dict:
        today_str = datetime.now().strftime("%Y-%m-%d") + "%"
        cursor = self.conn.cursor()
        
        cursor.execute(
            "SELECT COUNT(*) FROM hotel_vehicle_events WHERE occurred_at LIKE ? AND event_type IN ('ENTRADA_NORMAL', 'ENTRADA_POR_SALIDA')",
            (today_str,)
        )
        entradas = cursor.fetchone()[0]
        
        cursor.execute(
            "SELECT COUNT(*) FROM hotel_vehicle_events WHERE occurred_at LIKE ? AND event_type IN ('SALIDA_NORMAL', 'SALIDA_POR_ENTRADA', 'SALIDA_SIN_ENTRADA')",
            (today_str,)
        )
        salidas = cursor.fetchone()[0]
        
        cursor.execute(
            "SELECT COUNT(*) FROM hotel_vehicle_events WHERE occurred_at LIKE ?",
            (today_str,)
        )
        total = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM hotel_visits WHERE status = 'ABIERTA'")
        open_visits = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM hotel_vehicle_events WHERE review_required = 1")
        pending_review = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM hotel_vehicle_events WHERE plate_status IN ('PROBABLE', 'LEIDA')")
        ocr_completed = cursor.fetchone()[0]
        
        return {
            "entradas_hoy": entradas,
            "salidas_hoy": salidas,
            "total_vehiculos": total,
            "visits_open": open_visits,
            "review_pending": pending_review,
            "ocr_completed": ocr_completed
        }

    def get_events_filtered(self, filters: dict, page: int = 1, page_size: int = 25) -> dict:
        query = "SELECT * FROM hotel_vehicle_events WHERE 1=1"
        count_query = "SELECT COUNT(*) FROM hotel_vehicle_events WHERE 1=1"
        params = []
        
        if filters.get("date_from"):
            query += " AND occurred_at >= ?"
            count_query += " AND occurred_at >= ?"
            params.append(filters["date_from"])
            
        if filters.get("date_to"):
            dt_to = filters["date_to"]
            if len(dt_to) == 10:
                dt_to += "T23:59:59"
            query += " AND occurred_at <= ?"
            count_query += " AND occurred_at <= ?"
            params.append(dt_to)
            
        if filters.get("camera"):
            query += " AND camera_id = ?"
            count_query += " AND camera_id = ?"
            params.append(filters["camera"])
            
        if filters.get("event_type"):
            query += " AND event_type = ?"
            count_query += " AND event_type = ?"
            params.append(filters["event_type"])
            
        if filters.get("plate"):
            query += " AND plate LIKE ?"
            count_query += " AND plate LIKE ?"
            params.append(f"%{filters['plate']}%")
            
        if filters.get("room") is not None:
            query += " AND room_number = ?"
            count_query += " AND room_number = ?"
            params.append(int(filters["room"]))
            
        if filters.get("review_required") is not None:
            val = 1 if filters["review_required"] in (True, 1, "1", "true") else 0
            query += " AND review_required = ?"
            count_query += " AND review_required = ?"
            params.append(val)
            
        if filters.get("plate_status"):
            query += " AND plate_status = ?"
            count_query += " AND plate_status = ?"
            params.append(filters["plate_status"])
            
        cursor = self.conn.cursor()
        cursor.execute(count_query, params)
        total = cursor.fetchone()[0]
        
        query += " ORDER BY occurred_at DESC LIMIT ? OFFSET ?"
        offset = (page - 1) * page_size
        params_with_limit = params + [page_size, offset]
        
        cursor.execute(query, params_with_limit)
        rows = cursor.fetchall()
        
        events = [dict(row) for row in rows]
        pages = math.ceil(total / page_size) if page_size > 0 else 1
        
        return {
            "events": events,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages
        }

    def get_visits_filtered(self, page: int = 1, page_size: int = 25) -> dict:
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM hotel_visits")
        total = cursor.fetchone()[0]
        
        offset = (page - 1) * page_size
        cursor.execute("SELECT * FROM hotel_visits ORDER BY COALESCE(entry_at, exit_at) DESC LIMIT ? OFFSET ?", (page_size, offset))
        rows = cursor.fetchall()
        visits = [dict(row) for row in rows]
        pages = math.ceil(total / page_size) if page_size > 0 else 1
        
        return {
            "visits": visits,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages
        }

    def get_visit(self, visit_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM hotel_visits WHERE visit_id=?", (visit_id,)).fetchone()

    def update_event_manually(self, event_id: str, updates: dict, changed_by: str) -> bool:
        """
        Updates an event manually, logs the edits to hotel_event_audit,
        and rebuilds all visits transactionally.
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT * FROM hotel_vehicle_events WHERE event_id=?", (event_id,))
            original = cursor.fetchone()
            if not original:
                return False

            now_str = datetime.now().isoformat(timespec="seconds")
            audit_records = []
            
            editable_fields = [
                "plate", "plate_status", "room_number", "room_status", 
                "event_type", "review_required", "notes"
            ]

            sql_parts = []
            params = []
            
            for field in editable_fields:
                if field in updates:
                    old_val = str(original[field]) if original[field] is not None else None
                    new_val = updates[field]
                    if isinstance(new_val, bool):
                        new_val = 1 if new_val else 0
                    
                    new_val_str = str(new_val) if new_val is not None else None
                    
                    if old_val != new_val_str:
                        audit_records.append((
                            event_id,
                            now_str,
                            changed_by,
                            field,
                            old_val,
                            new_val_str,
                            updates.get("correction_notes", "")
                        ))
                        sql_parts.append(f"{field}=?")
                        params.append(new_val)

            if not sql_parts:
                return True

            sql_parts.append("corrected_at=?")
            params.append(now_str)
            
            sql_parts.append("correction_notes=?")
            params.append(updates.get("correction_notes", ""))

            params.append(event_id)
            query = f"UPDATE hotel_vehicle_events SET {', '.join(sql_parts)} WHERE event_id=?"
            
            self.conn.execute("BEGIN TRANSACTION")
            
            cursor.execute(query, params)
            
            for audit in audit_records:
                cursor.execute(
                    """
                    INSERT INTO hotel_event_audit (event_id, changed_at, changed_by, field_name, old_value, new_value, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    audit
                )

            self._rebuild_hotel_visits_in_transaction(cursor)
            self.conn.commit()
            return True
        except Exception as e:
            self.conn.rollback()
            raise e

    def rebuild_hotel_visits(self) -> None:
        """Exposed rebuild method running in a transaction."""
        cursor = self.conn.cursor()
        try:
            self.conn.execute("BEGIN TRANSACTION")
            self._rebuild_hotel_visits_in_transaction(cursor)
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            raise e

    def _rebuild_hotel_visits_in_transaction(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute("DELETE FROM hotel_visits")
        cursor.execute("SELECT * FROM hotel_vehicle_events ORDER BY occurred_at ASC")
        rows = cursor.fetchall()
        
        for row in rows:
            event = HotelVehicleEvent(
                event_id=row["event_id"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                camera_id=row["camera_id"],
                camera_name=row["camera_name"],
                track_id=row["track_id"],
                movement=row["movement"],
                movement_confidence=row["movement_confidence"],
                event_type=row["event_type"],
                plate=row["plate"],
                plate_confidence=row["plate_confidence"],
                plate_status=row["plate_status"],
                room_number=row["room_number"],
                room_confidence=row["room_confidence"],
                room_status=row["room_status"],
                vehicle_class=row["vehicle_class"],
                vehicle_confidence=row["vehicle_confidence"],
                evidence_path=row["evidence_path"],
                review_required=bool(row["review_required"]),
                source=row["source"],
                ocr_diagnostics_json=row["ocr_diagnostics_json"],
                notes=row["notes"]
            )
            if not _eligible_for_visit(event):
                continue
                
            cursor.execute(
                "SELECT * FROM hotel_visits WHERE plate=? AND status='ABIERTA' ORDER BY entry_at DESC LIMIT 1",
                (event.plate,)
            )
            open_visit = cursor.fetchone()
            
            if event.event_type in {"ENTRADA_NORMAL", "ENTRADA_POR_SALIDA"}:
                if open_visit is None:
                    cursor.execute(
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
            elif event.event_type in {"SALIDA_NORMAL", "SALIDA_POR_ENTRADA"} and open_visit is not None:
                cursor.execute(
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


def _eligible_for_visit(event: HotelVehicleEvent) -> bool:
    if event.ocr_diagnostics_json and event.plate_status != "LEIDA":
        return False
    if event.source == "geometry_gate" and (
        event.review_required or event.plate_status != "LEIDA" or not event.evidence_path
    ):
        return False
    return bool(event.plate) and event.plate_status != "NO_LEGIBLE"


def classify_event(camera_id: str, movement: str, plate: str | None, store: HotelStore | None) -> tuple[str, bool, str]:
    open_visit = store.get_open_visit(plate) if plate and store is not None else None
    m = str(movement).upper()
    if m == "UNKNOWN":
        return "EVENTO_NO_RESUELTO", True, "movimiento desconocido"
    if camera_id == "A1" and m == "INWARD":
        return "ENTRADA_NORMAL", False, ""
    if camera_id == "A1" and m == "OUTWARD":
        if open_visit is not None:
            return "SALIDA_POR_ENTRADA", False, ""
        return "SALIDA_SIN_ENTRADA", True, "salida por A1 sin visita abierta"
    if camera_id == "A5" and m == "OUTWARD":
        if open_visit is not None:
            return "SALIDA_NORMAL", False, ""
        return "SALIDA_SIN_ENTRADA", True, "salida por A5 sin visita abierta"
    if camera_id == "A5" and m == "INWARD":
        return "ENTRADA_POR_SALIDA", True, "entrada por camara de salida"
    return "EVENTO_NO_RESUELTO", True, f"camara/movimiento no reconocido: {camera_id}/{movement}"


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)
