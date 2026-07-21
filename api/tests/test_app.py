import os
import unittest

from fastapi.testclient import TestClient

KEY = {"X-API-Key": "changjuahn"}


class RestApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = os.path.join(os.getcwd(), "data", "mes_api_unittest.db")
        from mes_core import db, seed
        db.reset_db()
        seed.seed()
        from api.main import app
        cls.db = db
        cls.client = TestClient(app, headers=KEY)
        cls.open_client = TestClient(app)  # no key

    @classmethod
    def tearDownClass(cls):
        for suffix in ("", "-shm", "-wal"):
            path = os.environ["MES_DB_PATH"] + suffix
            if os.path.exists(path):
                os.remove(path)

    # --- auth gate --------------------------------------------------------- #
    def test_api_requires_key(self):
        self.assertEqual(self.open_client.get("/api/products").status_code, 401)
        self.assertEqual(self.client.get("/api/products").status_code, 200)

    def test_docs_and_web_open(self):
        self.assertEqual(self.open_client.get("/api/openapi.json").status_code, 200)
        self.assertEqual(self.open_client.get("/api/docs").status_code, 200)
        self.assertEqual(self.open_client.get("/").status_code, 200)

    def test_openapi_lists_new_paths(self):
        paths = self.open_client.get("/api/openapi.json").json()["paths"]
        for p in ("/api/products", "/api/product-inventory", "/api/product-results",
                  "/api/packaging", "/api/materials", "/api/materials/by-step",
                  "/api/materials/receipt", "/api/bom"):
            self.assertIn(p, paths)
        self.assertNotIn("/", paths)

    # --- queries ----------------------------------------------------------- #
    def test_products_and_inventory(self):
        self.assertEqual(len(self.client.get("/api/products").json()), 4)
        inv = self.client.get("/api/product-inventory", params={"item_type": "SEMI"}).json()
        self.assertTrue(all(r["item_type"] == "SEMI" for r in inv))

    def test_materials_and_by_step(self):
        mats = self.client.get("/api/materials", params={"category": "Gas"}).json()
        self.assertTrue(all(r["category"] == "Gas" for r in mats))
        by_step = self.client.get("/api/materials/by-step", params={"step_code": "PHOTO"}).json()
        self.assertTrue(all(r["step_code"] == "PHOTO" for r in by_step))

    # --- transactions ------------------------------------------------------ #
    def test_manual_product_result(self):
        lot = self.client.get("/api/product-results", params={"limit": 1}).json()
        # need a real lot id: pull one Done lot via materials-independent query
        from mes_core import db
        lot_id = db.list_lots(status="Done")[0]["lot_id"]
        r = self.client.post("/api/product-results",
                             json={"lot_id": lot_id, "item_type": "FIN", "good_qty": 50})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["source"], "MANUAL")

    def test_packaging_then_inventory(self):
        from mes_core import db
        semi = db.list_product_inventory(item_type="SEMI")
        pcode = next(r["product_code"] for r in semi if r["qty"] >= 2)
        r = self.client.post("/api/packaging", json={"product_code": pcode, "in_qty": 2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["item_type"], "FIN")

    def test_packaging_insufficient_semi_is_400(self):
        r = self.client.post("/api/packaging", json={"product_code": "LX9", "in_qty": 999999})
        self.assertEqual(r.status_code, 400)

    def test_material_receipt(self):
        r = self.client.post("/api/materials/receipt",
                             json={"material_code": "PR-EUV", "qty": 10})
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.json()["qty"], 10)

    def test_bom_upsert_and_delete(self):
        r = self.client.put("/api/bom", json={
            "product_code": "LX9", "step_code": "METRO",
            "material_code": "GAS-AR", "qty_per_wafer": 0.01})
        self.assertEqual(r.status_code, 200)
        bom_id = r.json()["id"]
        self.assertEqual(self.client.delete(f"/api/bom/{bom_id}").status_code, 200)


class WebConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = os.path.join(os.getcwd(), "data", "mes_web_unittest.db")
        from mes_core import db, seed
        db.reset_db()
        seed.seed()
        from api.main import app
        cls.db = db
        cls.client = TestClient(app)  # web is open (no key)

    @classmethod
    def tearDownClass(cls):
        for suffix in ("", "-shm", "-wal"):
            path = os.environ["MES_DB_PATH"] + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_all_pages_render(self):
        for path in ("/", "/lots", "/process", "/product-inventory",
                     "/product-results", "/materials", "/bom", "/wip", "/equipment"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_lot_detail_renders(self):
        lot_id = self.db.list_lot_ids()[0]
        self.assertEqual(self.client.get(f"/lots/{lot_id}").status_code, 200)

    def test_start_lot_form(self):
        before = len(self.db.list_lot_ids())
        r = self.client.post("/lots/start",
                             data={"product_code": "LX9", "start_qty": "25", "priority": "Hot"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(len(self.db.list_lot_ids()), before + 1)

    def test_process_result_form(self):
        lot = self.db.start_lot("DDR5", 20)
        r = self.client.post("/process/results",
                             data={"lot_id": lot["lot_id"], "step_code": "PHOTO", "scrap_qty": "1"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.db.get_lot(lot["lot_id"])["current_step"], "PHOTO")

    def test_material_receive_form(self):
        r = self.client.post("/materials/receive",
                             data={"material_code": "PR-EUV", "qty": "5"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    def test_bom_upsert_and_delete_form(self):
        r = self.client.post("/bom", data={
            "product_code": "LX9", "step_code": "METRO",
            "material_code": "GAS-AR", "qty_per_wafer": "0.02"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        bom_id = self.db.list_bom(product_code="LX9", step_code="METRO")[0]["id"]
        r2 = self.client.post(f"/bom/{bom_id}/delete", follow_redirects=False)
        self.assertEqual(r2.status_code, 303)

    def test_guide_page(self):
        r = self.client.get("/guide")
        self.assertEqual(r.status_code, 200)
        self.assertIn("공정", r.text)

    def test_modern_shell_contract(self):
        r = self.client.get("/process")
        self.assertEqual(r.status_code, 200)
        for token in (
            'class="app-shell"',
            'id="primary-navigation"',
            'aria-label="주요 메뉴"',
            'data-nav-toggle',
            'id="main-content"',
            'href="/guide"',
            'href="/api/docs"',
        ):
            self.assertIn(token, r.text)
        self.assertRegex(
            r.text,
            r'href="/process"[^>]*aria-current="page"|'
            r'aria-current="page"[^>]*href="/process"',
        )
        lot_id = self.db.list_lot_ids()[0]
        r2 = self.client.get(f"/lots/{lot_id}")
        self.assertEqual(r2.status_code, 200)
        self.assertRegex(
            r2.text,
            r'href="/lots"[^>]*aria-current="page"|'
            r'aria-current="page"[^>]*href="/lots"',
        )

    def test_feedback_banners_are_accessible(self):
        r = self.client.get("/lots", params={"message": "Saved", "error": "Problem"})
        self.assertIn('role="status"', r.text)
        self.assertIn('role="alert"', r.text)
        self.assertIn("Saved", r.text)
        self.assertIn("Problem", r.text)

    def test_design_system_stylesheet(self):
        css = self.client.get("/static/styles.css")
        self.assertEqual(css.status_code, 200)
        for token in (
            "--sidebar-width:",
            ".app-shell",
            ".action-panel",
            ".form-grid",
            ".table-wrap",
            ".badge",
            "@media (max-width: 960px)",
            "@media (prefers-reduced-motion: reduce)",
        ):
            self.assertIn(token, css.text)
