# Visitas canónicas del Hotel Relax

## Identidad y fuentes

El identificador público de una visita es el del evento de entrada. Una salida sin entrada tiene su propio caso provisional. Al vincularla, el detalle de la entrada incluye ambos eventos. Al desasociarla reaparece el mismo caso de salida. Una observación de habitación conserva su identidad en ambos escenarios.

| Fuente | Escritura | Lectura y transición |
|---|---|---|
| Base de eventos del detector | `app/database.py`, `DatabaseManager.save_event` | Espejo compatible. La clave única nueva `source_event_id` impide repetir el mismo envío. `scripts/migrate_legacy_events.py` importa eventos históricos. |
| `hotel_vehicle_events` | `EventConsumer._process_resolved_candidate` y `HotelStore.save_event_and_update_visit` | Fuente técnica de fecha, cámara, clasificación, imagen, clip e hipótesis C1. La transacción inserta el cruce y su caso. La API de eventos y Excel siguen leyendo este registro. |
| `geometry_events` | `scripts/run_hotel_mvp.py::_flush_geometry` y `HotelStore.save_geometry_events` | El lote técnico y su observación canónica se guardan en la misma transacción. Una confirmación puede llegar antes que la transacción de entrada. |
| `hotel_visits` | En C1, el almacén abría y cerraba visitas por placa. | Se conserva. El reconciliador examina los pares anteriores. El runner C2 usa `visits.ingest`. |
| `manual_visits` | En C1, `app/administration.py` asociaba y desasociaba en esta tabla. | Se conserva para reconciliación. `associate` y `unlink` escriben en `visit_links`. El ID entero del vínculo permite mantener las rutas existentes. |
| `event_plate_corrections`, `audit_log` | `administration.correct_plate` registra operador, valor anterior y valor nuevo. | El detalle conserva la lectura original del evento y presenta por separado la corrección. |
| Esquema C2 | `app/hotel/visits.py::SCHEMA`, inicializado por `HotelStore` | `visit_cases` guarda el ancla y estado de revisión. `visit_links` guarda versiones de asociaciones. `visit_rooms` guarda observaciones, evidencia y correcciones. `visit_history`, `visit_commands` y `visit_imports` registran cambios, reintentos y decisiones históricas importadas. |
| `current_visits` | Vista sobre eventos, correcciones y relaciones actuales | La lista, el detalle, los indicadores y la hoja *Visitas* comparten identidades. Las hojas *Eventos* y *Revisión* de Excel conservan el desglose técnico. `/api/hotel/summary` mantiene campos de compatibilidad. Para las cuatro tarjetas usar `visit-cases/indicators`. |

## Transiciones y asociación

Una entrada registrada queda activa incluso si no tiene placa. Una habitación confirmada cambia el estado visible a `HABITACION_CONFIRMADA`. Una salida vinculada completa la visita en la misma transacción. Una desasociación revoca el vínculo y devuelve ambos casos a revisión. Un administrador puede cerrar excepcionalmente una entrada abierta con un motivo. El cierre excepcional sigue contando como entrada sin salida física. Un evento de dirección indeterminada queda disponible para revisión.

Las claves foráneas, los índices únicos parciales y el disparador de inserción protegen las relaciones. Cada extremo admite un único vínculo vigente. Un vínculo exige entrada y salida compatibles y orden temporal. Cada reasociación posterior a una revocación crea una nueva versión. Los comandos administrativos aceptan `Idempotency-Key`, por usuario. Repetir una clave con otra operación devuelve HTTP 409.

La asociación automática exige dirección compatible, un único candidato plausible, archivos de evidencia existentes con contenido en ambos extremos y una estancia dentro de `HOTEL_VISIT_MAX_SECONDS` (86.400 segundos por defecto). La continuidad física exige misma cámara, sesión y track dentro de `HOTEL_VISIT_TRACK_MAX_SECONDS` (120 segundos). La otra vía requiere placa coincidente identificada y habitación confirmada coincidente. La lectura automática de placa utilizada para comparar requiere `LEIDA` y confianza mínima de 0,65. Una corrección manual de entrada también puede identificarla. Una contradicción de placas fiables o habitaciones confirmadas excluye al candidato. Una salida con varias posibilidades permanece huérfana.

La base registra el criterio de asociación. Para placa y habitación conserva la confianza OCR de salida. Para continuidad visual registra el criterio y deja vacía la confianza numérica. Estas medidas carecen de calibración contra una muestra de visitas reales. Las cámaras que cubren habitaciones distintas ofrecerán pocas oportunidades para corroborar entre cámaras. Una imagen existente acredita disponibilidad del archivo; la calidad óptica requiere inspección independiente.

Una confirmación geométrica de habitación se coteja con el track de la misma cámara y sesión, dentro del intervalo de visita. Si queda sin dueño se presenta en la cola de revisión. Las observaciones sucesivas permiten cambios de habitación. Confirmaciones simultáneas contradictorias quedan ambiguas. Una corrección manual tiene prioridad y conserva las observaciones anteriores. Las imágenes de habitación se guardan con nombre opaco cuando el detector aporta un recorte.

La identidad de cruce emplea cámara, instante de origen, sentido y geometría del cruce. Un reintento que conserva esos datos conserva el ID aunque cambien los contadores del proceso. Una nueva decodificación que asigne otros tiempos o coordenadas puede producir otro ID. La cola en memoria no garantiza recuperar trabajo todavía no persistido después de perder alimentación.

## API para C3

Todas las rutas requieren sesión. Las mutaciones requieren `X-CSRF-Token`. Se recomienda conservar una clave de idempotencia por acción de interfaz hasta recibir confirmación.

| Método y ruta bajo `/api/hotel` | Uso |
|---|---|
| `GET /visit-cases/indicators?month=YYYY-MM` | Cuatro totales en la misma instantánea SQLite |
| `GET /visit-cases?category=…&month=YYYY-MM&page=1&page_size=25` | Lista paginada. Cada clave del indicador se acepta como categoría |
| `GET /visit-cases/{id}` | Estado, entrada, salida, evidencias, habitaciones, historial y correcciones. Un ID de revisión de habitación devuelve su grupo de observaciones |
| `POST /visits/associate` | Vincular con `entry_event_id` y `exit_event_id`. Devuelve ID entero del vínculo y `canonical_id` |
| `DELETE /visits/{id_entero}/association` | Revocar esa versión del vínculo |
| `PATCH /visit-cases/{id}/room` y `PATCH /visit-cases/{id}/plate` | Corregir con `room_number` o `plate` |
| `POST /visit-cases/{id}/room-observations/{observacion}` | Asignar una observación pendiente |
| `POST /visit-cases/{id}/review` | Confirmar revisión humana del caso |
| `POST /visit-cases/{id}/exceptional-close` | Cierre excepcional con `reason`, reservado a administradores |

El mes se calcula en America/Guayaquil, desde el inicio local inclusive al inicio del siguiente mes exclusivo. Se respeta el desplazamiento de las fechas con zona. Las históricas sin zona se interpretan como locales. Sin mes se consulta todo el historial. El ancla temporal es la entrada para visitas, el instante de salida para salidas huérfanas y la primera observación pendiente para un grupo de habitación.

| Tarjeta | Clave | Unidad |
|---|---|---|
| Visitas completadas | `visits_completed` | Entrada con salida vinculada |
| Entradas sin salida | `entries_unpaired` | Entrada sin vínculo de salida vigente |
| Salidas sin entrada | `exits_unpaired` | Salida sin vínculo de entrada vigente |
| Revisión pendiente | `review_pending` | Un caso por visita o evento sin resolver y un caso por grupo de observaciones sin dueño de una misma cámara, sesión y track |

Revisión puede solaparse con las otras tarjetas. Vincular la salida a una entrada de otro mes atribuye la visita completa al mes de entrada. Los indicadores reflejan las asociaciones vigentes cuando se consulta.

## Reconciliación y restauración ensayadas

Preparar primero una instantánea SQLite con la API `Connection.backup` en un directorio privado. Para un corte operativo coherente de las dos bases y los archivos asociados se requiere coordinar la pausa de escritores y conservar también el árbol de evidencias. El arranque del servicio crea el esquema. El llenado histórico se ejecuta explícitamente antes de utilizar las consultas canónicas con una base anterior.

Ejemplo reproducible sobre la copia local de ensayo. El segundo comando requiere un destino nuevo.

```sh
.venv/cpu/bin/python scripts/reconcile_visits.py --source .venv/c2/history/pre-c2.db
.venv/cpu/bin/python scripts/reconcile_visits.py --source .venv/c2/history/pre-c2.db --output-copy .venv/c2/history/nueva-copia.db
```

Ambos comandos trabajan sobre una instantánea. Se publican copias con permisos 0600 después de verificar integridad y claves foráneas. El reconciliador respeta pares manuales explícitos compatibles y exige corroboración para los automáticos. Las propuestas contradictorias se cuentan individualmente como ambiguas. Los informes solo contienen conteos.

El ensayo local utilizó una copia del histórico de eventos importada con el código anterior a C2. Hubo 5.768 casos, 2.868 entradas sin salida, 2.874 salidas sin entrada y 26 eventos sin dirección resuelta. Los 5.768 quedaron en revisión. Hubo cero pares reconstruidos, propuestas ambiguas o conflictos y 247 eventos sin ruta de evidencia registrada. La prueba con este extracto mide preservación. Los escenarios positivos y los conflictos entre fuentes se ejercitaron con fixtures.

Para ensayar rollback, abrir la instantánea previa en modo de solo lectura y restaurarla con SQLite en otro nombre nuevo. Por ejemplo, con la utilidad SQLite y un directorio privado:

```sh
sqlite3 -readonly pre-c2.db ".backup 'restaurada-nueva.db'"
sqlite3 -readonly restaurada-nueva.db 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
```

En el ensayo se utilizó la API de backup de Python. Se verificó integridad, ausencia de infracciones de claves foráneas e igualdad fila a fila de las cinco tablas de origen tanto en la copia reconciliada como en la restaurada. El rollback operativo implica volver al código y a la instantánea coordinada del mismo corte. Las escrituras posteriores al corte requerirían conservación y conciliación separadas.

## Validación

La suite se ejecutó con Python 3.12 en una copia aislada del árbol del proyecto con Chromium local. `tests/test_canonical_visits.py` contiene las pruebas de ciclo, reintentos, ambigüedad, asociación sin placa, historial, correcciones, límites mensuales y migración. Los archivos de base, capturas e informes privados de ensayo permanecen fuera de Git. La validación de campo de A1/A5 y la integración visual de C3 siguen pendientes.
