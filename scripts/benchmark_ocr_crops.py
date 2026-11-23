"""Ejecuta el OCR de un checkout sobre secuencias privadas de recortes anotados."""
import argparse
import csv
import resource
import sys
import time
from pathlib import Path

import cv2
import easyocr


def iou(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2]-a[0]) * (a[3]-a[1]) + (b[2]-b[0]) * (b[3]-b[1]) - intersection
    return intersection / union if union > 0 else 0


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.checkout.resolve()))
    from app.ocr import PlateReader, locate_plate_candidates
    from app.hotel.plate_voting import vote_plates_multiframe

    reader = PlateReader(enabled=False)
    reader.reader = easyocr.Reader(["en"], gpu=False, download_enabled=False, verbose=False)
    reader.enabled = True
    sequences = {}
    with args.frames.open() as stream:
        for row in csv.DictReader(stream):
            sequences.setdefault(row["sample_id"], []).append(row)
    if not sequences:
        parser.error("Se requieren recortes autorizados")
    with args.output.open("x", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=["sample_id", "plate_text", "plate_status",
                                                    "plate_iou", "wall_ms", "cpu_ms", "rss_peak_mib"])
        writer.writeheader()
        for key, frames in sequences.items():
            candidates = []
            images = {}
            for row in frames:
                path = (args.frames.parent / row["image_path"]).resolve()
                if path in images:
                    raise ValueError("Fotograma repetido")
                images[path] = cv2.imread(str(path))
                if images[path] is None:
                    raise ValueError("Recorte ilegible")
            start, cpu = time.perf_counter(), time.process_time()
            if len(images) > 5:
                raise ValueError("El manifiesto admite hasta cinco fotogramas seleccionados por vehículo")
            for index, image in enumerate(images.values()):
                candidates.extend(reader.read_all_candidates(image, str(index)))
            options = {"observed_only": True} if hasattr(reader, "inspect_frame") else {}
            decision = vote_plates_multiframe(candidates, **options)
            elapsed = (time.perf_counter() - start) * 1000
            cpu_ms = (time.process_time() - cpu) * 1000
            overlaps = []
            for row, image in zip(frames, images.values()):
                if row.get("plate_bbox"):
                    box = list(map(float, row["plate_bbox"].split()))
                    if len(box) != 4 or box[0] >= box[2] or box[1] >= box[3]:
                        raise ValueError("plate_bbox inválida")
                    regions = locate_plate_candidates(image, max_candidates=6 if options else 3)
                    overlaps.append(max((iou(box, r["crop_bbox"]) for r in regions), default=0))
            text = decision["plate"] or decision.get("partial_text", "")
            writer.writerow(dict(sample_id=key, plate_text=text, plate_status=decision["status"],
                                 plate_iou=max(overlaps) if overlaps else "", wall_ms=elapsed, cpu_ms=cpu_ms,
                                 rss_peak_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024))


if __name__ == "__main__":
    run()
