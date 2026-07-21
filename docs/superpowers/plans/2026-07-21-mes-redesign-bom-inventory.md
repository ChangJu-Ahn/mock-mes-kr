# Mock MES Redesign (BOM · Two-Stage Production · Split Inventory) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the mock MES data model as a two-stage semiconductor fab (FAB → Packaging) with a simple BOM that drives per-wafer material consumption and two separate inventories (product/semi vs. material), then re-split REST and MCP so each exposes both queries and transactions, plus a human `/guide` page.

**Architecture:** One shared ephemeral SQLite DB (`mes_core/db.py`, WAL mode) re-seeded on every cold start. FastAPI serves the web console (Jinja2/HTMX at `/`) and the REST API (`/api/*`); a separate MCP streamable-HTTP server serves `/mcp`; a Caddy proxy fronts both. FAB lots move step-by-step (each move consumes BOM materials); completing the final FAB step (TEST) auto-receives SEMI stock; a single packaging transaction consumes SEMI + PKG materials and produces FIN stock.

**Tech Stack:** Python 3.12, FastAPI + Uvicorn, Jinja2 + a little HTMX, MCP Python SDK (`mcp`, streamable HTTP), built-in `sqlite3` (WAL + busy_timeout), `unittest`.

**Reference spec:** `docs/superpowers/specs/2026-07-21-mes-redesign-bom-inventory-design.md`

## Global Constraints

- Python 3.12; no new runtime dependencies (FastAPI, uvicorn, jinja2, mcp already in `requirements.txt`).
- ONE shared `mes_core` package imported by both `api` and `mcp_server`.
- ONE shared ephemeral SQLite DB at env `MES_DB_PATH` (default `/data/mes.db`); WAL + `busy_timeout`; re-seeded on every cold start; deterministic dataset (fixed RNG seed `random.Random(42)`).
- Auth unchanged: all REST `/api/*` and MCP `/mcp` require header `X-API-Key: changjuahn` (env `MES_API_KEY`, default `changjuahn`); web console and `/api/docs` / `/api/openapi.json` stay open. Do NOT modify `api/auth.py` or the MCP `_ApiKeyGuard`.
- Inventory item types are exactly `SEMI` (반제품) and `FIN` (제품). Product-result sources are exactly `AUTO_FAB`, `AUTO_PACK`, `MANUAL`.
- Material shortfall never blocks a process/packaging move (floor material qty at 0, return `shortages[]`); SEMI shortfall DOES block packaging (raise `ValueError`).
- Tests run with `python -m unittest`. Every data-access function opens its own short-lived connection via `db.get_conn()` and returns plain `dict` rows.
- No planning markdown committed except the spec + this plan under `docs/superpowers/`. README is the only narrative product doc.
- Every commit ends with these trailers:
  ```
  Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
  Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87
  ```
- Local dev: `. .venv/bin/activate`; seed a dev DB with `MES_DB_PATH=$(pwd)/data/mes.db python -m mes_core.seed`. To stop a uvicorn server: `lsof -nP -iTCP:8000 -sTCP:LISTEN -t` then `kill <PID>` (numeric PID only).

## File Structure

**Data layer (`mes_core/`)**
- `db.py` — REWRITE. New `SCHEMA_SQL` (9 tables), `TABLES` tuple, unchanged connection helpers (`get_db_path`, `connect`, `get_conn`, `_rows`, `_now_iso`, `init_db`, `reset_db`), and all data-access functions organised by domain (products/materials/bom, product inventory/results, lots, process, packaging, dashboard). Single module (matches existing pattern; api/mcp do `from mes_core import db`).
- `seed.py` — REWRITE. Deterministic dataset for the new schema.
- `test_db.py` — REWRITE. Unit tests for the new logic.

**REST + web (`api/`)**
- `rest.py` — REWRITE. New Pydantic models + endpoints; keep `router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])`.
- `web.py` — REWRITE. Routes + form handlers for all pages incl. `/guide`.
- `main.py` — UNCHANGED.
- `auth.py` — UNCHANGED.
- `templates/` — base.html (nav) REWRITE; new: `product_inventory.html`, `materials.html`, `bom.html`, `product_results.html`, `packaging.html`, `guide.html`; rewrite `dashboard.html`, `lots.html`, `lot_detail.html`, `process.html`, `wip.html`, `equipment.html`; delete `inventory.html`, `production.html`.
- `static/styles.css` — extend (small additions only).
- `tests/test_app.py` — REWRITE.

**MCP (`mcp_server/`)**
- `server.py` — REWRITE tool set; keep `_ApiKeyGuard`, `build_asgi_app`, `main`, and the `FastMCP(...)` constructor block.
- `test_server.py` — REWRITE.

**Docs**
- `README.md` — REWRITE for the new model/endpoints.

---

### Task 1: New schema + connection foundation

Replace the schema and table list in `mes_core/db.py`. Keep the connection helpers (`get_db_path`, `connect`, `get_conn`, `_rows`, `_now_iso`, `init_db`, `reset_db`) exactly as they are today — only `SCHEMA_SQL`, `TABLES`, and the module docstring change. Also provide `counts()`.

**Files:**
- Modify: `mes_core/db.py` (replace `SCHEMA_SQL`, `TABLES`, docstring; add `counts()`)
- Test: `mes_core/test_db.py` (new harness + schema test)

**Interfaces:**
- Produces: `SCHEMA_SQL: str`, `TABLES: tuple[str, ...]`, `init_db() -> None`, `reset_db() -> None`, `counts() -> dict[str, int]`, plus the unchanged connection helpers.

- [ ] **Step 1: Write the failing test**

Replace the entire contents of `mes_core/test_db.py` with this harness + first test:

```python
"""Unit tests for the redesigned mock MES data layer (mes_core.db)."""

import importlib
import os
import unittest
from pathlib import Path

TEST_DB = Path(__file__).resolve().parents[1] / "data" / "mes_core_unit_test.db"


class DbTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = str(TEST_DB)
        TEST_DB.parent.mkdir(exist_ok=True)
        from mes_core import db
        importlib.reload(db)
        cls.db = db

    @classmethod
    def tearDownClass(cls):
        for f in TEST_DB.parent.glob(TEST_DB.name + "*"):
            f.unlink(missing_ok=True)

    def setUp(self):
        self.db.reset_db()

    def _seed_master(self):
        """Minimal master data: 1 product, a 2-step FAB route + PKG, materials, BOM."""
        db = self.db
        with db.get_conn() as conn:
            conn.execute("INSERT INTO product VALUES ('P1','Prod One','5nm')")
            conn.executemany(
                "INSERT INTO process_step (seq, step_code, step_name, operation, eqp_type, stage)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (10, "PHOTO", "Photo", "Litho", "Scanner", "FAB"),
                    (80, "TEST", "Wafer Test", "EDS", "Prober", "FAB"),
                    (90, "PKG", "Packaging", "Assembly", "Bonder", "PACK"),
                ],
            )
            conn.execute("INSERT INTO equipment VALUES ('EQP-PHOT01','Scanner-A','Scanner','Run')")
            conn.executemany(
                "INSERT INTO material (material_code, material_name, category, qty, uom, location)"
                " VALUES (?,?,?,?,?,?)",
                [
                    ("PR-EUV", "EUV Photoresist", "Chemical", 100.0, "L", "WH-CHEM"),
                    ("SUBSTR", "Substrate", "Package", 100.0, "EA", "WH-PKG"),
                ],
            )
            conn.executemany(
                "INSERT INTO bom (product_code, step_code, material_code, qty_per_wafer, uom)"
                " VALUES (?,?,?,?,?)",
                [
                    ("P1", "PHOTO", "PR-EUV", 2.0, "L"),
                    ("P1", "PKG", "SUBSTR", 1.0, "EA"),
                ],
            )


class SchemaTests(DbTestBase):
    def test_all_tables_exist_after_reset(self):
        with self.db.get_conn() as conn:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for t in (
            "product", "process_step", "equipment", "material", "bom",
            "lot", "process_result", "product_inventory", "product_result",
        ):
            self.assertIn(t, names)

    def test_counts_returns_zero_after_reset(self):
        c = self.db.counts()
        self.assertEqual(c["lot"], 0)
        self.assertEqual(c["product"], 0)
        self.assertIn("bom", c)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest mes_core.test_db -v`
Expected: FAIL — tables like `product`/`bom` missing (old schema), or `counts` KeyError.

- [ ] **Step 3: Write the implementation**

In `mes_core/db.py`, replace the module docstring's "7 MES functions" mapping block with a short note, then replace `SCHEMA_SQL` and `TABLES`. New `SCHEMA_SQL`:

```python
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
```

Add `counts()` (place it right after `reset_db()`):

```python
def counts() -> dict[str, int]:
    """Row counts per table (for the seed summary + dashboard)."""
    with get_conn() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
```

Delete every old data-access function below the connection helpers (everything from the old `실적 (production_result)` section onward) — the later tasks re-add functions incrementally. After this task `db.py` contains only: docstring, imports, `get_db_path`, `SCHEMA_SQL`, `TABLES`, `connect`, `get_conn`, `_rows`, `_now_iso`, `init_db`, `reset_db`, `counts`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest mes_core.test_db -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add mes_core/db.py mes_core/test_db.py
git commit -m "feat(db): new 9-table schema for BOM/two-stage redesign

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87"
```

---

### Task 2: Products, materials, BOM + material consumption

Add product-master reads, the 자재 인벤토리 (material) reads/receipt, the BOM CRUD, and the internal `_consume_materials` helper used by process/packaging transactions.

**Files:**
- Modify: `mes_core/db.py` (add functions)
- Test: `mes_core/test_db.py` (add `MaterialBomTests`)

**Interfaces:**
- Consumes: `get_conn`, `_now_iso`, `_rows` (Task 1).
- Produces:
  - `list_products() -> list[dict]` — rows of `product` ordered by `product_code`.
  - `list_product_codes() -> list[str]`
  - `list_materials(category=None, location=None, q=None, min_qty=None, max_qty=None) -> list[dict]`
  - `get_material(material_code) -> dict | None`
  - `receive_material(material_code, qty, material_name=None, category=None, uom=None, location=None) -> dict`
  - `materials_by_step(step_code=None) -> list[dict]` — BOM⋈material⋈product rows: `{product_code, product_name, step_code, step_name, material_code, material_name, qty_per_wafer, on_hand_qty, uom, location}`.
  - `list_bom(product_code=None, step_code=None) -> list[dict]`
  - `upsert_bom(product_code, step_code, material_code, qty_per_wafer, uom=None) -> dict`
  - `delete_bom(bom_id) -> bool`
  - `_consume_materials(conn, product_code, step_code, units) -> list[dict]` — internal; floors qty at 0, returns `shortages[]` of `{material_code, material_name, needed, available, short}`.

- [ ] **Step 1: Write the failing test**

Append to `mes_core/test_db.py`:

```python
class MaterialBomTests(DbTestBase):
    def setUp(self):
        super().setUp()
        self._seed_master()

    def test_list_products_and_codes(self):
        self.assertEqual(self.db.list_product_codes(), ["P1"])
        self.assertEqual(self.db.list_products()[0]["product_name"], "Prod One")

    def test_receive_material_adds_and_creates(self):
        db = self.db
        db.receive_material("PR-EUV", 50)  # existing 100 -> 150
        self.assertEqual(db.get_material("PR-EUV")["qty"], 150.0)
        db.receive_material("NEW-GAS", 20, material_name="New Gas", category="Gas", uom="BTL")
        self.assertEqual(db.get_material("NEW-GAS")["qty"], 20.0)
        self.assertEqual(db.get_material("NEW-GAS")["material_name"], "New Gas")

    def test_upsert_and_delete_bom(self):
        db = self.db
        row = db.upsert_bom("P1", "TEST", "PR-EUV", 0.5, uom="L")
        self.assertEqual(db.list_bom(step_code="TEST")[0]["qty_per_wafer"], 0.5)
        db.upsert_bom("P1", "TEST", "PR-EUV", 0.9)  # update same triple
        self.assertEqual(db.list_bom(step_code="TEST")[0]["qty_per_wafer"], 0.9)
        self.assertTrue(db.delete_bom(row["id"]))
        self.assertEqual(db.list_bom(step_code="TEST"), [])

    def test_materials_by_step_joins_bom(self):
        rows = self.db.materials_by_step("PHOTO")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["material_code"], "PR-EUV")
        self.assertEqual(rows[0]["on_hand_qty"], 100.0)
        self.assertEqual(rows[0]["qty_per_wafer"], 2.0)

    def test_consume_materials_floors_and_reports_shortage(self):
        db = self.db
        with db.get_conn() as conn:
            short = db._consume_materials(conn, "P1", "PHOTO", 40)  # need 2*40=80 <= 100
            self.assertEqual(short, [])
        self.assertEqual(db.get_material("PR-EUV")["qty"], 20.0)
        with db.get_conn() as conn:
            short = db._consume_materials(conn, "P1", "PHOTO", 40)  # need 80 > 20 -> shortage
        self.assertEqual(len(short), 1)
        self.assertEqual(short[0]["material_code"], "PR-EUV")
        self.assertEqual(short[0]["short"], 60.0)
        self.assertEqual(db.get_material("PR-EUV")["qty"], 0.0)  # floored
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest mes_core.test_db.MaterialBomTests -v`
Expected: FAIL — `AttributeError: module 'mes_core.db' has no attribute 'list_products'`.

- [ ] **Step 3: Write the implementation**

Append to `mes_core/db.py`:

```python
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
            " DO UPDATE SET qty_per_wafer = excluded.qty_per_wafer, uom = excluded.uom",
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest mes_core.test_db.MaterialBomTests -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add mes_core/db.py mes_core/test_db.py
git commit -m "feat(db): product master, material inventory, BOM + consumption

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87"
```

---

### Task 3: Product inventory + product results (제품 인벤토리 / 제품실적)

Add the 제품 인벤토리 reads, the internal inventory/result insert helpers, product-result reads, and the manual `register_product_result` transaction.

**Files:**
- Modify: `mes_core/db.py` (add functions)
- Test: `mes_core/test_db.py` (add `ProductResultTests`)

**Interfaces:**
- Consumes: `get_conn`, `_now_iso` (Task 1).
- Produces:
  - `list_product_inventory(product_code=None, item_type=None) -> list[dict]` — join `product` for `product_name`; fields incl. `product_code, product_name, item_type, qty, uom, location`.
  - `list_product_results(product_code=None, item_type=None, source=None, lot_id=None, date_from=None, date_to=None, limit=100) -> list[dict]`
  - `register_product_result(lot_id, item_type, good_qty, scrap_qty=0) -> dict` — MANUAL result; derives `product_code` from lot; raises `ValueError` on unknown lot / bad `item_type` (must be `SEMI`/`FIN`).
  - `_add_product_inventory(conn, product_code, item_type, delta, uom="EA", location=None) -> None` — upsert `qty += delta`.
  - `_insert_product_result(conn, *, result_date, lot_id, product_code, item_type, good_qty, scrap_qty, yield_pct, source, line=None, eqp_id=None) -> int` — returns new id.

- [ ] **Step 1: Write the failing test**

Append to `mes_core/test_db.py`:

```python
class ProductResultTests(DbTestBase):
    def setUp(self):
        super().setUp()
        self._seed_master()
        with self.db.get_conn() as conn:
            conn.execute(
                "INSERT INTO lot (lot_id, product_code, tech_node, start_qty, wafer_qty,"
                " priority, current_step, status, start_date)"
                " VALUES ('LOT1','P1','5nm',25,25,'Normal','PHOTO','Running','2026-01-01')"
            )

    def test_add_product_inventory_upsert(self):
        db = self.db
        with db.get_conn() as conn:
            db._add_product_inventory(conn, "P1", "SEMI", 10)
            db._add_product_inventory(conn, "P1", "SEMI", 5)
        inv = db.list_product_inventory(product_code="P1", item_type="SEMI")
        self.assertEqual(inv[0]["qty"], 15.0)
        self.assertEqual(inv[0]["product_name"], "Prod One")

    def test_register_product_result_manual(self):
        db = self.db
        res = db.register_product_result("LOT1", "FIN", 100, scrap_qty=5)
        self.assertEqual(res["source"], "MANUAL")
        self.assertEqual(res["product_code"], "P1")
        self.assertEqual(res["item_type"], "FIN")
        inv = db.list_product_inventory(product_code="P1", item_type="FIN")
        self.assertEqual(inv[0]["qty"], 100.0)
        rows = db.list_product_results(product_code="P1", source="MANUAL")
        self.assertEqual(len(rows), 1)

    def test_register_product_result_bad_type_rejected(self):
        with self.assertRaises(ValueError):
            self.db.register_product_result("LOT1", "WIDGET", 10)

    def test_register_product_result_unknown_lot_rejected(self):
        with self.assertRaises(ValueError):
            self.db.register_product_result("NOPE", "FIN", 10)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest mes_core.test_db.ProductResultTests -v`
Expected: FAIL — `_add_product_inventory` / `register_product_result` missing.

- [ ] **Step 3: Write the implementation**

Append to `mes_core/db.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest mes_core.test_db.ProductResultTests -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add mes_core/db.py mes_core/test_db.py
git commit -m "feat(db): product inventory + product results (manual receipt)

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87"
```

---

### Task 4: Lots + WIP (로트 / 재공)

Add lot lifecycle reads/creation and the derived WIP query.

**Files:**
- Modify: `mes_core/db.py` (add functions)
- Test: `mes_core/test_db.py` (add `LotTests`)

**Interfaces:**
- Consumes: `get_conn`, `_now_iso`, `_rows` (Task 1).
- Produces:
  - `start_lot(product_code, start_qty, priority="Normal", lot_id=None) -> dict` — new Running lot at the first FAB step; `wafer_qty=start_qty`; auto lot_id `LOT####` if not given; returns `get_lot(...)`. Raises `ValueError` on unknown product / `start_qty<=0`.
  - `get_lot(lot_id) -> dict | None` — lot + `product_name` + `cumulative_yield` + `history` (list of process_result rows w/ `step_name`).
  - `list_lots(status=None, product_code=None, current_step=None, priority=None, tech_node=None, limit=200) -> list[dict]` — includes `product_name`.
  - `list_lot_ids(status=None) -> list[str]`
  - `get_wip(step_code=None) -> list[dict]` — non-Done lots grouped by `current_step`: `{seq, step_code, step_name, stage, lot_count, wafer_qty}`.
  - `_next_lot_id(conn) -> str` — internal.

- [ ] **Step 1: Write the failing test**

Append to `mes_core/test_db.py`:

```python
class LotTests(DbTestBase):
    def setUp(self):
        super().setUp()
        self._seed_master()

    def test_start_lot_creates_running_at_first_step(self):
        db = self.db
        lot = db.start_lot("P1", 25, priority="Hot")
        self.assertEqual(lot["status"], "Running")
        self.assertEqual(lot["current_step"], "PHOTO")  # lowest seq FAB step
        self.assertEqual(lot["wafer_qty"], 25)
        self.assertEqual(lot["start_qty"], 25)
        self.assertTrue(lot["lot_id"].startswith("LOT"))
        self.assertEqual(lot["cumulative_yield"], 100.0)

    def test_start_lot_unknown_product_rejected(self):
        with self.assertRaises(ValueError):
            self.db.start_lot("NOPE", 25)

    def test_start_lot_bad_qty_rejected(self):
        with self.assertRaises(ValueError):
            self.db.start_lot("P1", 0)

    def test_list_lots_filters_by_status(self):
        db = self.db
        db.start_lot("P1", 25)
        db.start_lot("P1", 12)
        self.assertEqual(len(db.list_lots(status="Running")), 2)
        self.assertEqual(db.list_lots(status="Done"), [])

    def test_get_wip_groups_non_done_lots(self):
        db = self.db
        db.start_lot("P1", 25)
        db.start_lot("P1", 12)
        wip = db.get_wip()
        photo = [w for w in wip if w["step_code"] == "PHOTO"][0]
        self.assertEqual(photo["lot_count"], 2)
        self.assertEqual(photo["wafer_qty"], 37)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest mes_core.test_db.LotTests -v`
Expected: FAIL — `start_lot` missing.

- [ ] **Step 3: Write the implementation**

Append to `mes_core/db.py`:

```python
# --------------------------------------------------------------------------- #
# 로트 (lot) + 재공 (WIP, derived)
# --------------------------------------------------------------------------- #

def _next_lot_id(conn) -> str:
    row = conn.execute(
        "SELECT lot_id FROM lot WHERE lot_id LIKE 'LOT%' ORDER BY lot_id DESC LIMIT 1"
    ).fetchone()
    n = 0
    if row:
        try:
            n = int(str(row["lot_id"])[3:])
        except ValueError:
            n = 0
    return f"LOT{n + 1:04d}"


def start_lot(product_code, start_qty, priority="Normal", lot_id=None):
    start_qty = int(start_qty)
    if start_qty <= 0:
        raise ValueError(f"start_qty must be > 0, got {start_qty}")
    with get_conn() as conn:
        prod = conn.execute("SELECT * FROM product WHERE product_code = ?", (product_code,)).fetchone()
        if prod is None:
            raise ValueError(f"unknown product_code: {product_code!r}")
        first = conn.execute(
            "SELECT step_code FROM process_step WHERE stage='FAB' ORDER BY seq LIMIT 1"
        ).fetchone()
        if first is None:
            raise ValueError("no FAB process steps defined")
        if lot_id is None:
            lot_id = _next_lot_id(conn)
        conn.execute(
            "INSERT INTO lot (lot_id, product_code, tech_node, start_qty, wafer_qty,"
            " priority, current_step, status, start_date) VALUES (?,?,?,?,?,?,?,?,?)",
            (lot_id, product_code, prod["tech_node"], start_qty, start_qty,
             priority, first["step_code"], "Running", _now_iso()[:10]),
        )
    return get_lot(lot_id)


def get_lot(lot_id):
    with get_conn() as conn:
        lot = conn.execute(
            "SELECT l.*, p.product_name FROM lot l "
            "LEFT JOIN product p ON p.product_code = l.product_code WHERE l.lot_id = ?",
            (lot_id,),
        ).fetchone()
        if lot is None:
            return None
        hist = conn.execute(
            "SELECT pr.*, ps.step_name FROM process_result pr "
            "LEFT JOIN process_step ps ON ps.step_code = pr.step_code "
            "WHERE pr.lot_id = ? ORDER BY pr.id",
            (lot_id,),
        ).fetchall()
    out = dict(lot)
    start_qty = out.get("start_qty") or 0
    wafer_qty = out.get("wafer_qty") or 0
    out["cumulative_yield"] = round(wafer_qty / start_qty * 100, 2) if start_qty else 0.0
    out["history"] = [dict(h) for h in hist]
    return out


def list_lots(status=None, product_code=None, current_step=None,
              priority=None, tech_node=None, limit=200):
    sql = (
        "SELECT l.*, p.product_name FROM lot l "
        "LEFT JOIN product p ON p.product_code = l.product_code WHERE 1=1"
    )
    args: list[Any] = []
    if status:
        sql += " AND l.status = ?"; args.append(status)
    if product_code:
        sql += " AND l.product_code = ?"; args.append(product_code)
    if current_step:
        sql += " AND l.current_step = ?"; args.append(current_step)
    if priority:
        sql += " AND l.priority = ?"; args.append(priority)
    if tech_node:
        sql += " AND l.tech_node = ?"; args.append(tech_node)
    sql += " ORDER BY l.lot_id LIMIT ?"; args.append(limit)
    with get_conn() as conn:
        return _rows(conn.execute(sql, args))


def list_lot_ids(status=None) -> list[str]:
    sql = "SELECT lot_id FROM lot"
    args: list[Any] = []
    if status:
        sql += " WHERE status = ?"; args.append(status)
    sql += " ORDER BY lot_id"
    with get_conn() as conn:
        return [r[0] for r in conn.execute(sql, args)]


def get_wip(step_code=None):
    sql = (
        "SELECT ps.seq, ps.step_code, ps.step_name, ps.stage, "
        "       COUNT(l.lot_id) AS lot_count, COALESCE(SUM(l.wafer_qty),0) AS wafer_qty "
        "FROM process_step ps "
        "JOIN lot l ON l.current_step = ps.step_code AND l.status != 'Done' "
        "WHERE 1=1"
    )
    args: list[Any] = []
    if step_code:
        sql += " AND ps.step_code = ?"; args.append(step_code)
    sql += " GROUP BY ps.seq, ps.step_code, ps.step_name, ps.stage ORDER BY ps.seq"
    with get_conn() as conn:
        return _rows(conn.execute(sql, args))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest mes_core.test_db.LotTests -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add mes_core/db.py mes_core/test_db.py
git commit -m "feat(db): lot lifecycle + derived WIP

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87"
```

---

### Task 5: 공정실적 — register_process_result (material consume + SEMI auto-receipt)

The central transaction. A process move records qty/scrap, advances the lot, consumes BOM materials for `(product, step)`, and on the final FAB step (TEST) passing, closes the lot (`Done`), inserts an `AUTO_FAB` SEMI `product_result`, and increments SEMI `product_inventory`.

**Files:**
- Modify: `mes_core/db.py` (add functions)
- Test: `mes_core/test_db.py` (add `ProcessResultTests`)

**Interfaces:**
- Consumes: `_consume_materials` (Task 2), `_add_product_inventory` / `_insert_product_result` (Task 3), lot rows (Task 4).
- Produces:
  - `register_process_result(lot_id, step_code, eqp_id=None, in_qty=None, scrap_qty=0, defect_code=None, operator=None, result="Pass", in_time=None, out_time=None) -> dict` — returns the process_result row (+ `step_name`) plus `shortages: list[dict]` and `semi_receipt: dict | None`.
  - `get_process_route(step_code=None, eqp_type=None, stage=None) -> list[dict]`
  - `list_process_results(lot_id=None, step_code=None, result=None, operator=None, defect_code=None, has_scrap=None, limit=100) -> list[dict]`
- Constant: `_FAB_PASS = ("pass", "ok", "good")` — result strings (case-insensitive) that count as a passing move.

**Semantics (from spec):**
- `in_qty` defaults to `lot.wafer_qty` (fallback `start_qty`). Guard `in_qty >= 0` and `0 <= scrap_qty <= in_qty`. `out_qty = in_qty - scrap_qty`.
- Always: insert process_result; consume materials for `in_qty`; set `lot.current_step = step_code`, `lot.wafer_qty = out_qty`.
- Final FAB step = the `process_step` with `stage='FAB'` and the max `seq`. On a passing move at that step when the lot was not already `Done`: set `status='Done'`, insert `product_result` (SEMI, AUTO_FAB, `good_qty=out_qty`, `scrap_qty=cumulative lot scrap`), `+out_qty` SEMI inventory.
- Re-processing a `Done` lot at a non-final step flips status back to `Running`.

- [ ] **Step 1: Write the failing test**

Append to `mes_core/test_db.py`:

```python
class ProcessResultTests(DbTestBase):
    def setUp(self):
        super().setUp()
        self._seed_master()
        self.lot = self.db.start_lot("P1", 25)

    def test_move_advances_lot_and_consumes_material(self):
        db = self.db
        lot_id = self.lot["lot_id"]
        res = db.register_process_result(lot_id, "PHOTO", scrap_qty=2)  # in defaults 25
        self.assertEqual(res["in_qty"], 25)
        self.assertEqual(res["out_qty"], 23)
        self.assertEqual(res["shortages"], [])
        self.assertIsNone(res["semi_receipt"])
        after = db.get_lot(lot_id)
        self.assertEqual(after["current_step"], "PHOTO")
        self.assertEqual(after["wafer_qty"], 23)
        self.assertEqual(db.get_material("PR-EUV")["qty"], 100.0 - 2.0 * 25)  # 50

    def test_scrap_exceeds_in_qty_rejected(self):
        with self.assertRaises(ValueError):
            self.db.register_process_result(self.lot["lot_id"], "PHOTO", in_qty=10, scrap_qty=11)

    def test_final_fab_pass_closes_lot_and_receives_semi(self):
        db = self.db
        lot_id = self.lot["lot_id"]
        res = db.register_process_result(lot_id, "TEST", in_qty=20, scrap_qty=1, result="Pass")
        self.assertEqual(res["out_qty"], 19)
        self.assertIsNotNone(res["semi_receipt"])
        self.assertEqual(res["semi_receipt"]["good_qty"], 19)
        self.assertEqual(db.get_lot(lot_id)["status"], "Done")
        inv = db.list_product_inventory(product_code="P1", item_type="SEMI")
        self.assertEqual(inv[0]["qty"], 19.0)
        auto = db.list_product_results(product_code="P1", source="AUTO_FAB")
        self.assertEqual(len(auto), 1)
        self.assertEqual(auto[0]["item_type"], "SEMI")

    def test_final_fab_fail_does_not_close(self):
        db = self.db
        res = db.register_process_result(self.lot["lot_id"], "TEST", result="Fail")
        self.assertIsNone(res["semi_receipt"])
        self.assertNotEqual(db.get_lot(self.lot["lot_id"])["status"], "Done")

    def test_material_shortage_does_not_block(self):
        db = self.db
        db.upsert_bom("P1", "PHOTO", "PR-EUV", 10.0)  # need 10*25=250 > 100
        res = db.register_process_result(self.lot["lot_id"], "PHOTO")
        self.assertEqual(len(res["shortages"]), 1)
        self.assertEqual(db.get_material("PR-EUV")["qty"], 0.0)
        self.assertEqual(db.get_lot(self.lot["lot_id"])["current_step"], "PHOTO")

    def test_process_route_and_results_queries(self):
        db = self.db
        db.register_process_result(self.lot["lot_id"], "PHOTO", scrap_qty=3, defect_code="Particle")
        self.assertEqual(len(db.get_process_route(stage="FAB")), 2)
        rows = db.list_process_results(lot_id=self.lot["lot_id"], has_scrap=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["defect_code"], "Particle")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest mes_core.test_db.ProcessResultTests -v`
Expected: FAIL — `register_process_result` missing.

- [ ] **Step 3: Write the implementation**

Append to `mes_core/db.py`:

```python
# --------------------------------------------------------------------------- #
# 공정 (process_step route) + 공정실적 (process_result)
# --------------------------------------------------------------------------- #

_FAB_PASS = ("pass", "ok", "good")


def get_process_route(step_code=None, eqp_type=None, stage=None):
    sql = "SELECT * FROM process_step WHERE 1=1"
    args: list[Any] = []
    if step_code:
        sql += " AND step_code = ?"; args.append(step_code)
    if eqp_type:
        sql += " AND eqp_type = ?"; args.append(eqp_type)
    if stage:
        sql += " AND stage = ?"; args.append(stage)
    sql += " ORDER BY seq"
    with get_conn() as conn:
        return _rows(conn.execute(sql, args))


def list_process_results(lot_id=None, step_code=None, result=None, operator=None,
                         defect_code=None, has_scrap=None, limit=100):
    sql = (
        "SELECT pr.*, ps.step_name FROM process_result pr "
        "LEFT JOIN process_step ps ON ps.step_code = pr.step_code WHERE 1=1"
    )
    args: list[Any] = []
    if lot_id:
        sql += " AND pr.lot_id = ?"; args.append(lot_id)
    if step_code:
        sql += " AND pr.step_code = ?"; args.append(step_code)
    if result:
        sql += " AND pr.result = ?"; args.append(result)
    if operator:
        sql += " AND pr.operator = ?"; args.append(operator)
    if defect_code:
        sql += " AND pr.defect_code = ?"; args.append(defect_code)
    if has_scrap is True:
        sql += " AND pr.scrap_qty > 0"
    elif has_scrap is False:
        sql += " AND pr.scrap_qty = 0"
    sql += " ORDER BY pr.id DESC LIMIT ?"; args.append(limit)
    with get_conn() as conn:
        return _rows(conn.execute(sql, args))


def register_process_result(lot_id, step_code, eqp_id=None, in_qty=None, scrap_qty=0,
                            defect_code=None, operator=None, result="Pass",
                            in_time=None, out_time=None):
    now = _now_iso()
    in_time = in_time or now
    out_time = out_time or now
    with get_conn() as conn:
        lot = conn.execute("SELECT * FROM lot WHERE lot_id = ?", (lot_id,)).fetchone()
        if lot is None:
            raise ValueError(f"unknown lot_id: {lot_id!r}")
        step = conn.execute("SELECT * FROM process_step WHERE step_code = ?", (step_code,)).fetchone()
        if step is None:
            raise ValueError(f"unknown step_code: {step_code!r}")
        if in_qty is None:
            in_qty = lot["wafer_qty"] if lot["wafer_qty"] is not None else (lot["start_qty"] or 0)
        in_qty = int(in_qty)
        scrap_qty = int(scrap_qty or 0)
        if in_qty < 0:
            raise ValueError(f"in_qty must be >= 0, got {in_qty}")
        if scrap_qty < 0 or scrap_qty > in_qty:
            raise ValueError(f"scrap_qty must be between 0 and in_qty ({in_qty}), got {scrap_qty}")
        out_qty = in_qty - scrap_qty

        new_id = conn.execute(
            "INSERT INTO process_result"
            " (lot_id, step_code, eqp_id, in_qty, out_qty, scrap_qty, defect_code,"
            "  in_time, out_time, operator, result) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (lot_id, step_code, eqp_id, in_qty, out_qty, scrap_qty, defect_code,
             in_time, out_time, operator, result),
        ).lastrowid

        product_code = lot["product_code"]
        shortages = _consume_materials(conn, product_code, step_code, in_qty)

        last_fab_seq = conn.execute(
            "SELECT MAX(seq) FROM process_step WHERE stage = 'FAB'"
        ).fetchone()[0]
        prev_status = lot["status"]
        passed = str(result).lower() in _FAB_PASS
        is_last_fab = step["stage"] == "FAB" and step["seq"] == last_fab_seq

        new_status = prev_status
        if is_last_fab and passed:
            new_status = "Done"
        elif prev_status == "Done":
            new_status = "Running"
        conn.execute(
            "UPDATE lot SET current_step = ?, status = ?, wafer_qty = ? WHERE lot_id = ?",
            (step_code, new_status, out_qty, lot_id),
        )

        semi_receipt = None
        if is_last_fab and passed and prev_status != "Done":
            cum_scrap = conn.execute(
                "SELECT COALESCE(SUM(scrap_qty),0) FROM process_result WHERE lot_id = ?",
                (lot_id,),
            ).fetchone()[0]
            start_qty = lot["start_qty"] or (out_qty + cum_scrap)
            y = round(out_qty / start_qty * 100, 2) if start_qty else 0.0
            pr_id = _insert_product_result(
                conn, result_date=now[:10], lot_id=lot_id, product_code=product_code,
                item_type="SEMI", good_qty=out_qty, scrap_qty=int(cum_scrap),
                yield_pct=y, source="AUTO_FAB", eqp_id=eqp_id,
            )
            _add_product_inventory(conn, product_code, "SEMI", out_qty, uom="EA", location="WH-SEMI")
            semi_receipt = {
                "product_result_id": pr_id, "product_code": product_code,
                "item_type": "SEMI", "good_qty": out_qty,
            }

        row = conn.execute(
            "SELECT pr.*, ps.step_name FROM process_result pr "
            "LEFT JOIN process_step ps ON ps.step_code = pr.step_code WHERE pr.id = ?",
            (new_id,),
        ).fetchone()
    out = dict(row)
    out["shortages"] = shortages
    out["semi_receipt"] = semi_receipt
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest mes_core.test_db.ProcessResultTests -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add mes_core/db.py mes_core/test_db.py
git commit -m "feat(db): process result txn with material consume + SEMI auto-receipt

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87"
```

---

### Task 6: 패키징 — package (SEMI → FIN)

Single packaging transaction: consume SEMI, produce FIN, consume PKG-step BOM materials, record an `AUTO_PACK` FIN product_result. SEMI shortfall blocks (raises).

**Files:**
- Modify: `mes_core/db.py` (add `package`)
- Test: `mes_core/test_db.py` (add `PackagingTests`)

**Interfaces:**
- Consumes: `_add_product_inventory`, `_insert_product_result` (Task 3), `_consume_materials` (Task 2).
- Produces: `package(product_code, in_qty, scrap_qty=0, lot_id=None, eqp_id=None, operator=None) -> dict` — returns the FIN product_result row + `shortages: list[dict]`. Raises `ValueError` on unknown product, `in_qty<=0`, bad scrap, or `SEMI < in_qty`.

- [ ] **Step 1: Write the failing test**

Append to `mes_core/test_db.py`:

```python
class PackagingTests(DbTestBase):
    def setUp(self):
        super().setUp()
        self._seed_master()
        with self.db.get_conn() as conn:
            conn.execute(
                "INSERT INTO lot (lot_id, product_code, tech_node, start_qty, wafer_qty,"
                " priority, current_step, status, start_date)"
                " VALUES ('LOT1','P1','5nm',25,20,'Normal','TEST','Done','2026-01-01')"
            )
            db = self.db
            db._add_product_inventory(conn, "P1", "SEMI", 20)  # 20 SEMI on hand

    def test_package_consumes_semi_produces_fin(self):
        db = self.db
        res = db.package("P1", 15, scrap_qty=1, lot_id="LOT1")
        self.assertEqual(res["item_type"], "FIN")
        self.assertEqual(res["source"], "AUTO_PACK")
        self.assertEqual(res["good_qty"], 14)
        semi = db.list_product_inventory(product_code="P1", item_type="SEMI")[0]["qty"]
        fin = db.list_product_inventory(product_code="P1", item_type="FIN")[0]["qty"]
        self.assertEqual(semi, 5.0)   # 20 - 15
        self.assertEqual(fin, 14.0)   # +14
        self.assertEqual(db.get_material("SUBSTR")["qty"], 100.0 - 15.0)  # PKG BOM 1*15

    def test_package_insufficient_semi_rejected(self):
        with self.assertRaises(ValueError):
            self.db.package("P1", 999)

    def test_package_unknown_product_rejected(self):
        with self.assertRaises(ValueError):
            self.db.package("NOPE", 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest mes_core.test_db.PackagingTests -v`
Expected: FAIL — `package` missing.

- [ ] **Step 3: Write the implementation**

Append to `mes_core/db.py`:

```python
# --------------------------------------------------------------------------- #
# 패키징 (SEMI -> FIN)
# --------------------------------------------------------------------------- #

def package(product_code, in_qty, scrap_qty=0, lot_id=None, eqp_id=None, operator=None):
    in_qty = int(in_qty)
    scrap_qty = int(scrap_qty or 0)
    if in_qty <= 0:
        raise ValueError(f"in_qty must be > 0, got {in_qty}")
    if scrap_qty < 0 or scrap_qty > in_qty:
        raise ValueError(f"scrap_qty must be between 0 and in_qty ({in_qty}), got {scrap_qty}")
    now = _now_iso()
    with get_conn() as conn:
        prod = conn.execute("SELECT * FROM product WHERE product_code = ?", (product_code,)).fetchone()
        if prod is None:
            raise ValueError(f"unknown product_code: {product_code!r}")
        semi = conn.execute(
            "SELECT qty FROM product_inventory WHERE product_code = ? AND item_type = 'SEMI'",
            (product_code,),
        ).fetchone()
        semi_qty = semi["qty"] if semi else 0
        if semi_qty < in_qty:
            raise ValueError(
                f"insufficient SEMI stock for {product_code}: need {in_qty}, have {semi_qty}"
            )
        out_qty = in_qty - scrap_qty
        _add_product_inventory(conn, product_code, "SEMI", -in_qty)
        _add_product_inventory(conn, product_code, "FIN", out_qty, uom="EA", location="WH-FG")
        shortages = _consume_materials(conn, product_code, "PKG", in_qty)
        y = round(out_qty / in_qty * 100, 2) if in_qty else 0.0
        pr_id = _insert_product_result(
            conn, result_date=now[:10], lot_id=lot_id, product_code=product_code,
            item_type="FIN", good_qty=out_qty, scrap_qty=scrap_qty, yield_pct=y,
            source="AUTO_PACK", eqp_id=eqp_id,
        )
        row = conn.execute("SELECT * FROM product_result WHERE id = ?", (pr_id,)).fetchone()
    out = dict(row)
    out["shortages"] = shortages
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest mes_core.test_db.PackagingTests -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add mes_core/db.py mes_core/test_db.py
git commit -m "feat(db): packaging transaction (SEMI->FIN + PKG material consume)

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87"
```

---

### Task 7: Dashboard summary

One roll-up read used by the web dashboard and (optionally) REST.

**Files:**
- Modify: `mes_core/db.py` (add `get_dashboard_summary`)
- Test: `mes_core/test_db.py` (add `DashboardTests`)

**Interfaces:**
- Produces: `get_dashboard_summary() -> dict` with keys:
  `lot_total`, `lots_by_status` (list of `{status, n}`), `wafers_started`, `wafers_current`,
  `wip_total_lots`, `wip_by_step` (from `get_wip()`), `semi_total`, `fin_total`,
  `material_total_items`, `low_materials` (list of materials with `qty < 20`, up to 8),
  `top_defects` (list of `{defect_code, n}` top 5, scrap>0), `recent_process` (last 8 process_result w/ step_name),
  `recent_products` (last 8 product_result), `equipment` (all rows).

- [ ] **Step 1: Write the failing test**

Append to `mes_core/test_db.py`:

```python
class DashboardTests(DbTestBase):
    def setUp(self):
        super().setUp()
        self._seed_master()

    def test_dashboard_summary_shape(self):
        db = self.db
        lot = db.start_lot("P1", 25)
        db.register_process_result(lot["lot_id"], "PHOTO", scrap_qty=2, defect_code="Particle")
        s = db.get_dashboard_summary()
        self.assertEqual(s["lot_total"], 1)
        self.assertEqual(s["wafers_started"], 25)
        self.assertTrue(any(d["defect_code"] == "Particle" for d in s["top_defects"]))
        self.assertIn("wip_by_step", s)
        self.assertIn("equipment", s)
        self.assertEqual(len(s["recent_process"]), 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest mes_core.test_db.DashboardTests -v`
Expected: FAIL — `get_dashboard_summary` missing.

- [ ] **Step 3: Write the implementation**

Append to `mes_core/db.py`:

```python
# --------------------------------------------------------------------------- #
# Dashboard roll-up
# --------------------------------------------------------------------------- #

def get_dashboard_summary() -> dict[str, Any]:
    with get_conn() as conn:
        lot_total = conn.execute("SELECT COUNT(*) FROM lot").fetchone()[0]
        lots_by_status = _rows(conn.execute(
            "SELECT status, COUNT(*) AS n FROM lot GROUP BY status ORDER BY status"))
        wafers_started = conn.execute("SELECT COALESCE(SUM(start_qty),0) FROM lot").fetchone()[0]
        wafers_current = conn.execute(
            "SELECT COALESCE(SUM(wafer_qty),0) FROM lot WHERE status != 'Done'").fetchone()[0]
        semi_total = conn.execute(
            "SELECT COALESCE(SUM(qty),0) FROM product_inventory WHERE item_type='SEMI'").fetchone()[0]
        fin_total = conn.execute(
            "SELECT COALESCE(SUM(qty),0) FROM product_inventory WHERE item_type='FIN'").fetchone()[0]
        material_total_items = conn.execute("SELECT COUNT(*) FROM material").fetchone()[0]
        low_materials = _rows(conn.execute(
            "SELECT * FROM material WHERE qty < 20 ORDER BY qty LIMIT 8"))
        top_defects = _rows(conn.execute(
            "SELECT defect_code, COUNT(*) AS n FROM process_result "
            "WHERE scrap_qty > 0 AND defect_code IS NOT NULL "
            "GROUP BY defect_code ORDER BY n DESC LIMIT 5"))
        recent_process = _rows(conn.execute(
            "SELECT pr.*, ps.step_name FROM process_result pr "
            "LEFT JOIN process_step ps ON ps.step_code = pr.step_code "
            "ORDER BY pr.id DESC LIMIT 8"))
        recent_products = _rows(conn.execute(
            "SELECT pr.*, p.product_name FROM product_result pr "
            "LEFT JOIN product p ON p.product_code = pr.product_code "
            "ORDER BY pr.id DESC LIMIT 8"))
        equipment = _rows(conn.execute("SELECT * FROM equipment ORDER BY eqp_id"))
    return {
        "lot_total": lot_total,
        "lots_by_status": lots_by_status,
        "wafers_started": wafers_started,
        "wafers_current": wafers_current,
        "wip_total_lots": sum(w["lot_count"] for w in get_wip()),
        "wip_by_step": get_wip(),
        "semi_total": semi_total,
        "fin_total": fin_total,
        "material_total_items": material_total_items,
        "low_materials": low_materials,
        "top_defects": top_defects,
        "recent_process": recent_process,
        "recent_products": recent_products,
        "equipment": equipment,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest mes_core.test_db -v`
Expected: PASS (all db tests: schema + material/bom + product-result + lot + process + packaging + dashboard).

- [ ] **Step 5: Commit**

```bash
git add mes_core/db.py mes_core/test_db.py
git commit -m "feat(db): dashboard roll-up summary

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87"
```

---

### Task 8: Deterministic seed for the new schema

Rewrite `mes_core/seed.py`. Insert master data (product, route, equipment, material, BOM) directly, then generate the dynamic data by CALLING the real transaction functions (`start_lot`, `register_process_result`, `package`, `register_product_result`) so the seeded snapshot is internally consistent (materials actually consumed, SEMI/FIN derived, results linked). Fixed `random.Random(42)`.

**Files:**
- Rewrite: `mes_core/seed.py`
- Test: `mes_core/test_db.py` (add `SeedTests`)

**Interfaces:**
- Consumes: all Task 1–7 functions.
- Produces: `seed() -> None`, `main() -> None`.

**Dataset:** 4 products; FAB 8 steps (DIFF/PHOTO/ETCH/IMPL/CVD/CMP/METRO/TEST, seq 10–80) + PKG (seq 90, stage PACK); 9 equipment; 12 materials; ~32 BOM rows; 16 lots (6 Done, 2 Hold, 8 Running); ~80 process_result; SEMI inventory from Done lots + FIN from 3 packagings; product_result = 6 AUTO_FAB + 3 AUTO_PACK + 1 MANUAL.

- [ ] **Step 1: Write the failing test**

Append to `mes_core/test_db.py`:

```python
class SeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = str(TEST_DB)
        TEST_DB.parent.mkdir(exist_ok=True)
        from mes_core import db, seed
        importlib.reload(db)
        importlib.reload(seed)
        cls.db, cls.seed = db, seed
        seed.seed()

    @classmethod
    def tearDownClass(cls):
        for f in TEST_DB.parent.glob(TEST_DB.name + "*"):
            f.unlink(missing_ok=True)

    def test_master_counts(self):
        c = self.db.counts()
        self.assertEqual(c["product"], 4)
        self.assertEqual(c["process_step"], 9)      # 8 FAB + PKG
        self.assertEqual(c["equipment"], 9)
        self.assertEqual(c["material"], 12)
        self.assertGreaterEqual(c["bom"], 24)
        self.assertEqual(c["lot"], 16)

    def test_done_lots_produced_semi(self):
        done = self.db.list_lots(status="Done")
        self.assertEqual(len(done), 6)
        auto_fab = self.db.list_product_results(source="AUTO_FAB")
        self.assertEqual(len(auto_fab), 6)
        semi = self.db.list_product_inventory(item_type="SEMI")
        self.assertTrue(sum(r["qty"] for r in semi) > 0)

    def test_packaging_and_manual_results_present(self):
        self.assertEqual(len(self.db.list_product_results(source="AUTO_PACK")), 3)
        self.assertEqual(len(self.db.list_product_results(source="MANUAL")), 1)
        fin = self.db.list_product_inventory(item_type="FIN")
        self.assertTrue(sum(r["qty"] for r in fin) > 0)

    def test_wip_only_non_done(self):
        wip = self.db.get_wip()
        self.assertTrue(all(w["lot_count"] > 0 for w in wip))
        self.assertEqual(sum(w["lot_count"] for w in wip), 10)  # 2 Hold + 8 Running
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest mes_core.test_db.SeedTests -v`
Expected: FAIL — old seed builds the old schema (no `product`/`bom` tables → error).

- [ ] **Step 3: Write the implementation**

Replace the entire contents of `mes_core/seed.py`:

```python
"""Seed the shared SQLite DB with a realistic mock semiconductor-fab dataset.

    python -m mes_core.seed

Reproducible: resets all tables and regenerates the same dataset every time
(fixed RNG seed). Master data is inserted directly; dynamic data (lots, process
results, inventory, product results) is generated by calling the real
transaction functions so the snapshot is internally consistent.
"""

from __future__ import annotations

import random

from . import db

# (product_code, product_name, tech_node)
PRODUCTS = [
    ("LX9", "LX9 AP", "5nm"),
    ("DDR5", "DDR5-16G", "10nm"),
    ("NAND", "V7 NAND", "128L"),
    ("PMIC", "PMIC-33", "28nm"),
]

# (seq, step_code, step_name, operation, eqp_type, stage)
ROUTE = [
    (10, "DIFF",  "Diffusion",           "Thermal Oxidation",          "Furnace",   "FAB"),
    (20, "PHOTO", "Photolithography",    "Pattern Exposure",           "Scanner",   "FAB"),
    (30, "ETCH",  "Etch",                "Plasma Etch",                "Etcher",    "FAB"),
    (40, "IMPL",  "Ion Implant",         "Dopant Implant",             "Implanter", "FAB"),
    (50, "CVD",   "Deposition (CVD)",    "Thin-Film Deposition",       "CVD",       "FAB"),
    (60, "CMP",   "Planarization (CMP)", "Chemical-Mechanical Polish", "Polisher",  "FAB"),
    (70, "METRO", "Metrology",           "Inline Inspection",          "Metrology", "FAB"),
    (80, "TEST",  "Wafer Test (EDS)",    "Electrical Die Sort",        "Prober",    "FAB"),
    (90, "PKG",   "Packaging",           "Assembly & Test",            "Bonder",    "PACK"),
]

# (eqp_id, eqp_name, type, status)
EQUIPMENT = [
    ("EQP-DIFF01", "Furnace-A",    "Furnace",   "Run"),
    ("EQP-PHOT01", "Scanner-EUV1", "Scanner",   "Run"),
    ("EQP-PHOT02", "Scanner-DUV1", "Scanner",   "Idle"),
    ("EQP-ETCH01", "Etcher-A",     "Etcher",    "Run"),
    ("EQP-IMPL01", "Implanter-A",  "Implanter", "Down"),
    ("EQP-CVD01",  "CVD-A",        "CVD",       "Run"),
    ("EQP-CMP01",  "Polisher-A",   "Polisher",  "Idle"),
    ("EQP-TEST01", "Prober-A",     "Prober",    "Run"),
    ("EQP-PKG01",  "Bonder-A",     "Bonder",    "Run"),
]

# (material_code, material_name, category, qty, uom, location)
MATERIALS = [
    ("RAW-WAFER-300", "300mm Blank Silicon Wafer", "Raw Wafer", 12000, "EA",  "WH-RAW"),
    ("RETICLE-5NM",   "EUV Reticle Set (5nm)",     "Mask",         45, "SET", "WH-MASK"),
    ("PR-EUV",        "EUV Photoresist",           "Chemical",    320, "L",   "WH-CHEM"),
    ("SLURRY-CMP",    "CMP Slurry",                "Chemical",    500, "L",   "WH-CHEM"),
    ("GAS-SIH4",      "Silane (SiH4) Gas",         "Gas",         180, "BTL", "WH-GAS"),
    ("GAS-AR",        "Argon (Ar) Gas",            "Gas",         400, "BTL", "WH-GAS"),
    ("TARGET-CU",     "Copper Sputter Target",     "Metal",        60, "EA",  "WH-MAT"),
    ("DOPANT-B",      "Boron Dopant Source",       "Chemical",     90, "L",   "WH-CHEM"),
    ("SUBSTRATE",     "BGA Package Substrate",     "Package",    8000, "EA",  "WH-PKG"),
    ("BOND-WIRE",     "Gold Bond Wire",            "Package",   50000, "M",   "WH-PKG"),
    ("MOLD-EMC",      "Epoxy Mold Compound",       "Package",     600, "KG",  "WH-PKG"),
    ("SOLDER-BALL",   "SnAg Solder Ball",          "Package",   90000, "EA",  "WH-PKG"),
]

# BOM template shared by all products: (step_code, material_code, qty_per_wafer, uom)
FAB_BOM = [
    ("DIFF",  "RAW-WAFER-300", 1.0,  "EA"),
    ("DIFF",  "GAS-AR",        0.05, "BTL"),
    ("PHOTO", "PR-EUV",        0.20, "L"),
    ("ETCH",  "GAS-AR",        0.10, "BTL"),
    ("IMPL",  "DOPANT-B",      0.08, "L"),
    ("CVD",   "GAS-SIH4",      0.15, "BTL"),
    ("CMP",   "SLURRY-CMP",    0.25, "L"),
]
PKG_BOM = [
    ("PKG", "SUBSTRATE",   1.0,  "EA"),
    ("PKG", "BOND-WIRE",   40.0, "M"),
    ("PKG", "MOLD-EMC",    0.02, "KG"),
    ("PKG", "SOLDER-BALL", 80.0, "EA"),
]

PRIORITIES = ["Hot", "Normal", "Normal", "Normal", "Low"]
OPERATORS = ["kim.js", "lee.mh", "park.sy", "choi.dw", "jung.hy"]
DEFECTS = ["Particle", "Scratch", "Overlay", "CD-OOS", "Etch-Residue", "Contamination"]

FAB_STEPS = [r[1] for r in ROUTE if r[5] == "FAB"]  # DIFF..TEST in order
STEP_EQP_TYPE = {code: eqp_type for _, code, _, _, eqp_type, _ in ROUTE}
EQP_BY_TYPE: dict[str, list[str]] = {}
for _eid, _name, _type, _status in EQUIPMENT:
    EQP_BY_TYPE.setdefault(_type, []).append(_eid)


def _pick_eqp(step_code: str, rnd: random.Random):
    choices = EQP_BY_TYPE.get(STEP_EQP_TYPE.get(step_code), [])
    return rnd.choice(choices) if choices else None


def _scrap_for(step_code: str, in_qty: int, rnd: random.Random) -> int:
    if in_qty <= 0 or rnd.random() < 0.6:
        return 0
    high = step_code in ("ETCH", "IMPL", "TEST")
    return min(in_qty, rnd.randint(1, 3 if high else 2))


def _insert_master() -> None:
    with db.get_conn() as conn:
        conn.executemany("INSERT INTO product VALUES (?,?,?)", PRODUCTS)
        conn.executemany(
            "INSERT INTO process_step (seq, step_code, step_name, operation, eqp_type, stage)"
            " VALUES (?,?,?,?,?,?)",
            ROUTE,
        )
        conn.executemany("INSERT INTO equipment VALUES (?,?,?,?)", EQUIPMENT)
        conn.executemany(
            "INSERT INTO material (material_code, material_name, category, qty, uom, location)"
            " VALUES (?,?,?,?,?,?)",
            MATERIALS,
        )
        bom_rows = []
        for pcode, _pn, _tn in PRODUCTS:
            for step, mat, qpw, uom in FAB_BOM + PKG_BOM:
                bom_rows.append((pcode, step, mat, qpw, uom))
            # one product-specific metal usage to vary the BOM
            bom_rows.append((pcode, "CVD", "TARGET-CU", 0.02, "EA"))
        conn.executemany(
            "INSERT INTO bom (product_code, step_code, material_code, qty_per_wafer, uom)"
            " VALUES (?,?,?,?,?)",
            bom_rows,
        )


def _advance(lot_id: str, upto_idx: int, rnd: random.Random) -> None:
    """Move a lot through FAB_STEPS[0..upto_idx] inclusive, with occasional scrap."""
    for idx in range(upto_idx + 1):
        step = FAB_STEPS[idx]
        lot = db.get_lot(lot_id)
        in_qty = lot["wafer_qty"]
        scrap = _scrap_for(step, in_qty, rnd)
        defect = rnd.choice(DEFECTS) if scrap > 0 else None
        result = "Pass" if rnd.random() > 0.05 else rnd.choice(["Rework", "Fail"])
        # ensure the final TEST step passes so Done lots produce SEMI
        if step == "TEST":
            result = "Pass"
        db.register_process_result(
            lot_id, step, eqp_id=_pick_eqp(step, rnd), in_qty=in_qty,
            scrap_qty=scrap, defect_code=defect,
            operator=rnd.choice(OPERATORS), result=result,
        )


def seed() -> None:
    rnd = random.Random(42)
    db.reset_db()
    _insert_master()

    done_products: list[str] = []
    for i in range(1, 17):
        pcode, _pn, _tn = PRODUCTS[(i - 1) % len(PRODUCTS)]
        start_qty = rnd.choice([25, 25, 25, 24, 12])
        priority = rnd.choice(PRIORITIES)
        lot = db.start_lot(pcode, start_qty, priority=priority)
        lot_id = lot["lot_id"]
        if i <= 6:                       # Done: full FAB route (TEST passes -> SEMI)
            _advance(lot_id, len(FAB_STEPS) - 1, rnd)
            done_products.append(pcode)
        elif i <= 8:                     # Hold: partway then held
            _advance(lot_id, rnd.randint(1, 5), rnd)
            with db.get_conn() as conn:
                conn.execute("UPDATE lot SET status='Hold' WHERE lot_id=?", (lot_id,))
        else:                            # Running: partway
            _advance(lot_id, rnd.randint(0, 6), rnd)

    # Package 3 of the completed products (consume SEMI -> FIN)
    for pcode in done_products[:3]:
        semi = db.list_product_inventory(product_code=pcode, item_type="SEMI")
        avail = int(semi[0]["qty"]) if semi else 0
        if avail > 0:
            db.package(pcode, max(1, avail // 2), scrap_qty=rnd.randint(0, 1),
                       eqp_id="EQP-PKG01", operator=rnd.choice(OPERATORS))

    # One manual FIN result against a Done lot
    done_lots = db.list_lots(status="Done")
    if done_lots:
        db.register_product_result(done_lots[0]["lot_id"], "FIN", rnd.randint(100, 400))


def main() -> None:
    path = db.get_db_path()
    seed()
    c = db.counts()
    print(f"[seed] mock MES database ready at {path}")
    print("[seed] rows: " + ", ".join(f"{k}={v}" for k, v in c.items()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest mes_core.test_db.SeedTests -v`
Expected: PASS (4 tests). Also run the whole db suite: `python -m unittest mes_core.test_db -v` → all PASS.

Sanity-run the seed against a scratch DB:
Run: `MES_DB_PATH=$(pwd)/data/mes.db python -m mes_core.seed`
Expected: prints `rows: ... lot=16 ... process_result=~80 ...`.

- [ ] **Step 5: Commit**

```bash
git add mes_core/seed.py mes_core/test_db.py
git commit -m "feat(seed): deterministic two-stage fab dataset with BOM + inventories

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87"
```

---

### Task 9: MCP surface — "Process & Lot" (queries + transactions)

Rewrite the `@mcp.tool` set in `mcp_server/server.py`. KEEP verbatim: the `FastMCP(...)` constructor block (lines ~11–17), `main()`, `DEFAULT_API_KEY`, `_ApiKeyGuard`, `build_asgi_app()`. Only the tool functions and `__all__` change.

**Files:**
- Modify: `mcp_server/server.py` (replace tool functions + `__all__`)
- Test: `mcp_server/test_server.py` (REWRITE)

**Interfaces:**
- Consumes: `db.start_lot`, `db.register_process_result`, `db.get_lot`, `db.list_lots`, `db.get_process_route`, `db.list_process_results`, `db.get_wip`.
- Produces MCP tools (transactions return `{"error": ...}` on `ValueError`): `start_lot`, `register_process_result`, `get_lot`, `list_lots`, `get_process_route`, `list_process_results`, `get_wip`.

- [ ] **Step 1: Write the failing test**

Replace the entire contents of `mcp_server/test_server.py`:

```python
import asyncio
import importlib
import os
import subprocess
import sys
import unittest
from pathlib import Path

TEST_DB = Path(__file__).resolve().parents[1] / "data" / "mes_mcp_unit_test.db"


class MCPToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = str(TEST_DB)
        TEST_DB.parent.mkdir(exist_ok=True)
        subprocess.run([sys.executable, "-m", "mes_core.seed"], check=True,
                       cwd=Path(__file__).resolve().parents[1])
        from mes_core import db
        from mcp_server import server
        importlib.reload(db)
        importlib.reload(server)
        cls.db, cls.server = db, server

    @classmethod
    def tearDownClass(cls):
        for f in TEST_DB.parent.glob(TEST_DB.name + "*"):
            f.unlink(missing_ok=True)

    def test_start_lot_then_process_result(self):
        s, db = self.server, self.db
        lot = s.start_lot("LX9", 25, priority="Hot")
        self.assertNotIn("error", lot)
        lot_id = lot["lot_id"]
        res = s.register_process_result(lot_id, "PHOTO", scrap_qty=2)
        self.assertEqual(res["out_qty"], 23)
        self.assertEqual(s.get_lot(lot_id)["current_step"], "PHOTO")
        self.assertTrue(any(r["id"] == res["id"] for r in s.list_process_results(lot_id=lot_id)))

    def test_final_test_pass_closes_lot(self):
        s = self.server
        lot = s.start_lot("DDR5", 20)
        res = s.register_process_result(lot["lot_id"], "TEST", in_qty=20, result="Pass")
        self.assertIsNotNone(res["semi_receipt"])
        self.assertEqual(s.get_lot(lot["lot_id"])["status"], "Done")

    def test_bad_transaction_returns_error(self):
        self.assertIn("error", self.server.start_lot("NOPE", 25))

    def test_queries(self):
        s = self.server
        self.assertEqual(len(s.get_process_route(stage="FAB")), 8)
        self.assertTrue(len(s.list_lots(limit=5)) <= 5)
        self.assertTrue(all(w["lot_count"] > 0 for w in s.get_wip()))

    def test_api_key_guard(self):
        async def dummy(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        guard = self.server._ApiKeyGuard(dummy)
        os.environ["MES_API_KEY"] = "changjuahn"

        async def run(headers):
            sent = []
            async def send(m): sent.append(m)
            async def receive(): return {"type": "http.request"}
            await guard({"type": "http", "headers": headers}, receive, send)
            return sent[0]["status"]

        self.assertEqual(asyncio.run(run([])), 401)
        self.assertEqual(asyncio.run(run([(b"x-api-key", b"changjuahn")])), 200)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest mcp_server.test_server -v`
Expected: FAIL — `server.start_lot` missing / old `register_process_move` referenced.

- [ ] **Step 3: Write the implementation**

In `mcp_server/server.py`, replace everything BETWEEN the `FastMCP(...)` block (keep lines 1–17) and `def main()` (keep from `def main()` onward: main, DEFAULT_API_KEY, _ApiKeyGuard, build_asgi_app) with these tools, and update `__all__`:

```python
@mcp.tool(
    description=(
        "로트 투입(start_lot): create a new FAB lot for a product. Starts at the "
        "first FAB step, status Running, wafer_qty = start_qty. Returns the lot."
    )
)
def start_lot(product_code: str, start_qty: int, priority: str = "Normal") -> dict[str, Any]:
    try:
        return db.start_lot(product_code, start_qty, priority=priority)
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool(
    description=(
        "공정실적 입력(register_process_result): record a lot's move through a "
        "process step. in_qty defaults to the lot's current wafer_qty; out = in - "
        "scrap. Consumes BOM materials for (product, step) and, on a passing final "
        "FAB step (TEST), closes the lot and auto-receives SEMI stock. Returns the "
        "process result plus 'shortages' and 'semi_receipt'."
    )
)
def register_process_result(
    lot_id: str,
    step_code: str,
    eqp_id: str | None = None,
    in_qty: int | None = None,
    scrap_qty: int = 0,
    defect_code: str | None = None,
    operator: str | None = None,
    result: str = "Pass",
) -> dict[str, Any]:
    try:
        return db.register_process_result(
            lot_id=lot_id, step_code=step_code, eqp_id=eqp_id, in_qty=in_qty,
            scrap_qty=scrap_qty, defect_code=defect_code, operator=operator, result=result,
        )
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool(description="로트 조회(one): return a lot with current step, wafer qty, cumulative_yield, and process history.")
def get_lot(lot_id: str) -> dict[str, Any]:
    lot = db.get_lot(lot_id)
    if lot is None:
        return {"not_found": f"Lot '{lot_id}' was not found."}
    return lot


@mcp.tool(description="로트 조회(list): matching lots filtered by status, product_code, current_step, priority, or tech_node.")
def list_lots(
    status: str | None = None,
    product_code: str | None = None,
    current_step: str | None = None,
    priority: str | None = None,
    tech_node: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return db.list_lots(
        status=status, product_code=product_code, current_step=current_step,
        priority=priority, tech_node=tech_node, limit=limit,
    )


@mcp.tool(description="공정 조회(route): the process route (FAB + PACK steps), optionally filtered by step_code, eqp_type, or stage.")
def get_process_route(
    step_code: str | None = None,
    eqp_type: str | None = None,
    stage: str | None = None,
) -> list[dict[str, Any]]:
    return db.get_process_route(step_code=step_code, eqp_type=eqp_type, stage=stage)


@mcp.tool(
    description=(
        "공정실적 조회(list): process result rows. Filter by lot_id, step_code, "
        "result, operator, defect_code, or has_scrap (True = only moves with scrap)."
    )
)
def list_process_results(
    lot_id: str | None = None,
    step_code: str | None = None,
    result: str | None = None,
    operator: str | None = None,
    defect_code: str | None = None,
    has_scrap: bool | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return db.list_process_results(
        lot_id=lot_id, step_code=step_code, result=result, operator=operator,
        defect_code=defect_code, has_scrap=has_scrap, limit=limit,
    )


@mcp.tool(description="재공 조회(WIP): non-Done lots grouped by current process step.")
def get_wip(step_code: str | None = None) -> list[dict[str, Any]]:
    return db.get_wip(step_code=step_code)
```

Update `__all__`:

```python
__all__ = [
    "DEFAULT_API_KEY",
    "build_asgi_app",
    "get_lot",
    "get_process_route",
    "get_wip",
    "list_lots",
    "list_process_results",
    "main",
    "mcp",
    "register_process_result",
    "start_lot",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest mcp_server.test_server -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add mcp_server/server.py mcp_server/test_server.py
git commit -m "feat(mcp): Process & Lot tools (start_lot, process result, queries)

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87"
```

---

### Task 10: REST surface — "Product · Material · BOM" (queries + transactions)

Rewrite `api/rest.py`. KEEP `router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])`. New Pydantic models + endpoints. Also rewrite `api/tests/test_app.py` with REST + auth tests (Task 11 appends web tests).

**Files:**
- Rewrite: `api/rest.py`
- Rewrite: `api/tests/test_app.py`

**Interfaces:**
- Consumes: `db.list_products`, `db.list_product_inventory`, `db.list_product_results`, `db.register_product_result`, `db.package`, `db.list_materials`, `db.materials_by_step`, `db.receive_material`, `db.list_bom`, `db.upsert_bom`, `db.delete_bom`, `db.counts`, `db.get_db_path`.
- Produces endpoints: `GET /api` + `/api/health` + `/api/products` + `/api/product-inventory` + `/api/product-results` (GET/POST) + `/api/packaging` (POST) + `/api/materials` + `/api/materials/by-step` + `/api/materials/receipt` (POST) + `/api/bom` (GET/PUT) + `/api/bom/{bom_id}` (DELETE).

- [ ] **Step 1: Write the failing test**

Replace the entire contents of `api/tests/test_app.py`:

```python
import os
import unittest

from fastapi.testclient import TestClient

KEY = {"X-API-Key": "changjuahn"}


class RestApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = os.path.join(os.getcwd(), "data", "mes_api_unittest.db")
        from mes_core import db, seed
        db.reset_db()
        seed.seed()
        from api.main import app
        cls.db = db
        cls.client = TestClient(app, headers=KEY)
        cls.open_client = TestClient(app)  # no key

    @classmethod
    def tearDownClass(cls):
        for suffix in ("", "-shm", "-wal"):
            path = os.environ["MES_DB_PATH"] + suffix
            if os.path.exists(path):
                os.remove(path)

    # --- auth gate --------------------------------------------------------- #
    def test_api_requires_key(self):
        self.assertEqual(self.open_client.get("/api/products").status_code, 401)
        self.assertEqual(self.client.get("/api/products").status_code, 200)

    def test_docs_and_web_open(self):
        self.assertEqual(self.open_client.get("/api/openapi.json").status_code, 200)
        self.assertEqual(self.open_client.get("/api/docs").status_code, 200)
        self.assertEqual(self.open_client.get("/").status_code, 200)

    def test_openapi_lists_new_paths(self):
        paths = self.open_client.get("/api/openapi.json").json()["paths"]
        for p in ("/api/products", "/api/product-inventory", "/api/product-results",
                  "/api/packaging", "/api/materials", "/api/materials/by-step",
                  "/api/materials/receipt", "/api/bom"):
            self.assertIn(p, paths)
        self.assertNotIn("/", paths)

    # --- queries ----------------------------------------------------------- #
    def test_products_and_inventory(self):
        self.assertEqual(len(self.client.get("/api/products").json()), 4)
        inv = self.client.get("/api/product-inventory", params={"item_type": "SEMI"}).json()
        self.assertTrue(all(r["item_type"] == "SEMI" for r in inv))

    def test_materials_and_by_step(self):
        mats = self.client.get("/api/materials", params={"category": "Gas"}).json()
        self.assertTrue(all(r["category"] == "Gas" for r in mats))
        by_step = self.client.get("/api/materials/by-step", params={"step_code": "PHOTO"}).json()
        self.assertTrue(all(r["step_code"] == "PHOTO" for r in by_step))

    # --- transactions ------------------------------------------------------ #
    def test_manual_product_result(self):
        lot = self.client.get("/api/product-results", params={"limit": 1}).json()
        # need a real lot id: pull one Done lot via materials-independent query
        from mes_core import db
        lot_id = db.list_lots(status="Done")[0]["lot_id"]
        r = self.client.post("/api/product-results",
                             json={"lot_id": lot_id, "item_type": "FIN", "good_qty": 50})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["source"], "MANUAL")

    def test_packaging_then_inventory(self):
        from mes_core import db
        semi = db.list_product_inventory(item_type="SEMI")
        pcode = next(r["product_code"] for r in semi if r["qty"] >= 2)
        r = self.client.post("/api/packaging", json={"product_code": pcode, "in_qty": 2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["item_type"], "FIN")

    def test_packaging_insufficient_semi_is_400(self):
        r = self.client.post("/api/packaging", json={"product_code": "LX9", "in_qty": 999999})
        self.assertEqual(r.status_code, 400)

    def test_material_receipt(self):
        r = self.client.post("/api/materials/receipt",
                             json={"material_code": "PR-EUV", "qty": 10})
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.json()["qty"], 10)

    def test_bom_upsert_and_delete(self):
        r = self.client.put("/api/bom", json={
            "product_code": "LX9", "step_code": "METRO",
            "material_code": "GAS-AR", "qty_per_wafer": 0.01})
        self.assertEqual(r.status_code, 200)
        bom_id = r.json()["id"]
        self.assertEqual(self.client.delete(f"/api/bom/{bom_id}").status_code, 200)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest api.tests.test_app -v`
Expected: FAIL — new endpoints/`db` functions not wired (old rest.py).

- [ ] **Step 3: Write the implementation**

Replace the entire contents of `api/rest.py`:

```python
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.auth import require_api_key
from mes_core import db

router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class ApiIndex(BaseModel):
    name: str
    docs: str
    endpoints: list[str]


class HealthResponse(BaseModel):
    status: str
    db: str
    counts: dict[str, int]


class ProductRow(BaseModel):
    product_code: str
    product_name: str
    tech_node: str | None = None


class ProductInventoryRow(BaseModel):
    product_code: str
    product_name: str | None = None
    item_type: str
    qty: float
    uom: str | None = None
    location: str | None = None


class ProductResultRow(BaseModel):
    id: int
    result_date: str
    lot_id: str | None = None
    product_code: str
    product_name: str | None = None
    item_type: str
    good_qty: int | None = None
    scrap_qty: int | None = None
    yield_pct: float | None = None
    source: str | None = None
    line: str | None = None
    eqp_id: str | None = None


class MaterialRow(BaseModel):
    material_code: str
    material_name: str
    category: str | None = None
    qty: float
    uom: str | None = None
    location: str | None = None


class MaterialByStepRow(BaseModel):
    product_code: str
    product_name: str | None = None
    step_code: str
    step_name: str | None = None
    material_code: str
    material_name: str | None = None
    qty_per_wafer: float
    on_hand_qty: float
    uom: str | None = None
    location: str | None = None


class BomRow(BaseModel):
    id: int
    product_code: str
    step_code: str
    step_name: str | None = None
    material_code: str
    material_name: str | None = None
    qty_per_wafer: float
    uom: str | None = None


class ProductResultCreate(BaseModel):
    lot_id: str
    item_type: Literal["SEMI", "FIN"]
    good_qty: int = Field(ge=0)
    scrap_qty: int = Field(default=0, ge=0)


class PackagingCreate(BaseModel):
    product_code: str
    in_qty: int = Field(gt=0)
    scrap_qty: int = Field(default=0, ge=0)
    lot_id: str | None = None
    eqp_id: str | None = None
    operator: str | None = None


class MaterialReceipt(BaseModel):
    material_code: str
    qty: float = Field(ge=0)
    material_name: str | None = None
    category: str | None = None
    uom: str | None = None
    location: str | None = None


class BomUpsert(BaseModel):
    product_code: str
    step_code: str
    material_code: str
    qty_per_wafer: float = Field(ge=0)
    uom: str | None = None


# --------------------------------------------------------------------------- #
# Index / health
# --------------------------------------------------------------------------- #

@router.get("", response_model=ApiIndex, tags=["Index"])
@router.get("/", response_model=ApiIndex, tags=["Index"], include_in_schema=False)
def api_index() -> dict[str, Any]:
    return {
        "name": "Mock MES REST API (Product · Material · BOM)",
        "docs": "/api/docs",
        "endpoints": [
            "GET /api/health",
            "GET /api/products",
            "GET /api/product-inventory",
            "GET|POST /api/product-results",
            "POST /api/packaging",
            "GET /api/materials",
            "GET /api/materials/by-step",
            "POST /api/materials/receipt",
            "GET|PUT /api/bom",
            "DELETE /api/bom/{bom_id}",
        ],
    }


@router.get("/health", response_model=HealthResponse, tags=["Health"])
def health() -> dict[str, Any]:
    return {"status": "ok", "db": db.get_db_path(), "counts": db.counts()}


@router.get("/products", response_model=list[ProductRow], tags=["Product"])
def get_products() -> list[dict[str, Any]]:
    return db.list_products()


# --------------------------------------------------------------------------- #
# 제품 인벤토리 / 제품실적
# --------------------------------------------------------------------------- #

@router.get("/product-inventory", response_model=list[ProductInventoryRow], tags=["Product"])
def get_product_inventory(
    product_code: str | None = None,
    item_type: Literal["SEMI", "FIN"] | None = None,
) -> list[dict[str, Any]]:
    return db.list_product_inventory(product_code=product_code, item_type=item_type)


@router.get("/product-results", response_model=list[ProductResultRow], tags=["Product"])
def get_product_results(
    product_code: str | None = None,
    item_type: Literal["SEMI", "FIN"] | None = None,
    source: Literal["AUTO_FAB", "AUTO_PACK", "MANUAL"] | None = None,
    lot_id: str | None = None,
    date_from: str | None = Query(default=None, description="YYYY-MM-DD inclusive"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD inclusive"),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    return db.list_product_results(
        product_code=product_code, item_type=item_type, source=source, lot_id=lot_id,
        date_from=date_from, date_to=date_to, limit=limit,
    )


@router.post("/product-results", tags=["Product"])
def create_product_result(payload: ProductResultCreate) -> dict[str, Any]:
    try:
        return db.register_product_result(
            lot_id=payload.lot_id, item_type=payload.item_type,
            good_qty=payload.good_qty, scrap_qty=payload.scrap_qty,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/packaging", tags=["Product"])
def create_packaging(payload: PackagingCreate) -> dict[str, Any]:
    try:
        return db.package(
            product_code=payload.product_code, in_qty=payload.in_qty,
            scrap_qty=payload.scrap_qty, lot_id=payload.lot_id,
            eqp_id=payload.eqp_id, operator=payload.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------------------------------- #
# 자재 인벤토리 (material)
# --------------------------------------------------------------------------- #

@router.get("/materials", response_model=list[MaterialRow], tags=["Material"])
def get_materials(
    category: str | None = None,
    location: str | None = None,
    q: str | None = Query(default=None, description="search material_code / material_name"),
    min_qty: float | None = Query(default=None, ge=0),
    max_qty: float | None = Query(default=None, ge=0),
) -> list[dict[str, Any]]:
    return db.list_materials(category=category, location=location, q=q,
                             min_qty=min_qty, max_qty=max_qty)


@router.get("/materials/by-step", response_model=list[MaterialByStepRow], tags=["Material"])
def get_materials_by_step(step_code: str | None = None) -> list[dict[str, Any]]:
    """공정별 자재 소요/잔량 (BOM ⋈ material)."""
    return db.materials_by_step(step_code=step_code)


@router.post("/materials/receipt", response_model=MaterialRow, tags=["Material"])
def create_material_receipt(payload: MaterialReceipt) -> dict[str, Any]:
    try:
        return db.receive_material(
            material_code=payload.material_code, qty=payload.qty,
            material_name=payload.material_name, category=payload.category,
            uom=payload.uom, location=payload.location,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------------------------------- #
# BOM
# --------------------------------------------------------------------------- #

@router.get("/bom", response_model=list[BomRow], tags=["BOM"])
def get_bom(product_code: str | None = None, step_code: str | None = None) -> list[dict[str, Any]]:
    return db.list_bom(product_code=product_code, step_code=step_code)


@router.put("/bom", response_model=BomRow, tags=["BOM"])
def put_bom(payload: BomUpsert) -> dict[str, Any]:
    try:
        return db.upsert_bom(
            product_code=payload.product_code, step_code=payload.step_code,
            material_code=payload.material_code, qty_per_wafer=payload.qty_per_wafer,
            uom=payload.uom,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/bom/{bom_id}", tags=["BOM"])
def remove_bom(bom_id: int) -> dict[str, Any]:
    if not db.delete_bom(bom_id):
        raise HTTPException(status_code=404, detail=f"bom {bom_id} not found")
    return {"deleted": bom_id}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest api.tests.test_app -v`
Expected: PASS (RestApiTests, ~11 tests).

- [ ] **Step 5: Commit**

```bash
git add api/rest.py api/tests/test_app.py
git commit -m "feat(rest): Product/Material/BOM API (queries + transactions)

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87"
```

---

### Task 11: Web console — routes + templates (all functions)

Rewrite `api/web.py` and the templates. KEEP `router = APIRouter(include_in_schema=False)`, `templates`, `_blank_to_none`, `_int_or_none`, `_context`. Add small read helpers `list_equipment()` / `list_step_codes()` to `db.py` (web dropdowns). Delete `templates/inventory.html` and `templates/production.html`. The `/guide` route + template come in Task 12.

**Files:**
- Modify: `mes_core/db.py` (add `list_equipment`, `list_step_codes`)
- Rewrite: `api/web.py`
- Rewrite: `api/templates/base.html`, `dashboard.html`, `lots.html`, `lot_detail.html`, `process.html`, `wip.html`
- Create: `api/templates/product_inventory.html`, `product_results.html`, `materials.html`, `bom.html`, `equipment.html`
- Delete: `api/templates/inventory.html`, `api/templates/production.html`
- Modify: `api/static/styles.css` (append utility classes)
- Modify: `api/tests/test_app.py` (append `WebConsoleTests`)

**Interfaces:**
- Consumes: all `db` reads/transactions.
- Produces (db): `list_equipment() -> list[dict]`, `list_step_codes() -> list[str]`.
- Produces (web routes): `/`, `/lots` (+ `POST /lots/start`), `/lots/{lot_id}`, `/process` (+ `POST /process/results`), `/product-inventory` (+ `POST /packaging`), `/product-results` (+ `POST /product-results`), `/materials` (+ `POST /materials/receive`), `/bom` (+ `POST /bom`, `POST /bom/{bom_id}/delete`), `/wip`, `/equipment`.

- [ ] **Step 1: Write the failing test**

Append to `api/tests/test_app.py`:

```python
class WebConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = os.path.join(os.getcwd(), "data", "mes_web_unittest.db")
        from mes_core import db, seed
        db.reset_db()
        seed.seed()
        from api.main import app
        cls.db = db
        cls.client = TestClient(app)  # web is open (no key)

    @classmethod
    def tearDownClass(cls):
        for suffix in ("", "-shm", "-wal"):
            path = os.environ["MES_DB_PATH"] + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_all_pages_render(self):
        for path in ("/", "/lots", "/process", "/product-inventory",
                     "/product-results", "/materials", "/bom", "/wip", "/equipment"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_lot_detail_renders(self):
        lot_id = self.db.list_lot_ids()[0]
        self.assertEqual(self.client.get(f"/lots/{lot_id}").status_code, 200)

    def test_start_lot_form(self):
        before = len(self.db.list_lot_ids())
        r = self.client.post("/lots/start",
                             data={"product_code": "LX9", "start_qty": "25", "priority": "Hot"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(len(self.db.list_lot_ids()), before + 1)

    def test_process_result_form(self):
        lot = self.db.start_lot("DDR5", 20)
        r = self.client.post("/process/results",
                             data={"lot_id": lot["lot_id"], "step_code": "PHOTO", "scrap_qty": "1"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.db.get_lot(lot["lot_id"])["current_step"], "PHOTO")

    def test_material_receive_form(self):
        r = self.client.post("/materials/receive",
                             data={"material_code": "PR-EUV", "qty": "5"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    def test_bom_upsert_and_delete_form(self):
        r = self.client.post("/bom", data={
            "product_code": "LX9", "step_code": "METRO",
            "material_code": "GAS-AR", "qty_per_wafer": "0.02"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        bom_id = self.db.list_bom(product_code="LX9", step_code="METRO")[0]["id"]
        r2 = self.client.post(f"/bom/{bom_id}/delete", follow_redirects=False)
        self.assertEqual(r2.status_code, 303)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest api.tests.test_app.WebConsoleTests -v`
Expected: FAIL — pages 404 / template errors (old web.py).

- [ ] **Step 3a: Add db read helpers**

Append to `mes_core/db.py`:

```python
def list_equipment() -> list[dict[str, Any]]:
    with get_conn() as conn:
        return _rows(conn.execute("SELECT * FROM equipment ORDER BY eqp_id"))


def list_step_codes() -> list[str]:
    with get_conn() as conn:
        return [r[0] for r in conn.execute("SELECT step_code FROM process_step ORDER BY seq")]
```

- [ ] **Step 3b: Rewrite `api/web.py`**

```python
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette import status

from mes_core import db

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(include_in_schema=False)

DEFECT_CODES = ["Particle", "Scratch", "Overlay", "CD-OOS", "Etch-Residue", "Contamination"]
_SEE_OTHER = status.HTTP_303_SEE_OTHER


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _int_or_none(value: str | None) -> int | None:
    value = _blank_to_none(value)
    return int(value) if value is not None else None


def _context(request: Request, **extra: Any) -> dict[str, Any]:
    return {"request": request, **extra}


def _redirect(path: str, *, message: str | None = None, error: str | None = None):
    q = []
    if message:
        q.append(f"message={message}")
    if error:
        q.append(f"error={error}")
    sep = "?" if q else ""
    return RedirectResponse(path + sep + "&".join(q), status_code=_SEE_OTHER)


def _shortage_note(shortages: list[dict[str, Any]]) -> str:
    if not shortages:
        return ""
    names = ", ".join(f"{s['material_code']}(-{s['short']})" for s in shortages)
    return f" (material shortage: {names})".replace(" ", "+")


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

@router.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(
        request, "dashboard.html", _context(request, summary=db.get_dashboard_summary()))


# --------------------------------------------------------------------------- #
# 로트 (lots) + 로트 투입
# --------------------------------------------------------------------------- #

@router.get("/lots")
def lots(request: Request, status_filter: str | None = None, product_code: str | None = None,
         current_step: str | None = None, message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "lots.html", _context(
        request,
        rows=db.list_lots(status=_blank_to_none(status_filter),
                          product_code=_blank_to_none(product_code),
                          current_step=_blank_to_none(current_step)),
        products=db.list_products(),
        steps=db.get_process_route(),
        filters={"status_filter": status_filter or "", "product_code": product_code or "",
                 "current_step": current_step or ""},
        message=message, error=error,
    ))


@router.post("/lots/start")
def lots_start(product_code: str = Form(...), start_qty: int = Form(...),
               priority: str = Form("Normal")):
    try:
        lot = db.start_lot(product_code, start_qty, priority=priority)
    except (ValueError, Exception) as exc:
        return _redirect("/lots", error=str(exc))
    return _redirect("/lots", message=f"Lot+{lot['lot_id']}+started")


@router.get("/lots/{lot_id}")
def lot_detail(request: Request, lot_id: str):
    lot = db.get_lot(lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")
    return templates.TemplateResponse(request, "lot_detail.html", _context(request, lot=lot))


# --------------------------------------------------------------------------- #
# 공정실적 (process results)
# --------------------------------------------------------------------------- #

@router.get("/process")
def process(request: Request, message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "process.html", _context(
        request,
        route=db.get_process_route(),
        results=db.list_process_results(limit=100),
        lot_ids=db.list_lot_ids(),
        steps=db.list_step_codes(),
        equipment=db.list_equipment(),
        defect_codes=DEFECT_CODES,
        message=message, error=error,
    ))


@router.post("/process/results")
def process_result(lot_id: str = Form(...), step_code: str = Form(...),
                   eqp_id: str | None = Form(None), in_qty: str | None = Form(None),
                   scrap_qty: str | None = Form("0"), defect_code: str | None = Form(None),
                   operator: str | None = Form(None), result: str = Form("Pass")):
    try:
        res = db.register_process_result(
            lot_id=lot_id, step_code=step_code, eqp_id=_blank_to_none(eqp_id),
            in_qty=_int_or_none(in_qty), scrap_qty=_int_or_none(scrap_qty) or 0,
            defect_code=_blank_to_none(defect_code), operator=_blank_to_none(operator),
            result=result,
        )
    except ValueError as exc:
        return _redirect("/process", error=str(exc))
    note = "Process+result+recorded" + _shortage_note(res["shortages"])
    if res["semi_receipt"]:
        note += "+|+SEMI+received"
    return _redirect("/process", message=note)


# --------------------------------------------------------------------------- #
# 제품 인벤토리 + 패키징
# --------------------------------------------------------------------------- #

@router.get("/product-inventory")
def product_inventory(request: Request, message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "product_inventory.html", _context(
        request,
        rows=db.list_product_inventory(),
        products=db.list_products(),
        lot_ids=db.list_lot_ids(),
        equipment=db.list_equipment(),
        message=message, error=error,
    ))


@router.post("/packaging")
def packaging(product_code: str = Form(...), in_qty: int = Form(...),
              scrap_qty: str | None = Form("0"), lot_id: str | None = Form(None),
              eqp_id: str | None = Form(None), operator: str | None = Form(None)):
    try:
        res = db.package(product_code=product_code, in_qty=in_qty,
                         scrap_qty=_int_or_none(scrap_qty) or 0, lot_id=_blank_to_none(lot_id),
                         eqp_id=_blank_to_none(eqp_id), operator=_blank_to_none(operator))
    except ValueError as exc:
        return _redirect("/product-inventory", error=str(exc))
    return _redirect("/product-inventory",
                     message="Packaged+" + str(res["good_qty"]) + "+FIN" + _shortage_note(res["shortages"]))


# --------------------------------------------------------------------------- #
# 제품실적 (product results) + 수동 입력
# --------------------------------------------------------------------------- #

@router.get("/product-results")
def product_results(request: Request, product_code: str | None = None,
                    item_type: str | None = None, source: str | None = None,
                    message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "product_results.html", _context(
        request,
        rows=db.list_product_results(product_code=_blank_to_none(product_code),
                                     item_type=_blank_to_none(item_type),
                                     source=_blank_to_none(source), limit=200),
        products=db.list_products(),
        lot_ids=db.list_lot_ids(),
        filters={"product_code": product_code or "", "item_type": item_type or "",
                 "source": source or ""},
        message=message, error=error,
    ))


@router.post("/product-results")
def product_result_create(lot_id: str = Form(...), item_type: str = Form(...),
                          good_qty: int = Form(...), scrap_qty: str | None = Form("0")):
    try:
        db.register_product_result(lot_id=lot_id, item_type=item_type,
                                   good_qty=good_qty, scrap_qty=_int_or_none(scrap_qty) or 0)
    except ValueError as exc:
        return _redirect("/product-results", error=str(exc))
    return _redirect("/product-results", message="Product+result+recorded")


# --------------------------------------------------------------------------- #
# 자재 인벤토리 (materials) + 입고 + 공정별 소요
# --------------------------------------------------------------------------- #

@router.get("/materials")
def materials(request: Request, category: str | None = None, step_code: str | None = None,
              message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "materials.html", _context(
        request,
        rows=db.list_materials(category=_blank_to_none(category)),
        by_step=db.materials_by_step(step_code=_blank_to_none(step_code)),
        steps=db.list_step_codes(),
        filters={"category": category or "", "step_code": step_code or ""},
        message=message, error=error,
    ))


@router.post("/materials/receive")
def materials_receive(material_code: str = Form(...), qty: float = Form(...),
                      material_name: str | None = Form(None), category: str | None = Form(None),
                      uom: str | None = Form(None), location: str | None = Form(None)):
    try:
        db.receive_material(material_code=material_code, qty=qty,
                            material_name=_blank_to_none(material_name),
                            category=_blank_to_none(category), uom=_blank_to_none(uom),
                            location=_blank_to_none(location))
    except ValueError as exc:
        return _redirect("/materials", error=str(exc))
    return _redirect("/materials", message="Material+received")


# --------------------------------------------------------------------------- #
# BOM
# --------------------------------------------------------------------------- #

@router.get("/bom")
def bom(request: Request, product_code: str | None = None, step_code: str | None = None,
        message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "bom.html", _context(
        request,
        rows=db.list_bom(product_code=_blank_to_none(product_code),
                         step_code=_blank_to_none(step_code)),
        products=db.list_products(),
        steps=db.list_step_codes(),
        materials=db.list_materials(),
        filters={"product_code": product_code or "", "step_code": step_code or ""},
        message=message, error=error,
    ))


@router.post("/bom")
def bom_upsert(product_code: str = Form(...), step_code: str = Form(...),
               material_code: str = Form(...), qty_per_wafer: float = Form(...),
               uom: str | None = Form(None)):
    try:
        db.upsert_bom(product_code=product_code, step_code=step_code,
                      material_code=material_code, qty_per_wafer=qty_per_wafer,
                      uom=_blank_to_none(uom))
    except ValueError as exc:
        return _redirect("/bom", error=str(exc))
    return _redirect("/bom", message="BOM+saved")


@router.post("/bom/{bom_id}/delete")
def bom_delete(bom_id: int):
    db.delete_bom(bom_id)
    return _redirect("/bom", message="BOM+deleted")


# --------------------------------------------------------------------------- #
# 재공 (WIP) + 설비 (equipment)
# --------------------------------------------------------------------------- #

@router.get("/wip")
def wip(request: Request):
    return templates.TemplateResponse(request, "wip.html", _context(request, rows=db.get_wip()))


@router.get("/equipment")
def equipment(request: Request):
    return templates.TemplateResponse(request, "equipment.html",
                                      _context(request, rows=db.list_equipment()))
```

- [ ] **Step 3c: Rewrite `api/templates/base.html`**

```html
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Mock MES{% endblock %}</title>
  <link rel="stylesheet" href="/static/styles.css">
  <script src="https://unpkg.com/htmx.org@2.0.4"></script>
</head>
<body>
  <header class="topbar">
    <a class="brand" href="/">Mock MES</a>
    <nav>
      <a href="/">Dashboard</a>
      <a href="/lots">로트 Lots</a>
      <a href="/process">공정실적 Process</a>
      <a href="/product-inventory">제품재고 Product</a>
      <a href="/product-results">제품실적 Results</a>
      <a href="/materials">자재 Material</a>
      <a href="/bom">BOM</a>
      <a href="/wip">재공 WIP</a>
      <a href="/equipment">설비 Equip</a>
      <a href="/guide">사용법 Guide</a>
      <a href="/api/docs">API Docs</a>
    </nav>
  </header>
  <main class="container">
    {% if message %}<div class="alert success">{{ message }}</div>{% endif %}
    {% if error %}<div class="alert error">{{ error }}</div>{% endif %}
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

- [ ] **Step 3d: Rewrite `api/templates/dashboard.html`**

```html
{% extends "base.html" %}
{% block title %}Dashboard · Mock MES{% endblock %}
{% block content %}
<h1>Fab Dashboard</h1>
<section class="cards">
  <div class="card"><div class="kpi">{{ summary.lot_total }}</div><div class="label">Lots</div></div>
  <div class="card"><div class="kpi">{{ summary.wafers_started }}</div><div class="label">Wafers started</div></div>
  <div class="card"><div class="kpi">{{ summary.wip_total_lots }}</div><div class="label">WIP lots</div></div>
  <div class="card"><div class="kpi">{{ summary.semi_total|round(0) }}</div><div class="label">SEMI stock</div></div>
  <div class="card"><div class="kpi">{{ summary.fin_total|round(0) }}</div><div class="label">FIN stock</div></div>
  <div class="card"><div class="kpi">{{ summary.material_total_items }}</div><div class="label">Materials</div></div>
</section>

<div class="grid2">
  <section>
    <h2>WIP by step</h2>
    <table><thead><tr><th>Seq</th><th>Step</th><th>Stage</th><th>Lots</th><th>Wafers</th></tr></thead>
    <tbody>
    {% for w in summary.wip_by_step %}
      <tr><td>{{ w.seq }}</td><td>{{ w.step_code }} {{ w.step_name }}</td><td>{{ w.stage }}</td>
          <td>{{ w.lot_count }}</td><td>{{ w.wafer_qty }}</td></tr>
    {% else %}<tr><td colspan="5">No WIP</td></tr>{% endfor %}
    </tbody></table>
  </section>
  <section>
    <h2>Lots by status</h2>
    <table><thead><tr><th>Status</th><th>Count</th></tr></thead><tbody>
    {% for s in summary.lots_by_status %}<tr><td>{{ s.status }}</td><td>{{ s.n }}</td></tr>{% endfor %}
    </tbody></table>
    <h2>Top defects</h2>
    <table><thead><tr><th>Defect</th><th>Count</th></tr></thead><tbody>
    {% for d in summary.top_defects %}<tr><td>{{ d.defect_code }}</td><td>{{ d.n }}</td></tr>
    {% else %}<tr><td colspan="2">None</td></tr>{% endfor %}
    </tbody></table>
  </section>
</div>

<div class="grid2">
  <section>
    <h2>Low materials</h2>
    <table><thead><tr><th>Code</th><th>Name</th><th>Qty</th><th>UoM</th></tr></thead><tbody>
    {% for m in summary.low_materials %}<tr><td>{{ m.material_code }}</td><td>{{ m.material_name }}</td>
        <td>{{ m.qty|round(2) }}</td><td>{{ m.uom }}</td></tr>
    {% else %}<tr><td colspan="4">All stocked</td></tr>{% endfor %}
    </tbody></table>
  </section>
  <section>
    <h2>Equipment</h2>
    <table><thead><tr><th>ID</th><th>Name</th><th>Type</th><th>Status</th></tr></thead><tbody>
    {% for e in summary.equipment %}<tr><td>{{ e.eqp_id }}</td><td>{{ e.eqp_name }}</td>
        <td>{{ e.type }}</td><td>{{ e.status }}</td></tr>{% endfor %}
    </tbody></table>
  </section>
</div>

<section>
  <h2>Recent process results</h2>
  <table><thead><tr><th>ID</th><th>Lot</th><th>Step</th><th>In</th><th>Out</th><th>Scrap</th><th>Defect</th><th>Result</th></tr></thead><tbody>
  {% for r in summary.recent_process %}<tr><td>{{ r.id }}</td><td>{{ r.lot_id }}</td>
      <td>{{ r.step_code }}</td><td>{{ r.in_qty }}</td><td>{{ r.out_qty }}</td>
      <td>{{ r.scrap_qty }}</td><td>{{ r.defect_code or '' }}</td><td>{{ r.result }}</td></tr>{% endfor %}
  </tbody></table>
</section>

<section>
  <h2>Recent product results</h2>
  <table><thead><tr><th>Date</th><th>Lot</th><th>Product</th><th>Type</th><th>Good</th><th>Yield</th><th>Source</th></tr></thead><tbody>
  {% for r in summary.recent_products %}<tr><td>{{ r.result_date }}</td><td>{{ r.lot_id or '' }}</td>
      <td>{{ r.product_code }}</td><td>{{ r.item_type }}</td><td>{{ r.good_qty }}</td>
      <td>{{ r.yield_pct }}</td><td>{{ r.source }}</td></tr>{% endfor %}
  </tbody></table>
</section>
{% endblock %}
```

- [ ] **Step 3e: Rewrite `api/templates/lots.html`**

```html
{% extends "base.html" %}
{% block title %}Lots · Mock MES{% endblock %}
{% block content %}
<h1>로트 Lots</h1>
<section class="panel">
  <h2>로트 투입 Start lot</h2>
  <form method="post" action="/lots/start" class="row">
    <label>Product
      <select name="product_code" required>
        {% for p in products %}<option value="{{ p.product_code }}">{{ p.product_code }} — {{ p.product_name }}</option>{% endfor %}
      </select></label>
    <label>Start qty <input type="number" name="start_qty" value="25" min="1" required></label>
    <label>Priority
      <select name="priority"><option>Normal</option><option>Hot</option><option>Low</option></select></label>
    <button type="submit">Start</button>
  </form>
</section>

<form method="get" action="/lots" class="row filters">
  <label>Status <input name="status_filter" value="{{ filters.status_filter }}" list="statuses"></label>
  <datalist id="statuses"><option>Running</option><option>Hold</option><option>Done</option></datalist>
  <label>Product <input name="product_code" value="{{ filters.product_code }}"></label>
  <label>Step <input name="current_step" value="{{ filters.current_step }}"></label>
  <button type="submit">Filter</button>
</form>

<table><thead><tr><th>Lot</th><th>Product</th><th>Tech</th><th>Start</th><th>Wafers</th>
  <th>Priority</th><th>Step</th><th>Status</th></tr></thead><tbody>
{% for r in rows %}<tr>
  <td><a href="/lots/{{ r.lot_id }}">{{ r.lot_id }}</a></td>
  <td>{{ r.product_code }} {{ r.product_name or '' }}</td><td>{{ r.tech_node }}</td>
  <td>{{ r.start_qty }}</td><td>{{ r.wafer_qty }}</td><td>{{ r.priority }}</td>
  <td>{{ r.current_step }}</td><td>{{ r.status }}</td></tr>
{% else %}<tr><td colspan="8">No lots</td></tr>{% endfor %}
</tbody></table>
{% endblock %}
```

- [ ] **Step 3f: Rewrite `api/templates/lot_detail.html`**

```html
{% extends "base.html" %}
{% block title %}{{ lot.lot_id }} · Mock MES{% endblock %}
{% block content %}
<p><a href="/lots">← Lots</a></p>
<h1>Lot {{ lot.lot_id }}</h1>
<ul class="meta">
  <li>Product: {{ lot.product_code }} {{ lot.product_name or '' }} ({{ lot.tech_node }})</li>
  <li>Start qty: {{ lot.start_qty }} · Current wafers: {{ lot.wafer_qty }}</li>
  <li>Cumulative yield: {{ lot.cumulative_yield }}%</li>
  <li>Current step: {{ lot.current_step }} · Status: {{ lot.status }}</li>
</ul>
<h2>Process history</h2>
<table><thead><tr><th>ID</th><th>Step</th><th>Eqp</th><th>In</th><th>Out</th><th>Scrap</th>
  <th>Defect</th><th>Result</th><th>Out time</th></tr></thead><tbody>
{% for h in lot.history %}<tr><td>{{ h.id }}</td><td>{{ h.step_code }} {{ h.step_name or '' }}</td>
  <td>{{ h.eqp_id or '' }}</td><td>{{ h.in_qty }}</td><td>{{ h.out_qty }}</td><td>{{ h.scrap_qty }}</td>
  <td>{{ h.defect_code or '' }}</td><td>{{ h.result }}</td><td>{{ h.out_time or '' }}</td></tr>
{% else %}<tr><td colspan="9">No moves yet</td></tr>{% endfor %}
</tbody></table>
{% endblock %}
```

- [ ] **Step 3g: Rewrite `api/templates/process.html`**

```html
{% extends "base.html" %}
{% block title %}Process · Mock MES{% endblock %}
{% block content %}
<h1>공정실적 Process results</h1>
<section class="panel">
  <h2>공정실적 입력 Register process result</h2>
  <form method="post" action="/process/results" class="row">
    <label>Lot <input name="lot_id" list="lot_ids" required></label>
    <datalist id="lot_ids">{% for l in lot_ids %}<option value="{{ l }}">{% endfor %}</datalist>
    <label>Step <input name="step_code" list="steps" required></label>
    <datalist id="steps">{% for s in steps %}<option value="{{ s }}">{% endfor %}</datalist>
    <label>Eqp <input name="eqp_id" list="eqps"></label>
    <datalist id="eqps">{% for e in equipment %}<option value="{{ e.eqp_id }}">{% endfor %}</datalist>
    <label>In qty <input type="number" name="in_qty" placeholder="(lot wafer qty)"></label>
    <label>Scrap <input type="number" name="scrap_qty" value="0" min="0"></label>
    <label>Defect <input name="defect_code" list="defects"></label>
    <datalist id="defects">{% for d in defect_codes %}<option value="{{ d }}">{% endfor %}</datalist>
    <label>Operator <input name="operator"></label>
    <label>Result
      <select name="result"><option>Pass</option><option>Rework</option><option>Fail</option></select></label>
    <button type="submit">Record</button>
  </form>
</section>

<h2>Route</h2>
<table><thead><tr><th>Seq</th><th>Step</th><th>Name</th><th>Operation</th><th>Eqp type</th><th>Stage</th></tr></thead><tbody>
{% for s in route %}<tr><td>{{ s.seq }}</td><td>{{ s.step_code }}</td><td>{{ s.step_name }}</td>
  <td>{{ s.operation }}</td><td>{{ s.eqp_type }}</td><td>{{ s.stage }}</td></tr>{% endfor %}
</tbody></table>

<h2>Recent results</h2>
<table><thead><tr><th>ID</th><th>Lot</th><th>Step</th><th>In</th><th>Out</th><th>Scrap</th>
  <th>Defect</th><th>Result</th></tr></thead><tbody>
{% for r in results %}<tr><td>{{ r.id }}</td><td>{{ r.lot_id }}</td><td>{{ r.step_code }}</td>
  <td>{{ r.in_qty }}</td><td>{{ r.out_qty }}</td><td>{{ r.scrap_qty }}</td>
  <td>{{ r.defect_code or '' }}</td><td>{{ r.result }}</td></tr>{% endfor %}
</tbody></table>
{% endblock %}
```

- [ ] **Step 3h: Create `api/templates/product_inventory.html`**

```html
{% extends "base.html" %}
{% block title %}Product Inventory · Mock MES{% endblock %}
{% block content %}
<h1>제품 인벤토리 Product inventory (SEMI / FIN)</h1>
<section class="panel">
  <h2>패키징 Packaging (SEMI → FIN)</h2>
  <form method="post" action="/packaging" class="row">
    <label>Product
      <select name="product_code" required>
        {% for p in products %}<option value="{{ p.product_code }}">{{ p.product_code }}</option>{% endfor %}
      </select></label>
    <label>In qty (SEMI) <input type="number" name="in_qty" min="1" required></label>
    <label>Scrap <input type="number" name="scrap_qty" value="0" min="0"></label>
    <label>Lot <input name="lot_id" list="lot_ids"></label>
    <datalist id="lot_ids">{% for l in lot_ids %}<option value="{{ l }}">{% endfor %}</datalist>
    <label>Eqp <input name="eqp_id" list="eqps"></label>
    <datalist id="eqps">{% for e in equipment %}<option value="{{ e.eqp_id }}">{% endfor %}</datalist>
    <button type="submit">Package</button>
  </form>
</section>
<table><thead><tr><th>Product</th><th>Name</th><th>Type</th><th>Qty</th><th>UoM</th><th>Location</th></tr></thead><tbody>
{% for r in rows %}<tr><td>{{ r.product_code }}</td><td>{{ r.product_name or '' }}</td>
  <td>{{ r.item_type }}</td><td>{{ r.qty|round(0) }}</td><td>{{ r.uom }}</td><td>{{ r.location or '' }}</td></tr>
{% else %}<tr><td colspan="6">Empty</td></tr>{% endfor %}
</tbody></table>
{% endblock %}
```

- [ ] **Step 3i: Create `api/templates/product_results.html`**

```html
{% extends "base.html" %}
{% block title %}Product Results · Mock MES{% endblock %}
{% block content %}
<h1>제품실적 Product results</h1>
<section class="panel">
  <h2>제품실적 수동 입력 Manual product result</h2>
  <form method="post" action="/product-results" class="row">
    <label>Lot <input name="lot_id" list="lot_ids" required></label>
    <datalist id="lot_ids">{% for l in lot_ids %}<option value="{{ l }}">{% endfor %}</datalist>
    <label>Type
      <select name="item_type"><option>FIN</option><option>SEMI</option></select></label>
    <label>Good qty <input type="number" name="good_qty" min="0" required></label>
    <label>Scrap <input type="number" name="scrap_qty" value="0" min="0"></label>
    <button type="submit">Record</button>
  </form>
</section>
<form method="get" action="/product-results" class="row filters">
  <label>Product <input name="product_code" value="{{ filters.product_code }}"></label>
  <label>Type <input name="item_type" value="{{ filters.item_type }}" list="types"></label>
  <datalist id="types"><option>SEMI</option><option>FIN</option></datalist>
  <label>Source <input name="source" value="{{ filters.source }}" list="sources"></label>
  <datalist id="sources"><option>AUTO_FAB</option><option>AUTO_PACK</option><option>MANUAL</option></datalist>
  <button type="submit">Filter</button>
</form>
<table><thead><tr><th>Date</th><th>Lot</th><th>Product</th><th>Type</th><th>Good</th><th>Scrap</th>
  <th>Yield</th><th>Source</th></tr></thead><tbody>
{% for r in rows %}<tr><td>{{ r.result_date }}</td><td>{{ r.lot_id or '' }}</td>
  <td>{{ r.product_code }} {{ r.product_name or '' }}</td><td>{{ r.item_type }}</td>
  <td>{{ r.good_qty }}</td><td>{{ r.scrap_qty }}</td><td>{{ r.yield_pct }}</td><td>{{ r.source }}</td></tr>
{% else %}<tr><td colspan="8">No results</td></tr>{% endfor %}
</tbody></table>
{% endblock %}
```

- [ ] **Step 3j: Create `api/templates/materials.html`**

```html
{% extends "base.html" %}
{% block title %}Materials · Mock MES{% endblock %}
{% block content %}
<h1>자재 인벤토리 Material inventory</h1>
<section class="panel">
  <h2>자재 입고 Receive material</h2>
  <form method="post" action="/materials/receive" class="row">
    <label>Code <input name="material_code" required></label>
    <label>Qty <input type="number" step="any" name="qty" min="0" required></label>
    <label>Name <input name="material_name"></label>
    <label>Category <input name="category"></label>
    <label>UoM <input name="uom"></label>
    <label>Location <input name="location"></label>
    <button type="submit">Receive</button>
  </form>
</section>
<form method="get" action="/materials" class="row filters">
  <label>Category <input name="category" value="{{ filters.category }}"></label>
  <label>Step (for BOM usage) <input name="step_code" value="{{ filters.step_code }}" list="steps"></label>
  <datalist id="steps">{% for s in steps %}<option value="{{ s }}">{% endfor %}</datalist>
  <button type="submit">Filter</button>
</form>
<h2>On-hand stock</h2>
<table><thead><tr><th>Code</th><th>Name</th><th>Category</th><th>Qty</th><th>UoM</th><th>Location</th></tr></thead><tbody>
{% for r in rows %}<tr><td>{{ r.material_code }}</td><td>{{ r.material_name }}</td>
  <td>{{ r.category }}</td><td>{{ r.qty|round(2) }}</td><td>{{ r.uom }}</td><td>{{ r.location }}</td></tr>
{% else %}<tr><td colspan="6">Empty</td></tr>{% endfor %}
</tbody></table>
<h2>공정별 자재 소요 Usage by step (BOM ⋈ stock)</h2>
<table><thead><tr><th>Step</th><th>Product</th><th>Material</th><th>Per wafer</th><th>On hand</th><th>UoM</th></tr></thead><tbody>
{% for r in by_step %}<tr><td>{{ r.step_code }} {{ r.step_name or '' }}</td><td>{{ r.product_code }}</td>
  <td>{{ r.material_code }} {{ r.material_name or '' }}</td><td>{{ r.qty_per_wafer }}</td>
  <td>{{ r.on_hand_qty|round(2) }}</td><td>{{ r.uom }}</td></tr>
{% else %}<tr><td colspan="6">No BOM rows</td></tr>{% endfor %}
</tbody></table>
{% endblock %}
```

- [ ] **Step 3k: Create `api/templates/bom.html`**

```html
{% extends "base.html" %}
{% block title %}BOM · Mock MES{% endblock %}
{% block content %}
<h1>BOM</h1>
<section class="panel">
  <h2>BOM 입력/수정 Upsert</h2>
  <form method="post" action="/bom" class="row">
    <label>Product
      <select name="product_code" required>
        {% for p in products %}<option value="{{ p.product_code }}">{{ p.product_code }}</option>{% endfor %}
      </select></label>
    <label>Step <input name="step_code" list="steps" required></label>
    <datalist id="steps">{% for s in steps %}<option value="{{ s }}">{% endfor %}</datalist>
    <label>Material <input name="material_code" list="mats" required></label>
    <datalist id="mats">{% for m in materials %}<option value="{{ m.material_code }}">{% endfor %}</datalist>
    <label>Qty/wafer <input type="number" step="any" name="qty_per_wafer" min="0" required></label>
    <label>UoM <input name="uom"></label>
    <button type="submit">Save</button>
  </form>
</section>
<form method="get" action="/bom" class="row filters">
  <label>Product <input name="product_code" value="{{ filters.product_code }}"></label>
  <label>Step <input name="step_code" value="{{ filters.step_code }}"></label>
  <button type="submit">Filter</button>
</form>
<table><thead><tr><th>ID</th><th>Product</th><th>Step</th><th>Material</th><th>Qty/wafer</th><th>UoM</th><th></th></tr></thead><tbody>
{% for r in rows %}<tr><td>{{ r.id }}</td><td>{{ r.product_code }}</td>
  <td>{{ r.step_code }} {{ r.step_name or '' }}</td><td>{{ r.material_code }} {{ r.material_name or '' }}</td>
  <td>{{ r.qty_per_wafer }}</td><td>{{ r.uom or '' }}</td>
  <td><form method="post" action="/bom/{{ r.id }}/delete"><button type="submit" class="link">delete</button></form></td></tr>
{% else %}<tr><td colspan="7">No BOM rows</td></tr>{% endfor %}
</tbody></table>
{% endblock %}
```

- [ ] **Step 3l: Rewrite `api/templates/wip.html` + create `api/templates/equipment.html`**

`wip.html`:
```html
{% extends "base.html" %}
{% block title %}WIP · Mock MES{% endblock %}
{% block content %}
<h1>재공 WIP (non-Done lots by step)</h1>
<table><thead><tr><th>Seq</th><th>Step</th><th>Name</th><th>Stage</th><th>Lots</th><th>Wafers</th></tr></thead><tbody>
{% for r in rows %}<tr><td>{{ r.seq }}</td><td>{{ r.step_code }}</td><td>{{ r.step_name }}</td>
  <td>{{ r.stage }}</td><td>{{ r.lot_count }}</td><td>{{ r.wafer_qty }}</td></tr>
{% else %}<tr><td colspan="6">No WIP</td></tr>{% endfor %}
</tbody></table>
{% endblock %}
```

`equipment.html`:
```html
{% extends "base.html" %}
{% block title %}Equipment · Mock MES{% endblock %}
{% block content %}
<h1>설비 Equipment</h1>
<table><thead><tr><th>ID</th><th>Name</th><th>Type</th><th>Status</th></tr></thead><tbody>
{% for r in rows %}<tr><td>{{ r.eqp_id }}</td><td>{{ r.eqp_name }}</td><td>{{ r.type }}</td><td>{{ r.status }}</td></tr>
{% else %}<tr><td colspan="4">No equipment</td></tr>{% endfor %}
</tbody></table>
{% endblock %}
```

- [ ] **Step 3m: Delete stale templates + extend CSS**

```bash
git rm api/templates/inventory.html api/templates/production.html
```

Append to `api/static/styles.css`:
```css
.panel { background:#f6f8fa; border:1px solid #d0d7de; border-radius:8px; padding:12px 16px; margin:16px 0; }
.row { display:flex; flex-wrap:wrap; gap:12px; align-items:flex-end; }
.row label { display:flex; flex-direction:column; font-size:12px; gap:4px; }
.row input, .row select { padding:6px 8px; }
.filters { margin:12px 0; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:24px; }
.cards { display:flex; flex-wrap:wrap; gap:12px; margin:16px 0; }
.card { background:#fff; border:1px solid #d0d7de; border-radius:8px; padding:12px 16px; min-width:120px; }
.card .kpi { font-size:26px; font-weight:700; }
.card .label { color:#57606a; font-size:12px; }
button.link { background:none; border:none; color:#cf222e; cursor:pointer; padding:0; }
ul.meta { list-style:none; padding:0; } ul.meta li { padding:2px 0; }
@media (max-width:800px){ .grid2 { grid-template-columns:1fr; } }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest api.tests.test_app -v`
Expected: PASS (RestApiTests + WebConsoleTests).

- [ ] **Step 5: Commit**

```bash
git add mes_core/db.py api/web.py api/templates api/static/styles.css api/tests/test_app.py
git commit -m "feat(web): full console for two-stage fab (lots, process, inventories, BOM)

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87"
```

---

### Task 12: `/guide` usage & process-flow page

A human-facing, open (no key) page explaining what the MES is, the overall flow, each function, and how agents connect.

**Files:**
- Modify: `api/web.py` (add `/guide` route)
- Create: `api/templates/guide.html`
- Modify: `api/tests/test_app.py` (append `test_guide_page` to `WebConsoleTests`)

**Interfaces:**
- Consumes: `templates`, `_context`.
- Produces: `GET /guide`.

- [ ] **Step 1: Write the failing test**

Append this method to the `WebConsoleTests` class in `api/tests/test_app.py`:

```python
    def test_guide_page(self):
        r = self.client.get("/guide")
        self.assertEqual(r.status_code, 200)
        self.assertIn("공정", r.text)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest api.tests.test_app.WebConsoleTests.test_guide_page -v`
Expected: FAIL — `/guide` 404.

- [ ] **Step 3a: Add the route** — append to `api/web.py`:

```python
@router.get("/guide")
def guide(request: Request):
    return templates.TemplateResponse(request, "guide.html", _context(request))
```

- [ ] **Step 3b: Create `api/templates/guide.html`**

```html
{% extends "base.html" %}
{% block title %}Guide · Mock MES{% endblock %}
{% block content %}
<h1>사용법 & 공정 흐름 Guide</h1>

<section class="panel">
  <h2>이게 뭔가요? What is this?</h2>
  <p>반도체 팹을 흉내 낸 <strong>목업 MES</strong>입니다. 하나의 SQLite DB를 세 가지 창구로 노출합니다:
     사람용 <strong>웹 콘솔</strong>, 에이전트용 <strong>REST API</strong>, 에이전트용 <strong>MCP 서버</strong>.
     콜드 스타트마다 데이터가 새로 시딩됩니다. 데모 전용이라 실제 보안은 없습니다.</p>
</section>

<section>
  <h2>전체 흐름 Overall flow</h2>
  <ol class="flow">
    <li><strong>로트 투입 (Start lot)</strong> — 제품을 골라 웨이퍼 로트를 시작합니다. 첫 FAB 공정에서 Running.</li>
    <li><strong>공정실적 (Process result) ×8</strong> — DIFF→PHOTO→ETCH→IMPL→CVD→CMP→METRO→TEST 순으로
        이동. 각 이동은 BOM에 따라 <strong>자재를 차감</strong>하고, 불량(scrap)을 기록합니다.</li>
    <li><strong>SEMI 자동 입고</strong> — 마지막 FAB 공정(TEST) Pass 시 로트가 Done이 되고
        <strong>반제품(SEMI)</strong>이 제품 인벤토리에 자동 입고 + 제품실적(AUTO_FAB) 생성.</li>
    <li><strong>패키징 (Packaging)</strong> — SEMI를 소비해 <strong>완제품(FIN)</strong>을 만듭니다.
        PKG 공정 BOM 자재도 차감, 제품실적(AUTO_PACK) 생성.</li>
    <li><strong>조회</strong> — 재공(WIP), 제품/자재 인벤토리, 제품/공정 실적, 로트 이력을 언제든 조회.</li>
  </ol>
  <p class="mono">Materials ──(BOM×wafers)──▶ Process results ──(TEST pass)──▶ SEMI ──(Packaging)──▶ FIN</p>
</section>

<section>
  <h2>기능별 사용법 Functions</h2>
  <table><thead><tr><th>기능</th><th>설명</th><th>창구</th></tr></thead><tbody>
    <tr><td>로트 투입</td><td>새 웨이퍼 로트 시작</td><td>Web /lots · MCP start_lot</td></tr>
    <tr><td>공정실적</td><td>공정 이동 + 자재 차감 + 불량</td><td>Web /process · MCP register_process_result</td></tr>
    <tr><td>공정/로트 조회</td><td>route·이력·WIP·로트</td><td>Web · MCP get_lot/list_lots/get_process_route/list_process_results/get_wip</td></tr>
    <tr><td>제품실적</td><td>SEMI/FIN 실적 (자동/수동)</td><td>Web /product-results · REST /api/product-results</td></tr>
    <tr><td>패키징</td><td>SEMI→FIN 변환</td><td>Web /product-inventory · REST /api/packaging</td></tr>
    <tr><td>제품 인벤토리</td><td>반제품/완제품 재고</td><td>Web · REST /api/product-inventory</td></tr>
    <tr><td>자재 인벤토리</td><td>자재 재고·입고·공정별 소요</td><td>Web /materials · REST /api/materials(/by-step, /receipt)</td></tr>
    <tr><td>BOM</td><td>제품·공정별 자재 소요량</td><td>Web /bom · REST /api/bom</td></tr>
  </tbody></table>
</section>

<section class="panel">
  <h2>에이전트 연결 Agent access</h2>
  <p>REST와 MCP는 헤더 <code>X-API-Key: changjuahn</code>가 필요합니다. 웹 콘솔과 <code>/api/docs</code>는 공개입니다.</p>
  <p><strong>REST</strong> (Product·Material·BOM):</p>
  <pre>curl -H "X-API-Key: changjuahn" https://&lt;fqdn&gt;/api/product-inventory
curl -X POST -H "X-API-Key: changjuahn" -H "Content-Type: application/json" \
     -d '{"product_code":"LX9","in_qty":50}' https://&lt;fqdn&gt;/api/packaging</pre>
  <p><strong>MCP</strong> (Process·Lot), streamable HTTP at <code>/mcp</code> with the same header. Tools:
     <code>start_lot</code>, <code>register_process_result</code>, <code>get_lot</code>, <code>list_lots</code>,
     <code>get_process_route</code>, <code>list_process_results</code>, <code>get_wip</code>.</p>
  <p>Full REST reference: <a href="/api/docs">/api/docs</a>.</p>
</section>
{% endblock %}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest api.tests.test_app -v`
Expected: PASS (all REST + web tests incl. guide).

- [ ] **Step 5: Commit**

```bash
git add api/web.py api/templates/guide.html api/tests/test_app.py
git commit -m "feat(web): add /guide usage & process-flow page

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87"
```

---

### Task 13: README rewrite

Rewrite `README.md` for the new model. No code/tests here; documentation only.

**Files:**
- Rewrite: `README.md`

- [ ] **Step 1: Rewrite `README.md`** with these sections (keep it accurate to the implemented system):
  1. **Title + one-paragraph intro** — throwaway mock MES for a semiconductor fab, agent demo connection point; three surfaces over one shared SQLite; re-seeds each cold start; no real security.
  2. **Architecture** — single ACA Container App, scale-to-zero; init(seed)/api/mcp/proxy containers; shared EmptyDir `/data/mes.db`; Caddy routes `/mcp*`→mcp:8001, `/*`→api:8000. Keep the existing ASCII/mermaid diagram if present, updated.
  3. **Data model** — the 9 tables + two-stage flow (FAB→SEMI→Packaging→FIN), BOM-driven material consumption, two inventories. Include the mermaid ERD from the spec.
  4. **Process flow** — the 5-step flow from the guide (start_lot → process ×8 → SEMI auto-receipt → packaging → FIN), material consumption rule (`qty_per_wafer × in_qty`, floor 0, shortages non-blocking; SEMI shortfall blocks packaging).
  5. **Surfaces & endpoints** — Web `/` (all functions, open) incl. `/guide`; REST `/api` (Product·Material·BOM) with the endpoint list + `/api/docs`; MCP `/mcp` (Process·Lot) with the tool list. State the `X-API-Key: changjuahn` requirement for REST+MCP; web + docs open.
  6. **Agent connection examples** — a `curl` REST example (with header) and a short Python MCP client snippet using `streamablehttp_client(url, headers={"X-API-Key": "changjuahn"})` calling `start_lot` + `get_wip`.
  7. **Local development** — venv, `pip install -r requirements.txt`, seed with `MES_DB_PATH=$(pwd)/data/mes.db python -m mes_core.seed`, run api (`uvicorn api.main:app --port 8000`) + mcp (`python -m mcp_server`), run tests (`python -m unittest mes_core.test_db api.tests.test_app mcp_server.test_server`).
  8. **Deploy / redeploy / teardown** — push branch → GitHub Actions builds the two public GHCR images → `./infra/deploy.sh` (RG `rg-mock-mes-kr`, `koreacentral`); redeploy rolls a fresh revision (cold-start reseed); teardown `az group delete -n rg-mock-mes-kr`.
  9. **Cost notes** — scale-to-zero (~$0 idle), 0.25 vCPU/0.5 GiB per container, single replica, ephemeral SQLite.
  10. **Seeded data overview** — 4 products, 8 FAB steps + PKG, 9 equipment, 12 materials, ~32 BOM rows, 16 lots (6 Done/2 Hold/8 Running), ~80 process results, SEMI/FIN inventory, product results (AUTO_FAB/AUTO_PACK/MANUAL).

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: rewrite README for two-stage BOM/inventory redesign

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
Copilot-Session: 0d70b324-d6e5-43c3-bc34-5805eac6db87"
```

---

### Task 14: Full-suite + local cross-channel verification

Prove the whole system works locally (no Docker): every unit test passes, and the two live processes share one SQLite so a write on one surface is visible on the other.

**Files:** none committed (verification only; use a throwaway `/tmp` script).

- [ ] **Step 1: Run the entire unit suite**

Run: `python -m unittest mes_core.test_db api.tests.test_app mcp_server.test_server -v`
Expected: PASS (all classes across the three modules). Fix any failure before continuing.

- [ ] **Step 2: Seed a shared dev DB and start both servers**

```bash
export MES_DB_PATH=$(pwd)/data/mes.db
python -m mes_core.seed
```
Start API (background): `MES_DB_PATH=$(pwd)/data/mes.db uvicorn api.main:app --host 127.0.0.1 --port 8000`
Start MCP (background): `MES_DB_PATH=$(pwd)/data/mes.db python -m mcp_server`
Confirm both listen: `curl -s -H "X-API-Key: changjuahn" http://127.0.0.1:8000/api/health` returns `status: ok`.

- [ ] **Step 3: Cross-channel smoke (MCP write → REST read → REST write)**

Write `/tmp/xchan_local.py`:

```python
import asyncio, httpx
from mcp import ClientSession
from mcp.client.streamablehttp import streamablehttp_client

REST = "http://127.0.0.1:8000"
MCP = "http://127.0.0.1:8001/mcp"
HDR = {"X-API-Key": "changjuahn"}


def _tool(res):
    sc = getattr(res, "structuredContent", None) or {}
    return sc.get("result", sc)


async def main():
    async with streamablehttp_client(MCP, headers=HDR) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            lot = _tool(await s.call_tool("start_lot", {"product_code": "LX9", "start_qty": 20}))
            lot_id = lot["lot_id"]
            print("MCP start_lot ->", lot_id)
            res = _tool(await s.call_tool("register_process_result",
                        {"lot_id": lot_id, "step_code": "TEST", "in_qty": 20, "result": "Pass"}))
            assert res["semi_receipt"], "expected SEMI auto-receipt on final TEST pass"
            print("MCP process TEST -> SEMI receipt", res["semi_receipt"])

    with httpx.Client(headers=HDR) as c:
        inv = c.get(f"{REST}/api/product-inventory", params={"product_code": "LX9", "item_type": "SEMI"}).json()
        semi = sum(r["qty"] for r in inv)
        assert semi >= 19, f"REST should see SEMI written via MCP, got {semi}"
        print("REST sees SEMI:", semi)
        pk = c.post(f"{REST}/api/packaging", json={"product_code": "LX9", "in_qty": 10, "lot_id": lot_id})
        assert pk.status_code == 200, pk.text
        print("REST packaging -> FIN good:", pk.json()["good_qty"])
        auto = c.get(f"{REST}/api/product-results", params={"source": "AUTO_PACK"}).json()
        assert any(r["product_code"] == "LX9" for r in auto), "expected AUTO_PACK result"
        print("REST product-results AUTO_PACK OK, rows:", len(auto))
    print("CROSS-CHANNEL OK")


asyncio.run(main())
```

Run: `python /tmp/xchan_local.py`
Expected: prints `CROSS-CHANNEL OK` (MCP-written SEMI is visible via REST; REST packaging produces FIN + AUTO_PACK).

- [ ] **Step 4: Stop servers**

Find PIDs and kill (numeric only): `lsof -nP -iTCP:8000 -sTCP:LISTEN -t` and `lsof -nP -iTCP:8001 -sTCP:LISTEN -t`, then `kill <PID>` each. Remove `/tmp/xchan_local.py`.

- [ ] **Step 5: No commit** (verification only). If Steps 1–3 surfaced a bug, fix it in the owning task's file, re-run that task's tests, and commit with the standard trailers before proceeding.

---

### Task 15: Cloud deploy + live verify + PR update

Reuse the existing infra unchanged (approach A). Build public GHCR images via GitHub Actions, deploy the Bicep, verify the live app, update PR #1, and report to the creating chat session.

**Files:** none (infra reused: `Dockerfile`, `proxy/*`, `.github/workflows/images.yml`, `infra/main.bicep`, `infra/deploy.sh`).

- [ ] **Step 1: Push the branch**

```bash
git push -u origin changju-ahn-feat-mock-mes
```

- [ ] **Step 2: Build + publish both public GHCR images**

Find the run and watch it: `gh run list --branch changju-ahn-feat-mock-mes --limit 1` then `gh run watch <run_id> --exit-status`.
Expected: workflow success; `ghcr.io/changju-ahn/mock-mes-app:latest` and `ghcr.io/changju-ahn/mock-mes-proxy:latest` re-published (public → no pull secret).

- [ ] **Step 3: Deploy to ACA**

```bash
./infra/deploy.sh
```
(RG `rg-mock-mes-kr`, region `koreacentral`, sub `347e0df7-94e9-4feb-b42d-57d7e49566f2`; `revisionSuffix=utcNow` rolls a fresh revision so the init container re-seeds.)
Capture the ingress FQDN from the script output (or `az containerapp show -g rg-mock-mes-kr -n mock-mes --query properties.configuration.ingress.fqdn -o tsv`).

- [ ] **Step 4: Live smoke (auth gate + web + REST + MCP + cross-channel + reseed)**

```bash
FQDN=<fqdn-from-step-3>
# auth gate
curl -s -o /dev/null -w "%{http_code}\n" https://$FQDN/api/products            # 401
curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: changjuahn" https://$FQDN/api/products  # 200
# web + guide + docs open
curl -s -o /dev/null -w "%{http_code}\n" https://$FQDN/                          # 200
curl -s -o /dev/null -w "%{http_code}\n" https://$FQDN/guide                     # 200
curl -s -o /dev/null -w "%{http_code}\n" https://$FQDN/api/docs                  # 200
```
Then run the live cross-channel check: copy `/tmp/xchan_local.py` to `/tmp/xchan_live.py`, set `REST=f"https://{FQDN}"` and `MCP=f"https://{FQDN}/mcp"`, run `python /tmp/xchan_live.py` → expect `CROSS-CHANNEL OK` (MCP tool call against the live `/mcp` with the header; SEMI written via MCP visible via live REST; live packaging → FIN + AUTO_PACK).
Cold-start reseed check: after the app scales to zero (or restart the revision with `az containerapp revision restart`), re-hit `GET /api/health` with the key and confirm `counts.lot == 16` (fresh deterministic dataset). Remove `/tmp/xchan_live.py`.

- [ ] **Step 5: Update PR #1 and report**

- Ensure the plan + all task commits are pushed. Update the PR body to describe the redesign (two-stage FAB→Packaging, BOM material consumption, split inventories, REST/MCP re-split, `/guide`, live FQDN + endpoints). Use the `update_pull_request` tool (not `gh pr edit`).
- Reply to the creating chat session `c02ae98d-0593-465c-a697-b896fa0b6256` via `send_session_message` with: PR URL `https://github.com/ChangJu-Ahn/mock-mes-kr/pull/1`, the live FQDN, the three endpoints (`/`, `/api` + `/api/docs`, `/mcp`), the `X-API-Key: changjuahn` note, and any assumptions/issues.

**Assumptions (state in the PR/report):**
- Auth scheme, image names, RG/region, and all infra are reused unchanged from the prior deploy.
- Timestamps in seeded rows vary per run (wall clock); row counts/quantities are deterministic.
- Material shortages are non-blocking by design (line keeps running); only SEMI shortfall blocks packaging.

## Done criteria

- All three unit-test modules pass (`python -m unittest mes_core.test_db api.tests.test_app mcp_server.test_server`).
- Local cross-channel smoke prints `CROSS-CHANNEL OK`.
- Both public GHCR images publish; ACA deploy succeeds; live auth gate + web + `/guide` + REST + MCP + cross-channel + cold-start reseed all verified.
- PR #1 updated; creating chat session notified with PR URL, FQDN, and endpoints.












