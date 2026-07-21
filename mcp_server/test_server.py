import os
import subprocess
import sys
import unittest
from pathlib import Path


TEST_DB = Path(__file__).resolve().parents[1] / "data" / "mes_mcp_unit_test.db"


class MCPPToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = str(TEST_DB)
        TEST_DB.parent.mkdir(exist_ok=True)
        subprocess.run([sys.executable, "-m", "mes_core.seed"], check=True, cwd=Path(__file__).resolve().parents[1])

    @classmethod
    def tearDownClass(cls):
        for db_file in TEST_DB.parent.glob(TEST_DB.name + "*"):
            db_file.unlink(missing_ok=True)

    def test_process_move_updates_lot_and_history(self):
        from mes_core import db
        from mcp_server.server import get_lot, get_process_history, register_process_move

        lot_id = db.list_lot_ids()[0]
        before = get_lot(lot_id)

        result = register_process_move(lot_id, "ETCH", eqp_id="EQP-ETCH01", operator="mcp-test", result="Pass")

        after = get_lot(lot_id)
        history = get_process_history(lot_id=lot_id, limit=10)
        self.assertNotIn("error", result)
        self.assertEqual(result["lot_id"], lot_id)
        self.assertEqual(result["step_code"], "ETCH")
        self.assertNotEqual(before["current_step"], after["current_step"])
        self.assertEqual(after["current_step"], "ETCH")
        self.assertTrue(any(row["id"] == result["id"] for row in history))

    def test_process_move_returns_clean_error_for_bad_lot(self):
        from mcp_server.server import register_process_move

        result = register_process_move("NO-SUCH-LOT", "ETCH")

        self.assertIn("error", result)
        self.assertIn("NO-SUCH-LOT", result["error"])

    def test_query_tools_return_json_serializable_data(self):
        import json
        from mes_core import db
        from mcp_server.server import get_lot, get_process_route, list_lots

        lot_id = db.list_lot_ids()[0]
        payloads = [get_process_route(), list_lots(limit=3), get_lot(lot_id), get_lot("NO-SUCH-LOT")]

        for payload in payloads:
            json.dumps(payload)
        self.assertGreater(len(payloads[0]), 0)
        self.assertLessEqual(len(payloads[1]), 3)
        self.assertIn("not_found", payloads[3])

    def test_process_move_with_scrap_reduces_wafers(self):
        from mes_core import db
        from mcp_server.server import get_lot, register_process_move

        lot_id = db.list_lots(status="Running", limit=1)[0]["lot_id"]
        before = get_lot(lot_id)["wafer_qty"]

        res = register_process_move(
            lot_id, "CMP", eqp_id="EQP-CMP01", in_qty=before,
            scrap_qty=2, defect_code="Scratch", operator="mcp-scrap",
        )

        self.assertNotIn("error", res)
        self.assertEqual(res["in_qty"], before)
        self.assertEqual(res["scrap_qty"], 2)
        self.assertEqual(res["out_qty"], before - 2)
        self.assertEqual(res["defect_code"], "Scratch")
        self.assertEqual(get_lot(lot_id)["wafer_qty"], before - 2)

    def test_history_has_scrap_filter(self):
        from mcp_server.server import get_process_history

        rows = get_process_history(has_scrap=True, limit=1000)
        self.assertTrue(rows)
        self.assertTrue(all((row["scrap_qty"] or 0) > 0 for row in rows))

    def test_list_lots_priority_filter(self):
        from mcp_server.server import list_lots

        priority = list_lots(limit=1000)[0]["priority"]
        rows = list_lots(priority=priority, limit=1000)
        self.assertTrue(all(row["priority"] == priority for row in rows))


if __name__ == "__main__":
    unittest.main()
