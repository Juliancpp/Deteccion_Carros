#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR=""
INSTALL_USER=""
ENABLE_NOW=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    --user) INSTALL_USER="$2"; shift 2 ;;
    --enable-now) ENABLE_NOW=1; shift ;;
    *) echo "Argumento no reconocido: $1" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Ejecutar con sudo/root." >&2
  exit 1
fi
if [[ -z "$PROJECT_DIR" || -z "$INSTALL_USER" ]]; then
  echo "Uso: sudo bash scripts/install_production.sh --project-dir /ruta --user usuario [--enable-now]" >&2
  exit 2
fi
if [[ ! -r /etc/os-release ]] || ! grep -qi ubuntu /etc/os-release; then
  echo "Este instalador está preparado para Ubuntu." >&2
  exit 1
fi

PROJECT_DIR="$(readlink -f "$PROJECT_DIR")"
cd "$PROJECT_DIR"

echo "[1/14] Instalando dependencias apt..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git ffmpeg sqlite3 curl ca-certificates build-essential pkg-config libgl1 libglib2.0-0 python3.12 python3.12-venv python3.12-dev

PYTHON_BIN="$(command -v python3.12)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.12 no encontrado." >&2
  exit 1
fi

echo "[2/14] Preparando venv..."
if [[ ! -x "$PROJECT_DIR/venv/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$PROJECT_DIR/venv"
fi
"$PROJECT_DIR/venv/bin/python" -m pip install --upgrade pip wheel setuptools

echo "[3/14] Instalando Torch CPU..."
"$PROJECT_DIR/venv/bin/python" -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision torchaudio

echo "[4/14] Instalando requirements..."
"$PROJECT_DIR/venv/bin/python" -m pip install -r requirements.txt
if [[ -f requirements-dev.txt ]]; then
  "$PROJECT_DIR/venv/bin/python" -m pip install -r requirements-dev.txt
fi
"$PROJECT_DIR/venv/bin/python" -m pip check

echo "[5/14] Creando directorios runtime..."
install -d -o "$INSTALL_USER" -g "$INSTALL_USER" "$PROJECT_DIR/data/runtime" "$PROJECT_DIR/data/captures" "$PROJECT_DIR/data/clips" "$PROJECT_DIR/data/reportes" "$PROJECT_DIR/database" "$PROJECT_DIR/logs"

echo "[6/14] Verificando archivos privados..."
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "Falta .env. Crear desde .env.example sin imprimir secretos." >&2
  exit 1
fi
if [[ ! -f "$PROJECT_DIR/models/yolo11n.pt" ]]; then
  echo "Falta models/yolo11n.pt." >&2
  exit 1
fi
chown -R "$INSTALL_USER:$INSTALL_USER" "$PROJECT_DIR"
chmod 750 "$PROJECT_DIR"
chmod 640 "$PROJECT_DIR/.env"

echo "[7/14] Ejecutando pruebas offline..."
sudo -u "$INSTALL_USER" "$PROJECT_DIR/venv/bin/python" -m pytest -q
sudo -u "$INSTALL_USER" "$PROJECT_DIR/venv/bin/python" scripts/release_check.py --skip-real-cameras

echo "[8/14] Instalando systemd..."
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
for unit in deteccion-carros.service deteccion-dashboard.service; do
  sed -e "s#REPLACE_USER#$INSTALL_USER#g" -e "s#REPLACE_WORKING_DIR#$PROJECT_DIR#g" \
    "$PROJECT_DIR/deploy/systemd/$unit" > "$tmpdir/$unit"
  install -m 0644 "$tmpdir/$unit" "/etc/systemd/system/$unit"
  systemd-analyze verify "/etc/systemd/system/$unit"
done

echo "[9/14] Recargando systemd..."
systemctl daemon-reload
if [[ "$ENABLE_NOW" -eq 1 ]]; then
  systemctl enable --now deteccion-dashboard.service
  systemctl enable --now deteccion-carros.service
fi

echo "[10/14] Estado:"
systemctl --no-pager --full status deteccion-dashboard.service || true
systemctl --no-pager --full status deteccion-carros.service || true
echo "Logs: journalctl -u deteccion-dashboard.service -u deteccion-carros.service -f"
