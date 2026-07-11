# Deteccion_Carros

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
│   └── api.py            ← (Fase 7) Dashboard web/API REST
├── models/               ← Modelos YOLO
├── data/
│   ├── videos/           ← Videos de prueba
│   └── captures/         ← Capturas de evidencia
├── database/             ← Archivos SQLite
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

### Usando un stream RTSP

```bash
python app/main.py --video "rtsp://usuario:clave@192.168.1.100:554/stream"
```

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
