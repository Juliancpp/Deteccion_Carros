# Autenticación y visitas manuales

## Inspección y decisiones

El backend existente es FastAPI (`app/api.py`). Sirve `web/index.html`,
`web/login.html` y los archivos JS/CSS de `web/` mediante `/static`.
`app/database.py` usa SQLAlchemy con `database/eventos.db` (tabla legacy `eventos`).
El dashboard operativo usa `app/hotel/store.py`, sqlite3 y
`database/hotel_mvp.db`: `hotel_vehicle_events`, `hotel_visits`, `hotel_event_audit`.
Los identificadores de eventos son TEXT. Las entradas son `ENTRADA_NORMAL` y
`ENTRADA_POR_SALIDA`; las salidas incluyen `SALIDA_NORMAL`, `SALIDA_POR_ENTRADA`
y `SALIDA_SIN_ENTRADA`. `EVENTO_NO_RESUELTO` no se puede asociar.

Antes, el login comparaba variables de entorno, admitía Basic Auth y podía
desactivarse. La cookie firmada no se revocaba en servidor. La edición sobrescribía
`plate` y reconstruía las visitas automáticas. Las pruebas existentes usan pytest.

`hotel_visits` interviene en `classify_event`, por lo que se conserva íntegra y el
detector sigue usándola. El nuevo registro `manual_visits` es la fuente de las
visitas confirmadas en dashboard/API. No se importan relaciones automáticas como
si hubiesen sido confirmadas por una persona. No se cambian OCR, seguimiento,
cámaras, RTSP ni clasificación. El endpoint web de reconstrucción devuelve 409.

## Instalación y primer usuario

```bash
venv/bin/pip install argon2-cffi
venv/bin/python scripts/create_user.py --username julian --role admin
```

La contraseña se solicita dos veces sin mostrarla (mínimo 12 caracteres); no se
acepta mediante argumento ni se almacena en claro. El mismo comando con
`--role operator` crea operadores. Un username duplicado falla sin reemplazarlo.
Ambos roles pueden corregir placas, revisar eventos y asociar/desasociar visitas.
No hay endpoint de gestión de usuarios: el alta requiere acceso local al servidor.
`users.is_active=0` impide login e invalida las sesiones existentes inmediatamente.

Al iniciar FastAPI o ejecutar el CLI se crean de forma idempotente las tablas
`users`, `auth_sessions`, `manual_visits`, `event_plate_corrections` y `audit_log`.
No se borran ni reescriben los eventos o visitas existentes. Antes de desplegar,
haz una copia consistente con SQLite Backup API (incluye los cambios en WAL).
No copies solamente el archivo .db mientras hay escritores activos.
El código actualizado se activa al reiniciar únicamente el servicio dashboard.

Las contraseñas usan Argon2id. Cada sesión usa un token aleatorio de 256 bits;
SQLite conserva su SHA-256, expiración y token CSRF ligado a esa sesión.
Logout revoca la sesión en SQLite. La cookie es HttpOnly, SameSite=Lax y dura
8 horas por defecto (configurable entre 5 minutos y 24 horas).
En producción configura HTTPS y `DASHBOARD_ENV=production`; esto fuerza Secure.
Para HTTP local usa `DASHBOARD_ENV=development`, `DASHBOARD_COOKIE_SECURE=false`.
El modo auto usa el esquema de la petición, interpretado por Uvicorn; configura
solamente proxies de confianza. No se guarda la sesión en localStorage/sessionStorage.
El token CSRF se obtiene con `/api/auth/me` y se envía en `X-CSRF-Token` para escrituras.
El bloqueo de intentos de login es temporal por IP y proceso, apropiado al despliegue
existente de un worker. Un despliegue con varios workers necesitaría compartir ese contador.

## Rutas

| Método | Ruta | Función |
| --- | --- | --- |
| POST | `/api/auth/login` | username/password de SQLite |
| POST | `/api/auth/logout` | Revocar sesión |
| GET | `/api/auth/me` | id, username, role y CSRF; nunca hashes |
| GET | `/api/hotel/events` | Eventos y filtro por placa efectiva |
| GET | `/api/hotel/events/{event_id}` | Original, corrección, visita y auditoría |
| PATCH | `/api/hotel/events/{event_id}` | JSON `{"plate":"PBC1234"}` |
| GET | `/api/hotel/events/entries/unpaired` | Entradas disponibles |
| GET | `/api/hotel/events/exits/unpaired?entry_event_id=...` | Salidas posteriores, ordenadas |
| POST | `/api/hotel/visits/associate` | entry_event_id + exit_event_id |
| GET | `/api/hotel/visits` | Visitas manuales completadas, paginadas |
| GET | `/api/hotel/visits/{visit_id}` | Visita con ambos eventos |
| DELETE | `/api/hotel/visits/{visit_id}/association` | Elimina sólo la relación |
| GET | `/api/hotel/summary` | Contadores operativos |

Sin sesión, la API responde 401 y `/` redirige a `/login`. Las escrituras necesitan
CSRF. Los archivos de evidencia también requieren autenticación. `/api/health`
sigue disponible para supervisión. Las asociaciones usan `BEGIN IMMEDIATE`, claves
foráneas y restricciones UNIQUE; un conflicto devuelve 409, datos inválidos 422,
un evento inexistente 404. Se permite igualdad de tiempos. Los tiempos sin zona
se interpretan según la zona local del servidor, igual que el histórico existente.

## Placas, visitas y contadores

`hotel_vehicle_events.plate` permanece como valor original registrado.
`event_plate_corrections` guarda `plate_manual`, autor y fecha. La API expone
`plate_original`, `plate_manual`, `plate_effective` y el alias compatible `plate`.
Se usa la manual cuando existe. Se eliminan espacios y guiones, conforme a la
convención OCR existente; se convierte a mayúsculas. Vacíos se rechazan; no se
exige un patrón nacional específico. La auditoría conserva cada valor anterior.
Las correcciones históricas anteriores a esta versión siguen en `hotel_event_audit`;
no se inventa un OCR original si ya había sido sobrescrito antes de esta migración.

Por ahora sólo se persisten visitas `COMPLETED`: entrada y salida válidas,
confirmadas manualmente. Una entrada sin asociación representa conceptualmente
una visita abierta; no se crea una fila OPEN. Desasociar borra sólo la fila de
`manual_visits`, dejando `VISIT_UNLINKED` con los datos de la relación en auditoría.
Los IDs de visita no se reutilizan. Cambiar la placa nunca altera la asociación.

Los cinco contadores del dashboard cubren **todo el histórico**, independientemente
de los filtros de la tabla: `entries`, `exits`, `visits_completed`,
`entries_unpaired`, `exits_unpaired`. Una visita cuenta una vez, incluso si la salida
ocurrió otro día. Los eventos no resueltos no suman como entrada ni salida.
Los campos legacy de resumen se conservan para compatibilidad, pero no alimentan
estos cinco contadores. Las consultas de eventos se filtran en memoria, adecuado
al volumen actual; para un histórico grande conviene llevar esa paginación a SQL.
El exportador Excel existente conserva su formato y sus datos del detector
(`hotel_visits` y placas originales); las visitas manuales y placas corregidas
se consultan en el dashboard y las rutas indicadas arriba.

## Validación

```bash
venv/bin/pip install httpx
venv/bin/pytest -q
```

`tests/test_manual_administration.py` cubre permisos, cookies, CSRF, expiración,
usuarios inactivos, correcciones, preservación de evidencia, orden de candidatos,
cronología, asociaciones concurrentes, desasociación, auditoría y contadores.
