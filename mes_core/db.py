"""SQLite schema, connection management, and data-access API for the mock MES.

Design notes
------------
* ONE shared SQLite file, path from ``MES_DB_PATH`` (default ``/data/mes.db``).
* WAL journal mode + ``busy_timeout`` make it safe for several processes in the
  same replica (init/api/mcp) to read and write concurrently.
* Every data-access function opens its own short-lived connection. This keeps
  callers simple and avoids sharing connection objects across threads/processes.
* Rows are returned as plain ``dict`` objects so both FastAPI (Pydantic/JSON)
  and the MCP server can serialize them trivially.

The 7 MES functions map onto these helpers:
    실적 입력  -> add_production_result
    실적 조회  -> list_production_results
    재고 조회  -> list_inventory
    재공 조회  -> get_wip                (derived from lot.current_step)
    공정 입력  -> add_process_move       (writes process_history, advances lot)
    공정 조회  -> get_process_route / get_process_history
    로트 조회  -> get_lot / list_lots
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
CREATE TABLE IF NOT EXISTS process_step (
    seq        INTEGER NOT NULL,
    step_code  TEXT PRIMARY KEY,
    step_name  TEXT NOT NULL,
    operation  TEXT,
    eqp_type   TEXT
);

CREATE TABLE IF NOT EXISTS equipment (
    eqp_id    TEXT PRIMARY KEY,
    eqp_name  TEXT NOT NULL,
    type      TEXT,
    status    TEXT
);

CREATE TABLE IF NOT EXISTS lot (
    lot_id        TEXT PRIMARY KEY,
    product       TEXT NOT NULL,
    tech_node     TEXT,
    wafer_qty     INTEGER,
    priority      TEXT,
    current_step  TEXT,
    status        TEXT,
    start_date    TEXT
);

CREATE TABLE IF NOT EXISTS process_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id     TEXT NOT NULL,
    step_code  TEXT NOT NULL,
    eqp_id     TEXT,
    in_time    TEXT,
    out_time   TEXT,
    operator   TEXT,
    result     TEXT
);

CREATE TABLE IF NOT EXISTS inventory (
    item_code  TEXT PRIMARY KEY,
    item_name  TEXT NOT NULL,
    category   TEXT,
    qty        REAL,
    uom        TEXT,
    location   TEXT
);

CREATE TABLE IF NOT EXISTS production_result (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    result_date  TEXT NOT NULL,
    line         TEXT,
    eqp_id       TEXT,
    product      TEXT,
    good_qty     INTEGER,
    scrap_qty    INTEGER,
    yield_pct    REAL
);

CREATE INDEX IF NOT EXISTS idx_hist_lot   ON process_history(lot_id);
CREATE INDEX IF NOT EXISTS idx_hist_step  ON process_history(step_code);
CREATE INDEX IF NOT EXISTS idx_prod_date  ON production_result(result_date);
CREATE INDEX IF NOT EXISTS idx_prod_prod  ON production_result(product);
CREATE INDEX IF NOT EXISTS idx_lot_step   ON lot(current_step);
"""

TABLES = (
    "process_history",
    "production_result",
    "inventory",
    "lot",
    "equipment",
    "process_step",
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


# --------------------------------------------------------------------------- #
# 실적 (production_result)
# --------------------------------------------------------------------------- #

def add_production_result(
    product: str,
    good_qty: int,
    scrap_qty: int = 0,
    result_date: Optional[str] = None,
    line: Optional[str] = None,
    eqp_id: Optional[str] = None,
    yield_pct: Optional[float] = None,
) -> dict[str, Any]:
    """실적 입력: insert one production result and return the stored row.

    ``yield_pct`` is auto-computed as good/(good+scrap)*100 when omitted.
    """
    good_qty = int(good_qty)
    scrap_qty = int(scrap_qty or 0)
    if result_date is None:
        result_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if yield_pct is None:
        total = good_qty + scrap_qty
        yield_pct = round(good_qty / total * 100, 2) if total > 0 else 0.0
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO production_result
                   (result_date, line, eqp_id, product, good_qty, scrap_qty, yield_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (result_date, line, eqp_id, product, good_qty, scrap_qty, yield_pct),
        )
        new_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM production_result WHERE id = ?", (new_id,)
        ).fetchone()
    return dict(row)


def list_production_results(
    product: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """실적 조회: list production results, newest first, with optional filters."""
    clauses: list[str] = []
    params: list[Any] = []
    if product:
        clauses.append("product = ?")
        params.append(product)
    if date_from:
        clauses.append("result_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("result_date <= ?")
        params.append(date_to)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with get_conn() as conn:
        cur = conn.execute(
            f"""SELECT * FROM production_result
                {where}
                ORDER BY result_date DESC, id DESC
                LIMIT ?""",
            params,
        )
        return _rows(cur)


# --------------------------------------------------------------------------- #
# 재고 (inventory)
# --------------------------------------------------------------------------- #

def list_inventory(
    category: Optional[str] = None,
    location: Optional[str] = None,
) -> list[dict[str, Any]]:
    """재고 조회: list inventory items with optional category/location filters."""
    clauses: list[str] = []
    params: list[Any] = []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if location:
        clauses.append("location = ?")
        params.append(location)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        cur = conn.execute(
            f"SELECT * FROM inventory {where} ORDER BY category, item_code", params
        )
        return _rows(cur)


# --------------------------------------------------------------------------- #
# 재공 / WIP (derived from lot.current_step)
# --------------------------------------------------------------------------- #

def get_wip() -> list[dict[str, Any]]:
    """재공 조회: WIP grouped by current process step (running lots only)."""
    with get_conn() as conn:
        cur = conn.execute(
            """SELECT ps.seq              AS seq,
                      ps.step_code        AS step_code,
                      ps.step_name        AS step_name,
                      COUNT(l.lot_id)     AS lot_count,
                      COALESCE(SUM(l.wafer_qty), 0) AS wafer_qty
               FROM process_step ps
               LEFT JOIN lot l
                      ON l.current_step = ps.step_code
                     AND l.status = 'Running'
               GROUP BY ps.seq, ps.step_code, ps.step_name
               ORDER BY ps.seq"""
        )
        return _rows(cur)


# --------------------------------------------------------------------------- #
# 공정 (process_step route + process_history)
# --------------------------------------------------------------------------- #

def get_process_route() -> list[dict[str, Any]]:
    """공정 조회 (route): the ordered process route / step master."""
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM process_step ORDER BY seq")
        return _rows(cur)


def get_process_history(
    lot_id: Optional[str] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """공정 조회 (history): process move history, optionally for one lot."""
    clauses: list[str] = []
    params: list[Any] = []
    if lot_id:
        clauses.append("h.lot_id = ?")
        params.append(lot_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with get_conn() as conn:
        cur = conn.execute(
            f"""SELECT h.id, h.lot_id, h.step_code, ps.step_name,
                       h.eqp_id, h.in_time, h.out_time, h.operator, h.result
                FROM process_history h
                LEFT JOIN process_step ps ON ps.step_code = h.step_code
                {where}
                ORDER BY h.id DESC
                LIMIT ?""",
            params,
        )
        return _rows(cur)


def add_process_move(
    lot_id: str,
    step_code: str,
    eqp_id: Optional[str] = None,
    operator: Optional[str] = None,
    result: str = "Pass",
    in_time: Optional[str] = None,
    out_time: Optional[str] = None,
) -> dict[str, Any]:
    """공정 입력: record a process move for a lot and advance the lot.

    Writes a ``process_history`` row and updates the lot's ``current_step``
    (and marks it Done when the final route step passes). Raises ``ValueError``
    if the lot or step does not exist.
    """
    now = _now_iso()
    if in_time is None:
        in_time = now
    if out_time is None:
        out_time = now
    with get_conn() as conn:
        lot = conn.execute(
            "SELECT * FROM lot WHERE lot_id = ?", (lot_id,)
        ).fetchone()
        if lot is None:
            raise ValueError(f"unknown lot_id: {lot_id!r}")
        step = conn.execute(
            "SELECT * FROM process_step WHERE step_code = ?", (step_code,)
        ).fetchone()
        if step is None:
            raise ValueError(f"unknown step_code: {step_code!r}")

        cur = conn.execute(
            """INSERT INTO process_history
                   (lot_id, step_code, eqp_id, in_time, out_time, operator, result)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (lot_id, step_code, eqp_id, in_time, out_time, operator, result),
        )
        new_id = cur.lastrowid

        # Advance the lot to this step. If it is the last step and passed, close it.
        last_seq = conn.execute("SELECT MAX(seq) FROM process_step").fetchone()[0]
        new_status = lot["status"]
        if step["seq"] == last_seq and result.lower() in ("pass", "ok", "good"):
            new_status = "Done"
        elif lot["status"] == "Done":
            new_status = "Running"
        conn.execute(
            "UPDATE lot SET current_step = ?, status = ? WHERE lot_id = ?",
            (step_code, new_status, lot_id),
        )
        row = conn.execute(
            """SELECT h.id, h.lot_id, h.step_code, ps.step_name,
                      h.eqp_id, h.in_time, h.out_time, h.operator, h.result
               FROM process_history h
               LEFT JOIN process_step ps ON ps.step_code = h.step_code
               WHERE h.id = ?""",
            (new_id,),
        ).fetchone()
    return dict(row)


# --------------------------------------------------------------------------- #
# 로트 (lot)
# --------------------------------------------------------------------------- #

def get_lot(lot_id: str) -> Optional[dict[str, Any]]:
    """로트 조회: one lot with its current step name and full move history."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT l.*, ps.step_name AS current_step_name
               FROM lot l
               LEFT JOIN process_step ps ON ps.step_code = l.current_step
               WHERE l.lot_id = ?""",
            (lot_id,),
        ).fetchone()
        if row is None:
            return None
        lot = dict(row)
        hist = conn.execute(
            """SELECT h.id, h.lot_id, h.step_code, ps.step_name,
                      h.eqp_id, h.in_time, h.out_time, h.operator, h.result
               FROM process_history h
               LEFT JOIN process_step ps ON ps.step_code = h.step_code
               WHERE h.lot_id = ?
               ORDER BY h.id""",
            (lot_id,),
        ).fetchall()
        lot["history"] = [dict(h) for h in hist]
    return lot


def list_lots(
    status: Optional[str] = None,
    product: Optional[str] = None,
    current_step: Optional[str] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """로트 조회 (list): lots with optional status/product/step filters."""
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("l.status = ?")
        params.append(status)
    if product:
        clauses.append("l.product = ?")
        params.append(product)
    if current_step:
        clauses.append("l.current_step = ?")
        params.append(current_step)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with get_conn() as conn:
        cur = conn.execute(
            f"""SELECT l.*, ps.step_name AS current_step_name
                FROM lot l
                LEFT JOIN process_step ps ON ps.step_code = l.current_step
                {where}
                ORDER BY l.lot_id
                LIMIT ?""",
            params,
        )
        return _rows(cur)


# --------------------------------------------------------------------------- #
# Equipment + small helpers used by the web console forms
# --------------------------------------------------------------------------- #

def list_equipment() -> list[dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM equipment ORDER BY eqp_id")
        return _rows(cur)


def list_products() -> list[str]:
    """Distinct product names currently present on lots (for form dropdowns)."""
    with get_conn() as conn:
        cur = conn.execute("SELECT DISTINCT product FROM lot ORDER BY product")
        return [r["product"] for r in cur.fetchall()]


def list_lot_ids() -> list[str]:
    with get_conn() as conn:
        cur = conn.execute("SELECT lot_id FROM lot ORDER BY lot_id")
        return [r["lot_id"] for r in cur.fetchall()]


def counts() -> dict[str, int]:
    """Small dashboard summary: row counts per table."""
    out: dict[str, int] = {}
    with get_conn() as conn:
        for table in ("lot", "process_step", "process_history",
                      "inventory", "production_result", "equipment"):
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return out
