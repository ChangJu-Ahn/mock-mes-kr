from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from mes_core import db


router = APIRouter(prefix="/api")


class ProductionResultCreate(BaseModel):
    product: str
    good_qty: int = Field(ge=0)
    scrap_qty: int = Field(default=0, ge=0)
    result_date: str | None = None
    line: str | None = None
    eqp_id: str | None = None
    yield_pct: float | None = None


class ProductionResult(BaseModel):
    id: int
    result_date: str
    line: str | None = None
    eqp_id: str | None = None
    product: str
    good_qty: int
    scrap_qty: int
    yield_pct: float


class ProductionSummaryRow(BaseModel):
    group: str
    records: int
    good_total: int
    scrap_total: int
    avg_yield: float


class InventoryItem(BaseModel):
    item_code: str
    item_name: str
    category: str
    qty: float
    uom: str
    location: str


class InventoryFacets(BaseModel):
    categories: list[str]
    locations: list[str]


class WipItem(BaseModel):
    seq: int
    step_code: str
    step_name: str
    lot_count: int
    wafer_qty: int


class HealthResponse(BaseModel):
    status: str
    db: str
    counts: dict[str, int]


class ApiIndex(BaseModel):
    name: str
    docs: str
    endpoints: list[str]


@router.get("", response_model=ApiIndex, tags=["Index"])
@router.get("/", response_model=ApiIndex, tags=["Index"], include_in_schema=False)
def api_index() -> dict[str, Any]:
    return {
        "name": "Mock MES REST API",
        "docs": "/api/docs",
        "endpoints": [
            "GET /api/health",
            "GET /api/products",
            "POST /api/production-results",
            "GET /api/production-results",
            "GET /api/production-results/summary",
            "GET /api/production-results/{id}",
            "GET /api/inventory",
            "GET /api/inventory/facets",
            "GET /api/inventory/{item_code}",
            "GET /api/wip",
        ],
    }


@router.get("/health", response_model=HealthResponse, tags=["Health"])
def health() -> dict[str, Any]:
    return {"status": "ok", "db": db.get_db_path(), "counts": db.counts()}


@router.get("/products", response_model=list[str], tags=["Index"])
def get_products() -> list[str]:
    """Distinct product names -- handy for discovering valid filter values."""
    return db.list_products()


# --------------------------------------------------------------------------- #
# 실적 (production results)
# --------------------------------------------------------------------------- #

@router.post("/production-results", response_model=ProductionResult, tags=["Production"])
def create_production_result(payload: ProductionResultCreate) -> dict[str, Any]:
    return db.add_production_result(**payload.model_dump())


@router.get("/production-results", response_model=list[ProductionResult], tags=["Production"])
def get_production_results(
    product: str | None = None,
    line: str | None = None,
    eqp_id: str | None = None,
    date_from: str | None = Query(default=None, description="YYYY-MM-DD inclusive"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD inclusive"),
    min_yield: float | None = Query(default=None, ge=0, le=100),
    sort: Literal["date", "yield", "good", "scrap"] = "date",
    order: Literal["asc", "desc"] = "desc",
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    return db.list_production_results(
        product=product, line=line, eqp_id=eqp_id, date_from=date_from,
        date_to=date_to, min_yield=min_yield, sort=sort, order=order, limit=limit,
    )


@router.get("/production-results/summary", response_model=list[ProductionSummaryRow], tags=["Production"])
def get_production_summary(
    group_by: Literal["product", "line"] = "product",
) -> list[dict[str, Any]]:
    rows = db.production_summary(group_by=group_by)
    return [{"group": r[group_by], **{k: v for k, v in r.items() if k != group_by}} for r in rows]


@router.get("/production-results/{result_id}", response_model=ProductionResult, tags=["Production"])
def get_production_result(result_id: int) -> dict[str, Any]:
    row = db.get_production_result(result_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"production result {result_id} not found")
    return row


# --------------------------------------------------------------------------- #
# 재고 (inventory)
# --------------------------------------------------------------------------- #

@router.get("/inventory", response_model=list[InventoryItem], tags=["Inventory"])
def get_inventory(
    category: str | None = None,
    location: str | None = None,
    q: str | None = Query(default=None, description="search item_code / item_name"),
    min_qty: float | None = Query(default=None, ge=0),
    max_qty: float | None = Query(default=None, ge=0),
) -> list[dict[str, Any]]:
    return db.list_inventory(
        category=category, location=location, q=q, min_qty=min_qty, max_qty=max_qty
    )


@router.get("/inventory/facets", response_model=InventoryFacets, tags=["Inventory"])
def get_inventory_facets() -> dict[str, Any]:
    return db.inventory_facets()


@router.get("/inventory/{item_code}", response_model=InventoryItem, tags=["Inventory"])
def get_inventory_item(item_code: str) -> dict[str, Any]:
    row = db.get_inventory_item(item_code)
    if row is None:
        raise HTTPException(status_code=404, detail=f"inventory item {item_code!r} not found")
    return row


# --------------------------------------------------------------------------- #
# 재공 (WIP)
# --------------------------------------------------------------------------- #

@router.get("/wip", response_model=list[WipItem], tags=["WIP"])
def get_wip(step_code: str | None = None) -> list[dict[str, Any]]:
    return db.get_wip(step_code=step_code)
