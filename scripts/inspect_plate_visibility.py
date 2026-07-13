#!/usr/bin/env python3
import sys
import os
import cv2
import numpy as np
from pathlib import Path

def calculate_sharpness(gray_img: np.ndarray) -> float:
    return float(cv2.Laplacian(gray_img, cv2.CV_64F).var())

def calculate_brightness_contrast(gray_img: np.ndarray) -> tuple[float, float]:
    mean, std = cv2.meanStdDev(gray_img)
    return float(mean[0][0]), float(std[0][0])

def check_border_violation(bbox: tuple[int, int, int, int], img_shape: tuple[int, int]) -> bool:
    x1, y1, x2, y2 = bbox
    h, w = img_shape[:2]
    # Margin of 2 pixels from the frame border
    return x1 <= 2 or y1 <= 2 or x2 >= w - 2 or y2 >= h - 2

def inspect_image_quality(img_path: str, bbox: tuple[int, int, int, int] = None) -> dict:
    img = cv2.imread(img_path)
    if img is None:
        return {"error": f"Could not load image: {img_path}"}
    
    h, w = img.shape[:2]
    if bbox is None:
        bbox = (0, 0, w, h)
        
    x1, y1, x2, y2 = [int(round(c)) for c in bbox]
    crop = img[y1:y2, x1:x2]
    if crop.size == 0:
        return {"error": f"Empty crop for bbox {bbox} in {img_path}"}
        
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    sharpness = calculate_sharpness(gray)
    brightness, contrast = calculate_brightness_contrast(gray)
    border_violation = check_border_violation((x1, y1, x2, y2), (h, w))
    area = int((x2 - x1) * (y2 - y1))
    
    # Assess suitability
    # Sharpness > 50, contrast > 25, width > 60, height > 30 as heuristic for decent plates
    is_suitable = sharpness >= 40.0 and contrast >= 15.0 and not border_violation and (x2 - x1) >= 40
    
    return {
        "width": x2 - x1,
        "height": y2 - y1,
        "area": area,
        "sharpness": sharpness,
        "brightness": brightness,
        "contrast": contrast,
        "border_violation": border_violation,
        "is_suitable": is_suitable,
        "shape": img.shape
    }

def main():
    if len(sys.argv) < 2:
        print("Usage: inspect_plate_visibility.py <image_path>")
        sys.exit(1)
        
    img_path = sys.argv[1]
    if not os.path.exists(img_path):
        # Check in validation/
        validation_alt = Path(__file__).resolve().parent.parent / "data" / "validation" / img_path
        if validation_alt.exists():
            img_path = str(validation_alt)
        else:
            print(f"File not found: {img_path}")
            sys.exit(1)
            
    stats = inspect_image_quality(img_path)
    if "error" in stats:
        print(f"Error: {stats['error']}")
        sys.exit(1)
        
    print(f"--- Optical Assessment for {Path(img_path).name} ---")
    print(f"Dimensions: {stats['width']}x{stats['height']} (Area: {stats['area']} px)")
    print(f"Sharpness (Laplacian Var): {stats['sharpness']:.2f}")
    print(f"Brightness (Mean): {stats['brightness']:.2f}")
    print(f"Contrast (Std Dev): {stats['contrast']:.2f}")
    print(f"Border Violation: {stats['border_violation']}")
    print(f"Optically Suitable: {stats['is_suitable']}")
    
    # Save annotated report image
    img = cv2.imread(img_path)
    annotated = img.copy()
    label_y = 35
    for k, v in stats.items():
        if k in ("shape", "area"):
            continue
        text = f"{k}: {v}"
        cv2.putText(annotated, text, (15, label_y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        label_y += 30
        
    report_dir = Path(__file__).resolve().parent.parent / "data" / "validation" / "reports"
    os.makedirs(report_dir, exist_ok=True)
    report_path = report_dir / f"report_{Path(img_path).name}"
    cv2.imwrite(str(report_path), annotated)
    print(f"Annotated report saved to: {report_path}")

if __name__ == "__main__":
    main()
