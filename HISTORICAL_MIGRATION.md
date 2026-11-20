# Histórico de eventos

El sistema conserva dos fuentes: `database/eventos.db` es el registro legacy escrito por el detector; `database/hotel_mvp.db` es la fuente operativa MVP (eventos enriquecidos, usuarios, correcciones y visitas). El dashboard consulta `hotel_vehicle_events`.

Se incorporó provenance (`source`, `source_event_id`) y un índice único para que `scripts/migrate_legacy_events.py` sea idempotente. Antes de cada ejecución el script crea una copia SQLite con timestamp en `backups/`; no elimina ni modifica `eventos.db`.

Auditoría de producción (2026-09-13):

- Legacy: 2.015 filas (`2026-07-13` a `2026-09-12`), movimientos INWARD/OUTWARD/UNKNOWN, cámaras A1/A5.
- MVP previo: 100 filas.
- Coincidencias legacy/MVP por cámara, movimiento, placa y ventana temporal de 2 s: 101.
- Importadas: 1.914 filas legacy.
- Total único visible en `hotel_vehicle_events`: 2.014 (1.914 `legacy_eventos` + 100 `mvp_hotel`).

Los eventos legacy sin campos administrativos reciben valores NULL/default y siguen apareciendo mediante `LEFT JOIN` con correcciones y visitas. Las visitas que cruzan meses se contabilizan en el mes de su entrada.
