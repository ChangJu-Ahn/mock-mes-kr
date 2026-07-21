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


if __name__ == "__main__":
    unittest.main()