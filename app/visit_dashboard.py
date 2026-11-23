"""Consultas de historial y reportes sobre la misma selección canónica."""

from datetime import date, timedelta
from io import BytesIO
from typing import Literal

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from pydantic import BaseModel, Field

from app.hotel import visits

State = Literal[
    "ACTIVA",
    "HABITACION_CONFIRMADA",
    "COMPLETADA",
    "SALIDA_HUERFANA",
    "SIN_RESOLVER",
    "CIERRE_EXCEPCIONAL",
    "HABITACION_SIN_ASIGNAR",
]


class Filters(BaseModel):
    model_config = {"extra": "forbid"}
    month: str | None = None
    category: str | None = None
    q: str = Field("", max_length=160)
    plate: str = Field("", max_length=64)
    camera: Literal["A1", "A5"] | None = None
    room: int | None = Field(None, gt=0)
    state: State | None = None
    date_from: date | None = None
    date_to: date | None = None
    order: Literal["desc", "asc"] = "desc"


def selection(filters):
    category = filters.category
    if category is None and filters.state == "HABITACION_SIN_ASIGNAR":
        category = "review_pending"
    base, args = visits.query(category, filters.month)
    # Category and monthly anchor are shared with the four C2 indicators.
    sql = (
        """ FROM (SELECT k.id,k.kind,k.instant,k.pending,v.entry_event_id,v.exit_event_id,
        v.entry_at,v.exit_at,v.association_id,v.association_origin,v.exceptional_reason,
        coalesce(v.camera_id,r.camera) camera_id, d.camera_id exit_camera,
        coalesce(v.plate_manual,v.plate) plate,v.plate plate_original,v.plate_manual,
        v.plate_status,v.plate_confidence,coalesce(v.room_number,r.number) room_number,
        e.source,CASE WHEN k.kind='room_review' THEN 'HABITACION_SIN_ASIGNAR'
        WHEN v.exceptional_reason IS NOT NULL THEN 'CIERRE_EXCEPCIONAL'
        WHEN k.kind='exit' THEN 'SALIDA_HUERFANA' WHEN k.kind='unknown' THEN 'SIN_RESOLVER'
        WHEN v.exit_event_id IS NOT NULL THEN 'COMPLETADA'
        WHEN v.room_number IS NOT NULL THEN 'HABITACION_CONFIRMADA' ELSE 'ACTIVA' END state
        FROM (SELECT id,kind,instant,pending"""
        + base
        + """) k
        LEFT JOIN current_visits v ON k.id=v.id
        LEFT JOIN hotel_vehicle_events e ON e.event_id=v.id
        LEFT JOIN hotel_vehicle_events d ON d.event_id=v.exit_event_id
        LEFT JOIN visit_rooms r ON k.kind='room_review' AND r.id=CAST(substr(k.id,13) AS INTEGER)) WHERE 1=1"""
    )
    if filters.date_from and filters.date_to and filters.date_to < filters.date_from:
        raise ValueError("La fecha final debe ser igual o posterior a la inicial")
    if filters.date_to == date.max:
        raise ValueError("Fecha final fuera del intervalo admitido")
    clauses = [
        ("room_number=?", filters.room),
        ("state=?", filters.state),
        ("instant>=?", visits.epoch(filters.date_from) if filters.date_from else None),
        ("instant<?", visits.epoch(filters.date_to + timedelta(days=1)) if filters.date_to else None),
        ("instr(upper(plate),upper(?))>0", filters.plate.strip() or None),
    ]
    for condition, value in clauses:
        if value is not None:
            sql += " AND " + condition
            args.append(value)
    if filters.camera:
        sql += " AND (camera_id=? OR exit_camera=?)"
        args.extend([filters.camera] * 2)
    if filters.q.strip():
        sql += (
            " AND (instr(lower(id),lower(?)) OR instr(upper(plate),upper(?)) OR CAST(room_number AS TEXT)=?)"
        )
        args.extend([filters.q.strip()] * 3)
    return sql, args


def rows(c, filters, limit=None, offset=0):
    sql, args = selection(filters)
    order = "ASC" if filters.order == "asc" else "DESC"
    sql += f" ORDER BY instant {order},id"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args.extend([limit, offset])
    return c.execute("SELECT *" + sql, args)


def excel(c, filters):
    wb = Workbook(write_only=True)
    sheet = wb.create_sheet("Visitas")
    sheet.append(
        ["Visita", "Estado", "Entrada", "Salida", "Cámara", "Habitación", "Placa", "Revisión", "Origen"]
    )
    count = 0
    for row in rows(c, filters):
        values = [
            row[k]
            for k in (
                "id",
                "state",
                "entry_at",
                "exit_at",
                "camera_id",
                "room_number",
                "plate",
                "pending",
                "source",
            )
        ]
        cells = []
        for value in values:
            cell = WriteOnlyCell(sheet, value=value)
            if isinstance(value, str):
                cell.data_type = "s"
            cells.append(cell)
        sheet.append(cells)
        count += 1
    output = BytesIO()
    wb.save(output)
    return output.getvalue(), count


class HistoryFilters(Filters):
    page: int = Field(1, ge=1)
    page_size: int = Field(25, ge=1, le=100)
