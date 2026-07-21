"""Unit tests for per-step quantity + defect logic in mes_core.db."""

import os
import unittest
from pathlib import Path

TEST_DB = Path(__file__).resolve().parents[1] / "data" / "mes_core_unit_test.db"


class DbQtyDefectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = str(TEST_DB)
        TEST_DB.parent.mkdir(exist_ok=True)
        from mes_core import db

        cls.db = db

    @classmethod
    def tearDownClass(cls):
        for f in TEST_DB.parent.glob(TEST_DB.name + "*"):
            f.unlink(missing_ok=True)

    def setUp(self):
        db = self.db
        db.reset_db()
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO process_step (seq, step_code, step_name, operation, eqp_type)"
                " VALUES (?,?,?,?,?)",
                [
                    (1, "PHOTO", "Photolithography", "Litho", "Scanner"),
                    (2, "ETCH", "Dry Etch", "Etch", "Etcher"),
                    (3, "TEST", "Wafer Test", "Test", "Tester"),
                ],
            )
            conn.execute(
                "INSERT INTO lot (lot_id, product, tech_node, start_qty, wafer_qty,"
                " priority, current_step, status, start_date)"
                " VALUES ('LOTX','PX','5nm',25,25,'Normal','PHOTO','Running','2026-01-01')"
            )

    def test_out_is_in_minus_scrap_and_lot_shrinks(self):
        db = self.db
        row = db.add_process_move("LOTX", "PHOTO", scrap_qty=2, operator="op1", defect_code="Particle")
        self.assertEqual(row["in_qty"], 25)
        self.assertEqual(row["scrap_qty"], 2)
        self.assertEqual(row["out_qty"], 23)
        self.assertEqual(row["defect_code"], "Particle")
        lot = db.get_lot("LOTX")
        self.assertEqual(lot["wafer_qty"], 23)
        self.assertEqual(lot["start_qty"], 25)

    def test_default_in_qty_follows_current_wafer_qty(self):
        db = self.db
        db.add_process_move("LOTX", "PHOTO", scrap_qty=5)  # 25 -> 20
        row2 = db.add_process_move("LOTX", "ETCH")          # in defaults to 20
        self.assertEqual(row2["in_qty"], 20)
        self.assertEqual(row2["out_qty"], 20)

    def test_scrap_greater_than_in_raises(self):
        db = self.db
        with self.assertRaises(ValueError):
            db.add_process_move("LOTX", "PHOTO", in_qty=10, scrap_qty=11)

    def test_negative_scrap_raises(self):
        db = self.db
        with self.assertRaises(ValueError):
            db.add_process_move("LOTX", "PHOTO", scrap_qty=-1)

    def test_cumulative_yield(self):
        db = self.db
        db.add_process_move("LOTX", "PHOTO", scrap_qty=5)  # 25 -> 20
        lot = db.get_lot("LOTX")
        self.assertAlmostEqual(lot["cumulative_yield"], 80.0, places=1)

    def test_last_step_creates_production_result_once(self):
        db = self.db
        db.add_process_move("LOTX", "PHOTO", scrap_qty=1)  # 25 -> 24
        db.add_process_move("LOTX", "ETCH")                # 24
        db.add_process_move("LOTX", "TEST", result="Pass")  # -> Done + auto 실적
        lot = db.get_lot("LOTX")
        self.assertEqual(lot["status"], "Done")
        results = db.list_production_results(product="PX", limit=1000)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["good_qty"], 24)
        self.assertEqual(results[0]["scrap_qty"], 1)  # cumulative scrap
        # re-passing the last step must not duplicate the auto 실적
        db.add_process_move("LOTX", "TEST", result="Pass")
        self.assertEqual(len(db.list_production_results(product="PX", limit=1000)), 1)

    def test_dashboard_summary_has_expected_keys(self):
        db = self.db
        s = db.get_dashboard_summary()
        for key in (
            "total_lots", "running_lots", "done_lots", "total_start_wafers",
            "wip_wafers", "cumulative_yield_pct", "defect_rate_pct",
            "recent_results", "wip_by_step", "equipment", "top_defects",
        ):
            self.assertIn(key, s)


if __name__ == "__main__":
    unittest.main()
