#!/usr/bin/env python3
"""Calibración visual de A1/A5 y exportación de revisiones locales."""

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.hotel.geometry import KINDS, Layout
from app.hotel.geometry_overlay import draw_features
from app.settings import load_settings
from scripts.calibrate_hotel_zones import _capture_frame


def export_review(destination, layout, frame):
    """Publica el par JSON/JPEG en un directorio nuevo."""
    if destination.exists():
        raise FileExistsError(f"La revisión ya existe: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    preview = frame.copy()
    draw_features(preview, layout.features, include_disabled=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        staging = Path(temp) / "revision"
        staging.mkdir()
        if not cv2.imwrite(str(staging / "preview.jpg"), preview):
            raise OSError("No se pudo escribir preview.jpg")
        (staging / "geometry.json").write_text(json.dumps(asdict(layout), indent=2) + "\n")
        staging.rename(destination)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", choices=["A1", "A5"])
    parser.add_argument("--interactive", action="store_true", help="Abrir el editor gráfico de cinco minutos")
    parser.add_argument("--resume", type=Path, help="Recuperar una sesión desde draft.json")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--image", type=Path)
    source.add_argument("--camera-frame", action="store_true")
    parser.add_argument("--config", type=Path, help="JSON inicial")
    parser.add_argument("--output", type=Path, help="Directorio nuevo de revisión")
    parser.add_argument("--edit", metavar="ID", help="Dibujar o redibujar una figura")
    parser.add_argument("--kind", choices=sorted(KINDS))
    parser.add_argument("--enabled", action=argparse.BooleanOptionalAction)
    parser.add_argument("--invert-direction", action=argparse.BooleanOptionalAction)
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error("--output ya existe")
    if not args.edit and any(v is not None for v in (args.kind, args.enabled, args.invert_direction)):
        parser.error("indique --edit para modificar una figura")
    if args.resume and (args.image or args.camera_frame or args.config or args.edit):
        parser.error("--resume recupera su propio frame y geometría")
    if not args.camera and (args.image or args.camera_frame or args.config or args.resume or args.edit):
        parser.error("indique --camera A1 o A5 para esta fuente")
    if (
        args.interactive
        or args.edit
        or args.resume
        or not args.output
        or not (args.image or args.camera_frame)
    ):
        from scripts.geometry_editor import desktop

        edit = (
            {
                "id": args.edit,
                "kind": args.kind,
                "enabled": args.enabled,
                "invert_direction": args.invert_direction,
            }
            if args.edit
            else None
        )
        return desktop(
            args.camera, args.image, args.camera_frame, args.config, args.output, args.resume, edit
        )
    layout = Layout.load(args.config, args.camera) if args.config else Layout(args.camera, ())
    if args.image:
        frame = cv2.imread(str(args.image))
        if frame is None:
            parser.error("imagen ilegible")
    else:
        settings = load_settings(override_env=True)
        camera = next((c for c in settings.cameras if c.camera_id == args.camera), None)
        if camera is None:
            parser.error("cámara desconocida")
        frame = _capture_frame(settings.dvr, camera)
    export_review(args.output, layout, frame)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
