# Dashboard operativo de Hotel Relax · C3

## Auditoría sobre 9997d84

| Hallazgo | Origen en C2 | Efecto observado en código | Cambio en C3 |
|---|---|---|---|
| Seis indicadores y dos unidades de conteo | `web/index.html` y `loadStats` | Mezclaba totales de eventos con visitas en la misma fila. Las tarjetas eran artículos sin acción. | Cuatro botones. Cada total y su lista usan la categoría canónica y el mismo mes. |
| Historial inconsistente | `loadEvents`, `renderEvents`, `renderVisits` | El tab de visitas enviaba filtros sin soporte y podía volver a eventos tras un error de red. El buscador carecía de evento asociado. | Búsqueda, filtros, orden y paginación en SQLite. Estados de carga, vacío y error independientes. |
| Detalle y corrección confundidos | `openDetail`, `openCorrection` | El detalle mostraba diagnóstico de un evento dentro del contenedor de corrección. Las acciones dependían del tipo del evento. | Diálogo de visita y editor separados. El estado canónico y el rol determinan las opciones. |
| Exportación desactualizada | `bindUI`, handler de `btnExport` | Descargaba una ruta fija sin generar el reporte. El enlace de navegación Reportes carecía de sección. | Excel generado por petición, con filtros y resumen del mismo conjunto de filas. |
| Cámara e imagen confundidas | `loadStatus`, `refreshSnapshot` | Una descarga de imagen mostraba “EN LÍNEA”. Cada refresco creaba otra URL de objeto sin liberarla. | La conexión procede del estado del detector y su antigüedad. La imagen informa su propia fecha y vigencia. |
| Configuración sin destino | Navegación en `web/index.html` | El enlace tenía un fragmento sin sección ni formulario funcional. | Preferencia persistente con rol, revisión, auditoría y restauración. |

## Matriz de controles

Las rutas de la tabla parten de `/api/hotel`, salvo las de autenticación. Los nombres de prueba sin prefijo de archivo corresponden a `tests/test_dashboard_c3.py`. Los escenarios previos identificados como *regresión* están en `tests/test_dashboard_regression.py`.

| Control y acción esperada | Handler o mecanismo | Endpoint | Estado y prueba |
|---|---|---|---|
| Usuario, contraseña, mostrar/ocultar e iniciar sesión | Formulario y botón de `web/login.html` | `POST /api/auth/login` | Funcional. `test_login_controls_and_combined_history_filters` cubre credenciales inválidas y válidas |
| Marca y enlaces Control, Historial, Reportes, Cámaras y Configuración | Fragmento + `navigate` | Consultas de cada sección | Corregido. `test_browser_history_report_navigation_mobile` |
| Ir al contenido y abrir historial | Enlaces internos; foco y navegación nativos | Local | Funcional. Destinos `main` e `historial`; abrir historial probado en Chromium |
| Cerrar sesión | `bindUI`, `request`, `login` | `POST /api/auth/logout` | Funcional. *Regresión*, `test_browser_complete_flow` |
| Periodo del control y cuatro tarjetas | `selectMonth`, `loadStats`, `setCategory` | `GET /visit-cases/indicators` y `GET /visit-cases` | Corregido. `test_browser_history_report_navigation_mobile` compara cada total con el listado |
| Buscar por ID, placa o habitación; filtrar por mes, fechas, cámara, estado, habitación y placa; ordenar, filtrar y limpiar | `bindUI`, `resetFilters`, `loadHistory` | `GET /visit-cases` | Corregido. `test_login_controls_and_combined_history_filters`, `test_history_and_workbook_share_rows`, `test_browser_history_report_navigation_mobile` |
| Página anterior y siguiente | `bindUI`, `loadHistory` | `GET /visit-cases?page=…&page_size=…` | Funcional. `test_browser_admin_rollback_pagination_and_close` |
| Ver detalle y cerrar con botón o Escape | `openDetail`, `closeDialog`, diálogo nativo | `GET /visit-cases/{id}` | Corregido. `test_browser_media_room_and_review`, `test_browser_history_report_navigation_mobile` |
| Editar placa o habitación, guardar y cerrar | `openEditor`, `saveCorrection` | `PATCH /visit-cases/{id}/plate` o `/room` | Corregido. *Regresión*, `test_browser_complete_flow`, y `test_browser_media_room_and_review` |
| Asociar salida, seleccionar radio, ver más, confirmar o cancelar | `openAssociation`, `appendExits`, `associate` | `GET /visit-cases/{id}/available-exits`, `POST /visits/associate` | Funcional. *Regresión*, `test_browser_complete_flow`, y `test_browser_more_exits_and_missing_media` |
| Desasociar tras confirmación | `unlink` | `DELETE /visits/{idAsociacion}/association` | Funcional. *Regresión*, `test_browser_complete_flow` |
| Marcar y resolver revisión | `openDetail`, `action` | `POST /visit-cases/{id}/flag` o `/review` | Funcional. `test_browser_media_room_and_review` |
| Asignar observación de habitación sin dueño | `openDetail`, `mutate` | `POST /visit-cases/{id}/room-observations/{observacion}` | Funcional. `test_browser_unassigned_room_and_expired_session` |
| Cierre excepcional, motivo y confirmación | `openDetail`, `action` | `POST /visit-cases/{id}/exceptional-close` | Funcional para administrador. `test_browser_admin_rollback_pagination_and_close` y prueba de ocultación para operador |
| Ver y abrir fotografía, inspeccionar hipótesis/región OCR, reproducir video | `eventPanel`, `addMedia`, `evidencePreview`, visor y reproductor nativos | `GET /evidence/{id}/{photo,clip,índice}` o `/evidence/{observacion}/room` | Funcional. `test_browser_media_room_and_review`, `test_browser_more_exits_and_missing_media` y *regresión* `test_ocr_detail_exposes_observation_and_protected_frame` |
| Mes, rango, estado, generar y descargar Excel | `report`, `discardReport` y enlace de descarga | `POST /reports/visits` | Corregido. `test_browser_history_report_navigation_mobile` abre la descarga y simula un fallo. `test_history_and_workbook_share_rows` compara IDs con la API |
| Consultar A1/A5, estado e imagen | `loadStatus`, `refreshSnapshot` | `GET /monitor`, `GET /cameras/{id}/snapshot?allow_stale=true` | Corregido. `test_monitor_stale_snapshot_and_heartbeat`, pruebas de navegación y *regresión* de imagen ausente |
| Leer preferencias, guardar tamaño de página y restaurar valores | `loadSettings`, `saveSize` | `GET/PATCH /display-settings` | Funcional. `test_settings_roles_history_and_csrf`, `test_browser_admin_rollback_pagination_and_close` |
| Caducidad de sesión y errores de operación | `request`, mensajes en formulario o diálogo | HTTP 401, 403, 409, 422 y fallo de reporte | Funcional. Pruebas de caducidad, CSRF, conflictos de revisión y error de Excel |

`test_rendered_buttons_have_click_or_form_handler` examina en Chromium los botones del DOM inicial y comprueba su listener de clic o el de envío de su formulario. Los botones creados después se ejercitan en los recorridos funcionales.

## Contrato de datos

- Las categorías y el mes utilizan `visits.query` de C2. El mes de una visita es el de su entrada, incluso cuando la salida sucede al mes siguiente. Una salida huérfana usa su propio instante. Revisión pendiente puede solaparse con los otros tres indicadores.
- Las fechas son inclusivas en America/Guayaquil. SQLite utiliza el comienzo del día siguiente como límite exclusivo. Los filtros se combinan. La cámara puede ser la de entrada o la de salida. La placa filtrada es la efectiva; el detalle también conserva la lectura original. El orden secundario por ID estabiliza la paginación.
- La procedencia del importador es `legacy_eventos`. La lista identifica esos registros. Las observaciones sin visita se consultan desde revisión pendiente o con el estado de habitación sin asignar.
- Los reportes usan la misma selección y el mismo orden que el historial. Cada POST devuelve un XLSX nuevo y el número de registros en `X-Visit-Count`. Cambiar filtros o fallar una nueva generación invalida la descarga anterior. El Excel no contiene imágenes ni rutas de disco.
- El detalle sirve evidencias por identidad e índice con sesión y comprobación del directorio autorizado. Los fragmentos gráficos representan las coordenadas guardadas por C1 cuando existen. El navegador libera los objetos de snapshot y descarga que sustituye o abandona.
- El monitor usa archivos locales del runner. Una señal de más de 30 segundos deja de confirmar conexión; una imagen mayor de 10 segundos se etiqueta como antigua. Los FPS proceden del detector. La última lectura OCR se identifica por cámara y fecha; las métricas de sesión se presentan como agregadas de ambas cámaras. Un runner anterior puede omitir geometría o métricas.
- Configuración permite cambiar de 1 a 100 visitas por página. Los cambios se aplican al dashboard y registran actor, revisión y valor. Restaurar un valor crea una nueva revisión. Una revisión concurrente devuelve 409. Las dos ventanas de asociación de C2 son de consulta. El módulo no recibe credenciales ni rutas configurables.

## Ensayo local y Fase D

La ejecución completa sobre Fedora 45 y Python 3.12.16 terminó con 264 pruebas aprobadas en 25,38 segundos. Incluye las 254 pruebas de la línea base y diez nuevas.

Las pruebas arrancan FastAPI en loopback, crean usuarios y bases temporales y sirven capturas de fixture. El MP4 incluido dura aproximadamente medio segundo y contiene un campo de color sintético. Las capturas de pantalla de esta carpeta corresponden a estos datos de prueba.

Desde la copia aislada de C3 en esta PC:

```sh
cd .venv/c3/source
PLAYWRIGHT_BROWSERS_PATH="$PWD/../../browsers" HOTEL_TEST_GUI=1 ./venv/bin/python -m pytest -ra --junitxml=../suite.xml
```

La copia incluye los módulos, el frontend, los scripts, las pruebas, la configuración versionada y los pesos locales para las pruebas anteriores. Tiene su propio directorio de base de datos. El informe XML y el registro de ejecución quedan en `.venv/c3/`. Para repetir con otra revisión se debe actualizar el código de esa copia.

Para Fase D queda medir latencia con el volumen histórico esperado, verificar en Safari/iOS físico y contrastar con material autorizado la cobertura óptica y las asociaciones de A1/A5. El ensayo C3 cubre Chromium de escritorio y un viewport de 390 × 844. Las métricas de cámara en las capturas y pruebas son sintéticas.
