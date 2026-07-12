#!/usr/bin/env bash
set -euo pipefail

python_bin="${PYTHON_BIN:-python}"

version="$("$python_bin" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
)"

if [[ "$version" != "3.12" ]]; then
  echo "ERROR: se requiere Python 3.12; detectado $version" >&2
  exit 1
fi

"$python_bin" -m pip install --upgrade pip
"$python_bin" -m pip install \
  --index-url https://download.pytorch.org/whl/cpu \
  torch==2.13.0+cpu torchvision==0.28.0+cpu

# EasyOCR declara opencv-python-headless; se instala sin dependencias para
# conservar un solo OpenCV: opencv-python.
"$python_bin" -m pip install --no-deps easyocr==1.7.2
"$python_bin" -m pip install -r requirements-cpu.txt

"$python_bin" - <<'PY'
import importlib.util
import sys

required = ["torch", "torchvision", "ultralytics", "easyocr", "cv2"]
missing = [name for name in required if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit(f"ERROR: paquetes ausentes: {', '.join(missing)}")

import torch

if torch.version.cuda is not None or torch.cuda.is_available():
    raise SystemExit("ERROR: Torch no es CPU-only")

print("Entorno CPU validado:", sys.executable)
PY

"$python_bin" -m pip check
