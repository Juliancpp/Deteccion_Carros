# Fase B · Fedora de escritorio

Línea base del 22 de noviembre de 2026 en Fedora Workstation 45 x86_64. La rama local `fix/estabilizacion-integral` parte de `05d12073232edb1b39ac6b49fb8939c1fd36bd9a`. El árbol inicial de main estaba limpio. El entorno anterior `venv/` conserva módulos de Python 3.14 y su ejecutable enlaza a Python 3.15.2. El instalador CPU del proyecto exige la serie 3.12.

## Usar el entorno preparado

Desde la raíz, ejecutar `source .venv/cpu/bin/activate` y comprobar `python -VV` y `python -m pip --version`. Para las pruebas usar la copia aislada descrita abajo. El frontend sirve HTML, CSS y JavaScript directamente desde FastAPI. Node 22.23.1 permite ejecutar `node --check web/app.js`. npm 10.9.8 está disponible. Las pruebas de navegador usan la biblioteca Python de Playwright.

## Reconstruir en un directorio de proyecto limpio

Ejecutar estos comandos en Fedora x86_64 con acceso a los índices de paquetes. El intérprete reside dentro del proyecto. El inventario de versiones en `requirements-fedora-baseline.txt` corresponde a los tres manifiestos del repositorio en el commit auditado, con los pines del perfil CPU. Volver a resolver las dependencias si cambia un manifiesto.

```sh
/usr/bin/python3 -m pip install --target .venv/tools uv==0.13.9
UV_PYTHON_INSTALL_DIR="$PWD/.venv/interpreters" UV_CACHE_DIR="$PWD/.venv/cache" \
  .venv/tools/bin/uv python install 3.12.16 --no-bin
.venv/interpreters/cpython-3.12.16-linux-x86_64-gnu/bin/python3.12 -m venv .venv/cpu
source .venv/cpu/bin/activate
python -m pip install --no-deps --index-url https://download.pytorch.org/whl/cpu \
  torch==2.13.0+cpu torchvision==0.28.0+cpu
sed '/^torch==/d; /^torchvision==/d' requirements-fedora-baseline.txt > .venv/fedora-pypi.txt
python -m pip install --no-deps -r .venv/fedora-pypi.txt
PLAYWRIGHT_BROWSERS_PATH="$PWD/.venv/browsers" python -m playwright install chromium --only-shell
```

La instalación inicial resolvió con pip las dependencias de `requirements-cpu.txt`, `requirements.txt` y `requirements-dev.txt` después de instalar Torch CPU. EasyOCR se instaló aparte con `--no-deps` según la decisión documentada por el propio perfil CPU. El uso de `--no-deps` en la reconstrucción presupone el inventario completo, incluidas sus dependencias transitivas.

## Repetir la suite sin escribir en el histórico

La importación de `app.api` crea un gestor de base por defecto. Para probar un commit, crear una copia de sus fuentes en un directorio privado. Dos pruebas de estabilidad ejecutan literalmente `venv/bin/python` y una usa `git check-ignore`. En la copia se proporciona ese nombre como enlace al intérprete correcto y se inicializa un repositorio local vacío con las reglas de exclusión originales.

```sh
repo="$PWD"
export PLAYWRIGHT_BROWSERS_PATH="$repo/.venv/browsers"
qa=$(mktemp -d "$repo/.venv/qa.XXXXXXXX")
git archive HEAD -- app tests scripts web config deploy .gitignore .env.example \
  requirements.txt requirements-cpu.txt requirements-dev.txt | tar -x -C "$qa"
git init -q "$qa"
mkdir -p "$qa/database" "$qa/models"
ln -s "$repo/.venv/cpu" "$qa/venv"
(cd "$qa" && ./venv/bin/python -m pytest --collect-only -q)
(cd "$qa" && HOTEL_TEST_GUI=1 ./venv/bin/python -m pytest -ra)
```

Este procedimiento toma el código confirmado en HEAD. Para probar ediciones pendientes, copiar también los archivos editados a la copia de ensayo. Los medios y credenciales reales quedan fuera. Las pruebas HTTP abren un puerto efímero local. La prueba opcional de gestos del editor requiere un escritorio Tk y `HOTEL_TEST_GUI=1`.

## SQLite y recuperación local

La fuente histórica es `database/eventos.db` (5.768 eventos). SQLAlchemy la abre desde `app/database.py`. Los archivos `-wal` y `-shm` son auxiliares de SQLite. La otra ruta configurada, `database/hotel_mvp.db`, la comparten HotelStore, administración, API, exportador y gestor de clips. No existía al inspeccionar este checkout. La suite crea sus bases hoteleras temporales.

Se conservan una copia privada y una restauración de ensayo en `.venv/baseline/db/`. Para esta instantánea estática se verificó que el WAL tenía cero bytes antes de abrir el original con `mode=ro&immutable=1`. Usar `immutable=1` únicamente sobre una fuente estática sin transacciones WAL pendientes. Para respaldar una base en uso, emplear la API de backup de SQLite desde una conexión de lectura que incluya el WAL. Copiar solo el archivo principal de una base activa puede perder transacciones.

Para repetir la restauración hacia un nombre nuevo, desde la raíz:

```python
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import mkdtemp

source = Path(".venv/baseline/db/eventos.backup.db").resolve()
target = Path(mkdtemp(dir=".venv/baseline/db")) / "eventos.db"
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
    with closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        assert dst.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert list(src.iterdump()) == list(dst.iterdump())
```

La restauración comprobada compara el contenido lógico completo y el esquema sin imprimir datos personales. El directorio de copias tiene permisos privados y está excluido de Git.

## Resultado verificado en Fedora

| Comprobación | Resultado |
|---|---|
| Python / pip / pytest | 3.12.16 / 26.3 / 9.2.0 |
| PyTorch / torchvision | 2.13.0+cpu / 0.28.0+cpu. Operación tensorial en CPU correcta, CUDA ausente |
| OpenCV / NumPy / EasyOCR | 5.0.0.93 / 2.5.1 / 1.7.2. Importaciones correctas y codificación/decodificación JPEG en memoria comprobada |
| FastAPI / SQLite | 0.165.0 / 3.53.1 |
| Frontend | Sintaxis de app.js válida. Playwright 1.65.2 con Chromium Headless Shell 1255 descargado localmente. Playwright seleccionó su build de compatibilidad para Linux |
| Colecta | 219 pruebas. Sin archivo de configuración pytest específico. tests/conftest.py incorpora la raíz al path |
| Ejecución | 219 aprobadas en 12,48 s, 0 fallidas, 0 errores, 0 omitidas. Incluye 8 pruebas de regresión del dashboard, de las cuales 4 usan Chromium, y los dos casos gráficos Tk de A1 y A5 |
| Evidencia de ejecución | Registros privados en `.venv/baseline/collect.log`, `.venv/baseline/suite.log` y `.venv/baseline/suite.xml` |
| Histórico | SHA-256 de eventos.db antes y después: `dc325733427809fb32f50f384b7e910fe1b0ac4d925c9da9e152ac617443a832`. También coincidieron los hashes iniciales y finales de los dos archivos auxiliares |

**Excepción de empaquetado preexistente.** `python -m pip check` devuelve código 1 con un único requisito declarado ausente: `easyocr 1.7.2 requires opencv-python-headless`. El manifiesto CPU requiere `opencv-python` y pide conservar un solo proveedor de `cv2`. Instalar EasyOCR en un paso aparte y después resolver de nuevo todas sus dependencias, como hace el script general, puede incorporar la segunda distribución. El inventario de Fedora conserva una sola y deja visible la discrepancia de metadatos. No hubo errores de importación o de pruebas atribuibles a ella.

La validación cubre el entorno y la batería existente. La instalación de modelos de EasyOCR, inferencia con imágenes autorizadas, exactitud y latencias de A1/A5 pertenecen a C1. El siguiente punto de partida es fijar los pesos y su procedencia y ejecutar el benchmark con etiquetas por vehículo.
