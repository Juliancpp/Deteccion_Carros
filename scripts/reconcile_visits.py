"""Simular la transición sobre una instantánea SQLite o conservar una copia validada."""

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.hotel import visits
from app.hotel.geometry import GeometryEvent
from app.hotel.store import HotelStore


def reconcile(c):
    """Transformación transaccional sobre una copia preparada por HotelStore."""
    c.execute("BEGIN IMMEDIATE")
    missing = c.execute("""SELECT event_id FROM hotel_vehicle_events WHERE NOT EXISTS
                         (SELECT 1 FROM visit_cases WHERE id=event_id) ORDER BY occurred_at,event_id""").fetchall()
    for row in missing:
        visits.ingest(c, row[0], historical=True)
    # Previous geometry rows carry session and sequence but older entry rows may lack them.
    for r in c.execute("SELECT * FROM geometry_events WHERE event_type='ROOM_ENTRY' ORDER BY occurred_at"):
        visits.geometry(
            c,
            GeometryEvent(
                r["session_id"],
                r["camera_id"],
                r["track_id"],
                r["source_sequence"],
                datetime.fromisoformat(r["occurred_at"]),
                r["feature_id"],
                r["event_type"],
            ),
        )
    sources = [("hotel_visits", "visit_id")]
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='manual_visits'").fetchone():
        sources.insert(0, ("manual_visits", "id"))
    proposals = []
    by_entry, by_exit = defaultdict(set), defaultdict(set)
    for table, id_field in sources:
        for r in c.execute("SELECT * FROM " + table):
            a, b = r["entry_event_id"], r["exit_event_id"]
            if a and b:
                by_entry[a].add(b)
                by_exit[b].add(a)
            if a and b or table == "hotel_visits" and not a:
                proposals.append((table, str(r[id_field]), a, b, dict(r)))
    report = dict(
        new_cases=len(missing), reconstructed_links=0, conflicts=0, ambiguous=0, unsubstantiated_links=0
    )
    for table, legacy_id, a, b, r in proposals:
        if c.execute(
            "SELECT 1 FROM visit_imports WHERE source=? AND source_id=?", (table, legacy_id)
        ).fetchone():
            continue
        if len(by_entry[a]) > 1 or len(by_exit[b]) > 1:
            report["ambiguous"] += 1
            c.execute("UPDATE visit_cases SET review=1 WHERE id IN (?,?)", (a, b))
            continue
        if table == "hotel_visits":
            event = c.execute("SELECT * FROM hotel_vehicle_events WHERE event_id=?", (b,)).fetchone()
            active = c.execute(
                "SELECT 1 FROM visit_links WHERE entry_event_id=? AND exit_event_id=? AND revoked_at IS NULL",
                (a, b),
            ).fetchone()
            if not active and (
                not event
                or visits.candidates(c, event, visits.epoch(event["occurred_at"]), None, None)
                != [(a, True, "plate_room")]
            ):
                report["unsubstantiated_links"] += 1
                c.execute("UPDATE visit_cases SET review=1 WHERE id IN (?,?)", (a, b))
                continue
        try:
            previous = c.total_changes
            visits.link(
                c,
                a,
                b,
                "historical_manual" if table == "manual_visits" else "historical_corroborated",
                r.get("linked_by_user_id", "reconciliation"),
                historical_id=int(legacy_id) if table == "manual_visits" else None,
            )
            report["reconstructed_links"] += c.total_changes > previous
        except (ValueError, LookupError, FileExistsError, sqlite3.IntegrityError):
            report["conflicts"] += 1
            c.execute("UPDATE visit_cases SET review=1 WHERE id IN (?,?)", (a, b))
            continue
        c.execute("INSERT INTO visit_imports VALUES(?,?)", (table, legacy_id))
    report.update(visits.indicators(c))
    report["events_without_evidence"] = c.execute(
        "SELECT count(*) FROM hotel_vehicle_events WHERE coalesce(evidence_path,'')=''"
    ).fetchone()[0]
    report["unresolved_events"] = c.execute(
        "SELECT count(*) FROM visit_cases WHERE kind='unknown'"
    ).fetchone()[0]
    report["unassigned_rooms"] = c.execute(
        "SELECT count(*) FROM visit_rooms WHERE visit_id IS NULL"
    ).fetchone()[0]
    if (
        c.execute("PRAGMA foreign_key_check").fetchone()
        or c.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
    ):
        raise ValueError("Integridad SQLite inválida")
    c.commit()
    return report


def run(source, destination=None):
    # A named output is published only after reconciliation and validation succeed.
    with tempfile.TemporaryDirectory(dir=destination.parent if destination else None) as tmp:
        scratch = Path(tmp) / "migration.db"
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            if not src.execute("SELECT 1 FROM sqlite_master WHERE name='hotel_vehicle_events'").fetchone():
                raise ValueError("Se requiere una base hotelera")
            with closing(sqlite3.connect(scratch)) as dst:
                src.backup(dst)
        store = HotelStore(str(scratch))
        try:
            report = reconcile(store.conn)
        finally:
            store.close()
        if destination:
            scratch.chmod(0o600)
            os.link(scratch, destination)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-copy", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.output_copy), indent=2))
