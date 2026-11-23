from __future__ import annotations

import json
import os
import sqlite3
import math
from datetime import datetime

from app.hotel import visits
from app.hotel.models import HotelVehicleEvent


class HotelStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self.conn.executescript(visits.SCHEMA)

    def save_geometry_events(self, events):
        events = list(events)
        with self.conn:
            self.conn.executemany(
                """INSERT INTO geometry_events
                   (session_id,camera_id,track_id,source_sequence,occurred_at,feature_id,event_type)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                [(e.session_id, e.camera_id, e.track_id, e.source_sequence, e.occurred_at.isoformat(),
                  e.feature_id, e.event_type) for e in events],
            )

            for event in events:
                visits.geometry(self.conn, event)

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


        with self.conn:
            cursor = self.conn.execute(
                f"""
                INSERT INTO hotel_vehicle_events (
                    event_id, occurred_at, camera_id, camera_name, track_id, movement, movement_confidence, event_type,
                    plate, plate_confidence, plate_status, plate_candidates, room_number,
                    room_confidence, room_status, vehicle_class, vehicle_confidence, evidence_path,
                    review_required, notes, clip_path, clip_status, clip_error, reviewed_at, reviewed_by,
                    corrected_at, correction_notes, logical_vehicle_id, original_track_ids_json,
                    plate_raw_candidates_json, plate_runner_up, plate_runner_up_confidence,
                    plate_vote_margin, plate_supporting_frames_json, room_score_breakdown_json,
                    room_runner_up, room_vote_margin, review_reasons_json, source, source_event_id, ocr_diagnostics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
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
                visits.ingest(self.conn, event.event_id, event.capture_session, event.physical_track_id,
                              historical=event.source == "legacy_eventos")

    def get_open_visit(self, plate: str):
        return self.conn.execute("""SELECT * FROM current_visits WHERE kind='entry' AND plate=? AND
                    exit_event_id IS NULL AND exceptional_reason IS NULL LIMIT 1""", (plate,)).fetchone()

    def count_open_visits(self) -> int:
        # Legacy status counts identified entries still awaiting a credible departure
        # signal. A possible signal never changes the canonical lifecycle.
        return self.conn.execute("""SELECT count(*) FROM current_visits v JOIN hotel_vehicle_events e ON e.event_id=v.id
            WHERE v.kind='entry' AND v.exit_event_id IS NULL AND v.exceptional_reason IS NULL
            AND v.plate_status='LEIDA' AND v.pending=0 AND NOT EXISTS (
                SELECT 1 FROM visit_cases x JOIN hotel_vehicle_events d ON d.event_id=x.id
                WHERE e.ocr_diagnostics_json IS NULL AND x.kind='exit' AND d.camera_id<>e.camera_id
                AND x.instant BETWEEN v.instant AND v.instant+? AND d.plate=e.plate AND d.plate_status='LEIDA'
                AND d.review_required=0 AND (d.source<>'geometry_gate' OR d.evidence_path IS NOT NULL))""",
            (visits.window(),)).fetchone()[0]

    def list_events(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM hotel_vehicle_events ORDER BY occurred_at DESC"))

    def list_visits(self) -> list[sqlite3.Row]:
        return [v for v in visits.export_rows(self.conn) if v["entry_at"] and v["plate"] and
                v["plate_status"] == "LEIDA" and (v["exit_at"] or not v["review_required"])]

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
        
        open_visits = self.count_open_visits()

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
        rows = visits.export_rows(self.conn)
        return dict(visits=rows[(page-1)*page_size:page*page_size],total=len(rows),page=page,
                    page_size=page_size,pages=math.ceil(len(rows)/page_size))

    def get_visit(self, visit_id):
        return self.conn.execute("SELECT * FROM current_visits WHERE id=?", (visit_id,)).fetchone()

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

    def _rebuild_hotel_visits_in_transaction(self, cursor):
        # Import only missing identities; operator decisions are durable.
        for row in cursor.execute("""SELECT event_id FROM hotel_vehicle_events e WHERE NOT EXISTS
                  (SELECT 1 FROM visit_cases v WHERE v.id=e.event_id)""").fetchall():
            visits.ingest(self.conn, row["event_id"], historical=True)


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
