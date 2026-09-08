import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette import status

from api import mcp_spec
from mes_core import db

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(include_in_schema=False)

DEFECT_CODES = ["Particle", "Scratch", "Overlay", "CD-OOS", "Etch-Residue", "Contamination"]
_SEE_OTHER = status.HTTP_303_SEE_OTHER


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _int_or_none(value: str | None) -> int | None:
    value = _blank_to_none(value)
    return int(value) if value is not None else None


def _context(request: Request, **extra: Any) -> dict[str, Any]:
    return {"request": request, **extra}


def _redirect(path: str, *, message: str | None = None, error: str | None = None):
    """Redirect back to a page carrying a flash message.

    The values are percent-encoded: a rejected delete explains itself in prose
    ("still referenced by 4 lots, 12 BOM rows"), and raw spaces and commas in a
    Location header would otherwise produce a malformed URL.
    """
    q = {k: v for k, v in (("message", message), ("error", error)) if v}
    return RedirectResponse(path + ("?" + urlencode(q) if q else ""),
                            status_code=_SEE_OTHER)


def _shortage_note(shortages: list[dict[str, Any]]) -> str:
    if not shortages:
        return ""
    names = ", ".join(f"{s['material_code']}(-{s['short']})" for s in shortages)
    return f" (material shortage: {names})".replace(" ", "+")


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

@router.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(
        request, "dashboard.html", _context(request, summary=db.get_dashboard_summary()))


# --------------------------------------------------------------------------- #
# 로트 (lots) + 로트 투입
# --------------------------------------------------------------------------- #

@router.get("/lots")
def lots(request: Request, status_filter: str | None = None, product_code: str | None = None,
         current_step: str | None = None, message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "lots.html", _context(
        request,
        rows=db.list_lots(status=_blank_to_none(status_filter),
                          product_code=_blank_to_none(product_code),
                          current_step=_blank_to_none(current_step)),
        products=db.list_products(),
        steps=db.get_process_route(),
        filters={"status_filter": status_filter or "", "product_code": product_code or "",
                 "current_step": current_step or ""},
        message=message, error=error,
    ))


@router.post("/lots/start")
def lots_start(product_code: str = Form(...), start_qty: int = Form(...),
               priority: str = Form("Normal")):
    try:
        lot = db.start_lot(product_code, start_qty, priority=priority)
    except ValueError as exc:
        return _redirect("/lots", error=str(exc))
    return _redirect("/lots", message=f"Lot {lot['lot_id']} started")


@router.get("/lots/{lot_id}")
def lot_detail(request: Request, lot_id: str):
    lot = db.get_lot(lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")
    return templates.TemplateResponse(request, "lot_detail.html", _context(request, lot=lot))


# --------------------------------------------------------------------------- #
# 공정실적 (process results)
# --------------------------------------------------------------------------- #

@router.get("/process")
def process(request: Request, message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "process.html", _context(
        request,
        route=db.get_process_route(),
        results=db.list_process_results(limit=100),
        lot_ids=db.list_lot_ids(),
        steps=db.list_step_codes(),
        equipment=db.list_equipment(),
        defect_codes=DEFECT_CODES,
        message=message, error=error,
    ))


@router.post("/process/results")
def process_result(lot_id: str = Form(...), step_code: str = Form(...),
                   eqp_id: str | None = Form(None), in_qty: str | None = Form(None),
                   scrap_qty: str | None = Form("0"), defect_code: str | None = Form(None),
                   operator: str | None = Form(None), result: str = Form("Pass")):
    try:
        res = db.register_process_result(
            lot_id=lot_id, step_code=step_code, eqp_id=_blank_to_none(eqp_id),
            in_qty=_int_or_none(in_qty), scrap_qty=_int_or_none(scrap_qty) or 0,
            defect_code=_blank_to_none(defect_code), operator=_blank_to_none(operator),
            result=result,
        )
    except ValueError as exc:
        return _redirect("/process", error=str(exc))
    note = "Process+result+recorded" + _shortage_note(res["shortages"])
    if res["semi_receipt"]:
        note += "+|+SEMI+received"
    return _redirect("/process", message=note)


# --------------------------------------------------------------------------- #
# 제품 인벤토리 + 패키징
# --------------------------------------------------------------------------- #

@router.get("/product-inventory")
def product_inventory(request: Request, message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "product_inventory.html", _context(
        request,
        rows=db.list_product_inventory(),
        products=db.list_products(),
        lot_ids=db.list_lot_ids(),
        equipment=db.list_equipment(),
        message=message, error=error,
    ))


@router.post("/packaging")
def packaging(product_code: str = Form(...), in_qty: int = Form(...),
              scrap_qty: str | None = Form("0"), lot_id: str | None = Form(None),
              eqp_id: str | None = Form(None), operator: str | None = Form(None)):
    try:
        res = db.package(product_code=product_code, in_qty=in_qty,
                         scrap_qty=_int_or_none(scrap_qty) or 0, lot_id=_blank_to_none(lot_id),
                         eqp_id=_blank_to_none(eqp_id), operator=_blank_to_none(operator))
    except ValueError as exc:
        return _redirect("/product-inventory", error=str(exc))
    return _redirect("/product-inventory",
                     message="Packaged " + str(res["good_qty"]) + " FIN" + _shortage_note(res["shortages"]))


# --------------------------------------------------------------------------- #
# 제품실적 (product results) + 수동 입력
# --------------------------------------------------------------------------- #

@router.get("/product-results")
def product_results(request: Request, product_code: str | None = None,
                    item_type: str | None = None, source: str | None = None,
                    message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "product_results.html", _context(
        request,
        rows=db.list_product_results(product_code=_blank_to_none(product_code),
                                     item_type=_blank_to_none(item_type),
                                     source=_blank_to_none(source), limit=200),
        products=db.list_products(),
        lot_ids=db.list_lot_ids(),
        filters={"product_code": product_code or "", "item_type": item_type or "",
                 "source": source or ""},
        message=message, error=error,
    ))


@router.post("/product-results")
def product_result_create(lot_id: str = Form(...), item_type: str = Form(...),
                          good_qty: int = Form(...), scrap_qty: str | None = Form("0")):
    try:
        db.register_product_result(lot_id=lot_id, item_type=item_type,
                                   good_qty=good_qty, scrap_qty=_int_or_none(scrap_qty) or 0)
    except ValueError as exc:
        return _redirect("/product-results", error=str(exc))
    return _redirect("/product-results", message="Product result recorded")


# --------------------------------------------------------------------------- #
# 자재 인벤토리 (materials) + 입고 + 공정별 소요
# --------------------------------------------------------------------------- #

@router.get("/materials")
def materials(request: Request, category: str | None = None, step_code: str | None = None,
              message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "materials.html", _context(
        request,
        rows=db.list_materials(category=_blank_to_none(category)),
        by_step=db.materials_by_step(step_code=_blank_to_none(step_code)),
        steps=db.list_step_codes(),
        filters={"category": category or "", "step_code": step_code or ""},
        message=message, error=error,
    ))


@router.post("/materials/receive")
def materials_receive(material_code: str = Form(...), qty: float = Form(...),
                      material_name: str | None = Form(None), category: str | None = Form(None),
                      uom: str | None = Form(None), location: str | None = Form(None)):
    try:
        db.receive_material(material_code=material_code, qty=qty,
                            material_name=_blank_to_none(material_name),
                            category=_blank_to_none(category), uom=_blank_to_none(uom),
                            location=_blank_to_none(location))
    except ValueError as exc:
        return _redirect("/materials", error=str(exc))
    return _redirect("/materials", message="Material received")


# --------------------------------------------------------------------------- #
# BOM
# --------------------------------------------------------------------------- #

@router.get("/bom")
def bom(request: Request, product_code: str | None = None, step_code: str | None = None,
        message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "bom.html", _context(
        request,
        rows=db.list_bom(product_code=_blank_to_none(product_code),
                         step_code=_blank_to_none(step_code)),
        products=db.list_products(),
        steps=db.list_step_codes(),
        materials=db.list_materials(),
        filters={"product_code": product_code or "", "step_code": step_code or ""},
        message=message, error=error,
    ))


@router.post("/bom")
def bom_upsert(product_code: str = Form(...), step_code: str = Form(...),
               material_code: str = Form(...), qty_per_wafer: float = Form(...),
               uom: str | None = Form(None)):
    try:
        db.upsert_bom(product_code=product_code, step_code=step_code,
                      material_code=material_code, qty_per_wafer=qty_per_wafer,
                      uom=_blank_to_none(uom))
    except ValueError as exc:
        return _redirect("/bom", error=str(exc))
    return _redirect("/bom", message="BOM saved")


@router.post("/bom/{bom_id}/delete")
def bom_delete(bom_id: int):
    db.delete_bom(bom_id)
    return _redirect("/bom", message="BOM deleted")


# --------------------------------------------------------------------------- #
# 재공 (WIP) + 설비 (equipment)
# --------------------------------------------------------------------------- #

@router.get("/wip")
def wip(request: Request):
    return templates.TemplateResponse(request, "wip.html", _context(request, rows=db.get_wip()))


@router.get("/equipment")
def equipment(request: Request, message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "equipment.html", _context(
        request, rows=db.list_equipment(),
        statuses=db.EQUIPMENT_STATUSES, message=message, error=error,
    ))


@router.post("/equipment")
def equipment_create(eqp_id: str = Form(...), eqp_name: str = Form(...),
                     type: str | None = Form(None), status: str = Form("Idle")):
    try:
        db.create_equipment(eqp_id, eqp_name, type=_blank_to_none(type), status=status)
    except ValueError as exc:
        return _redirect("/equipment", error=str(exc))
    return _redirect("/equipment", message="설비 등록 완료")


@router.post("/equipment/{eqp_id}/update")
def equipment_update(eqp_id: str, eqp_name: str | None = Form(None),
                     type: str | None = Form(None), status: str | None = Form(None)):
    try:
        row = db.update_equipment(eqp_id, eqp_name=_blank_to_none(eqp_name),
                                  type=_blank_to_none(type),
                                  status=_blank_to_none(status))
    except ValueError as exc:
        return _redirect("/equipment", error=str(exc))
    if row is None:
        return _redirect("/equipment", error=f"설비 {eqp_id} 을(를) 찾을 수 없습니다")
    return _redirect("/equipment", message="설비 수정 완료")


@router.post("/equipment/{eqp_id}/delete")
def equipment_delete(eqp_id: str):
    try:
        deleted = db.delete_equipment(eqp_id)
    except ValueError as exc:
        return _redirect("/equipment", error=str(exc))
    if not deleted:
        return _redirect("/equipment", error=f"설비 {eqp_id} 을(를) 찾을 수 없습니다")
    return _redirect("/equipment", message="설비 삭제 완료")


# --------------------------------------------------------------------------- #
# 제품 (product master)
# --------------------------------------------------------------------------- #

@router.get("/products")
def products(request: Request, message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "products.html", _context(
        request, rows=db.list_products(), message=message, error=error,
    ))


@router.post("/products")
def product_create(product_code: str = Form(...), product_name: str = Form(...),
                   tech_node: str | None = Form(None)):
    try:
        db.create_product(product_code, product_name, _blank_to_none(tech_node))
    except ValueError as exc:
        return _redirect("/products", error=str(exc))
    return _redirect("/products", message="제품 등록 완료")


@router.post("/products/{product_code}/update")
def product_update(product_code: str, product_name: str | None = Form(None),
                   tech_node: str | None = Form(None)):
    try:
        row = db.update_product(product_code,
                                product_name=_blank_to_none(product_name),
                                tech_node=_blank_to_none(tech_node))
    except ValueError as exc:
        return _redirect("/products", error=str(exc))
    if row is None:
        return _redirect("/products", error=f"제품 {product_code} 을(를) 찾을 수 없습니다")
    return _redirect("/products", message="제품 수정 완료")


@router.post("/products/{product_code}/delete")
def product_delete(product_code: str):
    try:
        deleted = db.delete_product(product_code)
    except ValueError as exc:
        return _redirect("/products", error=str(exc))
    if not deleted:
        return _redirect("/products", error=f"제품 {product_code} 을(를) 찾을 수 없습니다")
    return _redirect("/products", message="제품 삭제 완료")


@router.get("/guide")
def guide(request: Request):
    return templates.TemplateResponse(request, "guide.html", _context(request))


# --------------------------------------------------------------------------- #
# MCP 문서 (the agent surface Swagger cannot describe)
# --------------------------------------------------------------------------- #

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "testserver"}


def _public_base_url(request: Request) -> str:
    """The URL a client outside the container should paste into its MCP config.

    ``request.base_url`` cannot be trusted for the scheme here. Caddy listens on
    plaintext ``:8080`` and rewrites ``X-Forwarded-Proto`` to its own listener's
    scheme (it only preserves the incoming value for a trusted proxy, and we
    declare none), so ACA's ``https`` is lost and uvicorn always sees ``http``.
    Publishing ``http://`` would hand out URLs that ACA redirects, and a
    redirected JSON-RPC POST breaks MCP clients and the documented ``curl -sN``.

    So: honour an explicit override, otherwise assume any non-local host is
    served over TLS, which is true for ACA ingress (``allowInsecure: false``).
    """
    override = os.getenv("MES_PUBLIC_BASE_URL")
    if override:
        return override.rstrip("/")

    url = request.base_url
    hostname = (url.hostname or "").lower()
    scheme = url.scheme if hostname in _LOCAL_HOSTS or hostname.endswith(".local") else "https"
    return str(url.replace(scheme=scheme)).rstrip("/")


@router.get("/mcp-docs")
def mcp_docs(request: Request):
    """Human-readable reference for the MCP surface, the sibling of /api/docs.

    Routed to the API container, not the MCP one: Caddy only forwards the exact
    path ``/mcp`` and the prefix ``/mcp/``, so ``/mcp-docs`` lands here.
    """
    return templates.TemplateResponse(
        request, "mcp_docs.html",
        _context(request, spec=mcp_spec.build_spec(), base_url=_public_base_url(request)))


@router.get("/mcp-docs.json")
def mcp_docs_json() -> dict[str, Any]:
    """Machine-readable MCP tool spec — the ``/api/openapi.json`` of the MCP side."""
    return mcp_spec.build_raw_spec()
