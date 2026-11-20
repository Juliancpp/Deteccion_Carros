import sqlite3
from datetime import datetime
from pathlib import Path

from scripts.migrate_legacy_events import migrate


def make_db(tmp_path):
    legacy = tmp_path / 'eventos.db'; hotel = tmp_path / 'hotel.db'
    with sqlite3.connect(legacy) as c:
        c.execute('''CREATE TABLE eventos(id INTEGER PRIMARY KEY,fecha_hora DATETIME NOT NULL,placa VARCHAR(16),confianza_ocr FLOAT,tipo_evento VARCHAR(20) NOT NULL,vehicle_id INTEGER NOT NULL,imagen VARCHAR(500),camara VARCHAR(100) NOT NULL)''')
        rows=[(1,'2026-09-01 10:00:00','ABC1234',.9,'INWARD',2,None,'A1'),(2,'2026-09-01 11:00:00','ABC1234',.8,'OUTWARD',2,None,'A5'),(3,'2026-09-02 12:00:00',None,0,'UNKNOWN',3,None,'A1')]
        c.executemany('INSERT INTO eventos VALUES(?,?,?,?,?,?,?,?)',rows)
    return legacy,hotel


def test_legacy_import_is_idempotent_and_provenance_safe(tmp_path):
    legacy, hotel = make_db(tmp_path)
    first = migrate(legacy, hotel, tmp_path/'backups')
    second = migrate(legacy, hotel, tmp_path/'backups')
    assert first['inserted'] == 3 and first['duplicates'] == 0
    assert second['inserted'] == 0 and second['duplicates'] == 3
    with sqlite3.connect(hotel) as c:
        assert c.execute('SELECT count(*) FROM hotel_vehicle_events').fetchone()[0] == 3
        assert c.execute("SELECT count(*) FROM hotel_vehicle_events WHERE source='legacy_eventos'").fetchone()[0] == 3
        assert c.execute('SELECT count(*) FROM hotel_vehicle_events WHERE event_type="ENTRADA_NORMAL"').fetchone()[0] == 1
    assert len(list((tmp_path/'backups').glob('*.db'))) == 4
