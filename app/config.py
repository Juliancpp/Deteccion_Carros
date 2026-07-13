"""
Deteccion_Carros — Configuración Global
========================================
Archivo centralizado de configuración para el sistema
de detección vehicular. Fase 1: Detección básica.
"""

import os

# ─── Rutas base ───────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ─── Modelo YOLO ──────────────────────────────────────────────
# Usar YOLO11n (nano) para máxima eficiencia en CPU.
# Si el archivo no existe localmente, Ultralytics lo descarga
# automáticamente a la carpeta models/.
MODEL_PATH = os.path.join(BASE_DIR, "models", "yolo11n.pt")

# ─── Configuración de Cámaras / Fuentes de Video ───────────────
CAMERAS = [
    {
        "id": 1,
        "source": os.path.join(BASE_DIR, "data", "videos", "test.mp4")
    }
]

VIDEO_PATH = CAMERAS[0]["source"]
CAMERA_ID = CAMERAS[0]["id"]
DATABASE_PATH = os.path.join(BASE_DIR, "database", "eventos.db")
HOTEL_DATABASE_PATH = os.path.join(BASE_DIR, "database", "hotel_mvp.db")

# ─── Umbral de confianza ─────────────────────────────────────
# Detecciones por debajo de este valor serán ignoradas.
# Rango recomendado para CPU: 0.35 – 0.50
CONFIDENCE_THRESHOLD = 0.40

# ─── Clases permitidas (COCO dataset IDs) ────────────────────
# Solo detectamos vehículos. IDs del dataset COCO:
#   2  = car
#   3  = motorcycle
#   5  = bus
#   7  = truck
CLASSES_ALLOWED = [2, 3, 5, 7]

# Mapa legible de nombres por clase
CLASS_NAMES = {
    2: "Auto",
    3: "Moto",
    5: "Bus",
    7: "Camion",
}

# ─── Colores por clase (BGR para OpenCV) ─────────────────────
CLASS_COLORS = {
    2: (0, 255, 128),    # Auto   → Verde lima
    3: (0, 200, 255),    # Moto   → Naranja
    5: (255, 150, 0),    # Bus    → Azul claro
    7: (80, 80, 255),    # Camión → Rojo suave
}

# ─── Visualización ───────────────────────────────────────────
# Tamaño máximo del frame para visualización (ancho).
# Redimensionar reduce carga de CPU.
DISPLAY_WIDTH = 960

# ─── Línea virtual (Fase 3) ──────────────────────────────────
# Coordenadas sobre el frame mostrado/procesado. Ajustar estos puntos
# cuando se conecten cámaras reales o cambie la perspectiva.
LINE_POINT_A = (300, 300)
LINE_POINT_B = (900, 300)
LINE_DIRECTION = "horizontal"

# False: abajo=detección de ENTRADA, arriba=SALIDA en línea horizontal.
# True invierte el criterio sin cambiar el código.
LINE_INVERT_DIRECTION = False

# Tiempo para recordar IDs que ya generaron evento y evitar duplicados.
LINE_PROCESSED_ID_TTL_MINUTES = 10

# ─── Captura de evidencia (Fase 4) ───────────────────────────
CAPTURE_ENABLED = True
CAPTURE_DIR = os.path.join(BASE_DIR, "data", "captures")
PADDING_CAPTURE = 20

# ─── OCR de placas (Fase 5) ──────────────────────────────────
OCR_ENABLED = True
OCR_LANGUAGES = ["en"]
OCR_MIN_CONFIDENCE = 0.40

# ─── Optimización CPU ────────────────────────────────────────
# Procesar solo 1 de cada N frames para ahorrar CPU.
# 1 = procesar todos, 2 = saltar 1, 3 = saltar 2, etc.
FRAME_SKIP = 1

# Tamaño de inferencia YOLO (menor = más rápido, menos preciso).
# Valores recomendados: 320, 416, 480, 640
INFERENCE_SIZE = 480

# ─── RTSP robusto (Fase 8B) ──────────────────────────────────
# No contiene credenciales. Las URLs se construyen desde app/settings.py.
RTSP_TRANSPORT = "tcp"
RTSP_OPEN_TIMEOUT_MS = 5000
RTSP_READ_TIMEOUT_MS = 5000
RTSP_STALE_TIMEOUT_SECONDS = 10
RTSP_RECONNECT_INITIAL_SECONDS = 1
RTSP_RECONNECT_MAX_SECONDS = 15
RTSP_MAX_CONSECUTIVE_READ_FAILURES = 3
RTSP_STOP_JOIN_TIMEOUT_SECONDS = 8

# ─── Multicámara + inferencia compartida (Fase 9A) ───────────
MULTICAMERA_ENABLED = True
INFERENCE_SCHEDULER_IDLE_SLEEP_SECONDS = 0.002
INFERENCE_RESULT_COPY_FRAME = True
INFERENCE_STATUS_WINDOW_SECONDS = 5

# ─── Eventos multicámara por línea virtual (Fase 9B) ─────────
EVENT_QUEUE_MAXSIZE = 64
EVENT_TRACK_TTL_SECONDS = 10
EVENT_DUPLICATE_COOLDOWN_SECONDS = 5
EVENT_FRAME_COPY_ENABLED = True

# ─── Corredores virtuales direccionales (Fase 9B) ────────────
PASSAGE_TRACK_POINT = "bottom_center"
PASSAGE_MIN_OBSERVATIONS = 3
PASSAGE_MIN_PROGRESS_DELTA = 0.08
PASSAGE_MAX_LATERAL_DISTANCE_NORMALIZED = 0.08
PASSAGE_TRACK_TTL_SECONDS = 10
PASSAGE_DUPLICATE_COOLDOWN_SECONDS = 10

# ─── MVP hotel ───────────────────────────────────────────────
HOTEL_EVIDENCE_DIR = os.path.join(BASE_DIR, "data", "hotel_events")
HOTEL_REPORT_PATH = os.path.join(BASE_DIR, "data", "reportes", "control_vehiculos.xlsx")
HOTEL_FRAME_BUFFER_MAX_CANDIDATES = 6
HOTEL_FRAME_BUFFER_TTL_SECONDS = 20
HOTEL_OCR_QUEUE_MAXSIZE = 32
HOTEL_EVENT_QUEUE_MAXSIZE = 64
HOTEL_GATE_MIN_OBSERVATIONS = 3
HOTEL_GATE_MIN_DELTA = 0.06
HOTEL_GATE_DUPLICATE_COOLDOWN_SECONDS = 10
HOTEL_ROOM_MIN_PROBABLE_OBSERVATIONS = 2
HOTEL_ROOM_MIN_CONFIRMED_OBSERVATIONS = 4
HOTEL_PLATE_MIN_CONFIDENCE = 0.55

# ─── MVP hotel fase 1: dirección física + habitación ─────────
DIRECTION_MIN_OBSERVATIONS = 3
DIRECTION_MIN_PROGRESS_DELTA = 0.06
DIRECTION_TRACK_TTL_SECONDS = 8
DIRECTION_HISTORY_SIZE = 8
ROOM_TRACK_MISSING_GRACE_SECONDS = 1.5
ROOM_TRACK_TTL_SECONDS = 10
ROOM_MIN_PROBABLE_OBSERVATIONS = 2
ROOM_MIN_CONFIRMED_OBSERVATIONS = 4

# ─── Enlazador de fragmentos de tracks ───────────────────────
TRACK_LINKER_MAX_GAP_SECONDS = 2.0
TRACK_LINKER_MAX_DISTANCE = 0.12

# ─── Hardening hotel critico ─────────────────────────────────
HOTEL_EVENT_SOURCE = "phase1_analyzer"
PLATE_MAX_CANDIDATE_FRAMES = 8
PLATE_MAX_FRAMES_FOR_OCR = 5
PLATE_MAX_CROPS_PER_FRAME = 3
PLATE_MAX_VARIANTS_PER_CROP = 3
ROOM_MAX_CORRIDOR_OVERLAP_RATIO = 0.20
ROOM_MAX_MUTUAL_OVERLAP_RATIO = 0.05
