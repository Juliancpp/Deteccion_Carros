from __future__ import annotations

import os
import tempfile
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from app.hotel.store import HotelStore, parse_dt


class HotelExcelExporter:
    def __init__(self, store: HotelStore, output_path: str) -> None:
        self.store = store
        self.output_path = output_path

    def export(self) -> str:
        wb = Workbook()
        events_ws = wb.active
        events_ws.title = "Eventos"
        visits_ws = wb.create_sheet("Visitas")
        review_ws = wb.create_sheet("Revision")
        self._write_events(events_ws)
        self._write_visits(visits_ws)
        self._write_review(review_ws)
        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="control_vehiculos_", suffix=".xlsx", dir=os.path.dirname(self.output_path))
        os.close(fd)
        try:
            wb.save(tmp_path)
            os.replace(tmp_path, self.output_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        return self.output_path

    def _write_events(self, ws) -> None:
        headers = [
            "ID", "Fecha", "Hora", "Camara", "Track", "Movimiento", "Tipo de evento",
            "Placa", "Confianza placa", "Estado placa", "Habitacion", "Confianza habitacion",
            "Estado habitacion", "Clase vehiculo", "Evidencia", "Revision requerida", "Observaciones",
        ]
        rows = []
        for event in self.store.list_events():
            occurred = parse_dt(event["occurred_at"]) or datetime.now()
            rows.append([
                event["event_id"], occurred.date(), occurred.time().replace(microsecond=0), event["camera_id"],
                event["track_id"], event["movement"], event["event_type"], event["plate"] or "",
                event["plate_confidence"], event["plate_status"], event["room_number"] or "",
                event["room_confidence"], event["room_status"], event["vehicle_class"],
                event["evidence_path"] or "", bool(event["review_required"]), event["notes"],
            ])
        _write_table(ws, headers, rows)

    def _write_visits(self, ws) -> None:
        headers = [
            "ID visita", "Placa", "Fecha/hora entrada", "Acceso de entrada", "Habitacion",
            "Fecha/hora salida", "Acceso de salida", "Tipo de salida", "Duracion minutos",
            "Estado", "Revision requerida",
        ]
        rows = []
        for visit in self.store.list_visits():
            entry_at = parse_dt(visit["entry_at"])
            exit_at = parse_dt(visit["exit_at"])
            duration = ""
            if entry_at and exit_at:
                duration = round((exit_at - entry_at).total_seconds() / 60, 1)
            rows.append([
                visit["visit_id"], visit["plate"], visit["entry_at"] or "", visit["entry_camera"] or "",
                visit["room_number"] or "", visit["exit_at"] or "", visit["exit_camera"] or "",
                visit["exit_type"] or "", duration, visit["status"], bool(visit["review_required"]),
            ])
        _write_table(ws, headers, rows)

    def _write_review(self, ws) -> None:
        headers = ["ID", "Motivo", "Camara", "Fecha/hora", "Placa", "Tipo", "Evidencia"]
        rows = []
        for event in self.store.list_events():
            reasons = []
            if not event["plate"]:
                reasons.append("sin placa")
            if event["plate_status"] != "LEIDA":
                reasons.append("placa baja/no legible")
            if event["room_status"] == "NO_DETERMINADA":
                reasons.append("habitacion no determinada")
            if event["event_type"] in {"SALIDA_SIN_ENTRADA", "EVENTO_NO_RESUELTO"}:
                reasons.append("evento requiere revision")
            if not event["evidence_path"]:
                reasons.append("sin evidencia")
            if event["review_required"] or reasons:
                rows.append([
                    event["event_id"], ", ".join(reasons) or "revision requerida", event["camera_id"],
                    event["occurred_at"], event["plate"] or "", event["event_type"], event["evidence_path"] or "",
                ])
        _write_table(ws, headers, rows)


def _write_table(ws, headers: list[str], rows: list[list]) -> None:
    ws.append(headers)
    fill = PatternFill("solid", fgColor="1F4E78")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
    for row in rows:
        ws.append(row)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for column_cells in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in column_cells)
        ws.column_dimensions[get_column_letter(column_cells[0].column)].width = min(max(12, max_len + 2), 60)
