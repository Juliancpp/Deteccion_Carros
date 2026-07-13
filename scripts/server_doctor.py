#!/usr/bin/env python3
import os
import sys
import shutil
import sqlite3
import logging
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("server_doctor")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def check_python_version() -> bool:
    v = sys.version_info
    logger.info("Verificando versión de Python...")
    logger.info("  Versión actual: %s.%s.%s", v.major, v.minor, v.micro)
    if v.major < 3 or (v.major == 3 and v.minor < 10):
        logger.error("  Error: Python 3.10 o superior es obligatorio.")
        return False
    logger.info("  [OK] Versión soportada.")
    return True


def check_ffmpeg() -> bool:
    logger.info("Verificando disponibilidad de FFmpeg...")
    path = shutil.which("ffmpeg")
    if not path:
        logger.error("  Error: FFmpeg no se encuentra instalado o no está en el PATH.")
        return False
    logger.info("  [OK] FFmpeg encontrado en: %s", path)
    return True


def check_sqlite() -> bool:
    logger.info("Verificando motor SQLite...")
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()
        logger.info("  [OK] SQLite funciona correctamente.")
        return True
    except Exception as e:
        logger.error("  Error al iniciar SQLite: %s", e)
        return False


def check_pytorch_cuda() -> bool:
    logger.info("Verificando PyTorch y aceleración CUDA...")
    try:
        import torch
        logger.info("  Versión de PyTorch: %s", torch.__version__)
        if torch.cuda.is_available():
            logger.info("  [OK] CUDA está disponible. Dispositivo: %s", torch.cuda.get_device_name(0))
        else:
            logger.warning("  [WARN] CUDA NO está disponible. YOLO se ejecutará en CPU.")
    except ImportError:
        logger.warning("  [WARN] PyTorch no instalado. YOLO fallará si no está en el entorno virtual.")
    return True


def check_directories() -> bool:
    logger.info("Verificando directorios de datos y permisos de escritura...")
    paths_to_check = [
        "data",
        "data/captures",
        "data/clips",
        "data/runtime",
        "data/reportes"
    ]
    ok = True
    for p in paths_to_check:
        abs_p = PROJECT_ROOT / p
        try:
            os.makedirs(abs_p, exist_ok=True)
            # Test write permission
            test_file = abs_p / ".doctor_write_test"
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
            logger.info("  [OK] %s/ es escribible.", p)
        except Exception as e:
            logger.error("  Error de permisos en %s/: %s", p, e)
            ok = False
    return ok


def main():
    print("==================================================")
    print("          DIAGNÓSTICO DEL SERVIDOR HOTEL MVP     ")
    print("==================================================")
    
    steps = [
        check_python_version(),
        check_ffmpeg(),
        check_sqlite(),
        check_pytorch_cuda(),
        check_directories()
    ]
    
    print("--------------------------------------------------")
    if all(steps):
        print("RESULTADO: [ÉXITO] Todo está listo para el despliegue.")
        sys.exit(0)
    else:
        print("RESULTADO: [FALLO] Algunos requerimientos no se cumplen. Verifique los errores.")
        sys.exit(1)


if __name__ == "__main__":
    main()
