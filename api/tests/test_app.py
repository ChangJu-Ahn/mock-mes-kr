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
        cls.client = TestClient(app, headers={"X-API-Key": "changjuahn"})

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


    def test_enriched_rest_query_surface(self):
        client = self.client

        products = client.get("/api/products")
        self.assertEqual(products.status_code, 200)
        self.assertIn("LX9 AP", products.json())

        summary = client.get("/api/production-results/summary", params={"group_by": "product"})
        self.assertEqual(summary.status_code, 200)
        self.assertTrue(all("group" in row and "avg_yield" in row for row in summary.json()))
        self.assertEqual(client.get("/api/production-results/summary", params={"group_by": "bogus"}).status_code, 422)

        filtered = client.get("/api/production-results", params={"min_yield": 98, "sort": "yield", "order": "desc"})
        self.assertEqual(filtered.status_code, 200)
        ylist = [row["yield_pct"] for row in filtered.json()]
        self.assertTrue(all(y >= 98 for y in ylist))
        self.assertEqual(ylist, sorted(ylist, reverse=True))

        one = client.get("/api/production-results", params={"limit": 1}).json()[0]
        self.assertEqual(client.get(f"/api/production-results/{one['id']}").status_code, 200)
        self.assertEqual(client.get("/api/production-results/999999").status_code, 404)

        inv = client.get("/api/inventory", params={"q": "wafer", "min_qty": 1})
        self.assertEqual(inv.status_code, 200)
        self.assertTrue(all("wafer" in row["item_name"].lower() or "wafer" in row["item_code"].lower() for row in inv.json()))

        facets = client.get("/api/inventory/facets").json()
        self.assertIn("categories", facets)
        self.assertIn("locations", facets)

        item_code = client.get("/api/inventory", params={"limit": 1}).json()[0]["item_code"]
        self.assertEqual(client.get(f"/api/inventory/{item_code}").status_code, 200)
        self.assertEqual(client.get("/api/inventory/NO-SUCH-ITEM").status_code, 404)

        wip = client.get("/api/wip", params={"step_code": "DIFF"})
        self.assertEqual(wip.status_code, 200)
        self.assertTrue(all(row["step_code"] == "DIFF" for row in wip.json()))

    def test_dashboard_shows_kpis_and_process_form_records_scrap(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("Cumulative yield", home.text)
        self.assertIn("Top defects", home.text)

        running = self.db.list_lots(status="Running", limit=1)[0]
        lot_id = running["lot_id"]
        before = self.db.get_lot(lot_id)["wafer_qty"]

        response = self.client.post(
            "/process/moves",
            data={
                "lot_id": lot_id, "step_code": "ETCH", "eqp_id": "EQP-ETCH01",
                "in_qty": str(before), "scrap_qty": "3", "defect_code": "Particle",
                "operator": "web-qa", "result": "Pass",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        after = self.db.get_lot(lot_id)
        self.assertEqual(after["wafer_qty"], before - 3)
        latest = after["history"][-1]
        self.assertEqual(latest["scrap_qty"], 3)
        self.assertEqual(latest["defect_code"], "Particle")

        detail = self.client.get(f"/lots/{lot_id}")
        self.assertIn("Cumulative yield", detail.text)
        self.assertIn("Particle", detail.text)


    def test_api_key_gate(self):
        from api.main import app

        nokey = TestClient(app)  # no default header

        # data endpoints require the key
        self.assertEqual(nokey.get("/api/wip").status_code, 401)
        self.assertEqual(nokey.get("/api/production-results").status_code, 401)
        self.assertEqual(nokey.get("/api/health").status_code, 401)
        self.assertEqual(
            nokey.post("/api/production-results", json={"product": "X", "good_qty": 1}).status_code,
            401,
        )
        # wrong key -> 401
        self.assertEqual(nokey.get("/api/wip", headers={"X-API-Key": "nope"}).status_code, 401)
        # correct key -> 200
        self.assertEqual(nokey.get("/api/wip", headers={"X-API-Key": "changjuahn"}).status_code, 200)
        # web console + docs stay open (human-facing, no key)
        self.assertEqual(nokey.get("/").status_code, 200)
        self.assertEqual(nokey.get("/process").status_code, 200)
        self.assertEqual(nokey.get("/api/docs").status_code, 200)
        self.assertEqual(nokey.get("/api/openapi.json").status_code, 200)
        # the security scheme is advertised in the OpenAPI schema
        schemes = nokey.get("/api/openapi.json").json()["components"]["securitySchemes"]
        self.assertIn("APIKeyHeader", schemes)


if __name__ == "__main__":
    unittest.main()
