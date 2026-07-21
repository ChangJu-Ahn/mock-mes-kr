"""Tiny header API-key gate for the REST surface.

Demo-grade only: a single shared key (default ``changjuahn``, overridable via
the ``MES_API_KEY`` env var) must be sent in the ``X-API-Key`` header. The web
console and ``/api/docs`` / ``/api/openapi.json`` stay open so a human can still
browse; only the JSON data endpoints are gated.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

API_KEY_NAME = "X-API-Key"
DEFAULT_API_KEY = "changjuahn"

_api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def expected_api_key() -> str:
    """The accepted key (read at call time so env changes/tests take effect)."""
    return os.environ.get("MES_API_KEY", DEFAULT_API_KEY)


def require_api_key(api_key: str | None = Security(_api_key_header)) -> str:
    if api_key != expected_api_key():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or missing API key. Send header '{API_KEY_NAME}'.",
        )
    return api_key
