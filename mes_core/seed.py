"""Load the shipped dataset into the shared SQLite DB.

    python -m mes_core.seed

This runs once from an init container, before the API and MCP servers start.
``/data`` is an EmptyDir volume, so it is empty every time a container is
created -- which makes a boot the only moment the dataset is written, and the
only moment it is reset. Stop the app and start it again to get a clean slate;
while it is up, whatever clients do to the data stands.

Two things come out of ``dataset.json``:

* **The rows themselves**, verbatim. Products, lots, equipment, yields, defect
  codes and every identifier are literals in that file, so they cannot drift
  when someone edits the code that first produced them, and a change to them
  shows up in a diff.
* **The shape of the timeline** -- the gaps between steps, not the dates. On
  load the whole history is translated so it *starts three months ago*, which
  keeps a demo that runs all year looking current. Intervals survive exactly:
  if a step ran 55 minutes and the next lot entered that tool 17 minutes later,
  that is still true afterwards.

The shift is a whole number of days, so times of day are preserved too and the
dataset only moves when the calendar date does. Set ``MES_HISTORY_START`` to an
ISO date to pin it outright.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db

DATASET_PATH = Path(__file__).resolve().parent / "dataset.json"

#: How far back the first event sits. Three months keeps the history recent
#: while leaving room for its ~3-day makespan.
HISTORY_STARTS_AGO = timedelta(days=90)

#: Pins the first event to an exact date, for reproducible runs.
HISTORY_START_ENV = "MES_HISTORY_START"

#: Every column carrying a point in time. These have to move together, or a
#: lot would start after the step it started with.
TIME_COLUMNS: dict[str, tuple[str, ...]] = {
    "lot": ("start_date",),
    "process_result": ("in_time", "out_time"),
    "product_result": ("result_date",),
}


def load_dataset(path: Path | None = None) -> dict[str, list[list]]:
    """Read the shipped dataset off disk."""
    return json.loads((path or DATASET_PATH).read_text(encoding="utf-8"))


def resolve_history_start(today: date | None = None) -> date:
    """The date the dataset's first process step should land on."""
    raw = os.environ.get(HISTORY_START_ENV, "").strip()
    if raw:
        return datetime.fromisoformat(raw).date()
    return (today or datetime.now(timezone.utc).date()) - HISTORY_STARTS_AGO


def _origin(rows: list[list], in_time: int) -> date:
    """The earliest date in the dataset; the shift is measured from here."""
    return min(datetime.fromisoformat(r[in_time]).date() for r in rows)


def _shift(value: str | None, days: timedelta) -> str | None:
    if value is None:
        return None
    moved = datetime.fromisoformat(value) + days
    # Keep each column's shape: a date column stays a date.
    return moved.date().isoformat() if len(value) <= 10 else moved.isoformat()


def retime(tables: dict[str, list[list]], columns: dict[str, list[str]],
           start: date) -> dict[str, list[list]]:
    """Translate the history so its first event falls on ``start``.

    A pure translation by whole days, so every interval, ordering and
    equipment overlap in the dataset is preserved and only the calendar moves.
    """
    results = tables["process_result"]
    days = timedelta(
        days=(start - _origin(results, columns["process_result"].index("in_time"))).days)
    if not days:
        return tables

    out = {}
    for table, rows in tables.items():
        names = TIME_COLUMNS.get(table)
        if not names:
            out[table] = rows
            continue
        at = [columns[table].index(n) for n in names]
        moved = []
        for row in rows:
            row = list(row)
            for i in at:
                row[i] = _shift(row[i], days)
            moved.append(row)
        out[table] = moved
    return out


def seed(path: Path | None = None) -> None:
    """Reset every table and reload it from the dataset file.

    The only inputs are that file and today's date, so two boots on the same
    day produce byte-identical databases, timestamps included.
    """
    tables = load_dataset(path)

    db.reset_db()
    with db.get_conn() as conn:
        columns = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
                   for t in tables}
        tables = retime(tables, columns, resolve_history_start())

        conn.execute("BEGIN")
        try:
            for table, rows in tables.items():
                if not rows:
                    continue
                names = ", ".join(f'"{c}"' for c in columns[table])
                marks = ", ".join("?" * len(columns[table]))
                conn.executemany(
                    f"INSERT INTO {table} ({names}) VALUES ({marks})", rows)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def main() -> None:
    path = db.get_db_path()
    seed()
    c = db.counts()
    print(f"[seed] mock MES database ready at {path}")
    print("[seed] rows: " + ", ".join(f"{k}={v}" for k, v in c.items()))
    print(f"[seed] history starts {resolve_history_start().isoformat()}")


if __name__ == "__main__":
    main()
