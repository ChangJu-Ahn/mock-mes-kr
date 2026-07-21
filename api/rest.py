from typing import Any

from fastapi import APIRouter, Query
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


class InventoryItem(BaseModel):
    item_code: str
    item_name: str
    category: str
    qty: int
    uom: str
    location: str


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
            "POST /api/production-results",
            "GET /api/production-results",
            "GET /api/inventory",
            "GET /api/wip",
        ],
    }


@router.get("/health", response_model=HealthResponse, tags=["Health"])
def health() -> dict[str, Any]:
    return {"status": "ok", "db": db.get_db_path(), "counts": db.counts()}


@router.post("/production-results", response_model=ProductionResult, tags=["Production"])
def create_production_result(payload: ProductionResultCreate) -> dict[str, Any]:
    return db.add_production_result(**payload.model_dump())


@router.get("/production-results", response_model=list[ProductionResult], tags=["Production"])
def get_production_results(
    product: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    return db.list_production_results(product=product, date_from=date_from, date_to=date_to, limit=limit)


@router.get("/inventory", response_model=list[InventoryItem], tags=["Inventory"])
def get_inventory(category: str | None = None, location: str | None = None) -> list[dict[str, Any]]:
    return db.list_inventory(category=category, location=location)


@router.get("/wip", response_model=list[WipItem], tags=["WIP"])
def get_wip() -> list[dict[str, Any]]:
    return db.get_wip()
