---
name: fab-process-operations
description: Use when the user wants to start, track, or advance wafer lots on the fab floor — creating a lot (로트 투입), recording a process-step result (공정실적) with scrap or a defect code, checking where a lot is, listing lots, viewing the process route (공정 경로), or checking work-in-process (재공/WIP). Keywords include lot, 로트, LOT0001, wafer, 웨이퍼, 공정, step, DIFF/PHOTO/ETCH/TEST, scrap, 스크랩, WIP, 재공. Backed by the Mock MES MCP tools (Process & Lot domain).
---

# Fab Process Operations (공정·로트)

## Overview
Drives the wafer-fab floor of the Mock MES over the **MCP** channel (Process & Lot). One shared MES database is exposed through these MCP tools plus a separate REST tool (Product & Inventory). This skill owns wafer lots moving through fab steps; finished-goods, material, packaging, and BOM work belong to the `product-inventory-management` skill.

**Core principle:** A lot is created once with `start_lot`, then advanced one step at a time with `register_process_result` until the final FAB step (TEST) passes — which auto-closes the lot and hands SEMI stock to the inventory domain.

## Process flow (FAB)
DIFF → PHOTO → ETCH → IMPL → CVD → CMP → METRO → TEST → (TEST Pass) → lot **Done** + SEMI stock auto-received → *[handoff: packaging lives in the REST/product skill]*

## When to use
- "새 로트 투입" / start a lot / kick off wafers for a product
- "이 로트 지금 어느 공정이야?" / where is LOT0007, lot status, current step
- "공정 실적 등록" / register a step move, record Pass / Rework / Fail, scrap, defect code
- "재공 얼마나 쌓였어?" / WIP / 재공 grouped by step
- listing lots by status / product / step; viewing the route

**Not for:** 제품·반제품 재고, 자재, 패키징, BOM → use `product-inventory-management`.

## Tools (MCP · Process & Lot)
| Intent | Tool | Key args |
|--------|------|----------|
| 로트 투입 | `start_lot` | product_code, start_qty, priority="Normal" |
| 공정실적 입력 | `register_process_result` | lot_id, step_code, [eqp_id, in_qty, scrap_qty=0, defect_code, operator, result="Pass"] |
| 로트 조회(1건) | `get_lot` | lot_id |
| 로트 조회(목록) | `list_lots` | status?, product_code?, current_step?, priority?, tech_node? |
| 공정 경로 | `get_process_route` | step_code?, eqp_type?, stage? |
| 공정실적 조회 | `list_process_results` | lot_id?, step_code?, result?, defect_code?, has_scrap? |
| 재공(WIP) | `get_wip` | step_code? |

Reference data — products: `DDR5`, `LX9`, `NAND`, `PMIC`. FAB steps in order: `DIFF, PHOTO, ETCH, IMPL, CVD, CMP, METRO, TEST`.

## Key rules
- `register_process_result` defaults `in_qty` to the lot's current wafer_qty and computes out = in − scrap. `result="Pass"` advances the lot to the next step; **TEST Pass closes the lot and auto-receives SEMI product stock**.
- It also consumes that (product, step) BOM's materials. The response includes `shortages` when material stock was insufficient — the move still records, so check `shortages`.
- Always pass a real `lot_id` (looks like `LOT0007`). If unknown, call `list_lots` or `get_wip` first.
- Auth (`X-API-Key`) is handled by the MCP tool connection — never send it manually.

## Example
Partner: "LX9 25장 새로 투입하고 ETCH까지 태워줘."
1. `start_lot(product_code="LX9", start_qty=25)` → returns a lot at `DIFF`.
2. `register_process_result(lot_id=…, step_code="DIFF")` → advances to PHOTO.
3. `register_process_result(lot_id=…, step_code="PHOTO")` → advances to ETCH.
4. `register_process_result(lot_id=…, step_code="ETCH")` → advances to IMPL.

Scrap/defect variant: `register_process_result(lot_id=…, step_code="PHOTO", scrap_qty=1, defect_code="PARTICLE", result="Rework")`.

## Common mistakes
- Using this skill for 제품재고 / 자재 / 패키징 / BOM → wrong domain (that is the REST product skill).
- Registering steps out of order — advance sequentially along the route.
- Confusing **공정실적** (per-step process result, this skill) with **제품실적** (product result, REST skill). Similar words, different tables.
