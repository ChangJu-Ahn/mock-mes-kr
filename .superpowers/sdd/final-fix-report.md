# Final Fix Report

## Fixes Applied

### Fix 1 (I1) — Atomic transactions + relative material consume (`mes_core/db.py`)

#### 1a. `register_process_result` wrapped in `BEGIN IMMEDIATE`

```diff
     with get_conn() as conn:
-        lot = conn.execute("SELECT * FROM lot WHERE lot_id = ?", (lot_id,)).fetchone()
-        ...
-        row = conn.execute(...).fetchone()
+        conn.execute("BEGIN IMMEDIATE")
+        try:
+            lot = conn.execute("SELECT * FROM lot WHERE lot_id = ?", (lot_id,)).fetchone()
+            ...
+            row = conn.execute(...).fetchone()
+            conn.execute("COMMIT")
+        except Exception:
+            conn.execute("ROLLBACK")
+            raise
```

#### 1b. `package` wrapped in `BEGIN IMMEDIATE`

```diff
     with get_conn() as conn:
-        prod = conn.execute(...)
-        ...
-        row = conn.execute("SELECT * FROM product_result WHERE id = ?", (pr_id,)).fetchone()
+        conn.execute("BEGIN IMMEDIATE")
+        try:
+            prod = conn.execute(...)
+            ...
+            row = conn.execute("SELECT * FROM product_result WHERE id = ?", (pr_id,)).fetchone()
+            conn.execute("COMMIT")
+        except Exception:
+            conn.execute("ROLLBACK")
+            raise
```

#### 1c. `_consume_materials` — relative decrement, remove lost-update

```diff
-        on_hand = b["on_hand"] or 0
-        new_qty = on_hand - need
-        if new_qty < 0:
-            shortages.append({...})
-            new_qty = 0
-        conn.execute("UPDATE material SET qty = ? WHERE material_code = ?", (new_qty, b["material_code"]))
+        on_hand = b["on_hand"] or 0
+        if on_hand < need:
+            shortages.append({...})
+        conn.execute("UPDATE material SET qty = MAX(0, qty - ?) WHERE material_code = ?", (need, b["material_code"]))
```

### Fix 2 (M1) — Remove dead imports (`mes_core/db.py`, line 24)

```diff
-from typing import Any, Iterable, Optional
+from typing import Any
```

Confirmed `Iterable` and `Optional` had zero usages in the file outside the import line.

### Fix 3 (M2) — Narrow over-broad except (`api/web.py`, line 86)

```diff
-    except (ValueError, Exception) as exc:
+    except ValueError as exc:
```

## Public signature confirmation

No public function signatures used by `api/` or `mcp_server/` were changed:
- `register_process_result(lot_id, step_code, in_qty, scrap_qty, defect_code, eqp_id, operator, result, in_time, out_time)` — unchanged
- `package(product_code, in_qty, scrap_qty, lot_id, eqp_id, operator)` — unchanged
- `_consume_materials` is a private helper; its signature `(conn, product_code, step_code, units)` is unchanged

## Test Results

```
Command: cd /Users/changjuahn/Repo/copilot-worktrees/mock-mes-kr/changju-ahn-glowing-spork && .venv/bin/python -m pytest mes_core/test_db.py api/tests/test_app.py mcp_server/test_server.py -q

............................................................             [100%]
=============================== warnings summary ===============================
.venv/lib/python3.12/site-packages/fastapi/testclient.py:1
  /Users/changjuahn/Repo/copilot-worktrees/mock-mes-kr/changju-ahn-glowing-spork/.venv/lib/python3.12/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
60 passed, 1 warning in 3.75s
```

## Seed Sanity Check

```
Command: MES_DB_PATH=$(pwd)/data/mes.db .venv/bin/python -m mes_core.seed

[seed] mock MES database ready at .../data/mes.db
[seed] rows: product_result=10, process_result=91, product_inventory=7, bom=48, lot=16, material=12, equipment=9, process_step=9, product=4
```

lot=16 confirmed, no exception.

## Commit SHA

(see below — populated after commit)

## Trailer Count Check

(see below — populated after commit)
