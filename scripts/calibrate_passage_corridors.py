#!/usr/bin/env python3
"""Calibra corredores virtuales direccionales por cámara."""

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

from app.passage.corridor_detector import PassageCorridorDetector
from app.settings import CameraSettings, SettingsError, load_settings
from app.video.rtsp_stream import RTSPStream


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrador de corredores virtuales")
    parser.add_argument("--camera", choices=["A1", "A5", "all"], default="all")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "data" / "passage_calibration"))
    parser.add_argument("--write-env", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.write_env and args.camera == "all":
        print("[PASSAGE] --write-env requiere seleccionar una sola camara", file=sys.stderr)
        return 2
    try:
        settings = load_settings(override_env=True)
    except SettingsError as exc:
        print(f"[PASSAGE] Configuracion invalida: {exc}", file=sys.stderr)
        return 2

    selected = [camera for camera in settings.cameras if args.camera == "all" or camera.camera_id == args.camera]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    updates: dict[str, str] = {}
    exit_code = 0
    for camera in selected:
        result = _calibrate_camera(settings.dvr, camera, output_dir, interactive=args.interactive)
        if result is None:
            exit_code = 1
            continue
        points, half_width = result
        _print_env_block(camera, points, half_width)
        if args.write_env:
            prefix = f"CAMERA_{camera.camera_id}"
            updates.update(
                {
                    f"{prefix}_PASSAGE_ENABLED": "true",
                    f"{prefix}_PASSAGE_EVENT": camera.passage.event if camera.passage else _default_event(camera),
                    f"{prefix}_PASSAGE_DIRECTION": camera.passage.direction if camera.passage else "forward",
                    f"{prefix}_TRIGGER_PROGRESS": f"{(camera.passage.trigger_progress if camera.passage else _default_trigger(camera)):.6f}",
                    f"{prefix}_CORRIDOR_POINTS": _format_points(points),
                    f"{prefix}_CORRIDOR_HALF_WIDTH": f"{half_width:.6f}",
                }
            )
    if args.write_env and updates:
        _update_env_file(PROJECT_ROOT / ".env", updates)
        print("[PASSAGE] Variables de corredor actualizadas en .env")
    return exit_code


def _calibrate_camera(dvr_settings, camera: CameraSettings, output_dir: Path, *, interactive: bool):
    stream = RTSPStream(dvr_settings, camera)
    stream.start()
    try:
        if not stream.wait_until_connected(timeout=10):
            print(f"[PASSAGE] {camera.camera_id}: no conecto al stream", file=sys.stderr)
            return None
        latest = _wait_for_frame(stream, timeout=10)
        if latest is None:
            print(f"[PASSAGE] {camera.camera_id}: no se recibio frame", file=sys.stderr)
            return None
        frame = latest.frame.copy()
        passage = camera.passage
        points = list(passage.points if passage else _default_points(camera))
        half_width = passage.half_width if passage else 0.08
        if interactive:
            selected = _interactive_select(camera, frame, points, half_width)
            if selected is None:
                return None
            points, half_width = selected
        annotated = frame.copy()
        _draw_corridor(annotated, camera, points, half_width)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filepath = output_dir / f"{camera.camera_id}_current_corridor_{timestamp}.jpg"
        _write_image(filepath, annotated)
        print(f"[PASSAGE] {camera.camera_id}: snapshot {filepath}")
        print(f"[PASSAGE] {camera.camera_id}: points={_format_points(points)} half_width={half_width:.6f}")
        return tuple(points), half_width
    finally:
        if stream.stop():
            print(f"[PASSAGE] {camera.camera_id}: stream detenido correctamente")


def _interactive_select(camera: CameraSettings, frame, points, half_width):
    import cv2

    selected = list(points)
    current_width = half_width
    window = f"Corredor {camera.camera_id}"

    def redraw():
        view = frame.copy()
        _draw_corridor(view, camera, selected, current_width)
        cv2.putText(view, "click punto | u/backspace deshacer | r reset | +/- ancho | d invertir | s guardar | q salir", (16, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
        cv2.imshow(window, view)

    def on_mouse(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN:
            height, width = frame.shape[:2]
            selected.append((round(x / width, 6), round(y / height, 6)))
            redraw()

    cv2.namedWindow(window)
    cv2.setMouseCallback(window, on_mouse)
    redraw()
    while True:
        key = cv2.waitKey(20) & 0xFF
        if key in {27, ord("q")}:
            cv2.destroyWindow(window)
            return None
        if key in {8, ord("u")} and selected:
            selected.pop()
            redraw()
        elif key == ord("r"):
            selected.clear()
            redraw()
        elif key in {ord("+"), ord("=")}:
            current_width = min(1.0, current_width + 0.01)
            redraw()
        elif key in {ord("-"), ord("_")}:
            current_width = max(0.01, current_width - 0.01)
            redraw()
        elif key == ord("d"):
            selected.reverse()
            redraw()
        elif key in {10, 13}:
            redraw()
        elif key == ord("s") and len(selected) >= 2:
            cv2.destroyWindow(window)
            return tuple(selected), current_width


def _draw_corridor(frame, camera: CameraSettings, points: list[tuple[float, float]] | tuple[tuple[float, float], ...], half_width: float) -> None:
    import cv2

    height, width = frame.shape[:2]
    event = camera.passage.event if camera.passage else _default_event(camera)
    trigger = camera.passage.trigger_progress if camera.passage else _default_trigger(camera)
    detector = PassageCorridorDetector(
        points=points,
        half_width=half_width,
        event_direction=event,
        trigger_progress=trigger,
    )
    pixel_points = detector.points_for_frame(width, height)
    for start, end in zip(pixel_points, pixel_points[1:]):
        cv2.line(frame, start, end, (0, 255, 255), 2)
        cv2.arrowedLine(frame, start, end, (0, 200, 255), 2, tipLength=0.18)
        cv2.circle(frame, start, round(half_width * min(width, height)), (0, 255, 255), 1)
    if pixel_points:
        cv2.circle(frame, pixel_points[-1], round(half_width * min(width, height)), (0, 255, 255), 1)
    trigger_point = detector.trigger_point_for_frame(width, height)
    cv2.circle(frame, trigger_point, 7, (0, 120, 255), -1)
    cv2.putText(frame, f"{camera.camera_id} {event} progreso={trigger:.2f}", (16, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    cv2.putText(frame, "0.0", pixel_points[0], cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
    cv2.putText(frame, "1.0", pixel_points[-1], cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)


def _wait_for_frame(stream: RTSPStream, timeout: float):
    end_at = time.monotonic() + timeout
    while time.monotonic() < end_at:
        latest = stream.get_latest_frame()
        if latest is not None:
            return latest
        time.sleep(0.05)
    return None


def _write_image(filepath: Path, frame) -> None:
    import cv2

    if not cv2.imwrite(str(filepath), frame):
        raise RuntimeError(f"no se pudo guardar {filepath}")


def _print_env_block(camera: CameraSettings, points, half_width: float) -> None:
    prefix = f"CAMERA_{camera.camera_id}"
    print("[PASSAGE] Bloque sugerido:")
    print(f"{prefix}_PASSAGE_ENABLED=true")
    print(f"{prefix}_PASSAGE_EVENT={camera.passage.event if camera.passage else _default_event(camera)}")
    print(f"{prefix}_PASSAGE_DIRECTION={camera.passage.direction if camera.passage else 'forward'}")
    print(f"{prefix}_TRIGGER_PROGRESS={(camera.passage.trigger_progress if camera.passage else _default_trigger(camera)):.6f}")
    print(f"{prefix}_CORRIDOR_POINTS={_format_points(points)}")
    print(f"{prefix}_CORRIDOR_HALF_WIDTH={half_width:.6f}")


def _format_points(points) -> str:
    return ";".join(f"{x:.6f},{y:.6f}" for x, y in points)


def _update_env_file(env_path: Path, updates: dict[str, str]) -> None:
    backup_path = env_path.with_name(".env.backup_passage_calibration")
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


def _default_points(camera: CameraSettings):
    if camera.camera_id == "A5":
        return ((0.06, 0.98), (0.14, 0.86), (0.22, 0.74), (0.29, 0.64))
    return ((0.27, 0.98), (0.43, 0.83), (0.59, 0.69), (0.70, 0.58))


def _default_event(camera: CameraSettings) -> str:
    return "SALIDA" if camera.camera_id == "A5" else "ENTRADA"


def _default_trigger(camera: CameraSettings) -> float:
    return 0.55 if camera.camera_id == "A5" else 0.15


if __name__ == "__main__":
    raise SystemExit(main())
