from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette import status

from mes_core import db


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(include_in_schema=False)


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _context(request: Request, **extra: Any) -> dict[str, Any]:
    return {"request": request, **extra}


@router.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _context(request, counts=db.counts(), wip=db.get_wip()),
    )


@router.get("/production")
def production(
    request: Request,
    product: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    message: str | None = None,
    error: str | None = None,
):
    return templates.TemplateResponse(
        request,
        "production.html",
        _context(
            request,
            rows=db.list_production_results(product=product, date_from=date_from, date_to=date_to, limit=limit),
            products=db.list_products(),
            filters={"product": product or "", "date_from": date_from or "", "date_to": date_to or "", "limit": limit},
            message=message,
            error=error,
        ),
    )


@router.post("/production-results")
def production_create(
    product: str = Form(...),
    good_qty: int = Form(...),
    scrap_qty: int = Form(0),
    result_date: str | None = Form(None),
    line: str | None = Form(None),
    eqp_id: str | None = Form(None),
    yield_pct: float | None = Form(None),
):
    try:
        db.add_production_result(
            product=product,
            good_qty=good_qty,
            scrap_qty=scrap_qty,
            result_date=_blank_to_none(result_date),
            line=_blank_to_none(line),
            eqp_id=_blank_to_none(eqp_id),
            yield_pct=yield_pct,
        )
    except Exception as exc:
        return RedirectResponse(f"/production?error={str(exc)}", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/production?message=Production+result+registered", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/inventory")
def inventory(request: Request, category: str | None = None, location: str | None = None):
    return templates.TemplateResponse(
        request,
        "inventory.html",
        _context(
            request,
            rows=db.list_inventory(category=category, location=location),
            filters={"category": category or "", "location": location or ""},
        ),
    )


@router.get("/wip")
def wip(request: Request):
    return templates.TemplateResponse(request, "wip.html", _context(request, rows=db.get_wip()))


@router.get("/process")
def process(request: Request, message: str | None = None, error: str | None = None):
    return templates.TemplateResponse(
        request,
        "process.html",
        _context(
            request,
            route=db.get_process_route(),
            history=db.get_process_history(limit=100),
            lot_ids=db.list_lot_ids(),
            equipment=db.list_equipment(),
            message=message,
            error=error,
        ),
    )


@router.post("/process/moves")
def process_move(
    lot_id: str = Form(...),
    step_code: str = Form(...),
    eqp_id: str | None = Form(None),
    operator: str | None = Form(None),
    result: str = Form("Pass"),
    in_time: str | None = Form(None),
    out_time: str | None = Form(None),
):
    try:
        db.add_process_move(
            lot_id=lot_id,
            step_code=step_code,
            eqp_id=_blank_to_none(eqp_id),
            operator=_blank_to_none(operator),
            result=result,
            in_time=_blank_to_none(in_time),
            out_time=_blank_to_none(out_time),
        )
    except ValueError as exc:
        return RedirectResponse(f"/process?error={str(exc)}", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/process?message=Process+move+registered", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/lots")
def lots(
    request: Request,
    status_filter: str | None = None,
    product: str | None = None,
    current_step: str | None = None,
    limit: int = 200,
):
    return templates.TemplateResponse(
        request,
        "lots.html",
        _context(
            request,
            rows=db.list_lots(status=_blank_to_none(status_filter), product=product, current_step=current_step, limit=limit),
            products=db.list_products(),
            route=db.get_process_route(),
            filters={
                "status_filter": status_filter or "",
                "product": product or "",
                "current_step": current_step or "",
                "limit": limit,
            },
        ),
    )


@router.get("/lots/{lot_id}")
def lot_detail(request: Request, lot_id: str):
    lot = db.get_lot(lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")
    return templates.TemplateResponse(request, "lot_detail.html", _context(request, lot=lot))
