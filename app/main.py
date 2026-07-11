"""
Deteccion_Carros — Punto de Entrada Principal
===============================================
FASE 1: Detección básica de vehículos en video.

Uso:
    python app/main.py                     # Usa el video configurado en config.py
    python app/main.py --video ruta.mp4    # Especifica un video
    python app/main.py --video 0           # Usa la webcam

Controles:
    Q     → Salir
    P     → Pausar / Reanudar
    S     → Guardar captura del frame actual
"""

import argparse
import os
import sys
import time
from typing import List

import cv2

from config import (
    VIDEO_PATH,
    DISPLAY_WIDTH,
    FRAME_SKIP,
    LINE_POINT_A,
    LINE_POINT_B,
    LINE_DIRECTION,
    LINE_INVERT_DIRECTION,
    LINE_PROCESSED_ID_TTL_MINUTES,
    PADDING_CAPTURE,
)
from database import DatabaseManager
from detector import VehicleDetector
from evidence import EvidenceCapture
from line_detector import VirtualLineDetector
from ocr import PlateReader
from tracker import VehicleTracker


def parse_args() -> argparse.Namespace:
    """Parsea los argumentos de línea de comandos."""
    parser = argparse.ArgumentParser(
        description="Deteccion_Carros — Sistema de detección y seguimiento vehicular"
    )
    parser.add_argument(
        "--video",
        type=str,
        default=None,
        help="Ruta al video o índice de cámara (0, 1...). Default: config.py",
    )
    return parser.parse_args()


def open_video(source: str) -> cv2.VideoCapture:
    """
    Abre una fuente de video.

    Args:
        source: Ruta a archivo, URL RTSP, o índice de cámara como string.

    Returns:
        Objeto VideoCapture abierto.

    Raises:
        SystemExit: Si no se puede abrir la fuente.
    """
    # Si es un número, interpretarlo como índice de cámara
    if source.isdigit():
        source = int(source)

    cap = cv2.VideoCapture(source)

    if not cap.isOpened():
        print(f"[✗] Error: No se pudo abrir la fuente de video: {source}")
        print()
        print("    Verifica que:")
        print("    • El archivo existe y la ruta es correcta.")
        print("    • El formato del video es compatible (.mp4, .avi, .mkv).")
        print("    • Si es RTSP, la URL y credenciales son correctas.")
        sys.exit(1)

    # Información del video
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    video_fps = cap.get(cv2.CAP_PROP_FPS)

    print(f"[✓] Video abierto: {source}")
    print(f"    Resolución: {width}x{height}")
    print(f"    FPS original: {video_fps:.1f}")
    if total_frames > 0:
        duration = total_frames / video_fps if video_fps > 0 else 0
        print(f"    Frames totales: {total_frames}")
        print(f"    Duración: {duration:.1f}s")
    print("─" * 50)

    return cap


def resize_frame(frame, target_width: int):
    """Redimensiona manteniendo proporción."""
    h, w = frame.shape[:2]
    if w <= target_width:
        return frame
    ratio = target_width / w
    new_h = int(h * ratio)
    return cv2.resize(frame, (target_width, new_h), interpolation=cv2.INTER_AREA)


def draw_virtual_line(frame, line_detector: VirtualLineDetector):
    """Dibuja la línea virtual configurada."""
    cv2.line(frame, line_detector.point_a, line_detector.point_b, (0, 255, 255), 3)
    cv2.putText(
        frame,
        "LINEA",
        (line_detector.point_a[0], max(20, line_detector.point_a[1] - 10)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (0, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return frame


def draw_vehicle_centers(frame, detections: List[dict]):
    """Dibuja el centro usado para detectar cruces."""
    for det in detections:
        x1, y1, x2, y2 = det["bbox"]
        center = (int((x1 + x2) / 2), int((y1 + y2) / 2))
        cv2.drawMarker(
            frame,
            center,
            (255, 255, 255),
            markerType=cv2.MARKER_CROSS,
            markerSize=12,
            thickness=2,
        )
    return frame


def draw_recent_events(frame, recent_events: List[dict]):
    """Muestra eventos de cruce durante algunos frames."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    visible_events = [event for event in recent_events if event["ttl"] > 0]

    for index, event in enumerate(visible_events[-3:]):
        label = f"{event['type'].upper()} ID:{event['vehicle_id']}"
        y = 115 + (index * 30)
        cv2.putText(frame, label, (15, y), font, 0.8, (0, 255, 255), 2, cv2.LINE_AA)

    for event in recent_events:
        event["ttl"] -= 1

    recent_events[:] = [event for event in recent_events if event["ttl"] > 0]
    return frame


def crop_vehicle_image(frame, vehicle: dict, padding: int = PADDING_CAPTURE):
    """Recorta el vehículo para OCR usando las coordenadas del frame procesado."""
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = [int(value) for value in vehicle["bbox"]]

    crop_x1 = max(0, x1 - padding)
    crop_y1 = max(0, y1 - padding)
    crop_x2 = min(width, x2 + padding)
    crop_y2 = min(height, y2 + padding)

    return frame[crop_y1:crop_y2, crop_x1:crop_x2].copy()


def main():
    """Bucle principal de detección y seguimiento vehicular."""
    args = parse_args()

    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║   DETECCION_CARROS — Fase 6: Persistencia SQLite    ║")
    print("║        Sistema de Control Vehicular IA           ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    # ── Inicializar detector y tracker ──
    print("[...] Cargando modelo y configurando tracker...")
    detector = VehicleDetector()
    tracker = VehicleTracker()
    evidence = EvidenceCapture()
    plate_reader = PlateReader()
    database = DatabaseManager()
    line_detector = VirtualLineDetector(
        LINE_POINT_A,
        LINE_POINT_B,
        direction=LINE_DIRECTION,
        invert_direction=LINE_INVERT_DIRECTION,
        processed_id_ttl_minutes=LINE_PROCESSED_ID_TTL_MINUTES,
    )

    # ── Abrir video ──
    video_source = args.video if args.video else VIDEO_PATH
    cap = open_video(video_source)

    # ── Variables de control ──
    frame_count = 0
    fps = 0.0
    fps_start_time = time.perf_counter()
    fps_frame_count = 0
    paused = False
    last_detections = []
    recent_events = []

    window_name = "Deteccion_Carros | Fase 6"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    print("[▶] Procesando video... (Q = salir, P = pausa, S = captura)")
    print()

    try:
        while True:
            # ── Manejo de pausa ──
            if paused:
                key = cv2.waitKey(100) & 0xFF
                if key == ord("q"):
                    break
                elif key == ord("p"):
                    paused = False
                    print("[▶] Reanudado")
                continue

            # ── Leer frame ──
            ret, frame = cap.read()
            if not ret:
                # Si es un archivo, reiniciar el video
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = cap.read()
                if not ret:
                    print("[✗] Error leyendo el video.")
                    break
                tracker.reset()
                line_detector.reset()
                last_detections.clear()
                recent_events.clear()
                print("[↻] Video reiniciado")

            frame_count += 1

            # ── Frame skip para optimización CPU ──
            if FRAME_SKIP > 1 and frame_count % FRAME_SKIP != 0:
                # Usar las últimas detecciones conocidas
                display = resize_frame(frame, DISPLAY_WIDTH)
                display = detector.draw_detections(display, last_detections)
                display = draw_virtual_line(display, line_detector)
                display = draw_vehicle_centers(display, last_detections)
                display = draw_recent_events(display, recent_events)
                display = detector.draw_hud(display, fps, len(last_detections), tracker.get_active_ids())
                cv2.imshow(window_name, display)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
                continue

            # ── Redimensionar para visualización ──
            display = resize_frame(frame, DISPLAY_WIDTH)

            # ── Detección y Tracking ──
            raw_results = detector.track(display)
            detections = tracker.update(raw_results)
            last_detections = detections

            # ── Detección de cruce de línea virtual ──
            for vehicle in detections:
                event = line_detector.update(vehicle)
                if event:
                    evidence_path = evidence.capture(display, vehicle, event)
                    vehicle_image = crop_vehicle_image(display, vehicle)
                    plate_result = plate_reader.read(vehicle_image)
                    event["placa"] = plate_result["plate"] if plate_result else ""
                    event["confianza"] = plate_result["confidence"] if plate_result else ""
                    event["plate"] = event["placa"]
                    event["confidence"] = event["confianza"]
                    event["image_path"] = evidence_path or ""
                    event_id = database.save_event(event)

                    recent_events.append({**event, "ttl": 60})
                    print(
                        f"[EVENTO] Vehículo ID {event['vehicle_id']} | "
                        f"{event['type'].upper()} | {event['timestamp']}"
                    )
                    if evidence_path:
                        print(f"[EVIDENCIA] {evidence_path}")
                    print("[OCR]")
                    print(f"Vehículo ID {event['vehicle_id']}")
                    print(f"Placa: {event['placa'] or 'No detectada'}")
                    print(f"Confianza: {event['confianza'] or 0}")
                    print(f"[DB] Evento guardado ID: {event_id}")

            # ── Dibujar resultados ──
            display = detector.draw_detections(display, detections)
            display = draw_virtual_line(display, line_detector)
            display = draw_vehicle_centers(display, detections)
            display = draw_recent_events(display, recent_events)
            display = detector.draw_hud(display, fps, len(detections), tracker.get_active_ids())

            # ── Calcular FPS (promedio móvil) ──
            fps_frame_count += 1
            elapsed = time.perf_counter() - fps_start_time
            if elapsed >= 0.5:  # Actualizar cada 0.5 segundos
                fps = fps_frame_count / elapsed
                fps_frame_count = 0
                fps_start_time = time.perf_counter()

            # ── Mostrar ──
            cv2.imshow(window_name, display)

            # ── Controles de teclado ──
            key = cv2.waitKey(1) & 0xFF

            if key == ord("q"):
                print("\n[■] Detenido por el usuario.")
                break

            elif key == ord("p"):
                paused = True
                print("[⏸] Pausado — presiona P para reanudar")

            elif key == ord("s"):
                # Guardar captura
                captures_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data",
                    "captures",
                )
                os.makedirs(captures_dir, exist_ok=True)
                timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
                filepath = os.path.join(captures_dir, f"captura_{timestamp}.jpg")
                cv2.imwrite(filepath, display)
                print(f"[📷] Captura guardada: {filepath}")

    except KeyboardInterrupt:
        print("\n[■] Interrumpido por el usuario (Ctrl+C).")

    finally:
        cap.release()
        cv2.destroyAllWindows()
        print()
        print(f"[✓] Frames procesados: {frame_count}")
        print("[✓] Sistema finalizado correctamente.")


if __name__ == "__main__":
    main()
