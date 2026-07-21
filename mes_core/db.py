"""SQLite schema, connection management, and data-access foundation for the mock MES.

Design notes
------------
* ONE shared SQLite file, path from ``MES_DB_PATH`` (default ``/data/mes.db``).
* WAL journal mode + ``busy_timeout`` make it safe for several processes in the
  same replica (init/api/mcp) to read and write concurrently.
* Every data-access function opens its own short-lived connection. This keeps
  callers simple and avoids sharing connection objects across threads/processes.
* Rows are returned as plain ``dict`` objects so both FastAPI (Pydantic/JSON)
  and the MCP server can serialize them trivially.

9-table schema: product, process_step, equipment, material, bom,
                lot, process_result, product_inventory, product_result.
Data-access functions are added incrementally in later tasks.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

DEFAULT_DB_PATH = "/data/mes.db"


def get_db_path() -> str:
    """Return the shared DB path (env ``MES_DB_PATH`` or the container default)."""
    return os.environ.get("MES_DB_PATH", DEFAULT_DB_PATH)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS product (
    product_code TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    tech_node    TEXT
);

CREATE TABLE IF NOT EXISTS process_step (
    seq       INTEGER NOT NULL,
    step_code TEXT PRIMARY KEY,
    step_name TEXT NOT NULL,
    operation TEXT,
    eqp_type  TEXT,
    stage     TEXT NOT NULL DEFAULT 'FAB'
);

CREATE TABLE IF NOT EXISTS equipment (
    eqp_id   TEXT PRIMARY KEY,
    eqp_name TEXT NOT NULL,
    type     TEXT,
    status   TEXT
);

CREATE TABLE IF NOT EXISTS material (
    material_code TEXT PRIMARY KEY,
    material_name TEXT NOT NULL,
    category      TEXT,
    qty           REAL NOT NULL DEFAULT 0,
    uom           TEXT,
    location      TEXT
);

CREATE TABLE IF NOT EXISTS bom (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code  TEXT NOT NULL,
    step_code     TEXT NOT NULL,
    material_code TEXT NOT NULL,
    qty_per_wafer REAL NOT NULL DEFAULT 0,
    uom           TEXT,
    UNIQUE (product_code, step_code, material_code)
);

CREATE TABLE IF NOT EXISTS lot (
    lot_id       TEXT PRIMARY KEY,
    product_code TEXT NOT NULL,
    tech_node    TEXT,
    start_qty    INTEGER,
    wafer_qty    INTEGER,
    priority     TEXT,
    current_step TEXT,
    status       TEXT,
    start_date   TEXT
);

CREATE TABLE IF NOT EXISTS process_result (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id      TEXT NOT NULL,
    step_code   TEXT NOT NULL,
    eqp_id      TEXT,
    in_qty      INTEGER,
    out_qty     INTEGER,
    scrap_qty   INTEGER DEFAULT 0,
    defect_code TEXT,
    in_time     TEXT,
    out_time    TEXT,
    operator    TEXT,
    result      TEXT
);

CREATE TABLE IF NOT EXISTS product_inventory (
    product_code TEXT NOT NULL,
    item_type    TEXT NOT NULL,
    qty          REAL NOT NULL DEFAULT 0,
    uom          TEXT,
    location     TEXT,
    PRIMARY KEY (product_code, item_type)
);

CREATE TABLE IF NOT EXISTS product_result (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    result_date  TEXT NOT NULL,
    lot_id       TEXT,
    product_code TEXT NOT NULL,
    item_type    TEXT NOT NULL,
    good_qty     INTEGER,
    scrap_qty    INTEGER,
    yield_pct    REAL,
    source       TEXT,
    line         TEXT,
    eqp_id       TEXT
);

CREATE INDEX IF NOT EXISTS idx_pr_lot   ON process_result (lot_id);
CREATE INDEX IF NOT EXISTS idx_pr_step  ON process_result (step_code);
CREATE INDEX IF NOT EXISTS idx_bom_ps   ON bom (product_code, step_code);
CREATE INDEX IF NOT EXISTS idx_lot_step ON lot (current_step);
CREATE INDEX IF NOT EXISTS idx_prd_res  ON product_result (product_code, item_type);
"""

TABLES = (
    "product_result",
    "process_result",
    "product_inventory",
    "bom",
    "lot",
    "material",
    "equipment",
    "process_step",
    "product",
)


# --------------------------------------------------------------------------- #
# Connection handling
# --------------------------------------------------------------------------- #

def connect() -> sqlite3.Connection:
    """Open a WAL-mode connection to the shared DB, creating its dir if needed."""
    path = get_db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # isolation_level=None -> autocommit; we manage transactions explicitly when needed.
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def get_conn():
    """Context-managed connection that always closes."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cursor.fetchall()]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db() -> None:
    """Create all tables/indexes if they do not exist."""
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)


def reset_db() -> None:
    """Drop all rows so seeding produces a clean, reproducible dataset."""
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)
        for table in TABLES:
            conn.execute(f"DELETE FROM {table};")
        # reset AUTOINCREMENT counters if the sqlite_sequence table exists
        try:
            conn.execute("DELETE FROM sqlite_sequence;")
        except sqlite3.OperationalError:
            pass


def counts() -> dict[str, int]:
    """Row counts per table (for the seed summary + dashboard)."""
    with get_conn() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


