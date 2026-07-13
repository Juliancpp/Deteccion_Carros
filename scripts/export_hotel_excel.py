#!/usr/bin/env python
import argparse
import sys
import os

# Asegurar que el directorio raíz del proyecto esté en el path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.export.excel_exporter import HotelExcelExporter
from app.hotel.store import HotelStore
import app.config as config


def main():
    parser = argparse.ArgumentParser(description="Exportar base de datos Hotel MVP a Excel")
    parser.add_argument(
        "--db-path",
        default=getattr(config, "HOTEL_DATABASE_PATH", "database/hotel_mvp.db"),
        help="Ruta al archivo SQLite de hotel",
    )
    parser.add_argument(
        "--output",
        default="data/reportes/control_vehiculos.xlsx",
        help="Ruta de salida para el reporte Excel",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        print(f"Error: La base de datos no existe en: {args.db_path}")
        sys.exit(1)

    print(f"Exportando {args.db_path} a {args.output}...")
    store = HotelStore(args.db_path)
    try:
        exporter = HotelExcelExporter(store, args.output)
        exporter.export()
        print("Exportación completada exitosamente.")
    except Exception as e:
        print(f"Fallo en exportación: {e}")
        sys.exit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
