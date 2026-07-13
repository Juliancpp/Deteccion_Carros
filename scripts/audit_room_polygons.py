#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cv2
import numpy as np

from app import config
from app.settings import load_settings
from app.video.rtsp_stream import RTSPStream


REPORT_DIR = PROJECT_ROOT / "data" / "validation" / "reports" / "rooms"


def parse_args():
    parser = argparse.ArgumentParser(description="Audita superposicion y geometria de habitaciones hotel")
    parser.add_argument("--camera", choices=["A1", "A5", "all"], default="all")
    parser.add_argument("--image")
    parser.add_argument("--no-rtsp", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings(override_env=True)
    cameras = [camera for camera in settings.cameras if args.camera == "all" or camera.camera_id == args.camera]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = {}
    for camera in cameras:
        frame = _load_frame(args.image, settings.dvr, camera, no_rtsp=args.no_rtsp)
        camera_report = audit_camera(camera)
        report[camera.camera_id] = camera_report
        _draw_audit(frame, camera, camera_report, REPORT_DIR / f"{camera.camera_id}_room_audit.jpg")
        print(f"[ROOM_AUDIT] {camera.camera_id}")
        for warning in camera_report["warnings"]:
            print(f"  WARNING: {warning}")
        if not camera_report["warnings"]:
            print("  OK sin advertencias criticas")
    (REPORT_DIR / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[ROOM_AUDIT] Reporte: {REPORT_DIR / 'report.json'}")
    return 0


def audit_camera(camera) -> dict:
    rooms = {room.room_number: room.polygon for room in camera.rooms}
    corridor = camera.corridor.points if camera.corridor else ()
    warnings = []
    room_reports = {}
    for room_number, polygon in rooms.items():
        area = polygon_area(polygon)
        corridor_overlap = overlap_ratio(polygon, corridor_polygon(corridor, camera.corridor.half_width if camera.corridor else 0.0))
        if abs(area) < 1e-6:
            warnings.append(f"ROOM_{room_number} degenerada")
        if corridor_overlap > config.ROOM_MAX_CORRIDOR_OVERLAP_RATIO:
            warnings.append(f"ROOM_{room_number} superpone corredor {corridor_overlap:.2f}")
        room_reports[str(room_number)] = {
            "area": abs(area),
            "corridor_overlap_ratio": corridor_overlap,
            "distance_to_corridor": distance_polygon_to_polyline(polygon, corridor),
        }
    for room_a, polygon_a in rooms.items():
        for room_b, polygon_b in rooms.items():
            if room_b <= room_a:
                continue
            overlap = overlap_ratio(polygon_a, polygon_b)
            if overlap > config.ROOM_MAX_MUTUAL_OVERLAP_RATIO:
                warnings.append(f"ROOM_{room_a}/ROOM_{room_b} superpuestas {overlap:.2f}")
    return {"rooms": room_reports, "warnings": warnings}


def _load_frame(image_path: str | None, dvr, camera, *, no_rtsp: bool):
    if image_path:
        frame = cv2.imread(image_path)
        if frame is not None:
            return frame
    if no_rtsp:
        return np.zeros((720, 1280, 3), dtype=np.uint8)
    stream = RTSPStream(dvr, camera)
    stream.start()
    try:
        stream.wait_until_connected(timeout=8)
        import time
        end = time.monotonic() + 8
        while time.monotonic() < end:
            latest = stream.get_latest_frame()
            if latest is not None:
                return latest.frame.copy()
            time.sleep(0.05)
    finally:
        stream.stop()
    return np.zeros((720, 1280, 3), dtype=np.uint8)


def _draw_audit(frame, camera, report: dict, output_path: Path) -> None:
    h, w = frame.shape[:2]
    annotated = frame.copy()
    if camera.corridor:
        pts = np.array([(round(x * w), round(y * h)) for x, y in camera.corridor.points], dtype=np.int32)
        cv2.polylines(annotated, [pts], False, (0, 255, 255), 3)
    for room in camera.rooms:
        pts = np.array([(round(x * w), round(y * h)) for x, y in room.polygon], dtype=np.int32)
        cv2.polylines(annotated, [pts], True, (0, 0, 255), 2)
        if len(pts):
            cv2.putText(annotated, f"ROOM_{room.room_number}", tuple(pts[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    y = 28
    for warning in report["warnings"][:6]:
        cv2.putText(annotated, warning, (16, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 180, 255), 2)
        y += 24
    cv2.imwrite(str(output_path), annotated)


def polygon_area(points) -> float:
    if len(points) < 3:
        return 0.0
    return sum(points[i][0] * points[(i + 1) % len(points)][1] - points[(i + 1) % len(points)][0] * points[i][1] for i in range(len(points))) / 2.0


def overlap_ratio(poly_a, poly_b, grid: int = 160) -> float:
    if len(poly_a) < 3 or len(poly_b) < 3:
        return 0.0
    a_count = 0
    overlap = 0
    for yi in range(grid):
        y = (yi + 0.5) / grid
        for xi in range(grid):
            x = (xi + 0.5) / grid
            inside_a = point_in_polygon((x, y), poly_a)
            if inside_a:
                a_count += 1
                if point_in_polygon((x, y), poly_b):
                    overlap += 1
    return overlap / max(1, a_count)


def corridor_polygon(points, half_width):
    if len(points) < 2:
        return ()
    min_x = min(p[0] for p in points) - half_width
    max_x = max(p[0] for p in points) + half_width
    min_y = min(p[1] for p in points) - half_width
    max_y = max(p[1] for p in points) + half_width
    return ((max(0, min_x), max(0, min_y)), (min(1, max_x), max(0, min_y)), (min(1, max_x), min(1, max_y)), (max(0, min_x), min(1, max_y)))


def distance_polygon_to_polyline(polygon, line) -> float | None:
    if not polygon or len(line) < 2:
        return None
    return min(_point_to_segment_distance(point, line[i], line[i + 1]) for point in polygon for i in range(len(line) - 1))


def _point_to_segment_distance(point, a, b):
    px, py = point
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2) ** 0.5


def point_in_polygon(point, polygon) -> bool:
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


if __name__ == "__main__":
    raise SystemExit(main())
