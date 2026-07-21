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
