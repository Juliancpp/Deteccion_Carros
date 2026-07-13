#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

OUT_DIR="$PROJECT_ROOT/dist"
OUT="$OUT_DIR/deteccion-carros-private.tar.gz"
SHA="$OUT.sha256"

if [[ ! -f .env ]]; then
  echo "Falta .env; no se puede construir el paquete privado." >&2
  exit 1
fi
if [[ ! -f models/yolo11n.pt ]]; then
  echo "Falta models/yolo11n.pt; no se puede construir el paquete privado." >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
tar -czf "$OUT" .env models/yolo11n.pt
sha256sum "$OUT" > "$SHA"
chmod 600 "$OUT" "$SHA"

echo "Paquete privado generado: $OUT"
echo "SHA-256: $SHA"
echo "ADVERTENCIA: contiene secretos/modelo privado; enviar solo mediante SCP/SSH."
