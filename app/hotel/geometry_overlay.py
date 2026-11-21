import math

import cv2

from app.hotel.geometry import LINE_KINDS


def draw_features(frame, features, *, include_disabled=False):
    height, width = frame.shape[:2]
    colors = {
        "hotel_entry": (80, 230, 80),
        "hotel_exit": (30, 100, 255),
        "room": (255, 190, 50),
        "approach": (60, 225, 255),
    }
    labels = []
    for feature in features:
        if not feature.enabled and not include_disabled:
            continue
        pixels = [(round(x * (width - 1)), round(y * (height - 1))) for x, y in feature.points]
        color = colors[feature.kind]
        if include_disabled and feature.kind == "room":
            palette = [(255, 190, 50), (245, 110, 225), (40, 180, 255)]
            color = palette[sum(feature.id.encode("utf8")) % len(palette)]
        if not feature.enabled:
            color = tuple(round(c * 0.75) for c in color)
        line = feature.kind in LINE_KINDS
        ends = pixels[1:] if line else pixels[1:] + pixels[:1]
        for a, b in zip(pixels, ends):
            cv2.line(frame, a, b, color, 2, cv2.LINE_AA)
        if line:
            (x, y), (u, v) = pixels
            length = max(1, math.hypot(u - x, v - y))
            scale = (-24 if feature.invert_direction else 24) / length
            mid = ((x + u) // 2, (y + v) // 2)
            tip = (round(mid[0] - (v - y) * scale), round(mid[1] + (u - x) * scale))
            cv2.arrowedLine(frame, mid, tip, color, 2, tipLength=0.35)
        label = f"{feature.id} ({feature.kind})" + ("" if feature.enabled else " OFF")
        label_size = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)[0]
        origin = (max(0, min(pixels[0][0], width - label_size[0] - 2)), max(16, pixels[0][1] - 4))
        while (
            any(
                abs(origin[1] - y) < 17 and origin[0] < right and origin[0] + label_size[0] > left
                for left, right, y in labels
            )
            and origin[1] + 18 < height
        ):
            origin = (origin[0], origin[1] + 18)
        labels.append((origin[0], origin[0] + label_size[0], origin[1]))
        for ink, thickness in (((0, 0, 0), 3), (color, 1)):
            cv2.putText(frame, label, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.5, ink, thickness, cv2.LINE_AA)
