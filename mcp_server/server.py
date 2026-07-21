"""MCP server exposing the mock MES Process & Lot tools."""

from __future__ import annotations

import os
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


@mcp.tool(
    description=(
        "공정 입력: register a lot movement at a process step. Records wafer "
        "quantities (in_qty defaults to the lot's current wafer_qty; out = in - "
        "scrap), advances the lot's current_step, and closes the lot (+ creates a "
        "production_result) when the final step passes."
    )
)
def register_process_move(
    lot_id: str,
    step_code: str,
    eqp_id: str | None = None,
    in_qty: int | None = None,
    scrap_qty: int = 0,
    defect_code: str | None = None,
    operator: str | None = None,
    result: str = "Pass",
) -> dict[str, Any]:
    try:
        return db.add_process_move(
            lot_id=lot_id,
            step_code=step_code,
            eqp_id=eqp_id,
            in_qty=in_qty,
            scrap_qty=scrap_qty,
            defect_code=defect_code,
            operator=operator,
            result=result,
        )
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool(description="공정 조회(route): return the process route, optionally filtered by step_code or eqp_type.")
def get_process_route(
    step_code: str | None = None,
    eqp_type: str | None = None,
) -> list[dict[str, Any]]:
    return db.get_process_route(step_code=step_code, eqp_type=eqp_type)


@mcp.tool(
    description=(
        "공정 조회(history): return process history rows. Filter by lot_id, "
        "step_code, result, operator, defect_code, or has_scrap (True = only "
        "moves with scrap)."
    )
)
def get_process_history(
    lot_id: str | None = None,
    step_code: str | None = None,
    result: str | None = None,
    operator: str | None = None,
    defect_code: str | None = None,
    has_scrap: bool | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return db.get_process_history(
        lot_id=lot_id,
        step_code=step_code,
        result=result,
        operator=operator,
        defect_code=defect_code,
        has_scrap=has_scrap,
        limit=limit,
    )


@mcp.tool(description="로트 조회(one): return a lot with current step, wafer qty, cumulative_yield, and process history.")
def get_lot(lot_id: str) -> dict[str, Any]:
    lot = db.get_lot(lot_id)
    if lot is None:
        return {"not_found": f"Lot '{lot_id}' was not found."}
    return lot


@mcp.tool(description="로트 조회(list): return matching lots filtered by status, product, current_step, priority, or tech_node.")
def list_lots(
    status: str | None = None,
    product: str | None = None,
    current_step: str | None = None,
    priority: str | None = None,
    tech_node: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return db.list_lots(
        status=status,
        product=product,
        current_step=current_step,
        priority=priority,
        tech_node=tech_node,
        limit=limit,
    )


def main() -> None:
    import uvicorn

    uvicorn.run(build_asgi_app(), host="0.0.0.0", port=8001)


DEFAULT_API_KEY = "changjuahn"


class _ApiKeyGuard:
    """Pure-ASGI header gate: only requests carrying a valid X-API-Key pass.

    Kept as pure ASGI (not Starlette BaseHTTPMiddleware) so it never buffers the
    streamable-HTTP / SSE responses that the MCP transport relies on. The
    accepted key is read from MES_API_KEY at request time (default 'changjuahn').
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"x-api-key")
            expected = os.environ.get("MES_API_KEY", DEFAULT_API_KEY).encode()
            if provided is None or provided != expected:
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({
                    "type": "http.response.body",
                    "body": b'{"error":"Invalid or missing API key. Send header X-API-Key."}',
                })
                return
        await self.app(scope, receive, send)


def build_asgi_app() -> _ApiKeyGuard:
    """The streamable-HTTP MCP app wrapped in the API-key guard."""
    return _ApiKeyGuard(mcp.streamable_http_app())


__all__ = [
    "DEFAULT_API_KEY",
    "build_asgi_app",
    "get_lot",
    "get_process_history",
    "get_process_route",
    "list_lots",
    "main",
    "mcp",
    "register_process_move",
]
