# Deteccion_Carros

El dashboard requiere usuarios SQLite y sesiones revocables. Consulta
[ADMINISTRATION.md](ADMINISTRATION.md) para crear el primer administrador,
asociar visitas y entender las métricas. Ese procedimiento sustituye las
instrucciones históricas de autenticación por variables de entorno.

## Sistema Profesional de Detección Vehicular con IA

Sistema de control de acceso vehicular basado en inteligencia artificial, diseñado para ejecutarse **on-premise** en hardware económico sin GPU dedicada.

![Python](https://img.shields.io/badge/Python-3.12-blue)
![YOLO](https://img.shields.io/badge/YOLO-11n-green)
![License](https://img.shields.io/badge/License-Private-red)

---

## 🎯 Objetivo

Detectar vehículos en tiempo real desde cámaras de seguridad, identificar placas y registrar eventos de entrada/salida para control de acceso.

## 🖥️ Hardware Objetivo

| Componente | Especificación |
|-----------|---------------|
| CPU | Intel Xeon E3-1230 (4C/8T) |
| RAM | 16 GB |
| Disco | 250 GB HDD |
| GPU | ❌ Sin GPU dedicada |

## 📦 Estructura del Proyecto

```
Deteccion_Carros/
├── app/
│   ├── main.py          ← Punto de entrada
│   ├── config.py         ← Configuración global
│   ├── detector.py       ← Detector YOLO
│   ├── tracker.py        ← (Fase 2) Seguimiento
│   ├── line_detector.py  ← (Fase 3) Línea virtual y eventos
│   ├── evidence.py       ← (Fase 4) Captura de evidencia
│   ├── ocr.py            ← (Fase 5) Lectura básica de placas
│   ├── database.py       ← (Fase 6) Persistencia SQLite
│   ├── api.py            ← (Fase 7) Dashboard web/API REST
│   ├── settings.py       ← (Fase 8A) Configuración segura DVR/RTSP
│   ├── multicamera/      ← (Fase 9A) Coordinador multicámara
│   └── video/
│       └── rtsp_stream.py ← (Fase 8B) Captura RTSP robusta
├── models/               ← Modelos YOLO
├── data/
│   ├── videos/           ← Videos de prueba
│   └── captures/         ← Capturas de evidencia
├── database/             ← Archivos SQLite
├── scripts/
│   ├── validate_camera_config.py
│   ├── test_rtsp_streams.py
│   └── test_multicamera_inference.py
├── web/
│   ├── index.html        ← Dashboard
│   ├── style.css         ← Estilos
│   └── app.js            ← Consumo API
├── requirements.txt
└── README.md
```

---

## 🚀 Instalación — Fase 1

### 1. Clonar o ubicarse en el proyecto

```bash
cd Deteccion_Carros
```

### 2. Crear entorno virtual

```bash
python3 -m venv venv
```

### 3. Activar entorno virtual

```bash
# Linux / Mac
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 4. Instalar dependencias

```bash
pip install -r requirements.txt
```

Para ejecutar pruebas automatizadas:

```bash
pip install -r requirements-dev.txt
```

> ⏳ La primera instalación descarga PyTorch y EasyOCR/modelos asociados. Puede tomar varios minutos.

### 5. Descargar modelo YOLO11n

El modelo se descarga **automáticamente** la primera vez que se ejecuta el sistema. Alternativamente, puedes descargarlo manualmente:

```bash
# Descarga automática al ejecutar
python app/main.py

# O descarga manual
python -c "from ultralytics import YOLO; YOLO('yolo11n.pt')"
```

### 6. Colocar un video de prueba

Coloca un archivo de video en:

```
data/videos/test.mp4
```

Puedes usar cualquier video con tráfico vehicular. Videos de prueba recomendados:
- Grabación de cámaras de tráfico
- Videos de estacionamientos
- Cualquier video con coches, motos, buses o camiones

---

## ▶️ Ejecución

### Usando el video configurado (config.py)

```bash
python app/main.py
```

### Especificando un video

```bash
python app/main.py --video data/videos/mi_video.mp4
```

### Usando la webcam

```bash
python app/main.py --video 0
```

### Usando un stream RTSP manual

```bash
python app/main.py --video "<URL_RTSP_LOCAL>"
```

Evita compartir logs si contienen URLs RTSP completas con credenciales. La Fase
8A prepara configuracion segura, pero aun no conecta las camaras DVR al pipeline
principal.

### Dashboard web

```bash
uvicorn app.api:app --host 0.0.0.0 --port 8000
```

Luego abre:

```text
http://localhost:8000
```

---

## ⌨️ Controles

| Tecla | Acción |
|-------|--------|
| `Q` | Salir del programa |
| `P` | Pausar / Reanudar |
| `S` | Guardar captura del frame actual |

---

## ⚙️ Configuración

Edita `app/config.py` para ajustar:

| Parámetro | Descripción | Default |
|-----------|------------|---------|
| `CONFIDENCE_THRESHOLD` | Umbral mínimo de confianza | `0.40` |
| `INFERENCE_SIZE` | Tamaño de inferencia YOLO | `480` |
| `FRAME_SKIP` | Procesar 1 de cada N frames | `1` |
| `DISPLAY_WIDTH` | Ancho de ventana de visualización | `960` |
| `LINE_POINT_A` | Primer punto de la línea virtual | `(300, 300)` |
| `LINE_POINT_B` | Segundo punto de la línea virtual | `(900, 300)` |
| `LINE_DIRECTION` | Orientación usada para entrada/salida | `"horizontal"` |
| `LINE_INVERT_DIRECTION` | Invierte entrada/salida sin mover la línea | `False` |
| `LINE_PROCESSED_ID_TTL_MINUTES` | Minutos para recordar IDs ya procesados | `10` |
| `CAPTURE_ENABLED` | Activa captura automática de evidencia | `True` |
| `CAPTURE_DIR` | Carpeta base de evidencias | `data/captures` |
| `PADDING_CAPTURE` | Margen alrededor del vehículo recortado | `20` |
| `OCR_ENABLED` | Activa lectura básica de placas | `True` |
| `OCR_LANGUAGES` | Idiomas EasyOCR | `["en"]` |
| `OCR_MIN_CONFIDENCE` | Confianza mínima para aceptar OCR | `0.40` |
| `DATABASE_PATH` | Archivo SQLite de eventos | `database/eventos.db` |
| `RTSP_TRANSPORT` | Transporte RTSP usado por OpenCV/FFmpeg | `"tcp"` |
| `RTSP_OPEN_TIMEOUT_MS` | Timeout de apertura RTSP | `5000` |
| `RTSP_READ_TIMEOUT_MS` | Timeout de lectura RTSP | `5000` |
| `RTSP_STALE_TIMEOUT_SECONDS` | Segundos sin frames antes de reconectar | `10` |
| `RTSP_RECONNECT_INITIAL_SECONDS` | Backoff inicial de reconexión | `1` |
| `RTSP_RECONNECT_MAX_SECONDS` | Backoff máximo de reconexión | `15` |
| `RTSP_MAX_CONSECUTIVE_READ_FAILURES` | Fallos consecutivos antes de reconectar | `3` |
| `MULTICAMERA_ENABLED` | Habilita componentes multicámara | `True` |
| `INFERENCE_SCHEDULER_IDLE_SLEEP_SECONDS` | Pausa mínima del scheduler | `0.002` |
| `INFERENCE_RESULT_COPY_FRAME` | Copia frame anotado al publicar resultado | `True` |
| `INFERENCE_STATUS_WINDOW_SECONDS` | Ventana de métricas FPS | `5` |

### Configuracion segura DVR/RTSP — Fase 8A

Las credenciales del DVR no se guardan en Python ni deben subirse a Git. Crea
tu archivo local desde la plantilla:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Si antes exportaste credenciales en la terminal, limpialas:

```bash
unset DVR_HOST
unset DVR_RTSP_PORT
unset DVR_USERNAME
unset DVR_PASSWORD
unset DVR_PASS
```

Valida la configuracion sin conectar al DVR:

```bash
python scripts/validate_camera_config.py
```

Los scripts locales cargan `.env` con prioridad sobre variables exportadas para
evitar usar credenciales antiguas de la terminal.

El script muestra host, puerto, camaras habilitadas y URLs sanitizadas. La URL
interna se construye con usuario y password escapados correctamente, pero en
logs debe verse como:

```text
rtsp://detector:***@192.168.0.100:554/Streaming/Channels/101
```

Advertencias de seguridad:
- No subas `.env`.
- No copies contraseñas en capturas de pantalla.
- No ejecutes FFmpeg con URLs que contengan credenciales directamente si vas a compartir logs.
- Cambia inmediatamente cualquier contraseña que haya sido expuesta.

### Captura RTSP robusta — Fase 8B

`RTSPStream` implementa una capa independiente del pipeline de IA:

```text
DVR RTSP -> hilo por camara -> buffer de 1 frame -> consumidor lee el ultimo frame
```

El buffer conserva exactamente el frame mas reciente; un frame nuevo reemplaza
al anterior y no se forma una cola. La captura usa OpenCV con backend FFmpeg
cuando esta disponible, transporte RTSP por TCP y reconexion automatica con
backoff `1s, 2s, 4s, 8s, 15s max`.

El estado consultable incluye:
- `connected`, `frame_sequence`, `last_frame_at`, `frame_age_seconds`.
- `capture_fps` medido por la aplicacion y `source_fps` reportado por el stream.
- `connection_attempts`, `reconnect_count`, `read_error_count`.
- `last_error` sanitizado, sin URL completa ni contraseña.

El script de prueba no ejecuta YOLO, ByteTrack, OCR, SQLite ni dashboard. Solo
valida recepcion de frames y puede guardar snapshots JPG del ultimo frame.

Prueba A1:

```bash
source venv/bin/activate
python scripts/diagnose_rtsp_auth.py --camera A1
python scripts/test_rtsp_streams.py --camera A1 --duration 20 --save-snapshots
```

Prueba A5:

```bash
python scripts/diagnose_rtsp_auth.py --camera A5
python scripts/test_rtsp_streams.py --camera A5 --duration 20 --save-snapshots
```

Prueba simultanea:

```bash
python scripts/diagnose_rtsp_auth.py --camera all
python scripts/test_rtsp_streams.py --camera all --duration 30 --save-snapshots
```

Las salidas muestran resumen seguro por camara, por ejemplo:

```text
A1 connected=true frames=124 capture_fps=24.8 age=0.03s reconnects=0 errors=0
```

No se imprimen URLs RTSP completas ni contraseñas.

### Multicámara + YOLO compartido + tracking aislado — Fase 9A

La Fase 9A agrega un coordinador multicámara sin integrar todavía línea virtual,
OCR, SQLite ni dashboard. La arquitectura es:

```text
A1 RTSP latest-frame ┐
                     ├-> scheduler round-robin -> una instancia YOLO -> tracker A1
A5 RTSP latest-frame ┘                                      └-> tracker A5
```

Puntos clave:
- YOLO11n se carga una sola vez por coordinador.
- Validado en CPU con `./venv/bin/python`, Torch CPU y `torch.cuda.is_available() == False`.
- En prueba real simultánea A1+A5 se observó `model_loads=1` y backend `ultralytics-bytetrack`.
- La prueba de 300 segundos fue estable, con cierre limpio y sin segmentation fault.
- Rendimiento real observado: ~6.6 inferencias totales/s, CPU ~168–171% y RAM máxima ~515–520 MB.
- El modelo se invoca desde un único hilo de inferencia; no hay inferencia concurrente.
- Cada cámara tiene su propio tracker y sus IDs no se mezclan.
- El scheduler toma siempre el frame más reciente; no crea backlog.
- `CAMERA_A1_TARGET_FPS` y `CAMERA_A5_TARGET_FPS` vienen de `.env`.
- El FPS real puede ser menor al objetivo si el CPU no alcanza; se reporta como métrica.

Prueba real multicámara:

```bash
python scripts/test_multicamera_inference.py --duration 30 --save-snapshots
```

Prueba prolongada:

```bash
python scripts/test_multicamera_inference.py --duration 300
```

Los snapshots anotados se guardan en:

```text
data/multicamera_test/
```

### Corredores virtuales y eventos multicámara — Fase 9B

La Fase 9B agrega un corredor direccional independiente por cámara sobre el
coordinador multicámara. Todavía no ejecuta OCR, SQLite, dashboard ni evidencia
productiva.

Puntos clave:
- Los corredores se configuran como polilíneas normalizadas `0.0..1.0` por cámara.
- A1 usa un corredor de ingreso y genera `ENTRADA` temprano para no perder vehículos que se desvíen al primer portón.
- A5 usa un corredor de salida y genera `SALIDA` al recorrer la curva hacia el exterior.
- Cada `CameraContext` mantiene su propio `PassageCorridorDetector`; un ID repetido en A1 y A5 no comparte estado.
- El punto de seguimiento configurable usa `bottom_center`, el punto inferior central del bbox.
- La confirmación exige observaciones mínimas, avance longitudinal, dirección consistente y distancia lateral dentro del corredor.
- El scheduler genera `EventCandidate` y lo publica en una cola acotada.
- Si la cola se llena, el scheduler no bloquea: incrementa métrica y descarta el evento nuevo.
- La deduplicación usa estado por track, TTL y cooldown por `(camera_id, track_id, direction)`.
- Al reconectar una cámara se reinicia solo su tracker y su detector de corredor.

Variables de corredor en `.env`:

```env
CAMERA_A1_PASSAGE_ENABLED=true
CAMERA_A1_PASSAGE_EVENT=ENTRADA
CAMERA_A1_PASSAGE_DIRECTION=forward
CAMERA_A1_TRIGGER_PROGRESS=0.15
CAMERA_A1_CORRIDOR_POINTS=0.27,0.98;0.43,0.83;0.59,0.69;0.70,0.58
CAMERA_A1_CORRIDOR_HALF_WIDTH=0.08
```

Para calibrar líneas de acceso independientes, zonas de habitación o grupos y pasos de aproximación, consulta la [guía de geometría por cámara](GEOMETRIA_HOTEL.md).

Calibración sin GUI, guardando snapshots con el corredor actual:

```bash
./venv/bin/python scripts/calibrate_passage_corridors.py --camera all
```

Calibración interactiva opcional:

```bash
./venv/bin/python scripts/calibrate_passage_corridors.py --camera A1 --interactive
./venv/bin/python scripts/calibrate_passage_corridors.py --camera A5 --interactive
```

Validación de eventos:

```bash
./venv/bin/python scripts/test_multicamera_events.py --camera all --duration 120 --save-event-snapshots
```

Los snapshots de calibración se guardan en `data/passage_calibration/` y los
snapshots de eventos de prueba en `data/multicamera_events_test/`. La validación
operativa completa requiere un paso vehicular real; si no cruza un vehículo, el
script solo valida captura, inferencia, tracking, corredor, cola y cierre.

#### Nota OpenCV / EasyOCR

En esta fase se valida con una sola variante instalada de OpenCV. Evita mezclar
`opencv-python` y `opencv-python-headless` en el mismo entorno porque ambos
paquetes instalan el modulo nativo `cv2` y pueden provocar cierres inestables.

Estrategias recomendadas para una fase posterior:
- Usar entornos separados: produccion headless para servicio y desarrollo GUI para pruebas locales.
- Instalar EasyOCR sin dependencias y declarar manualmente sus dependencias compatibles.
- Migrar el modo local para no depender de `cv2.imshow` y usar solo `opencv-python-headless`.

### Línea virtual y eventos — Fase 3

La Fase 3 agrega una línea virtual visible sobre el video. El sistema usa el
centro del bounding box de cada vehículo con ID persistente:

```python
center_x = (x1 + x2) / 2
center_y = (y1 + y2) / 2
```

Cuando el centro cruza la línea configurada, se genera un evento temporal:

```python
{
    "vehicle_id": 5,
    "type": "entrada",
    "timestamp": "2026-07-10T15:32:10"
}
```

Para una línea horizontal, el movimiento hacia abajo se registra como
`entrada` y el movimiento hacia arriba como `salida`. Si la cámara real queda
orientada al revés, cambia:

```python
LINE_INVERT_DIRECTION = True
```

Los eventos se mantienen en memoria en `events` durante la ejecución. Todavía
no se crea SQLite en esta fase.

Cuando el video de prueba llega al final y se reinicia, el sistema limpia el
estado temporal del tracker y de la línea virtual para evitar falsos eventos en
la siguiente reproducción. Los IDs ya procesados se guardan con timestamp y se
purgan automáticamente después de `LINE_PROCESSED_ID_TTL_MINUTES`.

### Captura de evidencia — Fase 4

Cuando se genera un evento `entrada` o `salida`, el sistema guarda un recorte
del vehículo con bbox, ID, tipo de evento, fecha y hora. Las imágenes se
organizan por fecha:

```text
data/captures/YYYY-MM-DD/entrada_ID5_2026-07-10_22-45-10.jpg
```

La captura se puede desactivar con:

```python
CAPTURE_ENABLED = False
```

### Lectura básica de placas — Fase 5

Cuando ocurre un evento, el sistema intenta leer la placa desde el recorte del
vehículo usando EasyOCR en CPU. Antes del OCR aplica escala de grises, aumento
de contraste, resize y threshold. El texto se normaliza a mayúsculas, se dejan
sólo caracteres alfanuméricos y se valida el formato básico de Ecuador:

```text
AAA0000
```

Si la lectura no cumple el formato o no supera `OCR_MIN_CONFIDENCE`, el evento
mantiene placa vacía y se persiste igualmente.

### Persistencia SQLite — Fase 6

Cada evento generado se guarda automáticamente en SQLite usando SQLAlchemy. La
base se crea si no existe:

```text
database/eventos.db
```

Tabla `eventos`:

```text
id, fecha_hora, placa, confianza_ocr, tipo_evento, vehicle_id, imagen, camara
```

### Dashboard web — Fase 7

El dashboard usa FastAPI, HTML, CSS y JavaScript vanilla. Muestra estadísticas
del día, gráfico por hora con Chart.js y una tabla con los últimos eventos.
Las evidencias JPG se sirven desde `/captures`.

Endpoints:

```text
GET /api/events
GET /api/events/{limit}
GET /api/stats
```

### Optimización para CPU lento

Si el sistema va lento, ajusta en `config.py`:

```python
FRAME_SKIP = 2          # Procesar 1 de cada 2 frames
INFERENCE_SIZE = 320     # Reducir tamaño de inferencia
CONFIDENCE_THRESHOLD = 0.45  # Subir umbral para menos detecciones
```

---

## 📍 Fases del Proyecto

| Fase | Descripción | Estado |
|------|------------|--------|
| **1** | Detección básica de vehículos | ✅ Completada |
| **2** | Tracking con ByteTrack | ✅ Completada |
| **3** | Línea virtual y eventos | ✅ Completada |
| **4** | Captura de evidencia | ✅ Completada |
| **5** | Reconocimiento de placas (OCR) | ✅ Completada |
| **6** | Base de datos SQLite | ✅ Completada |
| **7** | Dashboard web | ✅ Completada |
| **8A** | Configuración segura DVR/RTSP | ✅ Completada |
| **8B** | Capturador RTSP robusto | ✅ Completada |
| **9A** | Multicámara + YOLO compartido + tracking aislado | ✅ Completada |
| **9B** | Corredores virtuales y eventos multicámara | En validación |

---

## 🔧 Resolución de Problemas

### "No se pudo abrir la fuente de video"
- Verifica que el archivo de video existe en la ruta especificada.
- Asegúrate de que el formato sea compatible (.mp4, .avi, .mkv).

### FPS muy bajo (< 5)
- Incrementa `FRAME_SKIP` en `config.py`.
- Reduce `INFERENCE_SIZE` a 320.
- Cierra otras aplicaciones que consuman CPU.

### El modelo no se descarga
- Verifica tu conexión a internet.
- Descarga manualmente: `python -c "from ultralytics import YOLO; YOLO('yolo11n.pt')"`

---

## 📝 Licencia

Proyecto privado — Uso interno.
