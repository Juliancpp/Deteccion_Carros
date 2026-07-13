"""
Deteccion_Carros — Base de Datos (Fase 6)
=========================================
Persistencia de eventos vehiculares en SQLite usando SQLAlchemy.
"""

import os
from datetime import datetime
from typing import List

from sqlalchemy import Column, DateTime, Float, Integer, String, create_engine, select
from sqlalchemy.orm import declarative_base, sessionmaker

try:
    from app.config import CAMERA_ID, DATABASE_PATH
except ImportError:
    from config import CAMERA_ID, DATABASE_PATH  # type: ignore


Base = declarative_base()


class EventRecord(Base):
    """Modelo de la tabla eventos."""

    __tablename__ = "eventos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fecha_hora = Column(DateTime, nullable=False, index=True)
    placa = Column(String(16), nullable=True)
    confianza_ocr = Column(Float, nullable=True)
    tipo_evento = Column(String(20), nullable=False)
    vehicle_id = Column(Integer, nullable=False)
    imagen = Column(String(500), nullable=True)
    camara = Column(String(100), nullable=False)


class DatabaseManager:
    """Gestiona creación, inserción y consulta de eventos en SQLite."""

    def __init__(self, db_path: str = DATABASE_PATH, camera_id: str = CAMERA_ID):
        self.db_path = db_path
        self.camera_id = str(camera_id)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"timeout": 30},
            future=True
        )
        self.Session = sessionmaker(bind=self.engine, future=True)
        Base.metadata.create_all(self.engine)

        # Habilitar modo WAL y configuraciones de sincronización optimizadas para SQLite multihilo
        with self.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA synchronous=NORMAL")

    def save_event(self, event: dict) -> int:
        """
        Guarda un evento vehicular.

        Espera:
            {
                "vehicle_id": 5,
                "type": "entrada",
                "timestamp": "2026-07-10T22:45:10",
                "plate": "ABC1234",
                "confidence": 0.82,
                "image_path": "...jpg"
            }
        """
        record = EventRecord(
            fecha_hora=self._parse_timestamp(event.get("timestamp")),
            placa=event.get("plate") or event.get("placa") or None,
            confianza_ocr=self._parse_confidence(
                event.get("confidence", event.get("confianza"))
            ),
            tipo_evento=event["type"],
            vehicle_id=int(event["vehicle_id"]),
            imagen=event.get("image_path") or event.get("imagen") or None,
            camara=str(event.get("camera", self.camera_id)),
        )

        with self.Session() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            return int(record.id)

    def get_events(self, limit: int = 50) -> List[dict]:
        """Devuelve los últimos eventos ordenados del más reciente al más antiguo."""
        with self.Session() as session:
            records = session.execute(
                select(EventRecord).order_by(EventRecord.fecha_hora.desc()).limit(limit)
            ).scalars()
            return [self._record_to_dict(record) for record in records]

    @staticmethod
    def _record_to_dict(record: EventRecord) -> dict:
        return {
            "id": record.id,
            "fecha_hora": record.fecha_hora.isoformat(timespec="seconds"),
            "placa": record.placa or "",
            "confianza_ocr": record.confianza_ocr,
            "tipo_evento": record.tipo_evento,
            "vehicle_id": record.vehicle_id,
            "imagen": record.imagen or "",
            "camara": record.camara,
        }

    @staticmethod
    def _parse_timestamp(timestamp) -> datetime:
        if isinstance(timestamp, datetime):
            return timestamp
        if timestamp:
            try:
                return datetime.fromisoformat(str(timestamp))
            except ValueError:
                pass
        return datetime.now()

    @staticmethod
    def _parse_confidence(confidence):
        if confidence in ("", None):
            return None
        return float(confidence)
