"""MCP server exposing the mock MES Process & Lot tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mes_core import db

mcp = FastMCP(
    "mock-mes-mcp",
    host="0.0.0.0",
    port=8001,
    streamable_http_path="/mcp",
    stateless_http=True,
)


@mcp.tool(description="공정 입력: register a lot movement at a process step and advance the lot current_step.")
def register_process_move(
    lot_id: str,
    step_code: str,
    eqp_id: str | None = None,
    operator: str | None = None,
    result: str = "Pass",
) -> dict[str, Any]:
    try:
        return db.add_process_move(
            lot_id=lot_id,
            step_code=step_code,
            eqp_id=eqp_id,
            operator=operator,
            result=result,
        )
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool(description="공정 조회(route): return the ordered MES process route.")
def get_process_route() -> list[dict[str, Any]]:
    return db.get_process_route()


@mcp.tool(description="공정 조회(history): return process history rows, optionally filtered by lot_id.")
def get_process_history(lot_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return db.get_process_history(lot_id=lot_id, limit=limit)


@mcp.tool(description="로트 조회(one): return a lot with current step details and process history.")
def get_lot(lot_id: str) -> dict[str, Any]:
    lot = db.get_lot(lot_id)
    if lot is None:
        return {"not_found": f"Lot '{lot_id}' was not found."}
    return lot


@mcp.tool(description="로트 조회(list): return matching lots filtered by status, product, or current_step.")
def list_lots(
    status: str | None = None,
    product: str | None = None,
    current_step: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return db.list_lots(status=status, product=product, current_step=current_step, limit=limit)


def main() -> None:
    mcp.run(transport="streamable-http")


__all__ = [
    "get_lot",
    "get_process_history",
    "get_process_route",
    "list_lots",
    "main",
    "mcp",
    "register_process_move",
]
