#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.settings import load_settings
from app.video.rtsp_stream import RTSPStream


CAMERA_ROOMS = {
    "A1": (1, 2, 3),
    "A5": (6, 5, 4),
}

HOTEL_REAL_PRESET = {
    "A1": {
        "corridor": [(0.34, 0.98), (0.43, 0.84), (0.54, 0.71), (0.65, 0.60)],
        "half_width": 0.10,
        "invert_direction": False,
        "rooms": {
            1: [(0.49, 0.47), (0.68, 0.53), (0.76, 0.64), (0.55, 0.60)],
            2: [(0.29, 0.28), (0.41, 0.32), (0.39, 0.42), (0.26, 0.36)],
            3: [(0.18, 0.21), (0.28, 0.24), (0.27, 0.33), (0.16, 0.29)],
        },
    },
    "A5": {
        "corridor": [(0.06, 0.98), (0.16, 0.82), (0.29, 0.68), (0.46, 0.57), (0.66, 0.47), (0.87, 0.37)],
        "half_width": 0.09,
        "invert_direction": False,
        "rooms": {
            6: [(0.38, 0.58), (0.60, 0.48), (0.65, 0.61), (0.38, 0.73)],
            5: [(0.66, 0.39), (0.78, 0.33), (0.81, 0.46), (0.68, 0.52)],
            4: [(0.80, 0.29), (0.91, 0.24), (0.94, 0.35), (0.82, 0.40)],
        },
    },
}


@dataclass
class HotelLayout:
    camera_id: str
    corridor: list[tuple[float, float]]
    half_width: float
    invert_direction: bool
    rooms: dict[int, list[tuple[float, float]]]

    def stage_keys(self) -> list[str]:
        return [f"CORRIDOR_{self.camera_id}"] + [f"ROOM_{room}" for room in CAMERA_ROOMS[self.camera_id]]

    def points_for_stage(self, stage: str) -> list[tuple[float, float]]:
        if stage.startswith("CORRIDOR"):
            return self.corridor
        return self.rooms[int(stage.split("_", 1)[1])]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibra corredor y habitaciones del MVP Hotel")
    parser.add_argument("--camera", choices=["A1", "A5", "all"], required=True)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--capture-snapshot", action="store_true")
    parser.add_argument("--write-env", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--preset", choices=["hotel-real"], default=None)
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "data" / "hotel_phase1_calibration"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    camera_ids = ("A1", "A5") if args.camera == "all" else (args.camera,)

    if args.validate_only:
        return _validate_layouts(camera_ids, args.preset)

    for camera_id in camera_ids:
        settings = load_settings(override_env=True)
        camera = next(camera for camera in settings.cameras if camera.camera_id == camera_id)
        layout = load_layout(camera, preset=args.preset)
        frame = _capture_frame(settings.dvr, camera)

        if args.interactive:
            edited = _interactive(camera_id, frame, layout)
            if edited is None:
                return 1
            layout = edited

        if args.capture_snapshot or args.interactive or args.write_env:
            _save_snapshot(camera_id, frame, layout, Path(args.output_dir))

        _print_env_block(layout)
        if args.write_env:
            validate_layout(layout)
            updated = write_env_layout(layout)
            print("[HOTEL_CAL] .env actualizado con backup .env.backup_hotel_phase1")
            print("[HOTEL_CAL] Claves actualizadas:")
            for key in updated:
                print(f"  {key}")
            print(f"[HOTEL_CAL] Verificar claves: awk -F= '/CAMERA_{camera_id}_(CORRIDOR|ROOM_)/ {{print $1}}' .env")
    return 0


def _validate_layouts(camera_ids: tuple[str, ...], preset: str | None) -> int:
    settings = load_settings(override_env=True)
    ok = True
    for camera_id in camera_ids:
        camera = next(camera for camera in settings.cameras if camera.camera_id == camera_id)
        layout = load_layout(camera, preset=preset)
        try:
            validate_layout(layout)
        except ValueError as exc:
            print(f"[HOTEL_CAL] {camera_id}: INVALIDA - {exc}")
            ok = False
            continue
        print(f"[HOTEL_CAL] {camera_id}: corredor valido")
        for room in sorted(layout.rooms):
            print(f"[HOTEL_CAL] {camera_id}: ROOM_{room} valida")
    return 0 if ok else 1


def load_layout(camera, preset: str | None = None) -> HotelLayout:
    camera_id = camera.camera_id
    prefix = f"CAMERA_{camera_id}"
    rooms = {room: [] for room in CAMERA_ROOMS[camera_id]}
    has_env_rooms = False
    for room in camera.rooms:
        if room.room_number in rooms:
            rooms[room.room_number] = list(room.polygon)
            has_env_rooms = True

    corridor = list(camera.corridor.points) if camera.corridor and camera.corridor.points else []
    half_width = camera.corridor.half_width if camera.corridor else 0.10
    invert_direction = camera.corridor.invert_direction if camera.corridor else False
    has_env_corridor = _env_has_value(f"{prefix}_CORRIDOR_POINTS")

    if preset == "hotel-real" and not (has_env_corridor and has_env_rooms):
        preset_data = HOTEL_REAL_PRESET[camera_id]
        if not has_env_corridor:
            corridor = list(preset_data["corridor"])
            half_width = float(preset_data["half_width"])
            invert_direction = bool(preset_data["invert_direction"])
        if not has_env_rooms:
            rooms = {room: list(points) for room, points in preset_data["rooms"].items()}

    return HotelLayout(
        camera_id=camera_id,
        corridor=corridor,
        half_width=half_width,
        invert_direction=invert_direction,
        rooms=rooms,
    )


def _env_has_value(key: str) -> bool:
    return bool(str(os.environ.get(key, "")).strip().strip('"').strip("'"))


def _capture_frame(dvr, camera):
    stream = RTSPStream(dvr, camera)
    stream.start()
    try:
        stream.wait_until_connected(timeout=10)
        end_at = time.monotonic() + 10
        while time.monotonic() < end_at:
            latest = stream.get_latest_frame()
            if latest is not None:
                return latest.frame.copy()
            time.sleep(0.05)
        raise RuntimeError("no se recibio frame")
    finally:
        stream.stop()


def _interactive(camera_id: str, frame, layout: HotelLayout) -> HotelLayout | None:
    import cv2

    stages = layout.stage_keys()
    index = 0
    dragging: tuple[str, int] | None = None
    window = f"Hotel phase1 calibration {camera_id}"

    def redraw():
        view = frame.copy()
        _draw_layout(view, layout, active_stage=stages[index])
        lines = [
            f"Etapa: {stages[index]}",
            "click agrega | arrastra vertice | n/p etapa | u undo | r reset | d invertir | +/- ancho | s guardar | q salir",
            f"ancho={layout.half_width:.3f} invert={layout.invert_direction}",
        ]
        y = 28
        for line in lines:
            cv2.putText(view, line, (16, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 3)
            cv2.putText(view, line, (16, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
            y += 24
        cv2.imshow(window, view)

    def nearest_vertex(stage: str, x: int, y: int) -> int | None:
        h, w = frame.shape[:2]
        best_index = None
        best_distance = 16.0
        for point_index, (px, py) in enumerate(layout.points_for_stage(stage)):
            dx = x - px * w
            dy = y - py * h
            distance = (dx * dx + dy * dy) ** 0.5
            if distance < best_distance:
                best_distance = distance
                best_index = point_index
        return best_index

    def on_mouse(event, x, y, _flags, _param):
        nonlocal dragging
        h, w = frame.shape[:2]
        stage = stages[index]
        points = layout.points_for_stage(stage)
        if event == cv2.EVENT_LBUTTONDOWN:
            vertex_index = nearest_vertex(stage, x, y)
            if vertex_index is None:
                points.append((_clamp(x / w), _clamp(y / h)))
            else:
                dragging = (stage, vertex_index)
            redraw()
        elif event == cv2.EVENT_MOUSEMOVE and dragging is not None:
            drag_stage, vertex_index = dragging
            drag_points = layout.points_for_stage(drag_stage)
            if vertex_index < len(drag_points):
                drag_points[vertex_index] = (_clamp(x / w), _clamp(y / h))
            redraw()
        elif event == cv2.EVENT_LBUTTONUP:
            dragging = None
            redraw()

    cv2.namedWindow(window)
    cv2.setMouseCallback(window, on_mouse)
    redraw()

    while True:
        key = cv2.waitKey(20) & 0xFF
        if key in {27, ord("q")}:
            cv2.destroyWindow(window)
            return None
        if key in {ord("n"), 10, 13}:
            index = (index + 1) % len(stages)
            redraw()
        elif key == ord("p"):
            index = (index - 1) % len(stages)
            redraw()
        elif key in {ord("u"), 8, 127}:
            points = layout.points_for_stage(stages[index])
            if points:
                points.pop()
            redraw()
        elif key == ord("r"):
            layout.points_for_stage(stages[index]).clear()
            redraw()
        elif key == ord("d") and stages[index].startswith("CORRIDOR"):
            layout.corridor.reverse()
            layout.invert_direction = not layout.invert_direction
            redraw()
        elif key in {ord("+"), ord("=")}:
            layout.half_width = min(0.5, layout.half_width + 0.01)
            redraw()
        elif key in {ord("-"), ord("_")}:
            layout.half_width = max(0.01, layout.half_width - 0.01)
            redraw()
        elif key == ord("s"):
            try:
                validate_layout(layout)
            except ValueError as exc:
                print(f"[HOTEL_CAL] No se puede guardar: {exc}")
                redraw()
                continue
            cv2.destroyWindow(window)
            return layout


def _draw_layout(frame, layout: HotelLayout, active_stage: str | None = None) -> None:
    import cv2
    import numpy as np

    h, w = frame.shape[:2]
    overlay = frame.copy()

    if len(layout.corridor) >= 2:
        corridor_pixels = _to_pixels(layout.corridor, w, h)
        half_width_px = max(1, round(layout.half_width * min(w, h)))
        cv2.polylines(overlay, [np.array(corridor_pixels, dtype=np.int32)], False, (0, 255, 255), half_width_px * 2)
        cv2.addWeighted(overlay, 0.22, frame, 0.78, 0, dst=frame)
        cv2.polylines(frame, [np.array(corridor_pixels, dtype=np.int32)], False, (0, 255, 255), 3)
        for start, end in zip(corridor_pixels, corridor_pixels[1:]):
            cv2.arrowedLine(frame, start, end, (0, 255, 255), 2, tipLength=0.25)
        cv2.putText(frame, "EXTERIOR / 0.0", corridor_pixels[0], cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
        cv2.putText(frame, "INTERIOR / 1.0", corridor_pixels[-1], cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
        for point in corridor_pixels:
            cv2.circle(frame, point, 5, (0, 255, 255), -1)

    room_colors = {
        1: (0, 0, 255),
        2: (0, 80, 255),
        3: (0, 140, 255),
        4: (0, 0, 255),
        5: (0, 80, 255),
        6: (0, 140, 255),
    }
    for room, points in layout.rooms.items():
        pixels = _to_pixels(points, w, h)
        color = room_colors.get(room, (0, 0, 255))
        if len(pixels) >= 3:
            cv2.polylines(frame, [np.array(pixels, dtype=np.int32)], True, color, 2)
        elif len(pixels) >= 2:
            cv2.polylines(frame, [np.array(pixels, dtype=np.int32)], False, color, 2)
        for point in pixels:
            cv2.circle(frame, point, 5, color, -1)
        if pixels:
            cv2.putText(frame, f"ROOM_{room}", pixels[0], cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)

    if active_stage:
        points = layout.points_for_stage(active_stage)
        for point in _to_pixels(points, w, h):
            cv2.circle(frame, point, 9, (255, 255, 255), 2)

    cv2.putText(frame, f"{layout.camera_id} {w}x{h} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", (16, h - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)


def _save_snapshot(camera_id: str, frame, layout: HotelLayout, output_dir: Path) -> Path:
    import cv2

    output_dir.mkdir(parents=True, exist_ok=True)
    annotated = frame.copy()
    _draw_layout(annotated, layout)
    path = output_dir / f"{camera_id}_hotel_layout_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.jpg"
    cv2.imwrite(str(path), annotated)
    print(f"[HOTEL_CAL] Imagen de calibracion: {path}")
    return path


def _print_env_block(layout: HotelLayout) -> None:
    prefix = f"CAMERA_{layout.camera_id}"
    print(f"{prefix}_CORRIDOR_ENABLED=true")
    print(f'{prefix}_CORRIDOR_POINTS="{format_points(layout.corridor)}"')
    print(f"{prefix}_CORRIDOR_HALF_WIDTH={layout.half_width:.3f}")
    print(f"{prefix}_CORRIDOR_INVERT_DIRECTION={str(layout.invert_direction).lower()}")
    for room in sorted(layout.rooms):
        print(f'{prefix}_ROOM_{room}_POLYGON="{format_points(layout.rooms[room])}"')


def write_env_layout(layout: HotelLayout, env_path: Path | None = None, backup_path: Path | None = None) -> list[str]:
    env_path = env_path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        raise FileNotFoundError(".env no existe")
    backup = backup_path or PROJECT_ROOT / ".env.backup_hotel_phase1"
    shutil.copy2(env_path, backup)

    prefix = f"CAMERA_{layout.camera_id}"
    updates = {
        f"{prefix}_CORRIDOR_ENABLED": "true",
        f"{prefix}_CORRIDOR_POINTS": f'"{format_points(layout.corridor)}"',
        f"{prefix}_CORRIDOR_HALF_WIDTH": f"{layout.half_width:.3f}",
        f"{prefix}_CORRIDOR_INVERT_DIRECTION": str(layout.invert_direction).lower(),
    }
    for room, points in layout.rooms.items():
        updates[f"{prefix}_ROOM_{room}_POLYGON"] = f'"{format_points(points)}"'

    lines = env_path.read_text(encoding="utf-8").splitlines()
    output = []
    seen = set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")
    env_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
    return list(updates)


def validate_layout(layout: HotelLayout) -> None:
    if layout.camera_id not in CAMERA_ROOMS:
        raise ValueError("camara no soportada")
    allowed_rooms = set(CAMERA_ROOMS[layout.camera_id])
    if set(layout.rooms) != allowed_rooms:
        raise ValueError(f"{layout.camera_id} debe tener habitaciones {sorted(allowed_rooms)}")
    _validate_points(layout.corridor, minimum=2, name=f"CAMERA_{layout.camera_id}_CORRIDOR_POINTS")
    if layout.half_width <= 0.0 or layout.half_width > 0.5:
        raise ValueError(f"CAMERA_{layout.camera_id}_CORRIDOR_HALF_WIDTH invalido")
    for room, points in layout.rooms.items():
        _validate_points(points, minimum=3, name=f"CAMERA_{layout.camera_id}_ROOM_{room}_POLYGON")
        if abs(_polygon_area(points)) < 1e-6:
            raise ValueError(f"CAMERA_{layout.camera_id}_ROOM_{room}_POLYGON es degenerado")


def _validate_points(points: list[tuple[float, float]], *, minimum: int, name: str) -> None:
    if len(points) < minimum:
        raise ValueError(f"{name} requiere al menos {minimum} puntos")
    for x, y in points:
        if x < 0.0 or x > 1.0 or y < 0.0 or y > 1.0:
            raise ValueError(f"{name} contiene coordenadas fuera de 0.0..1.0")


def format_points(points: list[tuple[float, float]]) -> str:
    return ";".join(f"{x:.6f},{y:.6f}" for x, y in points)


def _to_pixels(points: list[tuple[float, float]], width: int, height: int) -> list[tuple[int, int]]:
    return [(round(x * width), round(y * height)) for x, y in points]


def _polygon_area(points: list[tuple[float, float]]) -> float:
    area = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, round(value, 6)))


if __name__ == "__main__":
    raise SystemExit(main())
