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
