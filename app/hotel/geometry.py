"""Geometrías normalizadas y transiciones independientes por cámara y track."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from shapely.geometry import LineString, Polygon
from shapely.geometry import Point as ShapePoint

from app.hotel.gate_detector import bottom_center
from app.hotel.room_detector import point_in_polygon

Point = tuple[float, float]
LINE_KINDS = {"hotel_entry", "hotel_exit"}
KINDS = LINE_KINDS | {"room", "approach"}


@dataclass(frozen=True)
class Feature:
    id: str
    kind: str
    points: tuple[Point, ...]
    enabled: bool = True
    invert_direction: bool = False


@dataclass(frozen=True)
class Layout:
    camera_id: str
    features: tuple[Feature, ...]
    observations: int = 2
    margin: float = 0.005
    ttl_seconds: float = 3.0
    use_for_visits: bool = False

    @classmethod
    def parse(cls, data, camera_id):
        if not isinstance(data, dict) or data.get("camera_id") != camera_id:
            raise ValueError("camera_id no coincide")
        if set(data) - {"camera_id", "features", "observations", "margin", "ttl_seconds", "use_for_visits"}:
            raise ValueError("campo de geometría desconocido")
        features = []
        ids = set()
        if not isinstance(data.get("features"), list):
            raise ValueError("features debe ser una lista")
        for raw in data["features"]:
            if not isinstance(raw, dict) or set(raw) - {
                "id",
                "kind",
                "points",
                "enabled",
                "invert_direction",
            }:
                raise ValueError("campos de figura inválidos")
            name, kind = raw.get("id"), raw.get("kind")
            if not isinstance(name, str) or not name.strip() or name in ids or kind not in KINDS:
                raise ValueError(f"id o kind inválido: {name}")
            ids.add(name)
            points = raw.get("points")
            if not isinstance(points, (tuple, list)) or any(
                not isinstance(p, (list, tuple))
                or len(p) != 2
                or any(type(v) not in (int, float) or not 0 <= v <= 1 for v in p)
                for p in points
            ):
                raise ValueError(f"coordenadas inválidas: {name}")
            points = tuple(map(tuple, points))
            line = kind in LINE_KINDS
            if (
                (line and len(points) != 2)
                or (not line and len(points) < 3)
                or len(set(points)) != len(points)
            ):
                raise ValueError(f"vértices inválidos: {name}")
            if not line and not Polygon(points).is_valid:
                raise ValueError(f"polígono inválido: {name}")
            enabled = raw.get("enabled", True)
            invert = raw.get("invert_direction", False)
            if type(enabled) != bool or type(invert) != bool or (invert and not line):
                raise ValueError(f"flags inválidos: {name}")
            features.append(Feature(name, kind, points, enabled, invert))
        layout = cls(
            camera_id,
            tuple(features),
            **{k: data[k] for k in ("observations", "margin", "ttl_seconds", "use_for_visits") if k in data},
        )
        if type(layout.use_for_visits) != bool:
            raise ValueError("use_for_visits debe ser booleano")
        if layout.use_for_visits:
            main = [f.kind for f in features if f.enabled and f.kind in LINE_KINDS]
            if not main or len(main) != len(set(main)):
                raise ValueError("active una línea por sentido operativo para visitas")
        if type(layout.observations) != int or layout.observations < 1:
            raise ValueError("observations debe ser entero positivo")
        for value in (layout.margin, layout.ttl_seconds):
            if type(value) not in (float, int) or not math.isfinite(value):
                raise ValueError("margin y ttl_seconds deben ser finitos")
        if not 0 <= layout.margin < 1 or layout.ttl_seconds <= 0:
            raise ValueError("margin o ttl_seconds fuera de rango")
        return layout

    @classmethod
    def load(cls, path: Path, camera_id: str):
        return cls.parse(json.loads(path.read_text(encoding="utf-8")), camera_id)


def cross(a, b, p):
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def side(feature, point, margin):
    pts = feature.points
    if feature.kind in LINE_KINDS:
        distance = cross(*pts, point) / math.dist(*pts)
        return None if abs(distance) <= margin else distance > 0
    for a, b in zip(pts, pts[1:] + pts[:1]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        t = max(0, min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / (dx * dx + dy * dy)))
        if math.dist(point, (a[0] + t * dx, a[1] + t * dy)) <= margin:
            return None
    return point_in_polygon(point, pts)


@dataclass
class _SideState:
    stable: bool | None = None
    last_off_line: Point | None = None
    crossed_segment: bool = False
    on_line: Point | None = None
    touched_segment: bool = False
    candidate: bool | None = None
    count: int = 0

    def observe(self, feature, point, margin, observations):
        if feature.kind in LINE_KINDS:
            # Follow the observed path even inside the uncertainty band. An
            # excursion around an endpoint must not turn into a chord crossing.
            signed = cross(*feature.points, point)
            previous_point = self.last_off_line
            if signed == 0:
                contact = ShapePoint(point) if self.on_line is None else LineString((self.on_line, point))
                self.touched_segment |= contact.intersects(LineString(feature.points))
                self.on_line = point
            else:
                if previous_point and signed * cross(*feature.points, previous_point) < 0:
                    if self.on_line is None:
                        self.crossed_segment = LineString((previous_point, point)).intersects(
                            LineString(feature.points)
                        )
                    else:
                        self.crossed_segment = self.touched_segment
                self.last_off_line = point
                self.on_line = None
                self.touched_segment = False
        current = side(feature, point, margin)
        if current is None or current == self.stable:
            self.count = 0
            return None
        if self.count == 0 or self.candidate != current:
            self.candidate, self.count = current, 0
        self.count += 1
        if self.count < observations:
            return None
        previous = self.stable
        self.stable, self.count = current, 0
        if previous is None:
            return None
        if feature.kind in LINE_KINDS:
            if current == feature.invert_direction:
                return None
            if self.crossed_segment:
                return feature.kind.upper()
        elif feature.kind == "room":
            return "ROOM_ENTRY" if current else "ROOM_EXIT"
        elif current:
            return "ROOM_APPROACH"
        return None


@dataclass
class _Track:
    last_seen: float
    states: dict[str, _SideState] = field(default_factory=dict)


@dataclass(frozen=True)
class GeometryEvent:
    session_id: str
    camera_id: str
    track_id: int
    source_sequence: int
    occurred_at: datetime
    feature_id: str
    event_type: str
    evidence_image: object = field(default=None, compare=False, repr=False)
    evidence_path: str | None = None


class GeometryDetector:
    def __init__(self, layout):
        self.layout = layout
        self.features = tuple(f for f in layout.features if f.enabled)
        self.reset()

    def reset(self):
        self.session_id = uuid4().hex
        self.tracks = {}
        self.last_sequence = -1

    def update(self, vehicles, width, height, timestamp, sequence, now):
        if sequence <= self.last_sequence:
            return []
        self.last_sequence = sequence
        self.tracks = {k: v for k, v in self.tracks.items() if now - v.last_seen <= self.layout.ttl_seconds}
        events = []
        for vehicle in vehicles:
            track_id = vehicle.get("id")
            if track_id is None:
                continue
            x, y = bottom_center(vehicle["bbox"])
            point = (x / width, y / height)
            track = self.tracks.setdefault(track_id, _Track(now))
            track.last_seen = now
            for feature in self.features:
                state = track.states.setdefault(feature.id, _SideState())
                event = state.observe(feature, point, self.layout.margin, self.layout.observations)
                if event:
                    events.append(
                        GeometryEvent(
                            self.session_id,
                            self.layout.camera_id,
                            track_id,
                            sequence,
                            timestamp,
                            feature.id,
                            event,
                        )
                    )
        return events
