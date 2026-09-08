"""Author the mock dataset. A maintenance tool -- **not** used at runtime.

    python -m mes_core.generate      # rewrites mes_core/dataset.json

The shipped dataset lives in ``dataset.json`` and is what every deploy loads.
This module is how that file is produced: it draws a plausible fab history
from a fixed RNG, lays it out on a clock (see :mod:`mes_core.schedule`), and
dumps the resulting tables.

Keeping generation out of the boot path is deliberate. Sensor archives outside
this repo are keyed by a lot's ``in_time``/``out_time`` on a given tool, so the
numbers have to survive edits to the code that first invented them. As a
literal file they are reviewable in a diff and can only change when someone
means to change them; as an algorithm they would quietly move whenever a draw
order or a duration band was touched.

Regenerating is therefore a deliberate act with a blast radius: it re-cuts
every window and orphans anything recorded against the old ones. The command
refuses to overwrite unless ``--force`` is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import db, schedule

DATASET_PATH = Path(__file__).resolve().parent / "dataset.json"

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


@dataclass
class PlannedRun:
    """One process result, decided but not yet written or timed."""

    lot_key: int
    step_code: str
    eqp_id: str | None
    in_qty: int
    scrap_qty: int
    defect_code: str | None
    operator: str
    result: str
    in_time: str = field(default="")
    out_time: str = field(default="")


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


def _plan_advance(lot_key: int, upto_idx: int, start_qty: int,
                  rnd: random.Random, out: list[PlannedRun]) -> None:
    """Decide FAB_STEPS[0..upto_idx] for one lot without touching the DB.

    Draw order from ``rnd`` must stay byte-identical to the previous
    register-as-you-go version: scrap, defect, pass roll, rework pick,
    equipment, operator. Anything else changes the whole dataset.

    ``wafer_qty`` used to come back from the DB after each write; the DB set it
    to ``in_qty - scrap_qty``, so tracking it locally is exact.
    """
    qty = start_qty
    for idx in range(upto_idx + 1):
        step = FAB_STEPS[idx]
        in_qty = qty
        scrap = _scrap_for(step, in_qty, rnd)
        defect = rnd.choice(DEFECTS) if scrap > 0 else None
        result = "Pass" if rnd.random() > 0.05 else rnd.choice(["Rework", "Fail"])
        # ensure the final TEST step passes so Done lots produce SEMI
        if step == "TEST":
            result = "Pass"
        out.append(PlannedRun(
            lot_key=lot_key, step_code=step, eqp_id=_pick_eqp(step, rnd),
            in_qty=in_qty, scrap_qty=scrap, defect_code=defect,
            operator=rnd.choice(OPERATORS), result=result,
        ))
        qty = in_qty - scrap


def build() -> None:
    """Build the dataset in three passes.

    1. Decide everything, drawing from ``rnd`` in exactly the historical order,
       but write nothing -- even creating the lot rows is deferred.
    2. Lay the decisions out on a clock using a separate generator, so the
       makespan ends at the anchor and equipment never double-books.
    3. Replay the decisions into the real transaction functions, oldest first,
       so row ids track time the way the console assumes they do.

    Pass 2 draws no numbers from ``rnd``, so the ``rnd`` sequence -- and hence
    the 91 results, 16 lots, defect mix and yields -- is unchanged.
    """
    rnd = random.Random(42)
    sched = random.Random(schedule.SCHEDULE_SEED)
    anchor = schedule.resolve_anchor()

    db.reset_db()
    _insert_master()

    # --- pass 1: decide ----------------------------------------------------
    lot_specs: list[tuple[str, int, str]] = []   # product_code, start_qty, priority
    planned: list[PlannedRun] = []
    hold_keys: set[int] = set()
    done_products: list[str] = []

    for i in range(1, 17):
        pcode, _pn, _tn = PRODUCTS[(i - 1) % len(PRODUCTS)]
        start_qty = rnd.choice([25, 25, 25, 24, 12])
        priority = rnd.choice(PRIORITIES)
        lot_specs.append((pcode, start_qty, priority))
        if i <= 6:                       # Done: full FAB route (TEST passes -> SEMI)
            _plan_advance(i, len(FAB_STEPS) - 1, start_qty, rnd, planned)
            done_products.append(pcode)
        elif i <= 8:                     # Hold: partway then held
            _plan_advance(i, rnd.randint(1, 5), start_qty, rnd, planned)
            hold_keys.add(i)
        else:                            # Running: partway
            _plan_advance(i, rnd.randint(0, 6), start_qty, rnd, planned)

    # --- pass 2: schedule --------------------------------------------------
    released = schedule.assign_times(planned, anchor, sched)

    # --- pass 3: write -----------------------------------------------------
    lot_ids: dict[int, str] = {}
    for i, (pcode, start_qty, priority) in enumerate(lot_specs, start=1):
        start_date = released.get(i, anchor)
        lot = db.start_lot(pcode, start_qty, priority=priority,
                           start_date=schedule.iso(start_date)[:10])
        lot_ids[i] = lot["lot_id"]

    for run in sorted(planned, key=lambda r: r.out_time):
        db.register_process_result(
            lot_ids[run.lot_key], run.step_code, eqp_id=run.eqp_id,
            in_qty=run.in_qty, scrap_qty=run.scrap_qty,
            defect_code=run.defect_code, operator=run.operator,
            result=run.result, in_time=run.in_time, out_time=run.out_time,
        )

    for key in sorted(hold_keys):
        with db.get_conn() as conn:
            conn.execute("UPDATE lot SET status='Hold' WHERE lot_id=?", (lot_ids[key],))

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


def dump() -> dict[str, list[list]]:
    """Build into a throwaway DB and return every table as plain rows.

    Tables come back in dependency order (parents first) so a loader can
    insert them verbatim without tripping the foreign keys.
    """
    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("MES_DB_PATH")
        os.environ["MES_DB_PATH"] = str(Path(tmp) / "generate.db")
        try:
            build()
            out: dict[str, list[list]] = {}
            with db.get_conn() as conn:
                for table in reversed(db.TABLES):
                    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                    order = ", ".join(f'"{c}"' for c in cols)
                    rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
                    out[table] = [list(r) for r in rows]
            return out
        finally:
            if previous is None:
                os.environ.pop("MES_DB_PATH", None)
            else:
                os.environ["MES_DB_PATH"] = previous


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="overwrite dataset.json (re-cuts every time window)")
    args = ap.parse_args(argv)

    if DATASET_PATH.exists() and not args.force:
        print(f"[generate] {DATASET_PATH.name} already exists; refusing to overwrite.",
              file=sys.stderr)
        print("[generate] Regenerating moves every in_time/out_time and orphans any",
              file=sys.stderr)
        print("[generate] sensor data keyed to the old windows. Pass --force if that",
              file=sys.stderr)
        print("[generate] is what you mean to do.", file=sys.stderr)
        return 1

    tables = dump()
    DATASET_PATH.write_text(
        json.dumps(tables, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    total = sum(len(v) for v in tables.values())
    print(f"[generate] wrote {DATASET_PATH} ({total} rows)")
    for name, rows in tables.items():
        print(f"[generate]   {name}={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
