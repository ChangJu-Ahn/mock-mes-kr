---
name: product-inventory-management
description: Use when the user wants to check finished/semi-finished product stock (제품재고 SEMI/FIN), record a product result (제품실적), run packaging that converts SEMI wafers into FIN units (패키징), receive or query raw-material stock (자재 입고/조회), see per-step material demand, or view and edit the BOM (자재 명세서) in the Mock MES. Keywords include 제품, 완제품, 반제품, SEMI, FIN, inventory, 재고, 자재, material, receipt, 입고, packaging, 패키징, BOM, yield, 실적. Backed by the REST API tool (Product · Inventory · Material · BOM domain).
---

# Product & Inventory Management (제품·재고·자재·BOM)

## Overview
Drives the inventory and finished-goods side of the Mock MES over the **REST API** channel (Product · Inventory · Material · BOM). One shared MES database is exposed through this REST tool plus a separate MCP tool (Process & Lot). This skill owns products, the two inventories, and the BOM; wafer lots moving through fab steps belong to the `fab-process-operations` skill.

**Core principle:** SEMI (반제품) stock arrives automatically when a lot passes final FAB TEST (from the MCP domain). **Packaging** consumes SEMI + the PKG-step BOM to produce FIN (완제품). Process and packaging moves draw down **material** stock according to the BOM.

## Two inventories — do not conflate
- **제품 재고 (product_inventory):** SEMI (반제품 wafers) and FIN (완제품 units). → `GET /product-inventory?item_type=SEMI|FIN`
- **자재 재고 (material):** raw wafers, reticles, chemicals, gases, substrates consumed by process steps. → `GET /materials`

## When to use
- "완제품/반제품 재고 얼마야?" / product, SEMI, FIN stock
- "패키징 돌려줘" / package SEMI → FIN
- "제품실적 조회/입력" / product results, yields, by source (AUTO_FAB / AUTO_PACK / MANUAL)
- "자재 재고 / 부족 자재 / 자재 입고" / material stock, low materials, receiving
- "공정별 자재 소요" / material needed per step (BOM ⋈ on-hand)
- "BOM 등록/수정/삭제" / bill of materials

**Not for:** 로트 투입, 공정실적, 공정 경로, 재공 → use `fab-process-operations`.

## Operations (REST · base path `/api`)
| Intent | Method + path | Body / params |
|--------|---------------|----------------|
| 제품 마스터 | `GET /products` | — |
| 제품재고 조회 | `GET /product-inventory` | product_code?, item_type=SEMI\|FIN? |
| 제품실적 조회 | `GET /product-results` | product_code?, item_type?, source?, lot_id?, date_from?, date_to?, limit? |
| 제품실적 입력(수동) | `POST /product-results` | lot_id, item_type(SEMI\|FIN), good_qty, scrap_qty=0 |
| 패키징(SEMI→FIN) | `POST /packaging` | product_code, in_qty, scrap_qty=0, lot_id?, eqp_id?, operator? |
| 자재 재고 조회 | `GET /materials` | category?, location?, q?, min_qty?, max_qty? |
| 공정별 자재 소요 | `GET /materials/by-step` | step_code? |
| 자재 입고 | `POST /materials/receipt` | material_code, qty, material_name?, category?, uom?, location? |
| BOM 조회 | `GET /bom` | product_code?, step_code? |
| BOM 등록/수정 | `PUT /bom` | product_code, step_code, material_code, qty_per_wafer, uom? |
| BOM 삭제 | `DELETE /bom/{bom_id}` | path: bom_id |

Reference — products: `DDR5`, `LX9`, `NAND`, `PMIC`. item_type: `SEMI` (반제품) / `FIN` (완제품). Packaging step code: `PKG`.

## Key rules
- `POST /packaging` returns an error when SEMI stock for the product is insufficient. On success it creates FIN stock + an `AUTO_PACK` product result and consumes the PKG BOM materials.
- Manual `POST /product-results` records `source=MANUAL`. `AUTO_FAB` (from TEST pass) and `AUTO_PACK` (from packaging) rows are created automatically — don't post those by hand.
- To find why materials ran low, use `GET /materials/by-step` (per-step BOM demand vs on-hand).
- Auth (`X-API-Key`) is injected by the OpenAPI tool connection — never add it manually.

## Example
Partner: "LX9 반제품 100장 패키징해서 완제품 재고 확인해줘."
1. `GET /product-inventory?product_code=LX9&item_type=SEMI` → confirm ≥ 100 SEMI on hand.
2. `POST /packaging {product_code:"LX9", in_qty:100}` → creates FIN stock + an AUTO_PACK result.
3. `GET /product-inventory?product_code=LX9&item_type=FIN` → show the new FIN quantity.

## Common mistakes
- Conflating **제품 재고** (SEMI/FIN goods) with **자재 재고** (raw materials) — different endpoints.
- Trying to package more SEMI than exists → packaging is blocked; check SEMI stock first.
- Confusing **제품실적** (this skill) with **공정실적** (per-step, MCP skill). Different tables.
