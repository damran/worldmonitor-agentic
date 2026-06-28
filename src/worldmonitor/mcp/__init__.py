"""FastMCP server — the graph-read MCP tool surface (ADR 0063, slice 2b).

Import-light: re-exports ``build_server`` / ``main`` for callers, but merely importing
this package opens NO connection (``build_server`` only constructs a client when run
without an injected one).
"""

from __future__ import annotations

from worldmonitor.mcp.server import build_server, main

__all__ = ["build_server", "main"]
