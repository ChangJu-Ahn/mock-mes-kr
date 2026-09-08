from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api import mcp_spec
from api.auth import require_api_key
from mes_core import db

router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class McpSurface(BaseModel):
    """Pointer to the sibling agent surface, so `/api` never looks like the whole story."""

    endpoint: str
    docs: str
    spec: str
    transport: str
    scope: str
    tools: list[str]


class ApiIndex(BaseModel):
    name: str
    docs: str
    endpoints: list[str]
    mcp: McpSurface


class HealthResponse(BaseModel):
    status: str
    db: str
    counts: dict[str, int]


class ProductRow(BaseModel):
    product_code: str
    product_name: str
    tech_node: str | None = None


class ProductCreate(BaseModel):
    product_code: str = Field(..., description="Key. Must be unused.", examples=["AP10"])
    product_name: str = Field(..., examples=["AP10 Mobile SoC"])
    tech_node: str | None = Field(None, examples=["3nm"])


class ProductUpdate(BaseModel):
    """Only the fields you send change. The code itself is immutable."""

    product_name: str | None = Field(None, examples=["LX9 AP (rev B)"])
    tech_node: str | None = Field(None, examples=["4nm"])


class EquipmentRow(BaseModel):
    eqp_id: str
    eqp_name: str
    type: str | None = None
    status: str | None = None


class EquipmentCreate(BaseModel):
    eqp_id: str = Field(..., description="Key. Must be unused.", examples=["EQP-ETCH02"])
    eqp_name: str = Field(..., examples=["Etcher-B"])
    type: str | None = Field(None, examples=["Etcher"])
    status: Literal["Run", "Idle", "Down"] = "Idle"


class EquipmentUpdate(BaseModel):
    """Only the fields you send change. The id itself is immutable."""

    eqp_name: str | None = Field(None, examples=["Etcher-B (spare)"])
    type: str | None = Field(None, examples=["Etcher"])
    status: Literal["Run", "Idle", "Down"] | None = None


class ProductInventoryRow(BaseModel):
    product_code: str
    product_name: str | None = None
    item_type: str
    qty: float
    uom: str | None = None
    location: str | None = None


class ProductResultRow(BaseModel):
    id: int
    result_date: str
    lot_id: str | None = None
    product_code: str
    product_name: str | None = None
    item_type: str
    good_qty: int | None = None
    scrap_qty: int | None = None
    yield_pct: float | None = None
    source: str | None = None
    line: str | None = None
    eqp_id: str | None = None


class MaterialRow(BaseModel):
    material_code: str
    material_name: str
    category: str | None = None
    qty: float
    uom: str | None = None
    location: str | None = None


class MaterialByStepRow(BaseModel):
    product_code: str
    product_name: str | None = None
    step_code: str
    step_name: str | None = None
    material_code: str
    material_name: str | None = None
    qty_per_wafer: float
    on_hand_qty: float
    uom: str | None = None
    location: str | None = None


class BomRow(BaseModel):
    id: int
    product_code: str
    step_code: str
    step_name: str | None = None
    material_code: str
    material_name: str | None = None
    qty_per_wafer: float
    uom: str | None = None


class ProductResultCreate(BaseModel):
    lot_id: str
    item_type: Literal["SEMI", "FIN"]
    good_qty: int = Field(ge=0)
    scrap_qty: int = Field(default=0, ge=0)


class PackagingCreate(BaseModel):
    product_code: str
    in_qty: int = Field(gt=0)
    scrap_qty: int = Field(default=0, ge=0)
    lot_id: str | None = None
    eqp_id: str | None = None
    operator: str | None = None


class MaterialReceipt(BaseModel):
    material_code: str
    qty: float = Field(ge=0)
    material_name: str | None = None
    category: str | None = None
    uom: str | None = None
    location: str | None = None


class BomUpsert(BaseModel):
    product_code: str
    step_code: str
    material_code: str
    qty_per_wafer: float = Field(ge=0)
    uom: str | None = None


# --------------------------------------------------------------------------- #
# Index / health
# --------------------------------------------------------------------------- #

@router.get("", response_model=ApiIndex, tags=["Index"])
@router.get("/", response_model=ApiIndex, tags=["Index"], include_in_schema=False)
def api_index() -> dict[str, Any]:
    return {
        "name": "Mock MES REST API (Product · Material · BOM)",
        "docs": "/api/docs",
        "endpoints": [
            "GET /api/health",
            "GET|POST /api/products",
            "GET|PATCH|DELETE /api/products/{product_code}",
            "GET|POST /api/equipments",
            "GET|PATCH|DELETE /api/equipments/{eqp_id}",
            "GET /api/product-inventory",
            "GET|POST /api/product-results",
            "POST /api/packaging",
            "GET /api/materials",
            "GET /api/materials/by-step",
            "POST /api/materials/receipt",
            "GET|PUT /api/bom",
            "DELETE /api/bom/{bom_id}",
        ],
        "mcp": {
            "endpoint": mcp_spec.MCP_PATH,
            "docs": mcp_spec.MCP_DOCS_PATH,
            "spec": mcp_spec.MCP_SPEC_PATH,
            "transport": "streamable-http",
            "scope": "Process · Lot (lot history, process results, WIP are NOT in this REST API)",
            "tools": mcp_spec.tool_names(),
        },
    }


@router.get("/health", response_model=HealthResponse, tags=["Health"])
def health() -> dict[str, Any]:
    return {"status": "ok", "db": db.get_db_path(), "counts": db.counts()}


@router.get("/products", response_model=list[ProductRow], tags=["Product"])
def get_products() -> list[dict[str, Any]]:
    return db.list_products()


@router.get("/products/{product_code}", response_model=ProductRow, tags=["Product"])
def get_product(product_code: str) -> dict[str, Any]:
    row = db.get_product(product_code)
    if row is None:
        raise HTTPException(status_code=404, detail=f"product {product_code} not found")
    return row


@router.post("/products", response_model=ProductRow, status_code=201, tags=["Product"])
def post_product(payload: ProductCreate) -> dict[str, Any]:
    try:
        return db.create_product(payload.product_code, payload.product_name,
                                 payload.tech_node)
    except db.DuplicateKey as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/products/{product_code}", response_model=ProductRow, tags=["Product"])
def patch_product(product_code: str, payload: ProductUpdate) -> dict[str, Any]:
    try:
        row = db.update_product(product_code, product_name=payload.product_name,
                                tech_node=payload.tech_node)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if row is None:
        raise HTTPException(status_code=404, detail=f"product {product_code} not found")
    return row


@router.delete("/products/{product_code}", tags=["Product"])
def remove_product(product_code: str) -> dict[str, Any]:
    try:
        deleted = db.delete_product(product_code)
    except ValueError as exc:
        # Still referenced by lots/BOM/inventory: a conflict, not bad input.
        raise HTTPException(status_code=409, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"product {product_code} not found")
    return {"deleted": product_code}


# --------------------------------------------------------------------------- #
# 설비 (equipment)
# --------------------------------------------------------------------------- #

@router.get("/equipments", response_model=list[EquipmentRow], tags=["Equipment"])
def get_equipments() -> list[dict[str, Any]]:
    return db.list_equipment()


@router.get("/equipments/{eqp_id}", response_model=EquipmentRow, tags=["Equipment"])
def get_equipment(eqp_id: str) -> dict[str, Any]:
    row = db.get_equipment(eqp_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"equipment {eqp_id} not found")
    return row


@router.post("/equipments", response_model=EquipmentRow, status_code=201, tags=["Equipment"])
def post_equipment(payload: EquipmentCreate) -> dict[str, Any]:
    try:
        return db.create_equipment(payload.eqp_id, payload.eqp_name,
                                   type=payload.type, status=payload.status)
    except db.DuplicateKey as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/equipments/{eqp_id}", response_model=EquipmentRow, tags=["Equipment"])
def patch_equipment(eqp_id: str, payload: EquipmentUpdate) -> dict[str, Any]:
    try:
        row = db.update_equipment(eqp_id, eqp_name=payload.eqp_name,
                                  type=payload.type, status=payload.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if row is None:
        raise HTTPException(status_code=404, detail=f"equipment {eqp_id} not found")
    return row


@router.delete("/equipments/{eqp_id}", tags=["Equipment"])
def remove_equipment(eqp_id: str) -> dict[str, Any]:
    try:
        deleted = db.delete_equipment(eqp_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"equipment {eqp_id} not found")
    return {"deleted": eqp_id}


# --------------------------------------------------------------------------- #
# 제품 인벤토리 / 제품실적
# --------------------------------------------------------------------------- #

@router.get("/product-inventory", response_model=list[ProductInventoryRow], tags=["Product"])
def get_product_inventory(
    product_code: str | None = None,
    item_type: Literal["SEMI", "FIN"] | None = None,
) -> list[dict[str, Any]]:
    return db.list_product_inventory(product_code=product_code, item_type=item_type)


@router.get("/product-results", response_model=list[ProductResultRow], tags=["Product"])
def get_product_results(
    product_code: str | None = None,
    item_type: Literal["SEMI", "FIN"] | None = None,
    source: Literal["AUTO_FAB", "AUTO_PACK", "MANUAL"] | None = None,
    lot_id: str | None = None,
    date_from: str | None = Query(default=None, description="YYYY-MM-DD inclusive"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD inclusive"),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    return db.list_product_results(
        product_code=product_code, item_type=item_type, source=source, lot_id=lot_id,
        date_from=date_from, date_to=date_to, limit=limit,
    )


@router.post("/product-results", tags=["Product"])
def create_product_result(payload: ProductResultCreate) -> dict[str, Any]:
    try:
        return db.register_product_result(
            lot_id=payload.lot_id, item_type=payload.item_type,
            good_qty=payload.good_qty, scrap_qty=payload.scrap_qty,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/packaging", tags=["Product"])
def create_packaging(payload: PackagingCreate) -> dict[str, Any]:
    try:
        return db.package(
            product_code=payload.product_code, in_qty=payload.in_qty,
            scrap_qty=payload.scrap_qty, lot_id=payload.lot_id,
            eqp_id=payload.eqp_id, operator=payload.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------------------------------- #
# 자재 인벤토리 (material)
# --------------------------------------------------------------------------- #

@router.get("/materials", response_model=list[MaterialRow], tags=["Material"])
def get_materials(
    category: str | None = None,
    location: str | None = None,
    q: str | None = Query(default=None, description="search material_code / material_name"),
    min_qty: float | None = Query(default=None, ge=0),
    max_qty: float | None = Query(default=None, ge=0),
) -> list[dict[str, Any]]:
    return db.list_materials(category=category, location=location, q=q,
                             min_qty=min_qty, max_qty=max_qty)


@router.get("/materials/by-step", response_model=list[MaterialByStepRow], tags=["Material"])
def get_materials_by_step(step_code: str | None = None) -> list[dict[str, Any]]:
    """공정별 자재 소요/잔량 (BOM ⋈ material)."""
    return db.materials_by_step(step_code=step_code)


@router.post("/materials/receipt", response_model=MaterialRow, tags=["Material"])
def create_material_receipt(payload: MaterialReceipt) -> dict[str, Any]:
    try:
        return db.receive_material(
            material_code=payload.material_code, qty=payload.qty,
            material_name=payload.material_name, category=payload.category,
            uom=payload.uom, location=payload.location,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------------------------------- #
# BOM
# --------------------------------------------------------------------------- #

@router.get("/bom", response_model=list[BomRow], tags=["BOM"])
def get_bom(product_code: str | None = None, step_code: str | None = None) -> list[dict[str, Any]]:
    return db.list_bom(product_code=product_code, step_code=step_code)


@router.put("/bom", response_model=BomRow, tags=["BOM"])
def put_bom(payload: BomUpsert) -> dict[str, Any]:
    try:
        return db.upsert_bom(
            product_code=payload.product_code, step_code=payload.step_code,
            material_code=payload.material_code, qty_per_wafer=payload.qty_per_wafer,
            uom=payload.uom,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/bom/{bom_id}", tags=["BOM"])
def remove_bom(bom_id: int) -> dict[str, Any]:
    if not db.delete_bom(bom_id):
        raise HTTPException(status_code=404, detail=f"bom {bom_id} not found")
    return {"deleted": bom_id}
