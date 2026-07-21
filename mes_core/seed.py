"""Seed the shared SQLite DB with a realistic mock semiconductor-fab dataset.

Run as a module so it works both locally and as the ACA init container:

    python -m mes_core.seed

It is idempotent and reproducible: it resets all tables and regenerates the
same dataset every time (fixed RNG seed), so each cold start yields a fresh,
consistent fab snapshot.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from . import db

# --------------------------------------------------------------------------- #
# Reference / master data
# --------------------------------------------------------------------------- #

# 4 products (product, tech_node)
PRODUCTS = [
    ("LX9 AP", "5nm"),       # application processor
    ("DDR5-16G", "10nm"),    # DRAM
    ("V7 NAND", "128L"),     # 3D NAND
    ("PMIC-33", "28nm"),     # power management IC
]

# 8-step wafer process route (seq, step_code, step_name, operation, eqp_type)
ROUTE = [
    (10, "DIFF",  "Diffusion",             "Thermal Oxidation",         "Furnace"),
    (20, "PHOTO", "Photolithography",      "Pattern Exposure",          "Scanner"),
    (30, "ETCH",  "Etch",                  "Plasma Etch",               "Etcher"),
    (40, "IMPL",  "Ion Implant",           "Dopant Implant",            "Implanter"),
    (50, "CVD",   "Deposition (CVD)",      "Thin-Film Deposition",      "CVD"),
    (60, "CMP",   "Planarization (CMP)",   "Chemical-Mechanical Polish","Polisher"),
    (70, "METRO", "Metrology",             "Inline Inspection",         "Metrology"),
    (80, "TEST",  "Wafer Test (EDS)",      "Electrical Die Sort",       "Prober"),
]

# 8 equipment rows (eqp_id, eqp_name, type, status)
EQUIPMENT = [
    ("EQP-DIFF01", "Furnace-A",     "Furnace",   "Run"),
    ("EQP-PHOT01", "Scanner-EUV1",  "Scanner",   "Run"),
    ("EQP-PHOT02", "Scanner-DUV1",  "Scanner",   "Idle"),
    ("EQP-ETCH01", "Etcher-A",      "Etcher",    "Run"),
    ("EQP-IMPL01", "Implanter-A",   "Implanter", "Down"),
    ("EQP-CVD01",  "CVD-A",         "CVD",       "Run"),
    ("EQP-CMP01",  "Polisher-A",    "Polisher",  "Idle"),
    ("EQP-TEST01", "Prober-A",      "Prober",    "Run"),
]

# 10 inventory items (item_code, item_name, category, qty, uom, location)
INVENTORY = [
    ("RAW-WAFER-300", "300mm Blank Silicon Wafer", "Raw Material", 12000, "EA",  "WH-RAW"),
    ("RETICLE-5NM",   "EUV Reticle Set (5nm)",     "Mask",           45, "SET", "WH-MASK"),
    ("PR-EUV",        "EUV Photoresist",           "Chemical",      320, "L",   "WH-CHEM"),
    ("SLURRY-CMP",    "CMP Slurry",                "Chemical",      500, "L",   "WH-CHEM"),
    ("GAS-SIH4",      "Silane (SiH4) Gas",         "Gas",            80, "BTL", "WH-GAS"),
    ("GAS-AR",        "Argon (Ar) Gas",            "Gas",           200, "BTL", "WH-GAS"),
    ("TARGET-CU",     "Copper Sputter Target",     "Material",       30, "EA",  "WH-MAT"),
    ("FG-LX9",        "LX9 AP Finished Die",       "Finished Goods", 8600, "EA", "WH-FG"),
    ("FG-DDR5",       "DDR5-16G Finished Die",     "Finished Goods", 15400, "EA","WH-FG"),
    ("SPARE-ESC",     "Electrostatic Chuck (Spare)","Spare Part",      6, "EA",  "WH-SPARE"),
]

PRIORITIES = ["Hot", "Normal", "Normal", "Normal", "Low"]
OPERATORS = ["kim.js", "lee.mh", "park.sy", "choi.dw", "jung.hy"]

PRODUCT_TECH = {p: t for p, t in PRODUCTS}
STEP_EQP_TYPE = {code: eqp_type for _, code, _, _, eqp_type in ROUTE}
EQP_BY_TYPE: dict[str, list[str]] = {}
for _eid, _name, _type, _status in EQUIPMENT:
    EQP_BY_TYPE.setdefault(_type, []).append(_eid)


def _pick_eqp(step_code: str, rnd: random.Random) -> str | None:
    eqp_type = STEP_EQP_TYPE.get(step_code)
    choices = EQP_BY_TYPE.get(eqp_type, [])
    return rnd.choice(choices) if choices else None


def seed() -> None:
    """Reset and repopulate every table with the mock fab dataset."""
    rnd = random.Random(42)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    db.reset_db()
    with db.get_conn() as conn:
        # --- process route ------------------------------------------------- #
        conn.executemany(
            "INSERT INTO process_step (seq, step_code, step_name, operation, eqp_type)"
            " VALUES (?, ?, ?, ?, ?)",
            ROUTE,
        )
        # --- equipment ----------------------------------------------------- #
        conn.executemany(
            "INSERT INTO equipment (eqp_id, eqp_name, type, status) VALUES (?, ?, ?, ?)",
            EQUIPMENT,
        )
        # --- inventory ----------------------------------------------------- #
        conn.executemany(
            "INSERT INTO inventory (item_code, item_name, category, qty, uom, location)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            INVENTORY,
        )

        # --- lots + their process history ---------------------------------- #
        n_lots = 18
        for i in range(1, n_lots + 1):
            lot_id = f"LOT{i:04d}"
            product, tech_node = PRODUCTS[(i - 1) % len(PRODUCTS)]
            wafer_qty = rnd.choice([25, 25, 25, 24, 12])
            priority = rnd.choice(PRIORITIES)
            # how far along the route this lot is (index into ROUTE)
            step_idx = rnd.randint(0, len(ROUTE) - 1)
            start_offset = rnd.randint(2, 25)
            start_dt = now - timedelta(days=start_offset)

            # Decide status: a few done, a few on hold, most running.
            if step_idx == len(ROUTE) - 1 and rnd.random() < 0.5:
                status = "Done"
                current_idx = len(ROUTE) - 1
            elif rnd.random() < 0.12:
                status = "Hold"
                current_idx = step_idx
            else:
                status = "Running"
                current_idx = step_idx

            current_step = ROUTE[current_idx][1]
            conn.execute(
                """INSERT INTO lot
                       (lot_id, product, tech_node, wafer_qty, priority,
                        current_step, status, start_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lot_id, product, tech_node, wafer_qty, priority,
                    current_step, status, start_dt.strftime("%Y-%m-%d"),
                ),
            )

            # History: completed steps before the current one (+ the current
            # step too when the lot is Done).
            completed_upto = current_idx if status != "Done" else current_idx + 1
            t = start_dt + timedelta(hours=rnd.randint(1, 8))
            for s in range(completed_upto):
                seq, step_code, step_name, operation, eqp_type = ROUTE[s]
                dwell = timedelta(hours=rnd.randint(3, 20))
                in_time = t
                out_time = t + dwell
                result = "Pass" if rnd.random() > 0.08 else rnd.choice(["Rework", "Fail"])
                conn.execute(
                    """INSERT INTO process_history
                           (lot_id, step_code, eqp_id, in_time, out_time, operator, result)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        lot_id, step_code, _pick_eqp(step_code, rnd),
                        in_time.isoformat(), out_time.isoformat(),
                        rnd.choice(OPERATORS), result,
                    ),
                )
                t = out_time + timedelta(hours=rnd.randint(1, 12))

        # --- production results (~30 over recent dates) -------------------- #
        lines = ["FAB1-L1", "FAB1-L2", "FAB2-L1"]
        n_results = 30
        for _ in range(n_results):
            product, _tech = rnd.choice(PRODUCTS)
            days_ago = rnd.randint(0, 10)
            result_date = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            good = rnd.randint(180, 640)
            scrap = rnd.randint(0, 40)
            total = good + scrap
            yield_pct = round(good / total * 100, 2) if total else 0.0
            conn.execute(
                """INSERT INTO production_result
                       (result_date, line, eqp_id, product, good_qty, scrap_qty, yield_pct)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    result_date, rnd.choice(lines), "EQP-TEST01",
                    product, good, scrap, yield_pct,
                ),
            )


def main() -> None:
    path = db.get_db_path()
    seed()
    c = db.counts()
    print(f"[seed] mock MES database ready at {path}")
    print(
        "[seed] rows: "
        + ", ".join(f"{k}={v}" for k, v in c.items())
    )


if __name__ == "__main__":
    main()
