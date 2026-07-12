#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import config
from app.export.excel_exporter import HotelExcelExporter
from app.hotel.store import HotelStore


def main() -> int:
    store = HotelStore(config.HOTEL_DATABASE_PATH)
    try:
        path = HotelExcelExporter(store, config.HOTEL_REPORT_PATH).export()
        print(f"[EXCEL] Exportado: {path}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
