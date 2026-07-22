# Virtual MES Agent — bundle

A ready-to-wire "virtual agent" for the Mock MES demo. It is assembled from **4 pieces over one shared MES**: **1 REST/OpenAPI tool + 1 MCP tool + 2 SKILL.md files**. The two skills map 1:1 to the two agent channels, with **no functional overlap** — each channel carries both queries (조회) and transactions (입력).

```
                         ┌─────────────────────────────┐
   product-inventory-    │  REST API tool (OpenAPI 3.0) │  제품 · 재고 · 자재 · BOM
   management/SKILL.md ──▶  /api  (docs: /api/docs)     │
                         └──────────────┬──────────────┘
                                        │   one shared SQLite MES
                         ┌──────────────┴──────────────┐
   fab-process-          │  MCP tool (streamable HTTP)  │  로트 · 공정 · 재공
   operations/SKILL.md ──▶  /mcp                        │
                         └─────────────────────────────┘
```

## Contents

| Path | Piece | Purpose |
|------|-------|---------|
| `skills/fab-process-operations/SKILL.md` | Skill #1 | Drives the **MCP** channel: 로트 투입, 공정실적 입력/조회, 공정 경로, 재공(WIP). |
| `skills/product-inventory-management/SKILL.md` | Skill #2 | Drives the **REST** channel: 제품/자재 재고, 제품실적, 패키징, 자재 입고, BOM. |
| `openapi/mes-openapi-3.0.json` | REST tool spec | OpenAPI 3.0.3 for the "Create an OpenAPI tool" flow (pretty). |
| `openapi/mes-openapi-3.0.min.json` | REST tool spec | Minified copy (easy to paste). |

## Live endpoints

Base FQDN: `https://mock-mes.greenrock-bb44c93a.koreacentral.azurecontainerapps.io`

| Channel | URL | Auth header |
|---------|-----|-------------|
| Web console (human) | `/` | — |
| REST API (agent) | `/api` · docs `/api/docs` | `X-API-Key: changjuahn` |
| MCP (agent) | `/mcp` | `X-API-Key: changjuahn` |

## Wiring it up

1. **REST tool** — "Create an OpenAPI tool": import `openapi/mes-openapi-3.0.json` (or the `.min` copy). The `servers` URL is already the FQDN root; add the API key as header `X-API-Key: changjuahn`.
2. **MCP tool** — "Add Model Context Protocol tool":
   - Name: `mock-mes-mcp`
   - Endpoint: `https://mock-mes.greenrock-bb44c93a.koreacentral.azurecontainerapps.io/mcp`
   - Auth: Key-based → header `X-API-Key` : `changjuahn`
3. **Skills** — attach both `SKILL.md` files. The agent reads each `description` to route intent to the right channel (MCP for lots/process, REST for products/inventory/materials/BOM).

## End-to-end demo flow (both skills hand off through the shared DB)

```
MCP skill : start_lot → register_process_result ×8 (DIFF…TEST) → TEST Pass
              └▶ lot Done + SEMI stock auto-received
REST skill: GET product-inventory(SEMI) → POST packaging → FIN stock + AUTO_PACK result
```

## Channel split (no overlap)

- **MCP / fab-process-operations:** `start_lot`, `register_process_result`, `get_lot`, `list_lots`, `get_process_route`, `list_process_results`, `get_wip`.
- **REST / product-inventory-management:** `GET` products · product-inventory · product-results · materials · materials/by-step · bom; `POST` product-results · packaging · materials/receipt; `PUT`/`DELETE` bom.

The only look-alike names are **공정실적** (MCP, per-step process result) vs **제품실적** (REST, product result) — different tables, different channels.
