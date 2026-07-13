from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

import app.config as config

VALID_RUNTIME_CAMERAS = {"A1", "A5"}
RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", str(Path(config.BASE_DIR) / "data" / "runtime"))).resolve()


def validate_runtime_camera(camera_id: str) -> str:
    camera = str(camera_id).upper()
    if camera not in VALID_RUNTIME_CAMERAS:
        raise ValueError(f"camera_id no permitido: {camera_id}")
    return camera


def runtime_snapshot_path(camera_id: str) -> Path:
    camera = validate_runtime_camera(camera_id)
    return RUNTIME_DIR / f"{camera}_latest.jpg"


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    import json

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def atomic_write_jpeg(path: str | Path, frame: np.ndarray, quality: int = 85) -> bool:
    if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0 or len(frame.shape) < 2:
        return False
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok or data is None or data.size == 0:
        return False
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data.tobytes())
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return True


@dataclass(frozen=True)
class SnapshotInfo:
    available: bool
    updated_at: str | None
    age_seconds: float | None
    stale: bool
    size_bytes: int


def snapshot_info(camera_id: str, stale_seconds: float = 10.0) -> SnapshotInfo:
    path = runtime_snapshot_path(camera_id)
    if not path.is_file():
        return SnapshotInfo(False, None, None, False, 0)
    stat = path.stat()
    age = max(0.0, time.time() - stat.st_mtime)
    return SnapshotInfo(
        available=stat.st_size > 0 and is_valid_jpeg(path),
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(stat.st_mtime)),
        age_seconds=round(age, 3),
        stale=age > stale_seconds,
        size_bytes=stat.st_size,
    )


def is_valid_jpeg(path: str | Path) -> bool:
    target = Path(path)
    try:
        if not target.is_file() or target.stat().st_size <= 0:
            return False
        with open(target, "rb") as handle:
            head = handle.read(3)
        if head != b"\xff\xd8\xff":
            return False
        img = cv2.imread(str(target), cv2.IMREAD_COLOR)
        return img is not None and img.size > 0
    except Exception:
        return False
