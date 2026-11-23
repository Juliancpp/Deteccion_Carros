"""Evalúa un CSV de predicciones contra referencias privadas, por vehículo."""
import argparse
import csv
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from statistics import median

MANIFEST = Path(__file__).resolve().parents[1] / "data/validation/plates/manifest.csv"


def clean(value):
    return re.sub(r"[\s-]", "", (value or "").upper())


def edit_distance(left, right):
    row = list(range(len(right) + 1))
    for i, first in enumerate(left, 1):
        following = [i]
        for j, second in enumerate(right, 1):
            following.append(min(row[j] + 1, following[-1] + 1, row[j - 1] + (first != second)))
        row = following
    return row[-1]


def read_index(path):
    with path.open(encoding="utf-8", newline="") as stream:
        records = {}
        for row in csv.DictReader(stream):
            key = row["sample_id"]
            if not key or key in records:
                raise ValueError("Cada vehículo requiere un sample_id único")
            records[key] = row
        return records


def ratio(num, denominator):
    return num / denominator if denominator else None


def evaluate(labels, predictions):
    if not labels or set(predictions) != set(labels):
        raise ValueError("Se requiere una predicción explícita por vehículo, incluidos los omitidos")
    visible = invisible = exact = partial = false_positive = errors = chars = labeled = 0
    located = bbox_labeled = false_confirmations = 0
    for key, row in labels.items():
        prediction = predictions[key]
        flag = row.get("plate_visible", "").strip().lower()
        if flag not in {"1", "0", "true", "false"}:
            raise ValueError("plate_visible requiere 1 o 0")
        truth, guess = clean(row["true_plate"]), clean(prediction["plate_text"])
        status = prediction["plate_status"]
        if flag in {"0", "false"}:
            if truth:
                raise ValueError("Una placa invisible no admite transcripción de referencia")
            invisible += 1
            false_positive += bool(guess)
        else:
            visible += 1
            if prediction.get("plate_iou", "") != "":
                iou = float(prediction["plate_iou"])
                if not 0 <= iou <= 1:
                    raise ValueError("IoU fuera de rango")
                bbox_labeled += 1
                located += iou >= 0.5
        if truth:
            labeled += 1
            errors += edit_distance(truth, guess)
            chars += len(truth)
            exact += truth == guess
            partial += bool(guess and guess != truth and guess in truth)
        false_confirmations += status == "LEIDA" and truth != guess and (bool(truth) or flag in {"0", "false"})
    result = dict(vehicles=len(labels), visible_fraction=ratio(visible, len(labels)), visible=visible,
                  localization_labeled=bbox_labeled, localized_at_iou50=ratio(located, bbox_labeled),
                  transcribed=labeled, exact_fraction=ratio(exact, labeled), partial_fraction=ratio(partial, labeled),
                  char_errors=errors, reference_chars=chars, cer=ratio(errors, chars),
                  invisible=invisible, false_positive_fraction=ratio(false_positive, invisible),
                  incorrect_leida=false_confirmations)
    for field in ("wall_ms", "cpu_ms", "rss_peak_mib"):
        values = [float(row[field]) for row in predictions.values() if row.get(field, "") != ""]
        result[field + "_median"] = median(values) if values else None
    return result


def replay_predictions(path, labels):
    """Evalúa una BD de replay dedicada. La correspondencia humana usa tiempo y cámara."""
    if not path.is_file():
        raise ValueError("Falta la base de replay")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        events = {r["event_id"]: r for r in conn.execute("SELECT * FROM hotel_vehicle_events")}
    mapped = set()
    predictions = {}
    for key, label in labels.items():
        if "event_id" not in label:
            raise ValueError("El manifiesto de replay requiere event_id o una celda vacía para una omisión")
        event_id = label["event_id"]
        pred = dict(plate_text="", plate_status="SIN_EVENTO")
        if event_id:
            if event_id in mapped or event_id not in events:
                raise ValueError("Correspondencia de evento inválida o duplicada")
            mapped.add(event_id)
            row = events[event_id]
            if label.get("camera_id") and label["camera_id"] != row["camera_id"]:
                raise ValueError("Cámara incompatible")
            diagnostic = json.loads(row["ocr_diagnostics_json"] or "{}") if "ocr_diagnostics_json" in row.keys() else {}
            pred.update(plate_text=row["plate"] or diagnostic.get("partial_text") or "", plate_status=row["plate_status"])
        predictions[key] = pred
    return predictions, len(events) - len(mapped)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--predictions", type=Path)
    source.add_argument("--replay-db", type=Path)
    parser.add_argument("--scope", choices=["recorte", "completo"], required=True)
    parser.add_argument("--pipeline", required=True)
    args = parser.parse_args()
    try:
        labels = read_index(args.manifest)
        if args.replay_db:
            if args.scope != "completo":
                parser.error("--replay-db requiere --scope completo")
            predictions, extra = replay_predictions(args.replay_db, labels)
            result = dict(evaluate(labels, predictions), unmatched_events=extra,
                          missed_vehicles=sum(p["plate_status"] == "SIN_EVENTO" for p in predictions.values()))
        else:
            result = evaluate(labels, read_index(args.predictions))
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"Benchmark incompleto: {exc}\n")
    print(json.dumps(dict(pipeline=args.pipeline, scope=args.scope, **result), indent=2))


if __name__ == "__main__":
    main()
