"""Unit tests for the redesigned mock MES data layer (mes_core.db)."""

import importlib
import os
import time
import unittest
from datetime import datetime, timezone
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

    def test_upsert_bom_preserves_uom_when_omitted(self):
        """Verify that upsert_bom preserves existing uom when caller omits it (passes None)."""
        db = self.db
        # Initial state: P1/PKG/SUBSTR has qty=1.0, uom='EA' (seeded)
        initial = db.list_bom("P1", "PKG")[0]
        self.assertEqual(initial["qty_per_wafer"], 1.0)
        self.assertEqual(initial["uom"], "EA")
        
        # Upsert same key with new qty but NO uom (uom=None, omitted)
        db.upsert_bom("P1", "PKG", "SUBSTR", 3.0)
        
        # Verify: qty updated, uom PRESERVED (not set to NULL)
        updated = db.list_bom("P1", "PKG")[0]
        self.assertEqual(updated["qty_per_wafer"], 3.0)
        self.assertEqual(updated["uom"], "EA", "uom should be preserved when omitted in upsert")
        
    def test_upsert_bom_updates_uom_when_provided(self):
        """Verify that passing a new uom DOES update it."""
        db = self.db
        # Initial state: P1/PKG/SUBSTR has uom='EA'
        initial = db.list_bom("P1", "PKG")[0]
        self.assertEqual(initial["uom"], "EA")
        
        # Upsert same key with new uom
        db.upsert_bom("P1", "PKG", "SUBSTR", 2.5, uom="BOX")
        
        # Verify: uom updated
        updated = db.list_bom("P1", "PKG")[0]
        self.assertEqual(updated["uom"], "BOX")


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

    def test_get_lot_unknown_returns_none(self):
        self.assertIsNone(self.db.get_lot("NOPE"))

    def test_get_wip_excludes_done_lots(self):
        db = self.db
        done_lot = db.start_lot("P1", 10)
        lot_id = done_lot["lot_id"]
        with db.get_conn() as c:
            c.execute("UPDATE lot SET status='Done' WHERE lot_id=?", (lot_id,))
        running_lot = db.start_lot("P1", 5)
        running_step = running_lot["current_step"]
        wip = db.get_wip()
        wip_lot_ids = set()
        for row in wip:
            if row["step_code"] == running_step:
                self.assertGreaterEqual(row["lot_count"], 1)
        # Done lot's step should not appear with lot_count that includes it
        done_step_rows = [w for w in wip if w["step_code"] == done_lot["current_step"]]
        if done_step_rows:
            # The only remaining lot at that step is the running one (if same step)
            self.assertEqual(done_step_rows[0]["lot_count"], 1)
        else:
            # No WIP row for that step at all is also correct (Done lot excluded)
            pass
        # Running lot must appear
        self.assertTrue(any(w["step_code"] == running_step for w in wip))

    def test_next_lot_id_ignores_custom_lot_ids(self):
        db = self.db
        # Auto-generate first: LOT0001 will exist
        first_auto = db.start_lot("P1", 10)
        self.assertRegex(first_auto["lot_id"], r'^LOT\d{4}$')
        # Insert a non-numeric custom id — DESC sort puts LOTCUSTOM ahead of LOT0001
        db.start_lot("P1", 10, lot_id="LOTCUSTOM")
        # Auto-generate again: broken code picks LOTCUSTOM -> ValueError -> n=0 -> LOT0001 (collision!)
        # Fixed code correctly yields LOT0002
        auto_lot = db.start_lot("P1", 5)
        auto_id = auto_lot["lot_id"]
        self.assertNotEqual(auto_id, "LOTCUSTOM")
        self.assertNotEqual(auto_id, first_auto["lot_id"])
        self.assertRegex(auto_id, r'^LOT\d{4}$')


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

    def test_unknown_lot_rejected(self):
        with self.assertRaises(ValueError):
            self.db.register_process_result("NO-SUCH-LOT", "PHOTO")

    def test_process_route_and_results_queries(self):
        db = self.db
        db.register_process_result(self.lot["lot_id"], "PHOTO", scrap_qty=3, defect_code="Particle")
        self.assertEqual(len(db.get_process_route(stage="FAB")), 2)
        rows = db.list_process_results(lot_id=self.lot["lot_id"], has_scrap=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["defect_code"], "Particle")


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
        self.assertEqual(res["shortages"], [])

    def test_package_insufficient_semi_rejected(self):
        with self.assertRaises(ValueError):
            self.db.package("P1", 999)

    def test_package_unknown_product_rejected(self):
        with self.assertRaises(ValueError):
            self.db.package("NOPE", 1)

    def test_package_bad_scrap_rejected(self):
        db = self.db
        with db.get_conn() as conn:
            db._add_product_inventory(conn, "P1", "SEMI", 200)
        with self.assertRaises(ValueError):
            db.package("P1", 10, scrap_qty=11)   # scrap > in
        with self.assertRaises(ValueError):
            db.package("P1", 10, scrap_qty=-1)   # scrap < 0

    def test_package_material_shortage_is_nonfatal(self):
        db = self.db
        with db.get_conn() as conn:
            db._add_product_inventory(conn, "P1", "SEMI", 200)
            conn.execute("UPDATE material SET qty=5 WHERE material_code='SUBSTR'")
        res = db.package("P1", 20)   # needs 20 SUBSTR, only 5 available
        self.assertIsNotNone(res)
        self.assertTrue(len(res["shortages"]) > 0)
        codes = [s["material_code"] for s in res["shortages"]]
        self.assertIn("SUBSTR", codes)
        fin = db.list_product_inventory(product_code="P1", item_type="FIN")
        self.assertGreater(fin[0]["qty"], 0)   # FIN inventory increased
        self.assertEqual(db.get_material("SUBSTR")["qty"], 0.0)   # floored at 0


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
        for key in ("lot_total", "lots_by_status", "wafers_started", "wafers_current",
                    "wip_total_lots", "wip_by_step", "semi_total", "fin_total",
                    "material_total_items", "low_materials", "top_defects",
                    "recent_process", "recent_products", "equipment"):
            self.assertIn(key, s)
        self.assertEqual(s["wip_total_lots"], sum(w["lot_count"] for w in s["wip_by_step"]))


class TimeArgumentTests(DbTestBase):
    def test_start_lot_accepts_explicit_start_date(self):
        self._seed_master()
        lot = self.db.start_lot("P1", 25, start_date="2026-08-30")
        self.assertEqual(lot["start_date"], "2026-08-30")

    def test_start_lot_defaults_to_today(self):
        self._seed_master()
        lot = self.db.start_lot("P1", 25)
        self.assertRegex(lot["start_date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_process_result_keeps_supplied_times(self):
        self._seed_master()
        lot = self.db.start_lot("P1", 25)
        self.db.register_process_result(
            lot["lot_id"], "PHOTO", in_qty=25,
            in_time="2026-08-30T01:00:00+00:00",
            out_time="2026-08-30T02:00:00+00:00",
        )
        rows = self.db.list_process_results(lot_id=lot["lot_id"])
        self.assertEqual(rows[0]["in_time"], "2026-08-30T01:00:00+00:00")
        self.assertEqual(rows[0]["out_time"], "2026-08-30T02:00:00+00:00")

    def test_auto_fab_receipt_is_dated_from_out_time(self):
        """A lot that finished last week must not book its SEMI receipt today."""
        self._seed_master()
        lot = self.db.start_lot("P1", 25)
        self.db.register_process_result(
            lot["lot_id"], "PHOTO", in_qty=25,
            in_time="2026-08-30T01:00:00+00:00",
            out_time="2026-08-30T02:00:00+00:00",
        )
        self.db.register_process_result(
            lot["lot_id"], "TEST", in_qty=25, result="Pass",
            in_time="2026-08-30T03:00:00+00:00",
            out_time="2026-08-30T05:00:00+00:00",
        )
        auto = self.db.list_product_results(source="AUTO_FAB")
        self.assertEqual(len(auto), 1)
        self.assertEqual(auto[0]["result_date"], "2026-08-30")


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
        self.assertEqual(c["bom"], 48)
        self.assertEqual(c["process_result"], 91)
        self.assertEqual(c["lot"], 16)

    def test_done_lots_produced_semi(self):
        done = self.db.list_lots(status="Done")
        self.assertEqual(len(done), 6)
        auto_fab = self.db.list_product_results(source="AUTO_FAB")
        self.assertEqual(len(auto_fab), 6)
        semi = self.db.list_product_inventory(item_type="SEMI")
        self.assertEqual(sum(r["qty"] for r in semi), 69)

    def test_packaging_and_manual_results_present(self):
        self.assertEqual(len(self.db.list_product_results(source="AUTO_PACK")), 3)
        self.assertEqual(len(self.db.list_product_results(source="MANUAL")), 1)
        fin = self.db.list_product_inventory(item_type="FIN")
        self.assertEqual(sum(r["qty"] for r in fin), 277)

    def test_wip_only_non_done(self):
        wip = self.db.get_wip()
        self.assertEqual(sum(w["lot_count"] for w in wip), 10)  # 2 Hold + 8 Running

    # --- time axis ---------------------------------------------------------

    def _results(self):
        rows = self.db.list_process_results(limit=500)
        self.assertEqual(len(rows), 91)
        return rows

    def test_process_results_have_distinct_in_and_out_times(self):
        for r in self._results():
            self.assertLess(r["in_time"], r["out_time"], r["lot_id"] + r["step_code"])

    def test_process_results_span_more_than_two_days(self):
        rows = self._results()
        lo = min(r["in_time"] for r in rows)
        hi = max(r["out_time"] for r in rows)
        span_h = (
            datetime.fromisoformat(hi) - datetime.fromisoformat(lo)
        ).total_seconds() / 3600
        self.assertGreater(span_h, 48)
        self.assertLess(span_h, 96)

    def test_no_process_result_is_in_the_future(self):
        now = datetime.now(timezone.utc).isoformat()
        for r in self._results():
            self.assertLessEqual(r["out_time"], now)

    def test_ids_are_assigned_in_chronological_order(self):
        """The console lists 'recent' results by id DESC, so id must track time."""
        rows = sorted(self._results(), key=lambda r: r["id"])
        times = [r["out_time"] for r in rows]
        self.assertEqual(times, sorted(times))

    def test_steps_within_a_lot_run_in_route_order(self):
        by_lot = {}
        for r in self._results():
            by_lot.setdefault(r["lot_id"], []).append(r)
        for lot_id, rows in by_lot.items():
            rows.sort(key=lambda r: r["id"])
            for prev, cur in zip(rows, rows[1:]):
                self.assertLessEqual(prev["out_time"], cur["in_time"], lot_id)

    def test_one_equipment_never_runs_two_lots_at_once(self):
        by_eqp = {}
        for r in self._results():
            if r["eqp_id"]:
                by_eqp.setdefault(r["eqp_id"], []).append(r)
        for eqp_id, rows in by_eqp.items():
            rows.sort(key=lambda r: r["in_time"])
            for prev, cur in zip(rows, rows[1:]):
                self.assertLessEqual(prev["out_time"], cur["in_time"], eqp_id)

    def test_lot_start_date_precedes_its_first_step(self):
        by_lot = {}
        for r in self._results():
            by_lot.setdefault(r["lot_id"], []).append(r)
        for lot in self.db.list_lots(limit=100):
            rows = by_lot.get(lot["lot_id"])
            if not rows:
                continue
            first_in = min(r["in_time"] for r in rows)
            self.assertLessEqual(lot["start_date"], first_in[:10], lot["lot_id"])

    def test_lot_start_dates_are_not_all_the_same_day(self):
        days = {l["start_date"] for l in self.db.list_lots(limit=100)}
        self.assertGreater(len(days), 1)

    def test_auto_fab_receipts_follow_their_lots(self):
        finals = {}
        for r in self._results():
            if r["step_code"] == "TEST":
                finals[r["lot_id"]] = r["out_time"][:10]
        auto = self.db.list_product_results(source="AUTO_FAB", limit=100)
        self.assertEqual(len(auto), 6)
        for row in auto:
            self.assertEqual(row["result_date"], finals[row["lot_id"]])


class KeyStabilityTests(unittest.TestCase):
    """The seed's key set is a public contract.

    The deployed app stores its SQLite DB on an ephemeral EmptyDir volume and
    re-runs ``mes_core.seed`` from an init container on every replica start, so
    with scale-to-zero the whole database is wiped and rebuilt routinely. That
    is only safe because ``seed()`` loads a literal file (``dataset.json``):
    the same identifiers come back every time.

    External demos connect to this MES by those identifiers, so anything that
    makes the seed non-reproducible -- deriving rows at boot, renaming a
    product, changing the lot count -- would silently break them on the next
    cold start. These tests turn that implicit property into an enforced one.
    """

    DB = Path(__file__).resolve().parents[1] / "data" / "mes_key_stability_test.db"

    LOTS = tuple(f"LOT{i:04d}" for i in range(1, 17))
    PRODUCTS = ("DDR5", "LX9", "NAND", "PMIC")
    MATERIALS = ("BOND-WIRE", "DOPANT-B", "GAS-AR", "GAS-SIH4", "MOLD-EMC", "PR-EUV",
                 "RAW-WAFER-300", "RETICLE-5NM", "SLURRY-CMP", "SOLDER-BALL",
                 "SUBSTRATE", "TARGET-CU")
    STEPS = ("DIFF", "PHOTO", "ETCH", "IMPL", "CVD", "CMP", "METRO", "TEST", "PKG")
    EQUIPMENT = ("EQP-CMP01", "EQP-CVD01", "EQP-DIFF01", "EQP-ETCH01", "EQP-IMPL01",
                 "EQP-PHOT01", "EQP-PHOT02", "EQP-PKG01", "EQP-TEST01")
    BOM_IDS = tuple(range(1, 49))

    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = str(cls.DB)
        cls.DB.parent.mkdir(exist_ok=True)
        from mes_core import db, seed
        importlib.reload(db)
        importlib.reload(seed)
        cls.db, cls.seed = db, seed

    @classmethod
    def tearDownClass(cls):
        for f in cls.DB.parent.glob(cls.DB.name + "*"):
            f.unlink(missing_ok=True)

    def _keys(self) -> dict[str, list]:
        db = self.db
        return {
            "lots": sorted(l["lot_id"] for l in db.list_lots()),
            "products": sorted(p["product_code"] for p in db.list_products()),
            "materials": sorted(m["material_code"] for m in db.list_materials()),
            "steps": db.list_step_codes(),
            "equipment": sorted(e["eqp_id"] for e in db.list_equipment()),
            "bom": sorted(b["id"] for b in db.list_bom()),
        }

    def _process_times(self) -> list[tuple]:
        rows = self.db.list_process_results(limit=500)
        return [(r["id"], r["in_time"], r["out_time"]) for r in rows]

    def test_seed_produces_the_documented_key_baseline(self):
        self.seed.seed()
        keys = self._keys()
        self.assertEqual(keys["lots"], list(self.LOTS))
        self.assertEqual(keys["products"], sorted(self.PRODUCTS))
        self.assertEqual(keys["materials"], sorted(self.MATERIALS))
        self.assertEqual(keys["steps"], list(self.STEPS))  # route order matters
        self.assertEqual(keys["equipment"], sorted(self.EQUIPMENT))
        self.assertEqual(keys["bom"], list(self.BOM_IDS))

    def test_reseeding_after_mutation_restores_identical_keys(self):
        """Simulates an ACA cold start: wipe + re-seed must be a no-op on keys."""
        self.seed.seed()
        before = self._keys()

        # Mutate the way a demo would: add a lot, delete a BOM row.
        self.db.start_lot("LX9", 25)
        self.db.delete_bom(before["bom"][0])
        self.assertNotEqual(self._keys(), before)

        self.seed.seed()
        self.assertEqual(self._keys(), before)

    def test_bom_ids_are_stable_because_autoincrement_is_reset(self):
        # BOM is the only deletable entity, and it is keyed by AUTOINCREMENT.
        # reset_db() clears sqlite_sequence, so ids restart at 1 -- without that,
        # every re-seed would hand out fresh ids and break BOM-keyed demos.
        self.seed.seed()
        first = sorted(b["id"] for b in self.db.list_bom())
        self.seed.seed()
        self.assertEqual(sorted(b["id"] for b in self.db.list_bom()), first)
        self.assertEqual(first[0], 1)

    def test_keys_are_stable_even_though_the_clock_moves(self):
        """Wall-clock time passing between two cold starts changes nothing.

        Deliberately exercises the real clock rather than patching a helper,
        so this holds however the seed derives its timestamps.
        """
        pinned = os.environ.pop("MES_ANCHOR", None)  # cold-start default
        try:
            self.seed.seed()
            keys, times = self._keys(), self._process_times()
            self.assertTrue(times)

            time.sleep(1.1)  # timestamps have second precision
            self.seed.seed()

            self.assertEqual(self._keys(), keys)
            self.assertEqual(self._process_times(), times)
        finally:
            if pinned is not None:
                os.environ["MES_ANCHOR"] = pinned


class MasterDataCrudTests(DbTestBase):
    """Master data is editable so external clients can run mutation tests.

    Nothing is durable -- a boot reloads ``dataset.json`` -- so the risk of a
    bad edit is bounded by a restart. What is *not* acceptable is a write that
    leaves the data self-contradictory, which this schema cannot prevent on its
    own: it declares no foreign keys, so a deleted product would leave lots
    pointing at nothing. The guard lives in the data layer and is pinned here.
    """

    def setUp(self):
        super().setUp()
        self._seed_master()

    def test_create_then_read_back_a_product(self):
        created = self.db.create_product("AP10", "AP10 Mobile SoC", "3nm")
        self.assertEqual(created["product_name"], "AP10 Mobile SoC")
        self.assertEqual(self.db.get_product("AP10"), created)

    def test_create_rejects_a_duplicate_or_unusable_code(self):
        self.db.create_product("AP10", "AP10 Mobile SoC")
        for code in ("AP10", "", "   ", "has space", "x" * 41):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.db.create_product(code, "Anything")

    def test_update_changes_only_what_it_is_given(self):
        self.db.create_product("AP10", "AP10 Mobile SoC", "3nm")
        row = self.db.update_product("AP10", product_name="AP10 (rev B)")
        self.assertEqual(row["product_name"], "AP10 (rev B)")
        self.assertEqual(row["tech_node"], "3nm")

    def test_update_refuses_to_blank_a_name(self):
        self.db.create_product("AP10", "AP10 Mobile SoC")
        with self.assertRaises(ValueError):
            self.db.update_product("AP10", product_name="   ")

    def test_update_of_a_missing_row_reports_rather_than_creates(self):
        self.assertIsNone(self.db.update_product("NOPE", product_name="x"))
        self.assertIsNone(self.db.update_equipment("NOPE", eqp_name="x"))

    def test_unused_master_data_can_be_deleted(self):
        self.db.create_product("AP10", "AP10 Mobile SoC")
        self.db.create_equipment("EQP-NEW01", "Etcher-B", type="Etcher")
        self.assertTrue(self.db.delete_product("AP10"))
        self.assertTrue(self.db.delete_equipment("EQP-NEW01"))
        self.assertIsNone(self.db.get_product("AP10"))
        self.assertFalse(self.db.delete_product("AP10"))

    def test_a_referenced_product_cannot_be_deleted(self):
        self.db.start_lot("P1", 25)
        with self.assertRaises(ValueError) as caught:
            self.db.delete_product("P1")
        self.assertIn("lot", str(caught.exception))
        self.assertIsNotNone(self.db.get_product("P1"))

    def test_a_referenced_tool_cannot_be_deleted(self):
        lot = self.db.start_lot("P1", 25)
        self.db.register_process_result(lot["lot_id"], "PHOTO", eqp_id="EQP-PHOT01")
        with self.assertRaises(ValueError) as caught:
            self.db.delete_equipment("EQP-PHOT01")
        self.assertIn("process result", str(caught.exception))
        self.assertIsNotNone(self.db.get_equipment("EQP-PHOT01"))

    def test_equipment_status_is_constrained_and_normalised(self):
        self.db.create_equipment("EQP-NEW01", "Etcher-B", status="run")
        self.assertEqual(self.db.get_equipment("EQP-NEW01")["status"], "Run")
        with self.assertRaises(ValueError):
            self.db.update_equipment("EQP-NEW01", status="Broken")

    def test_equipment_defaults_to_idle(self):
        self.assertEqual(self.db.create_equipment("EQP-NEW01", "Etcher-B")["status"], "Idle")


class SeedImmutabilityTests(unittest.TestCase):
    """What survives a boot, and what deliberately moves.

    ``/data`` is an EmptyDir volume, so the seed runs on every boot -- a cold
    start and a redeploy are the same path. Two different promises come out of
    that, and both matter to systems built on top of this MES:

    * **The rows are fixed.** Every identifier, quantity, yield and defect code
      is a literal in ``dataset.json``, so they cannot drift when someone edits
      the code that first produced them.
    * **The timeline slides, rigidly.** The history is translated so it starts
      three months before the boot date, which keeps a long-running demo
      looking current. It is a pure translation by whole days: every duration,
      every gap and every time of day is preserved exactly, so a sensor archive
      that brackets its readings by a lot's ``in_time``/``out_time`` still sees
      the same 55-minute window it always did.

    Pin ``MES_HISTORY_START`` to freeze the dates outright.
    """

    DB = Path(__file__).resolve().parents[1] / "data" / "mes_immutability_test.db"

    #: The date the pinned expectations below are expressed against.
    PINNED_START = "2026-06-10"

    # LOT0001's route as an external store would have recorded it, given
    # PINNED_START. The shape -- order, tools, durations, gaps -- is the join
    # key; only the calendar is allowed to move.
    LOT0001_WINDOWS = (
        ("DIFF", "EQP-DIFF01", "2026-06-10T07:13:00+00:00", "2026-06-10T08:58:00+00:00"),
        ("PHOTO", "EQP-PHOT01", "2026-06-10T09:14:00+00:00", "2026-06-10T10:09:00+00:00"),
        ("ETCH", "EQP-ETCH01", "2026-06-10T10:27:00+00:00", "2026-06-10T12:11:00+00:00"),
        ("IMPL", "EQP-IMPL01", "2026-06-10T12:28:00+00:00", "2026-06-10T13:25:00+00:00"),
        ("CVD", "EQP-CVD01", "2026-06-10T13:42:00+00:00", "2026-06-10T16:21:00+00:00"),
        ("CMP", "EQP-CMP01", "2026-06-10T16:46:00+00:00", "2026-06-10T17:20:00+00:00"),
        ("METRO", None, "2026-06-10T17:30:00+00:00", "2026-06-10T17:53:00+00:00"),
        ("TEST", "EQP-TEST01", "2026-06-10T18:24:00+00:00", "2026-06-10T21:10:00+00:00"),
    )

    @classmethod
    def setUpClass(cls):
        os.environ["MES_HISTORY_START"] = cls.PINNED_START
        os.environ["MES_DB_PATH"] = str(cls.DB)
        cls.DB.parent.mkdir(exist_ok=True)
        from mes_core import db, seed
        importlib.reload(db)
        importlib.reload(seed)
        cls.db, cls.seed = db, seed

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("MES_HISTORY_START", None)
        for f in cls.DB.parent.glob(cls.DB.name + "*"):
            f.unlink(missing_ok=True)

    def _snapshot(self) -> dict[str, list[tuple]]:
        """Every row of every table, ordered, as comparable tuples."""
        out = {}
        with self.db.get_conn() as conn:
            for table in self.db.TABLES:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                order = ", ".join(f'"{c}"' for c in cols)
                rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
                out[table] = [tuple(r) for r in rows]
        return out

    def test_reseeding_reproduces_every_table_byte_for_byte(self):
        self.seed.seed()
        before = self._snapshot()
        self.assertTrue(all(before[t] for t in self.db.TABLES))

        time.sleep(1.1)  # a later cold start must not mean later timestamps
        self.seed.seed()

        after = self._snapshot()
        for table in self.db.TABLES:
            self.assertEqual(after[table], before[table], table)

    def test_reseeding_after_a_deletion_restores_it_byte_for_byte(self):
        """The console's unauthenticated BOM delete must be self-healing."""
        self.seed.seed()
        before = self._snapshot()

        self.db.delete_bom(before["bom"][0][0])
        self.db.start_lot("LX9", 25)
        self.assertNotEqual(self._snapshot(), before)

        self.seed.seed()
        self.assertEqual(self._snapshot(), before)

    def test_lot0001_windows_match_the_published_baseline(self):
        self.seed.seed()
        rows = sorted(self.db.list_process_results(lot_id="LOT0001", limit=50),
                      key=lambda r: r["id"])
        got = tuple((r["step_code"], r["eqp_id"], r["in_time"], r["out_time"])
                    for r in rows)
        self.assertEqual(got, self.LOT0001_WINDOWS)

    def test_every_window_is_a_usable_sensor_interval(self):
        """Each row must describe a real span on a real tool, in the past.

        A zero-length or inverted window would give a sensor archive nothing
        to bracket its readings with.
        """
        self.seed.seed()
        rows = self.db.list_process_results(limit=500)
        self.assertEqual(len(rows), 91)
        now = datetime.now(timezone.utc).isoformat()
        for r in rows:
            where = f"{r['lot_id']}/{r['step_code']}"
            self.assertLess(r["in_time"], r["out_time"], where)
            self.assertLess(r["out_time"], now, where)


class HistoryWindowTests(unittest.TestCase):
    """The timeline slides on each boot, but only ever as a rigid whole.

    This is the half of the contract external systems actually consume. They
    do not care which calendar day a lot ran, they care that the step took 55
    minutes and that the tool was free for the next lot 17 minutes later. So
    the translation has to preserve every interval exactly, and the history has
    to stay in the recent past rather than drifting into the future.
    """

    DB = Path(__file__).resolve().parents[1] / "data" / "mes_history_test.db"

    @classmethod
    def setUpClass(cls):
        os.environ.pop("MES_HISTORY_START", None)  # exercise the shipped default
        os.environ["MES_DB_PATH"] = str(cls.DB)
        cls.DB.parent.mkdir(exist_ok=True)
        from mes_core import db, seed
        importlib.reload(db)
        importlib.reload(seed)
        cls.db, cls.seed = db, seed

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("MES_HISTORY_START", None)
        for f in cls.DB.parent.glob(cls.DB.name + "*"):
            f.unlink(missing_ok=True)

    def _windows(self) -> list[tuple[datetime, datetime]]:
        rows = sorted(self.db.list_process_results(limit=500), key=lambda r: r["id"])
        return [(datetime.fromisoformat(r["in_time"]), datetime.fromisoformat(r["out_time"]))
                for r in rows]

    def _fixture_windows(self) -> list[tuple[datetime, datetime]]:
        raw = self.seed.load_dataset()["process_result"]
        cols = self.seed.TIME_COLUMNS["process_result"]
        with self.db.get_conn() as conn:
            names = [r[1] for r in conn.execute("PRAGMA table_info(process_result)")]
        i, o = (names.index(c) for c in cols)
        return [(datetime.fromisoformat(r[i]), datetime.fromisoformat(r[o]))
                for r in sorted(raw, key=lambda r: r[names.index("id")])]

    def test_history_starts_three_months_before_today(self):
        self.seed.seed()
        first = min(w[0] for w in self._windows()).date()
        expected = datetime.now(timezone.utc).date() - self.seed.HISTORY_STARTS_AGO
        self.assertEqual(first, expected)

    def test_the_whole_history_moves_by_one_rigid_offset(self):
        """A translation, not a rescale: one delta explains every row."""
        self.seed.seed()
        offsets = {live[0] - fixture[0]
                   for fixture, live in zip(self._fixture_windows(), self._windows())}
        self.assertEqual(len(offsets), 1, f"expected one uniform shift, got {offsets}")
        self.assertEqual(offsets.pop().microseconds, 0)

    def test_every_duration_and_gap_survives_the_shift(self):
        self.seed.seed()
        before, after = self._fixture_windows(), self._windows()
        self.assertEqual([b[1] - b[0] for b in before], [a[1] - a[0] for a in after])
        gaps = lambda w: [w[i + 1][0] - w[i][0] for i in range(len(w) - 1)]
        self.assertEqual(gaps(before), gaps(after))

    def test_times_of_day_are_preserved(self):
        """The shift is whole days, so a 07:13 start stays a 07:13 start."""
        self.seed.seed()
        self.assertEqual([b[0].timetz() for b in self._fixture_windows()],
                         [a[0].timetz() for a in self._windows()])

    def test_lots_and_product_results_move_with_their_steps(self):
        """Every time-bearing column shifts together, or the data contradicts itself."""
        self.seed.seed()
        firsts = {}
        for r in self.db.list_process_results(limit=500):
            firsts.setdefault(r["lot_id"], []).append(r["in_time"])
        for lot in self.db.list_lots():
            earliest = min(firsts.get(lot["lot_id"], ["9999"]))
            self.assertLessEqual(lot["start_date"], earliest[:10], lot["lot_id"])

    def test_pinning_the_start_makes_the_dataset_reproducible(self):
        os.environ["MES_HISTORY_START"] = "2026-01-15"
        try:
            self.seed.seed()
            pinned = self._windows()
            self.assertEqual(min(w[0] for w in pinned).date().isoformat(), "2026-01-15")

            self.seed.seed()
            self.assertEqual(self._windows(), pinned)
        finally:
            os.environ.pop("MES_HISTORY_START", None)

    def test_two_boots_on_the_same_day_are_identical(self):
        """A restart mid-day must not move anything."""
        self.seed.seed()
        before = self._windows()
        time.sleep(1.1)
        self.seed.seed()
        self.assertEqual(self._windows(), before)


if __name__ == "__main__":
    unittest.main()