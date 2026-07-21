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


# --------------------------------------------------------------------------- #
# Product master
# --------------------------------------------------------------------------- #

def list_products() -> list[dict[str, Any]]:
    with get_conn() as conn:
        return _rows(conn.execute("SELECT * FROM product ORDER BY product_code"))


def list_product_codes() -> list[str]:
    with get_conn() as conn:
        return [r[0] for r in conn.execute("SELECT product_code FROM product ORDER BY product_code")]


# --------------------------------------------------------------------------- #
# 자재 인벤토리 (material)
# --------------------------------------------------------------------------- #

def list_materials(category=None, location=None, q=None, min_qty=None, max_qty=None):
    sql = "SELECT * FROM material WHERE 1=1"
    args: list[Any] = []
    if category:
        sql += " AND category = ?"; args.append(category)
    if location:
        sql += " AND location = ?"; args.append(location)
    if q:
        sql += " AND (material_code LIKE ? OR material_name LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if min_qty is not None:
        sql += " AND qty >= ?"; args.append(min_qty)
    if max_qty is not None:
        sql += " AND qty <= ?"; args.append(max_qty)
    sql += " ORDER BY material_code"
    with get_conn() as conn:
        return _rows(conn.execute(sql, args))


def get_material(material_code):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM material WHERE material_code = ?", (material_code,)).fetchone()
        return dict(row) if row else None


def receive_material(material_code, qty, material_name=None, category=None, uom=None, location=None):
    qty = float(qty)
    if qty < 0:
        raise ValueError(f"qty must be >= 0, got {qty}")
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM material WHERE material_code = ?", (material_code,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO material (material_code, material_name, category, qty, uom, location)"
                " VALUES (?,?,?,?,?,?)",
                (material_code, material_name or material_code, category, qty, uom, location),
            )
        else:
            conn.execute("UPDATE material SET qty = qty + ? WHERE material_code = ?", (qty, material_code))
        return dict(conn.execute("SELECT * FROM material WHERE material_code = ?", (material_code,)).fetchone())


def materials_by_step(step_code=None):
    sql = (
        "SELECT b.product_code, p.product_name, b.step_code, ps.step_name, "
        "       b.material_code, m.material_name, b.qty_per_wafer, "
        "       m.qty AS on_hand_qty, COALESCE(b.uom, m.uom) AS uom, m.location "
        "FROM bom b "
        "JOIN material m ON m.material_code = b.material_code "
        "LEFT JOIN product p ON p.product_code = b.product_code "
        "LEFT JOIN process_step ps ON ps.step_code = b.step_code "
        "WHERE 1=1"
    )
    args: list[Any] = []
    if step_code:
        sql += " AND b.step_code = ?"; args.append(step_code)
    sql += " ORDER BY ps.seq, b.product_code, b.material_code"
    with get_conn() as conn:
        return _rows(conn.execute(sql, args))


# --------------------------------------------------------------------------- #
# BOM
# --------------------------------------------------------------------------- #

def list_bom(product_code=None, step_code=None):
    sql = (
        "SELECT b.*, m.material_name, ps.step_name, ps.seq "
        "FROM bom b "
        "LEFT JOIN material m ON m.material_code = b.material_code "
        "LEFT JOIN process_step ps ON ps.step_code = b.step_code WHERE 1=1"
    )
    args: list[Any] = []
    if product_code:
        sql += " AND b.product_code = ?"; args.append(product_code)
    if step_code:
        sql += " AND b.step_code = ?"; args.append(step_code)
    sql += " ORDER BY b.product_code, ps.seq, b.material_code"
    with get_conn() as conn:
        return _rows(conn.execute(sql, args))


def upsert_bom(product_code, step_code, material_code, qty_per_wafer, uom=None):
    qty_per_wafer = float(qty_per_wafer)
    if qty_per_wafer < 0:
        raise ValueError(f"qty_per_wafer must be >= 0, got {qty_per_wafer}")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bom (product_code, step_code, material_code, qty_per_wafer, uom)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(product_code, step_code, material_code)"
            " DO UPDATE SET qty_per_wafer = excluded.qty_per_wafer, uom = COALESCE(excluded.uom, bom.uom)",
            (product_code, step_code, material_code, qty_per_wafer, uom),
        )
        row = conn.execute(
            "SELECT * FROM bom WHERE product_code=? AND step_code=? AND material_code=?",
            (product_code, step_code, material_code),
        ).fetchone()
        return dict(row)


def delete_bom(bom_id) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM bom WHERE id = ?", (bom_id,))
        return cur.rowcount > 0


def _consume_materials(conn, product_code, step_code, units):
    """Deduct BOM(product, step) materials * units. Floor at 0, return shortages."""
    shortages: list[dict[str, Any]] = []
    boms = conn.execute(
        "SELECT b.material_code, b.qty_per_wafer, m.qty AS on_hand, m.material_name "
        "FROM bom b JOIN material m ON m.material_code = b.material_code "
        "WHERE b.product_code = ? AND b.step_code = ?",
        (product_code, step_code),
    ).fetchall()
    for b in boms:
        need = (b["qty_per_wafer"] or 0) * units
        if need <= 0:
            continue
        on_hand = b["on_hand"] or 0
        new_qty = on_hand - need
        if new_qty < 0:
            shortages.append({
                "material_code": b["material_code"],
                "material_name": b["material_name"],
                "needed": round(need, 4),
                "available": round(on_hand, 4),
                "short": round(need - on_hand, 4),
            })
            new_qty = 0
        conn.execute("UPDATE material SET qty = ? WHERE material_code = ?", (new_qty, b["material_code"]))
    return shortages


# --------------------------------------------------------------------------- #
# 제품 인벤토리 (product_inventory) + 제품실적 (product_result)
# --------------------------------------------------------------------------- #

def list_product_inventory(product_code=None, item_type=None):
    sql = (
        "SELECT pi.product_code, p.product_name, pi.item_type, pi.qty, pi.uom, pi.location "
        "FROM product_inventory pi "
        "LEFT JOIN product p ON p.product_code = pi.product_code WHERE 1=1"
    )
    args: list[Any] = []
    if product_code:
        sql += " AND pi.product_code = ?"; args.append(product_code)
    if item_type:
        sql += " AND pi.item_type = ?"; args.append(item_type)
    sql += " ORDER BY pi.product_code, pi.item_type"
    with get_conn() as conn:
        return _rows(conn.execute(sql, args))


def list_product_results(product_code=None, item_type=None, source=None,
                         lot_id=None, date_from=None, date_to=None, limit=100):
    sql = (
        "SELECT pr.*, p.product_name FROM product_result pr "
        "LEFT JOIN product p ON p.product_code = pr.product_code WHERE 1=1"
    )
    args: list[Any] = []
    if product_code:
        sql += " AND pr.product_code = ?"; args.append(product_code)
    if item_type:
        sql += " AND pr.item_type = ?"; args.append(item_type)
    if source:
        sql += " AND pr.source = ?"; args.append(source)
    if lot_id:
        sql += " AND pr.lot_id = ?"; args.append(lot_id)
    if date_from:
        sql += " AND pr.result_date >= ?"; args.append(date_from)
    if date_to:
        sql += " AND pr.result_date <= ?"; args.append(date_to)
    sql += " ORDER BY pr.result_date DESC, pr.id DESC LIMIT ?"; args.append(limit)
    with get_conn() as conn:
        return _rows(conn.execute(sql, args))


def _add_product_inventory(conn, product_code, item_type, delta, uom="EA", location=None):
    conn.execute(
        "INSERT INTO product_inventory (product_code, item_type, qty, uom, location)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(product_code, item_type) DO UPDATE SET qty = qty + excluded.qty",
        (product_code, item_type, delta, uom, location),
    )


def _insert_product_result(conn, *, result_date, lot_id, product_code, item_type,
                           good_qty, scrap_qty, yield_pct, source, line=None, eqp_id=None):
    cur = conn.execute(
        "INSERT INTO product_result"
        " (result_date, lot_id, product_code, item_type, good_qty, scrap_qty,"
        "  yield_pct, source, line, eqp_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (result_date, lot_id, product_code, item_type, good_qty, scrap_qty,
         yield_pct, source, line, eqp_id),
    )
    return cur.lastrowid


def register_product_result(lot_id, item_type, good_qty, scrap_qty=0):
    item_type = str(item_type).upper()
    if item_type not in ("SEMI", "FIN"):
        raise ValueError(f"item_type must be SEMI or FIN, got {item_type!r}")
    good_qty = int(good_qty)
    scrap_qty = int(scrap_qty or 0)
    if good_qty < 0:
        raise ValueError(f"good_qty must be >= 0, got {good_qty}")
    now = _now_iso()
    with get_conn() as conn:
        lot = conn.execute("SELECT * FROM lot WHERE lot_id = ?", (lot_id,)).fetchone()
        if lot is None:
            raise ValueError(f"unknown lot_id: {lot_id!r}")
        product_code = lot["product_code"]
        total = good_qty + scrap_qty
        y = round(good_qty / total * 100, 2) if total else 0.0
        pr_id = _insert_product_result(
            conn, result_date=now[:10], lot_id=lot_id, product_code=product_code,
            item_type=item_type, good_qty=good_qty, scrap_qty=scrap_qty,
            yield_pct=y, source="MANUAL",
        )
        _add_product_inventory(
            conn, product_code, item_type, good_qty,
            uom="EA", location=("WH-FG" if item_type == "FIN" else "WH-SEMI"),
        )
        return dict(conn.execute("SELECT * FROM product_result WHERE id = ?", (pr_id,)).fetchone())

