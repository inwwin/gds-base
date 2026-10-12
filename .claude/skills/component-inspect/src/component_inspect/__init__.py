"""Inspect gdsfactory components with KLayout: a client holds the loaded cell, an MCP server drives it.

    from component_inspect import serve_component
    handle = serve_component(c, paths={"route": route}, block=False)
    handle.client_id  # pass to the component-inspect MCP tools
    handle.close()

`InspectSession` renders, cuts, checks and diffs without any server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import ClientHandle, serve_component
    from .session import InspectSession, Result

__all__ = ["ClientHandle", "InspectSession", "Result", "serve_component"]


def __getattr__(name: str) -> Any:
    # Lazy, so `component-inspect serve` never imports gdsfactory/KLayout.
    if name in ("ClientHandle", "serve_component"):
        from . import client

        return getattr(client, name)
    if name in ("InspectSession", "Result"):
        from . import session

        return getattr(session, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
