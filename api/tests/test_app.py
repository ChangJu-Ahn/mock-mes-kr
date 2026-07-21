import os
import unittest

from fastapi.testclient import TestClient


class MockMesApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = os.path.join(os.getcwd(), "data", "mes_api_unittest.db")
        from mes_core import db

        db.reset_db()
        db.init_db()
        import mes_core.seed as seed

        seed.main()
        from api.main import app

        cls.db = db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        for suffix in ("", "-shm", "-wal"):
            path = os.environ["MES_DB_PATH"] + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_rest_health_and_openapi_are_api_scoped(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["counts"]["lot"], 18)

        openapi = self.client.get("/api/openapi.json").json()
        self.assertIn("/api/production-results", openapi["paths"])
        self.assertIn("/api/wip", openapi["paths"])
        self.assertNotIn("/", openapi["paths"])
        self.assertNotIn("/process", openapi["paths"])

    def test_production_result_post_is_visible_in_rest_list(self):
        created = self.client.post(
            "/api/production-results",
            json={"product": "LX9 AP", "good_qty": 500, "scrap_qty": 10, "line": "FAB1-L1"},
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["product"], "LX9 AP")
        self.assertEqual(created.json()["good_qty"], 500)

        listed = self.client.get("/api/production-results", params={"product": "LX9 AP", "limit": 1})
        self.assertEqual(listed.status_code, 200)
        rows = listed.json()
        self.assertEqual(rows[0]["good_qty"], 500)
        self.assertEqual(rows[0]["line"], "FAB1-L1")

    def test_web_pages_render_and_process_form_updates_lot(self):
        for path, expected in [
            ("/", "Dashboard"),
            ("/production", "Production"),
            ("/inventory", "Inventory"),
            ("/wip", "WIP"),
            ("/process", "Process"),
            ("/lots", "Lots"),
        ]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(expected, response.text)

        response = self.client.post(
            "/process/moves",
            data={"lot_id": "LOT0001", "step_code": "TEST", "eqp_id": "EQP-TEST01", "operator": "qa", "result": "Pass"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.db.get_lot("LOT0001")["current_step"], "TEST")

        detail = self.client.get("/lots/LOT0001")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("LOT0001", detail.text)
        self.assertIn("TEST", detail.text)


if __name__ == "__main__":
    unittest.main()
