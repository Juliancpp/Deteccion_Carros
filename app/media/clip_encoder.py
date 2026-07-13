from __future__ import annotations

import os
import shutil
import subprocess
import logging
from datetime import datetime
import cv2
import numpy as np

logger = logging.getLogger(__name__)


def is_ffmpeg_available() -> bool:
    """Checks if FFmpeg is installed and accessible in the system path."""
    return shutil.which("ffmpeg") is not None


def _draw_clip_overlay(frame: np.ndarray, captured_at: datetime, sequence: int, metadata: dict) -> None:
    """Draws a discrete semi-transparent overlay at the bottom of the clip frame."""
    h, w = frame.shape[:2]
    overlay_h = 32
    
    # Draw dark banner
    cv2.rectangle(frame, (0, h - overlay_h), (w, h), (15, 15, 15), -1)
    
    # Extract metadata details
    cam = str(metadata.get("camera_id", "Unknown")).upper()
    evt = str(metadata.get("event_type", "EVENTO")).upper()
    plate = str(metadata.get("plate") or "NO_LEGIBLE").upper()
    room = metadata.get("room_number")
    room_str = f"HAB: {room}" if room else "HAB: NO_DET"
    track = metadata.get("track_id", 0)
    ts = captured_at.strftime("%Y-%m-%d %H:%M:%S")
    
    info_text = f"{ts} | CAM: {cam} | TIPO: {evt} | PLACA: {plate} | {room_str} | ID: {track} | SEQ: {sequence}"
    
    # Write text with dynamic scale
    cv2.putText(
        frame,
        info_text,
        (15, h - 10),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.4,
        (240, 240, 240),
        1,
        cv2.LINE_AA
    )


def encode_clip(
    frames: list[dict],
    metadata: dict,
    output_path: str,
    fps: int = 5,
    crf: int = 28,
    width: int = 960,
    height: int = 540
) -> None:
    """
    Encodes raw frames to a web-playable H.264 MP4 file using an FFmpeg subprocess.
    If FFmpeg is not available, raises a RuntimeError.
    """
    if not is_ffmpeg_available():
        raise RuntimeError("FFmpeg command line utility is not available on the system.")

    if not frames:
        raise ValueError("Cannot encode an empty frame list.")

    width = int(width) - (int(width) % 2)
    height = int(height) - (int(height) % 2)
    if width <= 0 or height <= 0:
        raise ValueError("Invalid clip dimensions.")

    valid_frames = [
        item for item in frames
        if item.get("frame") is not None
        and isinstance(item.get("frame"), np.ndarray)
        and item["frame"].size > 0
        and len(item["frame"].shape) >= 2
    ]
    if not valid_frames:
        raise ValueError("Cannot encode a clip without valid frames.")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    tmp_output = output_path + ".tmp.mp4"

    cmd = [
        "ffmpeg",
        "-y",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-r", str(fps),
        "-i", "-",
        "-an",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        tmp_output
    ]

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True
    )

    try:
        for item in valid_frames:
            frame = item["frame"]
            
            # Resize frame to target dimensions
            frame_resized = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            
            # Apply dynamic text overlays
            _draw_clip_overlay(frame_resized, item["captured_at"], item["sequence"], metadata)
            
            # Encode frame to JPEG format
            success, jpeg_data = cv2.imencode(".jpg", frame_resized, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not success:
                continue
                
            try:
                process.stdin.write(jpeg_data.tobytes())
            except BrokenPipeError as exc:
                raise RuntimeError("FFmpeg pipe closed while writing frames.") from exc

        if process.stdin:
            process.stdin.close()
            process.stdin = None
        stdout, stderr = process.communicate(timeout=15.0)

        if process.returncode != 0:
            err_msg = stderr.decode(errors="replace")
            raise RuntimeError(f"FFmpeg returned code {process.returncode}: {err_msg}")
        os.replace(tmp_output, output_path)

    except subprocess.TimeoutExpired as e:
        process.kill()
        try:
            process.communicate(timeout=2.0)
        except Exception:
            pass
        raise RuntimeError("FFmpeg timed out while encoding clip.") from e
    except Exception as e:
        try:
            process.kill()
        except Exception:
            pass
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass
        try:
            if os.path.exists(tmp_output):
                os.remove(tmp_output)
        except Exception:
            pass
        raise e
