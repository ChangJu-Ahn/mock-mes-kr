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


if __name__ == "__main__":
    unittest.main()