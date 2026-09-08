import asyncio
import os
import unittest
from unittest import mock

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

    def test_api_index_advertises_the_mcp_surface(self):
        # /api is the first thing an agent hits; it must not imply REST is the
        # whole system, because lot history / process results live on MCP only.
        mcp = self.client.get("/api").json()["mcp"]
        self.assertEqual(mcp["endpoint"], "/mcp")
        self.assertEqual(mcp["docs"], "/mcp-docs")
        self.assertEqual(mcp["spec"], "/mcp-docs.json")
        self.assertEqual(mcp["transport"], "streamable-http")
        for tool in ("get_lot", "list_lots", "list_process_results", "get_wip"):
            self.assertIn(tool, mcp["tools"])

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
                     "/product-results", "/materials", "/bom", "/wip", "/equipment",
                     "/mcp-docs"):
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
        for token in (
            'data-page="guide"',
            'class="guide-hero"',
            'class="guide-steps"',
            "로트 투입",
            "FAB 공정실적",
            "SEMI 자동 입고",
            "패키징",
            "FIN 완제품",
            "X-API-Key: changjuahn",
            "/api/docs",
            "/mcp",
            "start_lot",
            "register_process_result",
        ):
            self.assertIn(token, r.text)

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
            'href="/mcp-docs"',
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

    def test_dashboard_command_center_sections(self):
        r = self.client.get("/")
        for token in (
            'data-page="dashboard"',
            "Fab 운영 대시보드",
            "생산 흐름",
            "공정별 재공",
            "낮은 자재 재고",
            "최근 공정실적",
            "최근 제품실적",
            'class="kpi-grid"',
            'class="process-flow"',
        ):
            self.assertIn(token, r.text)

    def test_read_only_pages_use_shared_panels(self):
        wip = self.client.get("/wip")
        equipment = self.client.get("/equipment")
        self.assertIn('data-page="wip"', wip.text)
        self.assertIn('class="process-flow"', wip.text)
        self.assertIn('data-page="equipment"', equipment.text)
        self.assertIn("설비 상태", equipment.text)
        self.assertIn('class="table-wrap"', equipment.text)

    def test_wip_tables_label_aggregated_values_as_counts(self):
        for path in ("/", "/wip"):
            page = self.client.get(path).text
            self.assertIn('<th class="numeric">로트 수</th>', page)
            self.assertIn('<th class="numeric">웨이퍼 수</th>', page)

    def test_process_result_columns_use_consistent_clear_labels(self):
        lot_id = self.db.list_lot_ids()[0]
        dashboard = self.client.get("/").text
        process = self.client.get("/process").text
        detail = self.client.get(f"/lots/{lot_id}").text
        # out_qty -> 산출, scrap_qty -> 스크랩, consistently across every screen
        for page in (dashboard, process, detail):
            self.assertIn('<th class="numeric">산출</th>', page)
            self.assertIn('<th class="numeric">스크랩</th>', page)
        # scrap_qty must not read as a defect count next to the 불량 코드 column
        self.assertNotIn('<th class="numeric">불량</th>', dashboard)
        # out_qty must not read as a shipment ("출고") mid-process
        self.assertNotIn('<th class="numeric">출고</th>', process)
        self.assertNotIn('<th class="numeric">출고</th>', detail)

    def test_lots_list_labels_start_qty_with_unit(self):
        lots = self.client.get("/lots").text
        self.assertIn('<th class="numeric">투입 수량</th>', lots)
        self.assertIn('<th class="numeric">현재 수량</th>', lots)

    def test_production_pages_preserve_form_contracts(self):
        lots = self.client.get("/lots").text
        for token in (
            'data-page="lots"',
            'class="action-panel"',
            'action="/lots/start"',
            'method="post"',
            'name="product_code"',
            'name="start_qty"',
            'name="priority"',
            'action="/lots"',
            'name="status_filter"',
            'name="current_step"',
        ):
            self.assertIn(token, lots)

        process = self.client.get("/process").text
        for token in (
            'data-page="process"',
            'action="/process/results"',
            'name="lot_id"',
            'name="step_code"',
            'name="eqp_id"',
            'name="in_qty"',
            'name="scrap_qty"',
            'name="defect_code"',
            'name="operator"',
            'name="result"',
        ):
            self.assertIn(token, process)
        self.assertIn('class="process-flow"', process)

    def test_lot_detail_uses_summary_and_history_panels(self):
        lot_id = self.db.list_lot_ids()[0]
        html = self.client.get(f"/lots/{lot_id}").text
        self.assertIn('data-page="lot-detail"', html)
        self.assertIn('class="summary-grid"', html)
        self.assertIn("누적 수율", html)
        self.assertIn("공정 이력", html)

    def test_inventory_and_master_forms_preserve_contracts(self):
        contracts = {
            "/product-inventory": (
                'data-page="product-inventory"', 'action="/packaging"',
                'name="product_code"', 'name="in_qty"', 'name="scrap_qty"',
                'name="lot_id"', 'name="eqp_id"',
            ),
            "/product-results": (
                'data-page="product-results"', 'action="/product-results"',
                'name="lot_id"', 'name="item_type"', 'name="good_qty"',
                'name="scrap_qty"', 'name="source"',
            ),
            "/materials": (
                'data-page="materials"', 'action="/materials/receive"',
                'name="material_code"', 'name="qty"', 'name="material_name"',
                'name="category"', 'name="uom"', 'name="location"',
                'name="step_code"',
            ),
            "/bom": (
                'data-page="bom"', 'action="/bom"', 'name="product_code"',
                'name="step_code"', 'name="material_code"',
                'name="qty_per_wafer"', 'name="uom"',
            ),
        }
        for path, tokens in contracts.items():
            html = self.client.get(path).text
            for token in tokens:
                self.assertIn(token, html, f"{path}: {token}")
            self.assertIn('class="action-panel"', html)
            self.assertIn('class="table-wrap"', html)


class McpDocsTests(unittest.TestCase):
    """The MCP surface is only documented here — Swagger cannot describe it."""

    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = os.path.join(os.getcwd(), "data", "mes_mcpdocs_unittest.db")
        from mes_core import db, seed
        db.reset_db()
        seed.seed()
        from api.main import app
        cls.client = TestClient(app)  # docs are open, like /api/docs

    @classmethod
    def tearDownClass(cls):
        for suffix in ("", "-shm", "-wal"):
            path = os.environ["MES_DB_PATH"] + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_docs_page_is_open_and_uses_the_shared_shell(self):
        r = self.client.get("/mcp-docs")
        self.assertEqual(r.status_code, 200)
        for token in ('data-page="mcp-docs"', 'class="app-shell"', 'id="main-content"',
                      'href="/api/docs"', 'href="/mcp-docs.json"'):
            self.assertIn(token, r.text)
        self.assertRegex(
            r.text,
            r'href="/mcp-docs"[^>]*aria-current="page"|'
            r'aria-current="page"[^>]*href="/mcp-docs"',
        )

    def test_docs_page_lists_every_live_mcp_tool(self):
        """The page introspects the live registry, so it cannot silently drop a tool."""
        from api.mcp_spec import build_spec
        from mcp_server.server import mcp
        html = self.client.get("/mcp-docs").text
        names = build_spec()["tool_names"]
        live = [t.name for t in asyncio.run(mcp.list_tools())]
        self.assertCountEqual(names, live)
        self.assertTrue(names, "MCP server exposes no tools")
        for name in names:
            self.assertIn(f'id="tool-{name}"', html, name)
            self.assertIn(f'href="#tool-{name}"', html, name)

    def test_docs_page_explains_connection_and_clients(self):
        html = self.client.get("/mcp-docs").text
        for token in ("streamable-http", "X-API-Key", "changjuahn", "mock-mes-mcp",
                      "tools/call", "tools/list", "mcp-remote", "streamablehttp_client",
                      "http://testserver/mcp"):
            self.assertIn(token, html, token)

    def test_connection_urls_are_https_for_a_public_host(self):
        # Caddy listens on plaintext :8080 and rewrites X-Forwarded-Proto, so the
        # request scheme is always http in ACA. Publishing http:// would hand out
        # URLs that ACA (allowInsecure: false) redirects, breaking POSTed JSON-RPC.
        host = "mock-mes.example.azurecontainerapps.io"
        html = self.client.get("/mcp-docs", headers={"Host": host}).text
        self.assertIn(f"https://{host}/mcp", html)
        self.assertNotIn(f"http://{host}", html)

    def test_public_base_url_can_be_overridden(self):
        with mock.patch.dict(os.environ, {"MES_PUBLIC_BASE_URL": "https://mes.example.com/"}):
            html = self.client.get("/mcp-docs").text
        self.assertIn("https://mes.example.com/mcp", html)
        self.assertNotIn("http://testserver/mcp", html)

    def test_docs_page_documents_parameters_and_defaults(self):
        html = self.client.get("/mcp-docs").text
        # required + optional-with-default params must both be visible
        for token in ("product_code", "start_qty", "scrap_qty", "has_scrap",
                      "cumulative_yield", "필수", "선택", "기본값"):
            self.assertIn(token, html, token)

    def test_docs_page_states_lot_history_is_not_on_rest(self):
        html = self.client.get("/mcp-docs").text
        self.assertIn("REST에는 해당 엔드포인트가 없습니다", html)
        self.assertIn("/lots/{lot_id}", html)

    def test_spec_json_matches_the_running_server(self):
        r = self.client.get("/mcp-docs.json")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        self.assertEqual(payload["server"]["name"], "mock-mes-mcp")
        self.assertEqual(payload["server"]["endpoint"], "/mcp")
        self.assertEqual(payload["server"]["transport"], "streamable-http")
        self.assertEqual(payload["server"]["auth"]["header"], "X-API-Key")

        import asyncio
        from mcp_server.server import mcp
        live = {t.name for t in asyncio.run(mcp.list_tools())}
        self.assertEqual({t["name"] for t in payload["tools"]}, live)
        for tool in payload["tools"]:
            self.assertTrue(tool["description"])
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_protocol_version_tracks_the_installed_sdk(self):
        # Hardcoding this drifts: the image pins `mcp>=1.9,<2`, so a rebuild can
        # bump the revision the server actually negotiates on the wire.
        from mcp.types import LATEST_PROTOCOL_VERSION
        payload = self.client.get("/mcp-docs.json").json()
        self.assertEqual(payload["server"]["protocolVersion"], LATEST_PROTOCOL_VERSION)
        self.assertIn(LATEST_PROTOCOL_VERSION, self.client.get("/mcp-docs").text)

    def test_spec_json_is_open_but_the_mcp_endpoint_stays_gated(self):
        # The docs must be browsable without a key, exactly like /api/openapi.json.
        self.assertEqual(self.client.get("/mcp-docs.json").status_code, 200)
        self.assertEqual(self.client.get("/mcp-docs").status_code, 200)

    def test_docs_styles_are_shipped(self):
        css = self.client.get("/static/styles.css").text
        for token in (".doc-tab", ".tool-card", ".copy-btn", ".code-block", ".param-table"):
            self.assertIn(token, css, token)


class McpSpecUnitTests(unittest.TestCase):
    """The spec is built from the live registry, so it can never go stale."""

    def test_type_labels_flatten_optional_schemas(self):
        from api.mcp_spec import _type_label
        self.assertEqual(_type_label({"type": "string"}), "string")
        self.assertEqual(
            _type_label({"anyOf": [{"type": "string"}, {"type": "null"}]}),
            "string | null")
        self.assertEqual(_type_label({"type": "array", "items": {"type": "integer"}}),
                         "array<integer>")
        self.assertEqual(_type_label(None), "any")

    def test_description_is_split_into_label_and_summary(self):
        from api.mcp_spec import _split_description
        label, variant, summary = _split_description(
            "start_lot", "로트 투입(start_lot): create a new FAB lot.")
        self.assertEqual(label, "로트 투입")
        self.assertEqual(variant, "")  # matches the tool name -> not repeated
        self.assertEqual(summary, "create a new FAB lot.")

        label, variant, _ = _split_description("get_lot", "로트 조회(one): return a lot.")
        self.assertEqual((label, variant), ("로트 조회", "one"))

        self.assertEqual(_split_description("x", "no pattern here"),
                         ("", "", "no pattern here"))

    def test_spec_groups_tools_and_flags_writes(self):
        from api.mcp_spec import build_spec
        spec = build_spec()
        self.assertEqual(spec["server_name"], "mock-mes-mcp")
        self.assertTrue(spec["stateless"])
        # Anything that mutates has to be labelled as such -- the page is the
        # only warning a read-only integrator gets before calling one.
        self.assertEqual(spec["write_tools"],
                         ["start_lot", "register_process_result",
                          "create_product", "update_product", "delete_product",
                          "create_equipment", "update_equipment", "delete_equipment"])
        self.assertEqual([g["name"] for g in spec["groups"]],
                         ["로트 (Lot)", "공정 (Process)", "재공 (WIP)", "기준정보 (Master)"])
        # every tool is reachable through exactly one group
        grouped = [t["name"] for g in spec["groups"] for t in g["tools"]]
        self.assertCountEqual(grouped, spec["tool_names"])

    def test_required_params_sort_first_and_defaults_render(self):
        from api.mcp_spec import build_spec
        tool = build_spec()["tools_by_name"]["register_process_result"]
        params = {p["name"]: p for p in tool["params"]}
        self.assertEqual([p["name"] for p in tool["params"]][:2], ["lot_id", "step_code"])
        self.assertTrue(params["lot_id"]["required"])
        self.assertEqual(params["lot_id"]["default"], "—")
        self.assertEqual(params["scrap_qty"]["default"], "0")
        self.assertEqual(params["result"]["default"], '"Pass"')
        self.assertEqual(params["in_qty"]["type"], "integer | null")

    def test_every_tool_has_curated_docs(self):
        # A new MCP tool without an entry in _TOOL_META would silently render
        # with no return description; fail loudly instead.
        from api.mcp_spec import build_spec
        for tool in build_spec()["tools"]:
            self.assertTrue(tool["returns"], f"{tool['name']} is missing a 'returns' note")
            self.assertNotEqual(tool["group"], "기타 (Other)", tool["name"])
            self.assertTrue(tool["summary"], tool["name"])


class MasterDataWriteSurfaceTests(unittest.TestCase):
    """The mutation path an outside client actually exercises.

    The point of shipping master-data CRUD was to let someone drive this MES
    from outside -- rename a product over MCP, add a tool over REST -- and see
    the change reflected everywhere. Writes land in one shared SQLite file, so
    a change made through any surface must be visible through all of them.
    """

    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = os.path.join(os.getcwd(), "data", "mes_write_unittest.db")
        from mes_core import db, seed
        db.reset_db()
        seed.seed()
        from api.main import app
        from mcp_server import server
        cls.db, cls.server = db, server
        cls.client = TestClient(app, headers=KEY)
        cls.open_client = TestClient(app)  # no key

    @classmethod
    def tearDownClass(cls):
        for suffix in ("", "-shm", "-wal"):
            path = os.environ["MES_DB_PATH"] + suffix
            if os.path.exists(path):
                os.remove(path)

    def tearDown(self):
        for code in ("ZZ1", "ZZ2"):
            try:
                self.db.delete_product(code)
            except ValueError:
                pass
        for eqp in ("EQP-ZZ01", "EQP-ZZ02"):
            try:
                self.db.delete_equipment(eqp)
            except ValueError:
                pass

    # --- REST -------------------------------------------------------------- #
    def test_rest_round_trips_a_product(self):
        res = self.client.post("/api/products", json={
            "product_code": "ZZ1", "product_name": "ZZ1 Test", "tech_node": "3nm"})
        self.assertEqual(res.status_code, 201)

        res = self.client.patch("/api/products/ZZ1", json={"product_name": "ZZ1 Renamed"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["product_name"], "ZZ1 Renamed")
        self.assertEqual(res.json()["tech_node"], "3nm", "unset fields must be left alone")

        listed = {p["product_code"]: p for p in self.client.get("/api/products").json()}
        self.assertEqual(listed["ZZ1"]["product_name"], "ZZ1 Renamed")
        self.assertEqual(self.client.delete("/api/products/ZZ1").status_code, 200)
        self.assertEqual(self.client.get("/api/products/ZZ1").status_code, 404)

    def test_rest_round_trips_equipment(self):
        res = self.client.post("/api/equipments", json={
            "eqp_id": "EQP-ZZ01", "eqp_name": "Tester-Z", "type": "Prober"})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["status"], "Idle", "a new tool starts idle")

        res = self.client.patch("/api/equipments/EQP-ZZ01", json={"status": "Down"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "Down")
        self.assertEqual(res.json()["eqp_name"], "Tester-Z", "unset fields must be left alone")
        self.assertEqual(self.client.delete("/api/equipments/EQP-ZZ01").status_code, 200)
        self.assertEqual(self.client.get("/api/equipments/EQP-ZZ01").status_code, 404)

    def test_rest_documents_the_status_enum_it_enforces(self):
        """Swagger's dropdown and the data layer's rule must not drift apart."""
        from api.rest import EquipmentUpdate
        status = EquipmentUpdate.model_json_schema()["properties"]["status"]
        enum = next(v["enum"] for v in status["anyOf"] if "enum" in v)
        self.assertEqual(tuple(enum), self.db.EQUIPMENT_STATUSES)

    def test_rest_rejects_duplicates_unknown_rows_and_bad_status(self):
        self.client.post("/api/products", json={"product_code": "ZZ1", "product_name": "A"})
        dup = self.client.post("/api/products", json={"product_code": "ZZ1", "product_name": "B"})
        self.assertEqual(dup.status_code, 409)
        self.assertEqual(
            self.client.patch("/api/products/NOPE", json={"product_name": "x"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/products/NOPE").status_code, 404)

        self.client.post("/api/equipments", json={"eqp_id": "EQP-ZZ01", "eqp_name": "T"})
        bad = self.client.patch("/api/equipments/EQP-ZZ01", json={"status": "Exploded"})
        self.assertEqual(bad.status_code, 422)

    def test_master_writes_require_the_api_key(self):
        for call in (
            lambda c: c.post("/api/products", json={"product_code": "ZZ1", "product_name": "A"}),
            lambda c: c.patch("/api/products/LX9", json={"product_name": "A"}),
            lambda c: c.delete("/api/products/LX9"),
            lambda c: c.post("/api/equipments", json={"eqp_id": "EQP-ZZ01", "eqp_name": "A"}),
            lambda c: c.patch("/api/equipments/EQP-DIFF01", json={"eqp_name": "A"}),
            lambda c: c.delete("/api/equipments/EQP-DIFF01"),
        ):
            with self.subTest(call=call):
                self.assertEqual(call(self.open_client).status_code, 401)

    # --- MCP --------------------------------------------------------------- #
    def test_mcp_can_rename_a_seeded_product_and_put_it_back(self):
        """The exact scenario this feature exists for."""
        original = self.db.get_product("LX9")["product_name"]
        try:
            renamed = self.server.update_product("LX9", product_name="LX9 (renamed by agent)")
            self.assertEqual(renamed["product_name"], "LX9 (renamed by agent)")
            # visible through the other two surfaces, not just the caller's
            self.assertEqual(self.client.get("/api/products/LX9").json()["product_name"],
                             "LX9 (renamed by agent)")
            self.assertIn("LX9 (renamed by agent)", self.open_client.get("/products").text)
        finally:
            self.db.update_product("LX9", product_name=original)

    def test_mcp_round_trips_master_data(self):
        self.assertEqual(
            self.server.create_product("ZZ2", "ZZ2 Test")["product_code"], "ZZ2")
        self.assertIn("ZZ2", [p["product_code"] for p in self.server.list_products()])
        self.assertEqual(self.server.update_product("ZZ2", tech_node="2nm")["tech_node"], "2nm")
        self.assertTrue(self.server.delete_product("ZZ2")["deleted"])

        self.server.create_equipment("EQP-ZZ02", "Tester-Y", type="Prober")
        self.assertIn("EQP-ZZ02", [e["eqp_id"] for e in self.server.list_equipments()])
        self.assertEqual(
            self.server.update_equipment("EQP-ZZ02", status="down")["status"], "Down")
        self.assertTrue(self.server.delete_equipment("EQP-ZZ02")["deleted"])

    def test_mcp_reports_failures_as_errors_rather_than_raising(self):
        """A raised exception inside a tool reads as a broken server to a client."""
        for payload in (
            self.server.update_product("NOPE", product_name="x"),
            self.server.delete_product("NOPE"),
            self.server.create_product("LX9", "duplicate"),
            self.server.update_equipment("EQP-DIFF01", status="Exploded"),
        ):
            with self.subTest(payload=payload):
                self.assertIn("error", payload)

    # --- web console ------------------------------------------------------- #
    def test_web_forms_create_edit_and_delete(self):
        res = self.open_client.post(
            "/products",
            data={"product_code": "ZZ1", "product_name": "ZZ1 Web", "tech_node": "5nm"},
            follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertIn("ZZ1 Web", self.open_client.get("/products").text)

        self.open_client.post("/products/ZZ1/update", data={"product_name": "ZZ1 Web 2"},
                              follow_redirects=False)
        self.assertEqual(self.db.get_product("ZZ1")["product_name"], "ZZ1 Web 2")

        self.open_client.post("/products/ZZ1/delete", follow_redirects=False)
        self.assertIsNone(self.db.get_product("ZZ1"))

    def test_web_reports_a_refused_delete_instead_of_500ing(self):
        res = self.open_client.post("/products/LX9/delete", follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        page = self.open_client.get(res.headers["location"])
        self.assertEqual(page.status_code, 200)
        self.assertIn("still referenced by", page.text)
        self.assertIsNotNone(self.db.get_product("LX9"))


class DeletionSurfaceTests(unittest.TestCase):
    """Guards how much of this MES can be destroyed, and how it recovers.

    Deleting used to be forbidden almost everywhere, because a lost identifier
    was permanent. That is no longer the trade: ``/data`` is an EmptyDir volume
    reloaded from ``dataset.json`` on every boot, so any delete is undone by
    restarting the app. Destructive operations are therefore allowed *and*
    fenced -- a row that something still points at cannot be removed, since the
    schema declares no foreign keys and nothing else would catch it.

    These tests pin that fence. If a delete path appears for an entity without
    one, or an existing guard is dropped, they fail and the decision has to be
    deliberate.
    """

    GUARDED = {
        "product": ("LX9", "delete_product"),
        "equipment": ("EQP-DIFF01", "delete_equipment"),
    }

    @classmethod
    def setUpClass(cls):
        os.environ["MES_DB_PATH"] = os.path.join(os.getcwd(), "data", "mes_delete_unittest.db")
        from mes_core import db, seed
        db.reset_db()
        seed.seed()
        from api.main import app
        cls.app = app
        cls.open_client = TestClient(app)  # no key

    @classmethod
    def tearDownClass(cls):
        for suffix in ("", "-shm", "-wal"):
            path = os.environ["MES_DB_PATH"] + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_the_deletable_entities_are_exactly_the_documented_ones(self):
        # Inspect the routers we define rather than app.routes: FastAPI wraps
        # included routers (_IncludedRouter) and the shape of that wrapper is an
        # internal detail that changes between versions.
        from api.rest import router as rest_router
        from api.web import router as web_router
        deleting = sorted(
            f"{method} {route.path}"
            for router in (rest_router, web_router)
            for route in router.routes
            for method in (getattr(route, "methods", None) or set())
            if method == "DELETE" or route.path.endswith("/delete")
        )
        self.assertEqual(deleting, [
            "DELETE /api/bom/{bom_id}",
            "DELETE /api/equipments/{eqp_id}",
            "DELETE /api/products/{product_code}",
            "POST /bom/{bom_id}/delete",
            "POST /equipment/{eqp_id}/delete",
            "POST /products/{product_code}/delete",
        ])

    def test_the_data_layer_exposes_only_guarded_row_deletes(self):
        from mes_core import db
        self.assertEqual(
            sorted(n for n in dir(db) if n.startswith("delete_")),
            ["delete_bom", "delete_equipment", "delete_product"])

    def test_master_data_in_use_cannot_be_deleted(self):
        """The whole point of the fence: seeded keys survive a delete attempt."""
        from mes_core import db
        for kind, (code, fname) in self.GUARDED.items():
            with self.subTest(kind):
                with self.assertRaises(ValueError) as caught:
                    getattr(db, fname)(code)
                self.assertIn("still referenced by", str(caught.exception))
                self.assertIsNotNone(getattr(db, f"get_{kind}")(code))

    def test_deleting_in_use_master_data_over_http_is_a_conflict(self):
        client = TestClient(self.app)
        headers = {"X-API-Key": "changjuahn"}
        for path in ("/api/products/LX9", "/api/equipments/EQP-DIFF01"):
            with self.subTest(path):
                res = client.delete(path, headers=headers)
                self.assertEqual(res.status_code, 409)
                self.assertIn("still referenced by", res.json()["detail"])

    def test_every_mcp_delete_tool_is_fenced(self):
        """Agents drive MCP autonomously, so deletes there must refuse politely.

        They return an ``error`` payload rather than raising, because a raised
        exception inside a tool call reads as a broken server to the client.
        """
        import asyncio
        from mcp_server import server
        deleters = [t.name for t in asyncio.run(server.mcp.list_tools())
                    if t.name.startswith("delete_")]
        self.assertEqual(sorted(deleters), ["delete_equipment", "delete_product"])

        self.assertIn("still referenced by", server.delete_product("LX9")["error"])
        self.assertIn("still referenced by",
                      server.delete_equipment("EQP-DIFF01")["error"])

    def test_nothing_can_wipe_the_database_wholesale(self):
        """Row-level deletes are fine; a reset/purge/drop tool would not be."""
        import asyncio
        from mcp_server.server import mcp
        for tool in asyncio.run(mcp.list_tools()):
            self.assertNotRegex(tool.name, r"drop|reset|purge|clear|truncate")

    def test_rest_delete_requires_the_api_key(self):
        self.assertEqual(self.open_client.delete("/api/bom/1").status_code, 401)
