"""Shared core package for the mock MES.

Exposes the SQLite schema, a WAL-mode connection helper, and the full
data-access API used by BOTH the FastAPI app (web console + REST) and the
MCP server. One shared database, one shared code path.
"""

from . import db  # noqa: F401

__all__ = ["db"]
