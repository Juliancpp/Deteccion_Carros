# Diagnóstico y reparación del dashboard — 2026-09-13

## Evidencia obtenida antes de reparar

- `deteccion-carros.service` y `deteccion-dashboard.service`: active/running.
  El detector conserva PID 392732; no se modificó ni reinició.
- Comparación automática de todos los IDs literales de `web/app.js` contra
  `web/index.html`: **ninguno faltante en los archivos actuales**.
- `/static/app.js` respondió 200, 21431 bytes y SHA-256
  `aed05c9868b0d8d4e8b2483c73977924655dac7390e5dd2ad0ddebd30210cbf4`,
  idéntico al archivo local antes de esta reparación.
- Cabeceras anteriores: ETag y Last-Modified, sin Cache-Control. La plantilla
  Nginx del repositorio además recomendaba `expires 1d; Cache-Control public`.
  No se encontró una configuración Nginx activa accesible; la plantilla no prueba
  por sí sola qué proxy utiliza el navegador del operador.
- Los logs registraron login 200 y dashboard 200 del operador, sin peticiones
  posteriores de ese navegador a `/api/auth/me` o a los endpoints operativos.
- Chromium, sirviendo el JavaScript anterior (`git show HEAD:web/app.js`) sobre
  el HTML actual, reprodujo `Cannot read properties of null (reading 'addEventListener')`.
  El responsable es `btnRebuild`, eliminado del HTML nuevo. Permaneció exactamente
  `Cargando detector...` y no se llamó `/api/auth/me`.
- Una carga limpia con el JavaScript actual sí completó el flujo administrativo.

La incompatibilidad HTML nuevo / JS anterior está reproducida. La caché del
navegador afectado no se pudo inspeccionar directamente: su participación en el
incidente es una inferencia consistente con los logs, la URL sin versión y la
reproducción. No se encontró un ID ausente en la pareja actual ni un fallo del
detector que explicara ese texto inmóvil.

## Reparación

`app/api.py` sirve HTML con versiones SHA-256 de los assets: el contenido nuevo
genera otra URL, estable entre peticiones. HTML/login usan no-store y los estáticos
de FastAPI requieren revalidación. `/static/index.html` redirige a `/`, evitando
otro punto de entrada sin versión. La plantilla Nginx también exige revalidación.

`web/app.js` ya no registra listeners de componentes secundarios en el ámbito
global. `initializeDashboard()` valida primero la sesión, inicializa cada módulo
con aislamiento de errores, carga datos con `Promise.allSettled` y sólo entonces
inicia los cuatro timers. La inicialización y el polling son idempotentes.
Los elementos obligatorios de cada feature se verifican y se registran los faltantes;
no se ocultan errores de HTML. Una asociación ausente no impide status, eventos,
estadísticas ni logout. Una sesión inválida detiene el polling y redirige al login.
Los fallos de snapshots quedan aislados. Se añadieron logs con el nombre de la
operación, sin contraseñas, cookies ni tokens.

También se corrigió el filtro de texto vacío en `/api/hotel/events`: `camera=`,
`event_type=` o `plate_status=` significan sin filtro, conservando el valor booleano
false en `review_required`. No fue la causa del bloqueo de inicialización.
El CLI de usuarios ahora imprime la ruta absoluta de la base elegida.

## Bases, consultas y datos

| Base absoluta | Uso | Recuento observado |
| --- | --- | --- |
| `/home/relax_julian/Deteccion_Carros/database/eventos.db` | Tabla legacy `eventos`, `/api/events` (últimos 50) y `/api/events/{limit}` (máximo 500), `/api/stats` | 2015 eventos |
| `/home/relax_julian/Deteccion_Carros/database/hotel_mvp.db` | `hotel_vehicle_events`, dashboard `/api/hotel/*`, usuarios y administración | 100 eventos |

`users` está en **hotel_mvp.db**: 1 usuario (`admin`, rol admin, activo).
Su schema contiene id, username UNIQUE, password_hash, role, is_active,
created_at, updated_at, last_login. Se verificó sin mostrar hashes ni sesiones.
Las rutas se construyen desde `app/config.py` y no dependen del directorio actual.

La consulta administrativa usa LEFT JOIN a `event_plate_corrections` y
`manual_visits`: los registros anteriores sin esas relaciones permanecen visibles.
Se ordena por `occurred_at DESC`; se filtra y pagina en memoria, con tamaño máximo
100 y sin filtro de fecha predeterminado. Los límites de fecha usan los timestamps
ISO almacenados; una fecha final sola incluye hasta 23:59:59.999999. No se introdujo
conversión de zona horaria ni recorte por día en el histórico. La UI presenta las
fechas con la zona del navegador; los cinco contadores cubren todo el histórico.

Resultado real: entradas **7**, salidas **2**, visitas completadas **0**, entradas
sin salida **7**, salidas sin entrada **2**. Los otros **91** eventos son
`EVENTO_NO_RESUELTO`: aparecen en el historial, pero no cuentan como ENTRY/EXIT.
`hotel_visits` y `manual_visits` tenían 0 filas. No se fusionaron bases ni se
modificaron eventos, usuarios, contraseñas, asociaciones o backups de producción.

Backups preexistentes verificados y conservados:
`backups/eventos_20260913_041117.db` y `backups/hotel_mvp_20260913_041117.db`.
Esta reparación no requiere nueva migración.

## Pruebas reproducibles

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m playwright install chromium --only-shell
venv/bin/pytest -q
```

190 tests aprobados, 0 fallos. Incluyen auth, usuario inactivo, contraseña errónea,
logout, CSRF, status fixture, snapshots protegidos, datos legacy, placas,
asociaciones concurrentes y desasociación. `tests/test_dashboard_regression.py`
añade comprobación DOM/IDs, hash del JS servido, HTML versionado, API autenticada
y ejecución real de Chromium: corrección → asociación → COMPLETED → desasociación
→ logout. También simula un modal ausente, snapshots con 503, sesión con 401,
JS obsoleto en la URL antigua y varias llamadas de inicialización/polling.

Una prueba adicional sobre copias consistentes de las dos bases reales mediante
SQLite Backup API mostró en Chromium los 100 eventos, los contadores 7/2/0,
A1/A5 conectadas y ambas imágenes decodificadas. No alteró la base original.
La reproducción con el JS anterior fue una segunda comprobación diagnóstica.

## Activación y verificación del operador

Sólo necesita reiniciarse el dashboard:

```bash
sudo systemctl restart deteccion-dashboard.service
```

No reiniciar `deteccion-carros.service`. Si sudo solicita contraseña, ejecutar el
comando desde una terminal del operador; no enviar esa contraseña al asistente.

1. Abrir `/login`, entrar con admin y comprobar que llega al dashboard.
2. Confirmar `Detector Online`, A1/A5 conectadas e imágenes visibles.
3. Confirmar 100 registros históricos (salvo nuevos eventos), contadores 7/2/0/7/2.
4. Abrir una placa; comprobar original, guardar una corrección y volver a consultarla.
5. En una entrada disponible, elegir una salida posterior y confirmar COMPLETED.
6. Desasociar; ambos eventos deben seguir disponibles y conservarse.
7. Cerrar sesión y abrir `/`: debe regresar al login.

Las acciones 4–6 cambian datos administrativos reales y deben hacerse sobre eventos
que el operador quiera corregir. Las pruebas automatizadas ya recorren esas acciones
sobre datos aislados. No se usó la contraseña de admin de producción durante las pruebas.
