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
        from api.mcp_spec import build_spec
        html = self.client.get("/mcp-docs").text
        names = build_spec()["tool_names"]
        self.assertEqual(len(names), 7)
        for name in names:
            self.assertIn(f'id="tool-{name}"', html, name)
            self.assertIn(f'href="#tool-{name}"', html, name)

    def test_docs_page_explains_connection_and_clients(self):
        html = self.client.get("/mcp-docs").text
        for token in ("streamable-http", "X-API-Key", "changjuahn", "mock-mes-mcp",
                      "tools/call", "tools/list", "mcp-remote", "streamablehttp_client",
                      "http://testserver/mcp"):
            self.assertIn(token, html, token)

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
        self.assertEqual(spec["write_tools"], ["start_lot", "register_process_result"])
        self.assertEqual([g["name"] for g in spec["groups"]],
                         ["로트 (Lot)", "공정 (Process)", "재공 (WIP)"])
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
