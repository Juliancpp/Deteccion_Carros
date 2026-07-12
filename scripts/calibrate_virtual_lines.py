#!/usr/bin/env python3
"""Calibra visualmente las líneas virtuales por cámara."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.line_detector import VirtualLineDetector
from app.settings import CameraSettings, SettingsError, load_settings
from app.video.rtsp_stream import RTSPStream


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrador de líneas virtuales")
    parser.add_argument("--camera", choices=["A1", "A5", "all"], default="all")
    parser.add_argument("--capture-snapshot", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "data" / "line_calibration"))
    parser.add_argument("--write-env", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        settings = load_settings(override_env=True)
    except SettingsError as exc:
        print(f"[LINE] Configuracion invalida: {exc}", file=sys.stderr)
        return 2

    selected = [camera for camera in settings.cameras if args.camera == "all" or camera.camera_id == args.camera]
    if not selected:
        print("[LINE] No hay camaras seleccionadas", file=sys.stderr)
        return 2
    if args.write_env and args.camera == "all":
        print("[LINE] --write-env requiere seleccionar una sola camara", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    updates: dict[str, str] = {}
    exit_code = 0
    for camera in selected:
        result = _calibrate_camera(settings.dvr, camera, output_dir, interactive=args.interactive)
        if result is None:
            exit_code = 1
            continue
        normalized_start, normalized_end, invert_direction = result
        _print_env_block(camera, normalized_start, normalized_end, invert_direction)
        if args.write_env:
            prefix = f"CAMERA_{camera.camera_id}"
            updates.update(
                {
                    f"{prefix}_LINE_ENABLED": "true",
                    f"{prefix}_LINE_START_X": f"{normalized_start[0]:.6f}",
                    f"{prefix}_LINE_START_Y": f"{normalized_start[1]:.6f}",
                    f"{prefix}_LINE_END_X": f"{normalized_end[0]:.6f}",
                    f"{prefix}_LINE_END_Y": f"{normalized_end[1]:.6f}",
                    f"{prefix}_LINE_INVERT_DIRECTION": str(invert_direction).lower(),
                    f"{prefix}_EVENT_DIRECTIONS": camera.line.event_directions if camera.line else "both",
                }
            )

    if args.write_env and updates:
        _update_env_file(PROJECT_ROOT / ".env", updates)
        print("[LINE] Variables de linea actualizadas en .env")
    return exit_code


def _calibrate_camera(dvr_settings, camera: CameraSettings, output_dir: Path, *, interactive: bool):
    stream = RTSPStream(dvr_settings, camera)
    stream.start()
    try:
        if not stream.wait_until_connected(timeout=10):
            print(f"[LINE] {camera.camera_id}: no conecto al stream", file=sys.stderr)
            return None
        latest = _wait_for_frame(stream, timeout=10)
        if latest is None:
            print(f"[LINE] {camera.camera_id}: no se recibio frame", file=sys.stderr)
            return None
        frame = latest.frame.copy()
        line_detector = _line_detector_from_camera(camera)
        start, end = line_detector.line_points_for_frame(frame.shape[1], frame.shape[0])
        invert = camera.line.invert_direction if camera.line else False
        normalized_start = _normalize_point(start, frame.shape[1], frame.shape[0])
        normalized_end = _normalize_point(end, frame.shape[1], frame.shape[0])
        if interactive:
            selected = _interactive_select(camera.camera_id, frame, start, end, invert)
            if selected is None:
                return None
            normalized_start, normalized_end, invert = selected
            start = _pixel_point(normalized_start, frame.shape[1], frame.shape[0])
            end = _pixel_point(normalized_end, frame.shape[1], frame.shape[0])
        annotated = frame.copy()
        _draw_line(annotated, camera.camera_id, start, end, invert)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filepath = output_dir / f"{camera.camera_id}_current_line_{timestamp}.jpg"
        _write_image(filepath, annotated)
        print(f"[LINE] {camera.camera_id}: snapshot {filepath}")
        print(f"[LINE] {camera.camera_id}: inicio={normalized_start} fin={normalized_end} invert={invert}")
        return normalized_start, normalized_end, invert
    finally:
        if stream.stop():
            print(f"[LINE] {camera.camera_id}: stream detenido correctamente")


def _wait_for_frame(stream: RTSPStream, timeout: float):
    end_at = time.monotonic() + timeout
    while time.monotonic() < end_at:
        latest = stream.get_latest_frame()
        if latest is not None:
            return latest
        time.sleep(0.05)
    return None


def _line_detector_from_camera(camera: CameraSettings) -> VirtualLineDetector:
    line = camera.line
    return VirtualLineDetector(
        line_enabled=line.enabled if line else True,
        normalized_start=(line.start_x, line.start_y) if line else (0.20, 0.55),
        normalized_end=(line.end_x, line.end_y) if line else (0.80, 0.55),
        invert_direction=line.invert_direction if line else False,
        event_directions=line.event_directions if line else "both",
    )


def _interactive_select(camera_id: str, frame, start, end, invert):
    import cv2

    selected: list[tuple[int, int]] = [start, end]
    current_invert = invert
    window = f"Calibracion {camera_id}"

    def redraw():
        view = frame.copy()
        if len(selected) >= 1:
            cv2.circle(view, selected[0], 5, (0, 255, 255), -1)
        if len(selected) >= 2:
            cv2.line(view, selected[0], selected[1], (0, 255, 255), 2)
            cv2.circle(view, selected[1], 5, (0, 255, 255), -1)
        cv2.putText(view, "r reset | i invertir | s guardar | q salir", (16, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        cv2.imshow(window, view)

    def on_mouse(event, x, y, _flags, _param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        if len(selected) >= 2:
            selected.clear()
        selected.append((x, y))
        redraw()

    cv2.namedWindow(window)
    cv2.setMouseCallback(window, on_mouse)
    redraw()
    while True:
        key = cv2.waitKey(20) & 0xFF
        if key in {27, ord("q")}:
            cv2.destroyWindow(window)
            return None
        if key == ord("r"):
            selected.clear()
            redraw()
        if key == ord("i"):
            current_invert = not current_invert
            print(f"[LINE] invert_direction={str(current_invert).lower()}")
        if key == ord("s") and len(selected) == 2:
            cv2.destroyWindow(window)
            height, width = frame.shape[:2]
            return (
                _normalize_point(selected[0], width, height),
                _normalize_point(selected[1], width, height),
                current_invert,
            )


def _draw_line(frame, camera_id: str, start: tuple[int, int], end: tuple[int, int], invert: bool) -> None:
    import cv2

    cv2.line(frame, start, end, (0, 255, 255), 2)
    cv2.circle(frame, start, 5, (0, 255, 255), -1)
    cv2.circle(frame, end, 5, (0, 255, 255), -1)
    cv2.putText(frame, f"{camera_id} invert={str(invert).lower()}", (16, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)


def _write_image(filepath: Path, frame) -> None:
    import cv2

    if not cv2.imwrite(str(filepath), frame):
        raise RuntimeError(f"no se pudo guardar {filepath}")


def _normalize_point(point: tuple[int, int], width: int, height: int) -> tuple[float, float]:
    return (round(point[0] / width, 6), round(point[1] / height, 6))


def _pixel_point(point: tuple[float, float], width: int, height: int) -> tuple[int, int]:
    return (round(point[0] * width), round(point[1] * height))


def _print_env_block(camera: CameraSettings, start: tuple[float, float], end: tuple[float, float], invert: bool) -> None:
    prefix = f"CAMERA_{camera.camera_id}"
    directions = camera.line.event_directions if camera.line else "both"
    print("[LINE] Bloque sugerido:")
    print(f"{prefix}_LINE_ENABLED=true")
    print(f"{prefix}_LINE_START_X={start[0]:.6f}")
    print(f"{prefix}_LINE_START_Y={start[1]:.6f}")
    print(f"{prefix}_LINE_END_X={end[0]:.6f}")
    print(f"{prefix}_LINE_END_Y={end[1]:.6f}")
    print(f"{prefix}_LINE_INVERT_DIRECTION={str(invert).lower()}")
    print(f"{prefix}_EVENT_DIRECTIONS={directions}")


def _update_env_file(env_path: Path, updates: dict[str, str]) -> None:
    backup_path = env_path.with_name(".env.backup_line_calibration")
    shutil.copy2(env_path, backup_path)
    lines = env_path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    new_lines = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            new_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)


if __name__ == "__main__":
    raise SystemExit(main())
