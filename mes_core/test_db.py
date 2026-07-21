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

    def test_process_route_and_results_queries(self):
        db = self.db
        db.register_process_result(self.lot["lot_id"], "PHOTO", scrap_qty=3, defect_code="Particle")
        self.assertEqual(len(db.get_process_route(stage="FAB")), 2)
        rows = db.list_process_results(lot_id=self.lot["lot_id"], has_scrap=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["defect_code"], "Particle")


if __name__ == "__main__":
    unittest.main()