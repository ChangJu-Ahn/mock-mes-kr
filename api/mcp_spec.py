"""Live introspection of the MCP surface, rendered as human-readable docs.

The web console only ever advertised ``/api/docs`` (Swagger), so the second
agent surface — the MCP server at ``/mcp`` — was invisible to anyone browsing
the site. This module reads the *real* tool registry out of
``mcp_server.server.mcp`` (the same object the MCP container serves) and turns
the raw JSON-Schema into something a template can render, so the docs page can
never drift from the server.

Nothing here talks to the MCP transport: ``FastMCP.list_tools()`` is a local
registry lookup, so building the spec is cheap and has no side effects.
"""

from __future__ import annotations

import asyncio
import json
import re
from functools import lru_cache
from typing import Any

MCP_PATH = "/mcp"
MCP_DOCS_PATH = "/mcp-docs"
MCP_SPEC_PATH = "/mcp-docs.json"
API_KEY_HEADER = "X-API-Key"
DEFAULT_API_KEY = "changjuahn"


def protocol_version() -> str:
    """The spec revision the *installed* SDK negotiates.

    Read from the SDK rather than hardcoded: the image pins ``mcp>=1.9,<2``, so
    a rebuild can bump the protocol revision and the docs must follow.
    """
    try:
        from mcp.types import LATEST_PROTOCOL_VERSION

        return LATEST_PROTOCOL_VERSION
    except Exception:  # pragma: no cover - SDK layout changed
        return "unknown"


# Curated overlay: grouping, call examples and return notes that JSON-Schema
# cannot express. Tools missing from this map still render with sane defaults,
# so adding a tool to the MCP server never breaks this page.
_TOOL_META: dict[str, dict[str, Any]] = {
    "start_lot": {
        "group": "로트 (Lot)",
        "kind": "write",
        "returns": "생성된 로트 1건 — lot_id, product_code, start_qty, wafer_qty, current_step(DIFF), status(Running), priority.",
        "example": {"product_code": "LX9", "start_qty": 25, "priority": "Hot"},
        "notes": [
            "첫 FAB 공정(DIFF)에서 status=Running 으로 시작합니다.",
            "product_code 가 없으면 {\"error\": ...} 를 돌려줍니다.",
        ],
    },
    "register_process_result": {
        "group": "공정 (Process)",
        "kind": "write",
        "returns": "공정실적 1건 + shortages(자재 부족 목록) + semi_receipt(TEST Pass 시 자동 SEMI 입고).",
        "example": {"lot_id": "LOT0001", "step_code": "PHOTO", "scrap_qty": 1, "result": "Pass"},
        "notes": [
            "in_qty 를 생략하면 로트의 현재 wafer_qty 를 사용합니다. out_qty = in_qty − scrap_qty.",
            "(product, step) BOM 자재를 자동 차감하고, 부족분은 shortages 로 알려줍니다.",
            "마지막 FAB 공정(TEST)이 Pass 면 로트가 Done 이 되고 SEMI 재고가 자동 입고됩니다.",
        ],
    },
    "get_lot": {
        "group": "로트 (Lot)",
        "kind": "read",
        "returns": "로트 1건 + cumulative_yield + history[] (공정 이력 전체). 없으면 {\"not_found\": ...}.",
        "example": {"lot_id": "LOT0001"},
        "notes": ["웹 콘솔의 /lots/{lot_id} 화면과 동일한 데이터입니다."],
    },
    "list_lots": {
        "group": "로트 (Lot)",
        "kind": "read",
        "returns": "조건에 맞는 로트 배열. 모든 필터는 선택이며 AND 로 결합됩니다.",
        "example": {"status": "Running", "limit": 20},
        "notes": ["status 는 Running / Hold / Done 중 하나입니다."],
    },
    "get_process_route": {
        "group": "공정 (Process)",
        "kind": "read",
        "returns": "공정 라우트 배열 — step_code, step_name, seq, stage(FAB|PACK), eqp_type.",
        "example": {"stage": "FAB"},
        "notes": ["FAB 8단계(DIFF → PHOTO → ETCH → IMP → CMP → THIN → METRO → TEST)와 PACK 단계를 반환합니다."],
    },
    "list_process_results": {
        "group": "공정 (Process)",
        "kind": "read",
        "returns": "공정실적 배열 — lot_id, step_code, eqp_id, in_qty, out_qty, scrap_qty, defect_code, result.",
        "example": {"has_scrap": True, "limit": 20},
        "notes": ["has_scrap=true 로 스크랩이 발생한 실적만 골라낼 수 있습니다."],
    },
    "get_wip": {
        "group": "재공 (WIP)",
        "kind": "read",
        "returns": "Done 이 아닌 로트를 current_step 별로 집계한 배열 — step_code, lot_count, wafer_qty.",
        "example": {"step_code": "ETCH"},
        "notes": ["웹 콘솔의 /wip 화면과 동일한 집계입니다."],
    },
}

_GROUP_ORDER = ["로트 (Lot)", "공정 (Process)", "재공 (WIP)", "기타 (Other)"]
_DEFAULT_GROUP = "기타 (Other)"

# "로트 투입(start_lot): create a new FAB lot ..." -> label / variant / summary
_DESCRIPTION_RE = re.compile(r"^\s*(?P<label>[^(:]+?)\s*\((?P<variant>[^)]*)\)\s*:\s*(?P<summary>.*)$", re.S)


def _split_description(name: str, description: str | None) -> tuple[str, str, str]:
    """Return (korean_label, variant, english_summary) for a tool description."""
    text = " ".join((description or "").split())
    match = _DESCRIPTION_RE.match(text)
    if not match:
        return ("", "", text)
    variant = match.group("variant").strip()
    return (
        match.group("label").strip(),
        "" if variant == name else variant,
        match.group("summary").strip(),
    )


def _type_label(schema: dict[str, Any] | None) -> str:
    """Flatten a JSON-Schema fragment into a short readable type, e.g. 'string | null'."""
    if not schema:
        return "any"
    if "enum" in schema:
        return " | ".join(json.dumps(v, ensure_ascii=False) for v in schema["enum"])
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    for combinator in ("anyOf", "oneOf", "allOf"):
        if combinator in schema:
            parts = [_type_label(s) for s in schema[combinator]]
            seen = [p for i, p in enumerate(parts) if p not in parts[:i]]
            return " | ".join(seen) or "any"
    raw = schema.get("type")
    if isinstance(raw, list):
        return " | ".join(str(t) for t in raw)
    if raw == "array":
        return f"array<{_type_label(schema.get('items'))}>"
    return str(raw or "any")


def _format_default(schema: dict[str, Any], required: bool) -> str:
    if required:
        return "—"
    if "default" not in schema:
        return "null"
    return json.dumps(schema["default"], ensure_ascii=False)


def _build_params(input_schema: dict[str, Any]) -> list[dict[str, Any]]:
    required = set(input_schema.get("required") or [])
    params = []
    for pname, pschema in (input_schema.get("properties") or {}).items():
        pschema = pschema if isinstance(pschema, dict) else {}
        is_required = pname in required
        params.append({
            "name": pname,
            "type": _type_label(pschema),
            "required": is_required,
            "default": _format_default(pschema, is_required),
            "description": pschema.get("description") or "",
        })
    # Required arguments first, then declaration order.
    params.sort(key=lambda p: not p["required"])
    return params


def _list_tools() -> list[Any]:
    """Read the tool registry, tolerating an already-running event loop."""
    from mcp_server.server import mcp

    try:
        return asyncio.run(mcp.list_tools())
    except RuntimeError:
        # A loop is already running in this thread (rare here, but be safe).
        return list(mcp._tool_manager.list_tools())  # noqa: SLF001


@lru_cache(maxsize=1)
def _raw_tools_json() -> str:
    """Cached registry snapshot.

    The tool list is fixed at import time by the ``@mcp.tool()`` decorators and
    is never mutated afterwards, so re-reading it per request only burns a new
    event loop. Cached as JSON (not as objects) so every caller gets its own
    unaliased copy and no one can poison the cache by mutating the result.
    """
    return json.dumps([_tool_payload(t) for t in _list_tools()], ensure_ascii=False)


def _raw_tools() -> list[dict[str, Any]]:
    return json.loads(_raw_tools_json())


def tool_names() -> list[str]:
    """Just the tool names — for callers that don't need the full render spec."""
    return [t.get("name", "") for t in _raw_tools()]


def _tool_payload(tool: Any) -> dict[str, Any]:
    """Normalise both ``mcp.types.Tool`` and FastMCP's internal Tool objects."""
    data = tool.model_dump(exclude_none=True, by_alias=True) if hasattr(tool, "model_dump") else {}
    if "inputSchema" in data:  # mcp.types.Tool — already the wire shape
        return data
    return {
        "name": data.get("name") or getattr(tool, "name", ""),
        "description": data.get("description") or getattr(tool, "description", "") or "",
        "inputSchema": data.get("parameters") or getattr(tool, "parameters", {}) or {},
    }


def _call_example(name: str, arguments: dict[str, Any]) -> str:
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return json.dumps(envelope, ensure_ascii=False, indent=2)


def build_spec() -> dict[str, Any]:
    """The full, live MCP surface description used by the docs page and JSON feed."""
    from mcp_server.server import mcp

    raw_tools = _raw_tools()

    tools: list[dict[str, Any]] = []
    for raw in raw_tools:
        name = raw.get("name", "")
        meta = _TOOL_META.get(name, {})
        label, variant, summary = _split_description(name, raw.get("description"))
        input_schema = raw.get("inputSchema") or {}
        example_args = meta.get("example", {})
        tools.append({
            "name": name,
            "label": label,
            "variant": variant,
            "summary": summary,
            "group": meta.get("group", _DEFAULT_GROUP),
            "kind": meta.get("kind", "read"),
            "params": _build_params(input_schema),
            "returns": meta.get("returns", ""),
            "notes": meta.get("notes", []),
            "example_args": json.dumps(example_args, ensure_ascii=False),
            "call_example": _call_example(name, example_args),
            "input_schema": json.dumps(input_schema, ensure_ascii=False, indent=2),
            "output_schema": json.dumps(raw["outputSchema"], ensure_ascii=False, indent=2)
            if raw.get("outputSchema") else "",
        })

    by_name = {t["name"]: t for t in tools}
    groups = []
    ordered_names = list(_GROUP_ORDER) + [g for g in {t["group"] for t in tools} if g not in _GROUP_ORDER]
    for group_name in ordered_names:
        members = [t for t in tools if t["group"] == group_name]
        if members:
            groups.append({"name": group_name, "tools": members})

    return {
        "server_name": getattr(mcp, "name", "mock-mes-mcp"),
        "instructions": getattr(mcp, "instructions", None),
        "transport": "streamable-http",
        "stateless": bool(getattr(getattr(mcp, "settings", None), "stateless_http", True)),
        "protocol_version": protocol_version(),
        "endpoint": MCP_PATH,
        "docs_path": MCP_DOCS_PATH,
        "spec_path": MCP_SPEC_PATH,
        "api_key_header": API_KEY_HEADER,
        "api_key": DEFAULT_API_KEY,
        "scope": "Process · Lot",
        "tool_count": len(tools),
        "tool_names": [t["name"] for t in tools],
        "write_tools": [t["name"] for t in tools if t["kind"] == "write"],
        "read_tools": [t["name"] for t in tools if t["kind"] == "read"],
        "tools": tools,
        "tools_by_name": by_name,
        "groups": groups,
        "raw_tools": raw_tools,
    }


def build_raw_spec() -> dict[str, Any]:
    """The MCP ``tools/list`` result, plus the connection details agents need."""
    spec = build_spec()
    return {
        "server": {
            "name": spec["server_name"],
            "version": "0.1.0",
            "protocolVersion": protocol_version(),
            "transport": "streamable-http",
            "stateless": spec["stateless"],
            "endpoint": MCP_PATH,
            "auth": {"header": API_KEY_HEADER, "demoKey": DEFAULT_API_KEY},
            "scope": spec["scope"],
            "docs": MCP_DOCS_PATH,
        },
        "capabilities": {"tools": {"listChanged": False}},
        "tools": spec["raw_tools"],
    }


__all__ = [
    "API_KEY_HEADER",
    "DEFAULT_API_KEY",
    "MCP_DOCS_PATH",
    "MCP_PATH",
    "MCP_SPEC_PATH",
    "build_raw_spec",
    "build_spec",
    "protocol_version",
    "tool_names",
]
