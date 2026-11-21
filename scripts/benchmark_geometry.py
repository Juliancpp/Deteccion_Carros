"""Compare the checked-out coordinator and an archived base on identical MP4 frames."""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace

import cv2


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=root)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--geometry", action="store_true")
    parser.add_argument(
        "--samples", type=int, default=25, help="Frames medidos por cámara tras 3 de calentamiento"
    )
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo.resolve()))
    from app import detector
    from app.settings import load_settings
    from app.multicamera.coordinator import MultiCameraCoordinator
    from app.video.rtsp_stream import LatestFrame

    weights = root / "models/yolo11n.pt"
    if not weights.is_file():
        parser.error("se requieren los pesos locales")
    detector.MODEL_PATH = str(weights)
    env = {}
    for cam, channel in (("A1", 1), ("A5", 5)):
        for key, value in dict(
            ID=cam, NAME=cam, CHANNEL=str(channel), ENABLED="true", TARGET_FPS="5", VIDEO_PATH=str(args.video)
        ).items():
            env[f"CAMERA_{cam}_{key}"] = value
        if args.geometry:
            env[f"CAMERA_{cam}_GEOMETRY_FILE"] = str(root / f"config/geometry/{cam}.json")
    settings = load_settings(environ=env)
    timings = defaultdict(list)

    def timed(obj, method, label):
        original = getattr(obj, method)

        def measure(*args, **kwargs):
            start = time.perf_counter()
            result = original(*args, **kwargs)
            timings[label].append((time.perf_counter() - start) * 1000)
            return result

        setattr(obj, method, measure)

    class Replay:
        sequence = 0

        def get_status(self):
            return SimpleNamespace(connected=True, reconnect_count=0, frame_sequence=self.sequence)

        def get_latest_frame(self, after_sequence):
            return LatestFrame(self.image, datetime.now(timezone.utc), self.sequence)

    co = MultiCameraCoordinator(settings.dvr, settings.cameras, stream_factory=lambda *_: Replay())
    timed(co.detector, "detect", "yolo")
    timed(co, "_annotate_frame", "overlay")
    timed(
        co,
        "_process_crossings" if hasattr(co, "_process_crossings") else "_process_passage_confirmations",
        "events",
    )
    for ctx in co.contexts:
        timed(ctx.tracker, "update", "bytetrack")
    cap = cv2.VideoCapture(str(args.video))
    stride = max(1, round(cap.get(cv2.CAP_PROP_FPS) / 5))
    tracks = 0
    try:
        for sample in range(args.samples + 3):
            if sample == 3:
                timings.clear()
                tracks = 0
            start = time.perf_counter()
            for _ in range(stride):
                ok, frame = cap.read()
                if not ok:
                    raise ValueError("vídeo insuficiente para la cantidad de muestras")
            timings["decode_per_sample"].append((time.perf_counter() - start) * 1000)
            for ctx in co.contexts:
                ctx.stream.image = frame
                ctx.stream.sequence += 1
                start = time.perf_counter()
                co._process_camera(ctx, time.monotonic())
                timings["pipeline"].append((time.perf_counter() - start) * 1000)
                tracks += len(ctx.last_result.tracked_vehicles)
    finally:
        cap.release()
    print(
        json.dumps(
            dict(
                samples_per_camera=args.samples,
                stride=stride,
                track_observations=tracks,
                targets=[c.target_fps for c in co.contexts],
                video_sha256=hashlib.sha256(args.video.read_bytes()).hexdigest(),
                weights_sha256=hashlib.sha256(weights.read_bytes()).hexdigest(),
                mean_ms={k: round(statistics.mean(v), 3) for k, v in timings.items()},
            )
        )
    )


if __name__ == "__main__":
    main()
