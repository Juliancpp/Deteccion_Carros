#!/usr/bin/env python3
import os
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cleanup_old_media")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import app.config as config

RETENTION_DAYS = getattr(config, "RETENTION_DAYS", 15)


def cleanup_directory(root_dir: Path, days: int) -> tuple[int, int]:
    """
    Recursively scans root_dir, deleting files older than target days.
    Returns tuple: (deleted_files_count, total_bytes_freed).
    """
    if not root_dir.is_dir():
        logger.warning("El directorio %s no existe. Omitiendo...", root_dir)
        return 0, 0

    now = datetime.now()
    threshold = now - timedelta(days=days)
    deleted_count = 0
    bytes_freed = 0

    logger.info("Escaneando %s para depurar archivos anteriores a %s (%s días)...", root_dir, threshold.strftime("%Y-%m-%d"), days)

    for item in root_dir.rglob("*"):
        if not item.is_file():
            continue

        # Try to parse folder date YYYY-MM-DD
        file_date = None
        for parent in item.parents:
            try:
                # If parent directory name is YYYY-MM-DD
                file_date = datetime.strptime(parent.name, "%Y-%m-%d")
                break
            except ValueError:
                continue

        # Fallback to system file modification time if directory name is not YYYY-MM-DD
        if file_date is None:
            mtime = os.path.getmtime(item)
            file_date = datetime.fromtimestamp(mtime)

        if file_date < threshold:
            # Skip hidden files
            if item.name.startswith("."):
                continue

            try:
                size = item.stat().st_size
                os.remove(item)
                deleted_count += 1
                bytes_freed += size
                logger.debug("Eliminado: %s (Fecha: %s, Tamaño: %s bytes)", item.relative_to(root_dir), file_date.strftime("%Y-%m-%d"), size)
            except Exception as e:
                logger.error("Error al eliminar %s: %s", item, e)

    # Clean up empty parent directories
    for item in sorted(root_dir.glob("**/"), key=lambda x: len(x.parts), reverse=True):
        if item != root_dir and item.is_dir():
            try:
                # Check if empty (excluding hidden files)
                if not any(item.iterdir()):
                    item.rmdir()
                    logger.debug("Removido directorio vacío: %s", item.relative_to(root_dir))
            except Exception:
                pass

    return deleted_count, bytes_freed


def main():
    logger.info("Iniciando purga automática de evidencias y clips viejos...")
    
    # 1. Clean up captures
    captures_dir = Path(config.CAPTURE_DIR)
    del_caps, bytes_caps = cleanup_directory(captures_dir, RETENTION_DAYS)
    
    # 2. Clean up clips
    clips_dir = PROJECT_ROOT / "data" / "clips"
    del_clips, bytes_clips = cleanup_directory(clips_dir, RETENTION_DAYS)
    
    total_files = del_caps + del_clips
    total_mb = (bytes_caps + bytes_clips) / (1024.0 * 1024.0)
    
    logger.info("Purga completada exitosamente.")
    logger.info("  Archivos eliminados: %s", total_files)
    logger.info("  Espacio en disco liberado: %.2f MB", total_mb)


if __name__ == "__main__":
    main()
