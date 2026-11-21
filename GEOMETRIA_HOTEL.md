# Geometría por cámara en Hotel Relax

## Recorrido del frame

El servicio del repositorio ejecuta `scripts/run_hotel_mvp.py`. Carga `app.settings.load_settings(override_env=True)` desde la raíz. Las credenciales y los parámetros de A1/A5 proceden de `.env`. Los umbrales generales de YOLO, corredor y puntuación de habitaciones se encuentran en `app/config.py`.

`MultiCameraCoordinator` alterna cámaras, procesa el último frame disponible con un único YOLO y conserva un ByteTrack por cámara. En el mecanismo original, `_passage_detector_from_camera` elige `VirtualLineDetector` si `LINE_ENABLED` es verdadero (valor por defecto). En otro caso construye `PassageCorridorDetector` con la polilínea, sentido, anchura y progreso de disparo. La configuración de Fedora usa el primer camino en las dos cámaras, a 5 FPS objetivo. La puerta definida en los settings tampoco selecciona por sí sola el detector del coordinador.

Una confirmación alimenta una cola de `EventCandidate`. El runner alimenta en paralelo el buffer de frames y `HotelPhase1Analyzer` con los resultados recientes. El analizador usa `DirectionalCorridor`, el centro inferior del vehículo, `RoomDetector` y el enlazador de fragmentos. Estima dirección y habitación probable o confirmada. Los polígonos originales se cargan únicamente para las habitaciones 1–3 y 4–6. El consumidor espera una decisión o un timeout, consulta OCR y evidencia y escribe en SQLite. La clasificación comercial depende de cámara, movimiento y visita abierta. El nombre de la fuente `phase1_analyzer` por sí solo no describe este circuito.

## Configurar y calibrar en Fedora

Los dos JSON de `config/geometry/` recuperan los vértices locales de las habitaciones visibles en las capturas de calibración. Las dos líneas de cada borrador comparten la antigua puerta como referencia editable. Están apagadas. Las imágenes anotadas disponibles muestran corredor y habitaciones. La posición, el sentido y la separación física de los umbrales principales requieren revisión con vídeo de la instalación.

### Editor visual de cinco minutos

Ejecuta `venv/bin/python scripts/calibrate_geometry.py` para abrir el selector en Fedora (Tk y Pillow). Elige A1 o A5 y una fuente. **Abrir captura local** utiliza la última imagen archivada de esa cámara en `data/rtsp_test`. **Capturar frame del DVR** obtiene una imagen fija de la fuente configurada. También puedes elegir un archivo o recuperar un borrador. El reloj comienza en 05:00 al mostrar el editor. Al cerrar vuelves al selector para iniciar la otra cámara.

Selecciona la figura en la lista. Haz clic para dibujar, arrastra un vértice para ajustar y arrastra el segmento para mover una línea completa. Un clic cerca de un lado inserta un vértice de polígono; el botón derecho sobre un vértice lo elimina. Usa **Borrar puntos** para redibujar, **Deshacer** (Ctrl+Z) para revertir una operación o **Restaurar figura inicial** para volver a la calibración cargada. La flecha marca el sentido del cruce. Las líneas y polígonos apagados conservan su etiqueta OFF. **Ver solo la seleccionada** ayuda a editar figuras superpuestas.

**Guardar revisión** (Ctrl+S) valida la configuración y genera juntos `geometry.json` y `preview.jpg` en una revisión nueva dentro de `data/runtime/calibraciones/<cámara>/<sesión>/`. Cada sesión incluye `frame.png` y `draft.json`. Los trazos parciales se conservan en el borrador y deben completarse o vaciarse antes de exportar. Las figuras vacías se omiten de la revisión. Al llegar a 00:00 se guarda el borrador y la ventana sigue disponible. Puedes pausar, reanudar o sumar cinco minutos. **Recuperar borrador** carga el mismo frame y los puntos en una sesión nueva. Las revisiones son archivos locales para inspección; su activación es un paso separado.

### Atajos de consola y exportación existente

Desde la raíz del repositorio, en el entorno CPU preparado según el README, generar una revisión usando un JPEG local. El directorio de salida debe ser nuevo.

```bash
venv/bin/python scripts/calibrate_geometry.py --camera A1 \
  --config config/geometry/A1.json --image data/rtsp_test/A1_2026-07-12_00-07-59.jpg \
  --edit acceso-entry --kind hotel_entry --enabled --output data/runtime/revision-1
```

Con la ventana activa, usa Borrar puntos, dibuja con el mouse y pulsa Guardar revisión. Con --output, el primer guardado se publica en ese directorio y los siguientes quedan en el directorio de la sesión. Continúa con `--config data/runtime/revision-1/geometry.json`, una salida nueva y `--edit acceso-exit --kind hotel_exit --enabled`. Para una agrupación, usa por ejemplo `--edit hab-4-a-6 --kind room`. Para un paso previo, dibuja otro polígono con `--edit aproximacion-hab-4-a-6 --kind approach`. Usa `--invert-direction` o `--no-invert-direction` para las líneas. Sustituye `--image RUTA` por `--camera-frame` para obtener un frame de la cámara configurada desde Fedora. Omite `--edit` y sus opciones para exportar JSON y JPEG sin interfaz gráfica. Cada revisión contiene la configuración completa de una sola cámara. Revisa `preview.jpg` antes de seleccionar el archivo. Un cambio de resolución conserva las proporciones si la cámara mantiene encuadre y relación de aspecto.

| Campo | Interpretación |
|---|---|
| `camera_id` | Debe coincidir con el ID de la cámara en settings. |
| `features[].id` | Identificador único dentro de la cámara. Acepta habitaciones y grupos. |
| `kind` | `hotel_entry`, `hotel_exit`, `room` o `approach`. |
| `points` | Dos extremos por línea o al menos tres vértices por polígono simple, con coordenadas en [0,1] desde arriba a la izquierda. |
| `enabled` | Activa la figura. Por defecto verdadero. |
| `invert_direction` | Invierte el sentido admitido en una línea. La flecha del JPEG indica el sentido de cruce positivo. Para una línea horizontal de izquierda a derecha, el positivo va hacia abajo. |
| `observations` | Observaciones necesarias para fijar el lado inicial y confirmar el cambio. Por defecto 2. |
| `margin` | Banda de incertidumbre alrededor del borde, en coordenadas normalizadas. Por defecto 0.005. |
| `ttl_seconds` | Máxima ausencia antes de olvidar la posición del track. Por defecto 3 s. |
| `use_for_visits` | Por defecto falso. Activarlo sustituye el disparador original de esa cámara por sus líneas hoteleras habilitadas. Requiere al menos una línea activa y permite una por sentido. |

Cada track empieza en estado desconocido. Se observa el punto medio inferior del bbox. El primer lado estable fija una referencia. Un cambio estable genera un evento. Para líneas se comprueba la intersección con el segmento finito y el sentido habilitado. Para zonas se emiten entrada y salida. El paso de aproximación solo emite al entrar. Permanencia, muestras en el borde y repetición de secuencia no generan otro evento. Se admiten reingresos. Cada zona se evalúa por separado, por lo que dos polígonos solapados pueden generar dos transiciones. El timestamp corresponde al frame de confirmación.

La identidad geométrica es cámara, sesión de tracker y track de ByteTrack. Se descarta el estado al reconectar o al reiniciar el vídeo. Las transiciones confirmadas persisten en `geometry_events` de `hotel_mvp.db`, con clave única para reintentos. El seguimiento físico entre cámaras, reinicios o IDs reasignados requiere evidencia adicional. Una desaparición dentro de un polígono no demuestra salida.

## Prueba local e incorporación gradual

1. Ejecuta `venv/bin/python -m pytest -q tests/test_hotel_geometry.py`. Usa frames y base de datos temporales. Para regresión completa usa `venv/bin/python -m pytest tests -q` con las dependencias de desarrollo y Chromium de Playwright. La versión de Starlette instalada en esta laptop también solicita `httpx2` para TestClient.
2. Para habilitar una revisión, configura en el `.env` **local** `CAMERA_A1_GEOMETRY_FILE=ruta/al/geometry.json`. Hay una clave equivalente para A5. Una ruta relativa parte de la raíz del repo. Comienza por una cámara y conserva `use_for_visits=false`. Para reproducir vídeos locales, configura `CAMERA_A1_VIDEO_PATH` y `CAMERA_A5_VIDEO_PATH` en Fedora. El runner normal escribe en las bases locales habituales.
3. Ejecuta `venv/bin/python scripts/run_hotel_mvp.py --camera A1 --duration 60 --latest-snapshots`. Comprueba la vista de cámaras en el dashboard. Consulta `/api/hotel/geometry-events?limit=100` con una sesión autenticada, o usa `sqlite3 database/hotel_mvp.db 'SELECT occurred_at,camera_id,track_id,feature_id,event_type FROM geometry_events ORDER BY rowid DESC LIMIT 30'`.
4. Compara contra trayectorias etiquetadas por cámara y habitación, incluyendo paso por delante, estacionamiento, marcha atrás, oclusión y cruce fuera de segmento. Registra aciertos, falsos positivos y omisiones por clase, por día y noche. Revisa el punto inferior sobre la calzada. Un polígono en la puerta vertical puede quedar por encima de las ruedas.
5. Solo después de verificar ambos sentidos en la imagen y en vídeo, habilita las líneas aceptadas y prueba `use_for_visits=true` en Fedora. Los cruces principales entran al consumidor OCR con dirección explícita. La asociación comercial de habitaciones sigue usando la puntuación de Fase 1. El historial geométrico conserva también IDs de grupo y aproximaciones. Los eventos de habitación se consultan en ese historial.

## CPU y estabilidad

El smoke de 12 s reprodujo el mismo MP4 local en dos contextos con YOLO11n/ByteTrack en CPU. Con objetivo 5 FPS, los valores finales fueron 3.5/3.6 FPS para A1/A5 y 63/113 ms medios de inferencia. Se procesaron 37/38 frames, con cierre correcto y cero transiciones en ese vídeo genérico. Es una comprobación de integración de corta duración. La carga sostenida con OCR, clips, tráfico real y distintas condiciones debe medirse aparte.

- Mantén primero 5 FPS y registra FPS efectivos, tiempo de inferencia, frames omitidos, CPU, RSS, reconexiones y cola de candidatos. El motor geométrico reutiliza bbox existentes y remite solo metadatos a SQLite fuera del hilo de inferencia.
- Ensaya una ROI de inferencia por cámara que incluya la aproximación y el espacio a ambos lados de cada umbral. Convierte bbox al sistema del frame completo antes de ByteTrack, geometrías, OCR y evidencias. Compara el mismo vídeo a igual tamaño de entrada del modelo. Un recorte puede mejorar el detalle del coche sin reducir la latencia de una entrada YOLO de tamaño fijo.
- Prueba 5→6→8 FPS, una cámara cada vez, únicamente si se sostiene el objetivo anterior. Con las tasas observadas, primero mide dónde se consume CPU. Comprueba también el coste de las copias de frame, del buffer OCR y de los overlays.
- Ajusta primero margen y observaciones con ground truth. Más observaciones reducen activaciones por oscilación y aumentan la probabilidad de perder cruces breves. Ensaya después el umbral YOLO en torno al 0.40 local y mide fragmentación de IDs y precisión. Conserva el último perfil validado.

## Despliegue posterior y rollback

Tras la aprobación de geometrías y pruebas en Fedora, versiona el código y exclusivamente los JSON aprobados. Propuesta de commit: `feat(hotel): detectar cruces y transiciones de habitación por cámara`. Tras el push autorizado, en Ubuntu registra el SHA en servicio y respalda por separado los archivos de configuración y SQLite mediante su mecanismo de backup. Haz `git pull --ff-only` de la rama aprobada, verifica el SHA, sincroniza dependencias CPU según DEPLOYMENT.md y configura el JSON versionado de una cámara. Reinicia los servicios `deteccion-carros` y `deteccion-dashboard`. Verifica salud, snapshots, journal y continuidad de visitas. Activa la segunda cámara tras revisar la primera.

Para revertir el disparador de visitas, selecciona un JSON con `use_for_visits=false`. Para desactivar toda la extensión, quita las claves `CAMERA_A*_GEOMETRY_FILE` y reinicia el detector. Para rollback de código, revierte el commit en Fedora, prueba, publica el revert aprobado y aplica pull y reinicio en Ubuntu. La tabla adicional se puede conservar para auditoría. Restaurar una copia anterior de SQLite descartaría los eventos posteriores al backup.

## Evidencia local de esta entrega

Las 14 pruebas específicas pasaron. Cubren geometría inválida, grupos, aproximación, inversión de sentido, puertas separadas, rodear un extremo, borde, ausencia, deduplicación, múltiples tracks, reconexión, exportación y entrega al consumidor hotelero. La suite completa obtuvo 203 aprobadas y dos fallos en Chromium. Se reprodujeron ambos en una copia limpia de `f3b40a6b`: `test_browser_complete_flow` y `test_browser_missing_feature_and_snapshot_failure_are_isolated` esperan seis filas de septiembre de 2026 mientras el dashboard inicia en el mes actual. La prueba autenticada de la API de geometrías pasó.

También se ejecutó el runner normal durante 8 s sobre vídeos locales con OCR en CPU, dos snapshots y exportación de Excel en un directorio temporal. Cerró correctamente. Las vistas previas se exportaron y revisaron sobre imágenes locales A1/A5. La edición interactiva se probó con selección de vértices simulada. Queda pendiente la validación humana de accesos y la medición de precisión en recorridos etiquetados.

## Revisión técnica posterior en Fedora

**Resultado de revisión.** Las líneas principales siguen desactivadas en los dos JSON y `use_for_visits` sigue en falso. La correspondencia lógica de las habitaciones se verificó contra el `.env` local. La precisión física y el coste a carga real quedan pendientes.

### Pipeline, evidencias y reconexión

En `app/multicamera/coordinator.py`, `_process_camera` obtiene el frame, llama a YOLO y después a ByteTrack. Llama a `_process_crossings` con la lista de vehículos del mismo frame antes de dibujar y publicar `CameraInferenceResult`. Con geometría configurada, registra todas las transiciones confirmadas en el diario. Con `use_for_visits=false`, el coordinador ejecuta también el detector original como único productor de candidatos comerciales. Un mismo cruce puede verse en el diario y en el historial comercial. Son dos observaciones relacionadas que no se deben sumar para contar visitas. Con `true`, la fábrica omite el detector anterior y solo HOTEL_ENTRY/HOTEL_EXIT generan candidatos para OCR. ROOM_ENTRY, ROOM_EXIT y ROOM_APPROACH terminan en el diario en ambos modos.

En `scripts/run_hotel_mvp.py` el runner vacía los lotes del diario y lee el resultado más reciente de cada cámara para alimentar el buffer OCR y `HotelPhase1Analyzer`. El consumidor corre en un hilo independiente y espera decisión de habitación o timeout. La geometría ve cada frame de inferencia, mientras el analizador puede saltarse resultados si el runner va retrasado. En modo operativo el sentido del segmento determina el movimiento del candidato. La lectura de placa, evidencia y reglas comerciales siguen en el consumidor.

Un cambio de ID inicializa la geometría del nuevo ID. El track que empieza dentro de una habitación puede generar una salida posterior, pero no una entrada retrospectiva. El desaparecido caduca al procesar otro frame tras el TTL. Dentro del TTL un ID continuo conserva su último estado y puede inferir un cruce entre observaciones. El enlazador de fragmentos de Fase 1 puede reunir indicios de OCR de tracks compatibles dentro de la sesión. No reconstruye cruces perdidos. Una reconexión o vuelta del MP4 cambia sesión, descarta lados y reinicia ByteTrack. La generación de captura permite descartar una lectura que coincidió con el cambio de conexión. El runner acepta secuencias bajas en sesiones nuevas. Un candidato pendiente conserva su frame y solo consulta evidencia OCR de su propia sesión.

### Integridad, acceso y riesgos

| Severidad | Hallazgo | Resultado |
|---|---|---|
| Alta | Reiniciar un tracker de Ultralytics podía reciclar un ID todavía activo en la otra cámara. Además, una reconexión podía mezclar habitación y OCR de dos vehículos con el mismo ID. | Corregido con asignación por tracker, sesión en candidatos/resultados y buffers aislados. Probado con ByteTrack real y con un candidato pendiente durante una reconexión simulada. |
| Alta | Una placa probable o una salida pendiente de revisión podía afectar una visita automática. Un reintento podía volver a aplicar un evento geométrico. | La fuente `geometry_gate` exige placa LEIDA, evidencia guardada y ausencia de motivos de revisión para modificar visitas. El consumidor manda habitación o placa probable a revisión. La misma regla se aplica al reconstruir. Evento y actualización de visita son transaccionales. Reintentos de un ID geométrico ya persistido conservan el original. La asociación manual sigue siendo una operación autenticada explícita. |
| Alta para activación | Confundir paso por delante con ingreso físico. | Los seis polígonos coinciden exactamente con los settings. A1 contiene 1, 2 y 3. A5 contiene 4, 5 y 6. La auditoría local estima solapamiento con el corredor de 10,15 % en hab-1 y cero en los otros cinco. ROOM_APPROACH describe entrada a la zona previa explícita, ROOM_ENTRY el cambio estable a dentro del polígono de habitación y ROOM_EXIT el cambio estable a fuera. La banda de borde y dos observaciones filtran roces breves. Una trayectoria sostenida en el área solapada sí puede dar falso positivo. Los borradores actuales no tienen polígonos de aproximación activos. El manifiesto de 25 imágenes tiene habitación y sentido UNKNOWN en todas. |
| Media | Interrupción abrupta entre detección y commit del diario, fragmentación de IDs, contención de CPU y OCR con tráfico real. | La deduplicación de SQLite protege los lotes repetidos. El buffer previo al commit reside en RAM. La cola comercial tiene capacidad limitada. Se necesita una prueba prolongada con vídeo representativo y métricas de retraso, descartes y reinicios. |
| Media de verificación | Histórico real disponible solo como cifra documentada y dos regresiones previas del dashboard. | Alcance de la verificación detallado a continuación. |

`geometry_events` contiene siete campos obligatorios. La PK compuesta es (session_id, camera_id, track_id, source_sequence, feature_id). SQLite crea su índice único y `geometry_time` indexa occurred_at descendente. Los triggers de escritura limitan el tipo a los cinco eventos, exigen IDs de texto no vacíos y track/secuencia enteros no negativos. No tiene FK: una observación puede existir sin candidato, placa ni visita, y las figuras se definen en un JSON externo. La integridad referencial de un ID de figura respecto a una versión antigua del JSON no se puede comprobar en SQLite. Conviene conservar el JSON de cada calibración aceptada.

En Fedora se abrió la base original en modo de solo lectura, se copió con la API de backup de SQLite y se inicializó dos veces **la copia**. Contenía cero eventos y el integrity_check devolvió ok. El histórico real de 1.914 legacy y 100 MVP consta en HISTORICAL_MIGRATION.md. En una base de prueba con esa distribución se compararon todas las columnas de todas las filas antes y después de dos inicializaciones. Coincidieron, se preservaron las visitas y pasó foreign_key_check. La geometría no requiere recalcular esos registros ni ejecutar el importador legacy. La migración anterior de columnas conserva su backfill para esquemas antiguos sin procedencia completa. Ahora aborta y revierte los cambios de esa migración si falla.

La API autentica la sesión activa del dashboard antes de consultar observaciones. Se probaron una tabla poblada, acceso anónimo, sesión válida y token revocado, además de rutas de eventos, visitas, capturas y exportación. Las respuestas privadas llevan no-store. GET geometry-events consulta la tabla con un límite parametrizado entre 1 y 500. El endpoint de salud expone disponibilidad. Todos los usuarios autenticados con acceso al dashboard pueden consultar el diario; no existe autorización por habitación.

### Pruebas y benchmark

Las 14 pruebas iniciales incluyen trayectorias positivas y negativas, siete variantes de configuración inválida, cruce exterior, rodeo del extremo, inversión, reingreso, exportación y compatibilidad. Esta revisión añadió nueve casos parametrizados o individuales entre geometría, ByteTrack y API. Cubren una sola publicación comercial en ambos modos, ID perdido, reinicio de secuencia, evidencia de otra sesión, paso por delante, reintento/reapertura/reconstrucción SQLite, 2.014 filas sintéticas, OCR de dos frames con JPEG escrito y acceso al diario poblado. Conjunto focal de geometría y ByteTrack: 29 aprobadas. Suite completa final: 212 aprobadas, 2 fallidas, 1 advertencia de obsolescencia de Starlette. El runner real con modelos locales, dos MP4 (mismo archivo), OCR en CPU, bases temporales y 8 s de ejecución generó ambos snapshots y el Excel y cerró correctamente.

En una extracción de `f3b40a6b` se verificó la identidad del código del dashboard, sus fixtures y el coordinador contra Git. Al ejecutar `pytest -q tests/test_dashboard_regression.py -k 'browser_complete_flow or missing_feature'` en esa copia, fallaron `test_browser_complete_flow` y `test_browser_missing_feature_and_snapshot_failure_are_isolated` en la misma aserción que la rama de trabajo: seis filas esperadas, una visible. Los seis eventos de la fixture corresponden a septiembre de 2026 y el filtro inicial selecciona el mes de ejecución.

Benchmark CPU, orden base/híbrida/híbrida/base, procesos separados, mismo MP4, mismos pesos locales, dos cámaras a 5 FPS objetivo, 3 muestras de calentamiento y 25 medidas por cámara. Se repitieron exactamente los mismos frames. Un lector decodificó seis frames del MP4 por muestra compartida y el coordinador procesó el sexto en cada cámara. Los tiempos de etapa son medias por frame procesado, excepto decodificación, que es por muestra compartida. La reproducción sin esperas mide coste de servicio. El FPS real del scheduler incluye también espera, dado que contabiliza el intervalo de 200 ms desde el fin de la última inferencia de cada cámara.

| Ejecución | YOLO ms | ByteTrack ms | Cruces ms | Dibujo ms | Pipeline ms | Decodificación local ms |
|---|---:|---:|---:|---:|---:|---:|
| Base 1 | 53,07 | 0,48 | 0,008 | 0,33 | 54,64 | 10,08 |
| Híbrida 1 | 114,67 | 0,97 | 0,012 | 0,88 | 117,41 | 12,98 |
| Híbrida 2 | 53,32 | 0,53 | 0,012 | 0,57 | 55,05 | 8,83 |
| Base 2 | 52,61 | 0,50 | 0,008 | 0,33 | 54,12 | 8,96 |

El mayor coste medido fue YOLO. Una corrida híbrida mostró una variación grande dentro de YOLO; estas muestras no permiten atribuirla a la extensión. Hubo cero tracks en este tramo. No ejercitó escritura por eventos ni OCR bajo demanda; las cámaras de vídeo local tampoco incluyen latencia del DVR, red ni dos decodificadores simultáneos. Para evaluar carga operativa, repetir por cámara con secuencias etiquetadas de día/noche, cruces y vehículos simultáneos, además de medir P95 de OCR por candidato, longitud de cola, FPS efectivos y frames omitidos. Los umbrales, el modelo y los FPS objetivo siguen en sus valores iniciales.

Desde Fedora, para reproducir las dos variantes, con un directorio temporal nuevo:

```bash
base_dir=$(mktemp -d)
git archive f3b40a6b | tar -x -C "$base_dir"
venv/bin/python scripts/benchmark_geometry.py --repo "$base_dir" --video data/videos/test.mp4
venv/bin/python scripts/benchmark_geometry.py --geometry --video data/videos/test.mp4
```

### Archivos del diff acumulado

| Archivos | Propósito |
|---|---|
| `.env.example`, `app/settings.py` | Documentar la ruta opcional y cargar/validar el JSON de la cámara correspondiente. |
| `config/geometry/A1.json`, `config/geometry/A5.json` | Borradores de geometría por cámara, líneas apagadas. |
| `app/hotel/geometry.py`, `app/hotel/geometry_overlay.py` | Validación geométrica y máquina de estados; representación de figuras, IDs y sentido. |
| `app/tracking/bytetrack_adapter.py`, `app/video/rtsp_stream.py` | Contador de IDs propio de cada tracker y generación asociada al frame de captura. |
| `app/multicamera/camera_context.py`, `app/multicamera/coordinator.py` | Estado por cámara/sesión, selección del disparador, observación posterior a ByteTrack y entrega de resultados. |
| `app/multicamera/event_candidate.py`, `app/multicamera/event_consumer.py` | Transportar procedencia y sesión; aislar OCR, guardar soporte de voto y revisar evidencia insuficiente. Los nuevos candidatos del coordinador conservan su UUID al persistir para evitar colisiones por hora e ID reciclado. |
| `app/hotel/phase1_analyzer.py`, `app/hotel/frame_buffer.py` | Identificar la sesión del analizador y separar muestras de OCR por sesión, cámara y track. |
| `app/hotel/models.py`, `app/hotel/store.py` | Procedencia del evento, diario, restricciones, migración y elegibilidad transaccional para visitas. |
| `app/api.py` | Consulta autenticada con límite de resultados, sin inicialización de esquema dentro de la lectura. |
| `scripts/run_hotel_mvp.py`, `scripts/calibrate_geometry.py`, `scripts/benchmark_geometry.py` | Integrar el diario/snapshots y el cambio de sesión, editar/exportar calibraciones y reproducir la comparación de rendimiento. |
| `tests/test_hotel_geometry.py`, `tests/test_bytetrack_adapter.py`, `tests/test_dashboard_regression.py` | Regresiones geométricas y de persistencia, aislamiento de IDs y pruebas autenticadas de la API. |
| `requirements.txt`, `README.md`, `GEOMETRIA_HOTEL.md` | Declarar Shapely, enlazar las instrucciones y conservar el diagnóstico, operación y revisión. |
