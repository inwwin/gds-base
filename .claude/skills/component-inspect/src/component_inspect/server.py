"""The server: a stdio MCP server whose tools forward to registered clients over RPyC.

It never imports gdsfactory or KLayout, so it starts fast; stdout belongs to MCP and all
logging goes to stderr.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import rpyc
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from rpyc.utils.helpers import classpartial
from rpyc.utils.server import ThreadedServer

from .transport import CONFIG, Address, addr_file, server_address

log = logging.getLogger("component_inspect.server")

INSTRUCTIONS = """\
Inspect gdsfactory components / GDS cells rendered with KLayout.

1. Load the component in a client process, in the background (it stays alive, holding the
   loaded cell), and read the `client id:` line it prints:
     pixi run component-inspect load src/ring.py:ring --kwargs '{"radius": 10}'
   SOURCE may also be a .gds file (--cell), a gf expression, or a script (`-` for stdin).
2. Call these tools with that client_id: summary, overview, zoom, cut, check, diff.
   Images come back inline; the text gives exact coordinates.
3. After editing the code, load again with the same --out, call diff(against="last") on the
   new client, and close the old one.
"""


@dataclass
class Client:
    client_id: str
    name: str
    conn: Any
    info: dict[str, Any] = field(default_factory=dict)


class Registry:
    """Live clients by id; ids are ``<name>-<n>``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: dict[str, Client] = {}
        self._count = 0

    def add(self, conn: Any, name: str, info: dict[str, Any]) -> str:
        with self._lock:
            self._count += 1
            client_id = f"{name}-{self._count}"
            self._clients[client_id] = Client(client_id, name, conn, info)
        log.info("client %s registered (%s)", client_id, info.get("source"))
        return client_id

    def drop(self, conn: Any) -> None:
        with self._lock:
            for cid in [cid for cid, c in self._clients.items() if c.conn is conn]:
                del self._clients[cid]
                log.info("client %s disconnected", cid)

    def get(self, client_id: str) -> Client | None:
        with self._lock:
            return self._clients.get(client_id)

    def all(self) -> list[Client]:
        with self._lock:
            return list(self._clients.values())


class ServerService(rpyc.Service):
    """Exposed to clients: ``register(name, info_json) -> client_id``."""

    def __init__(self, registry: Registry) -> None:
        super().__init__()
        self.registry = registry
        self._conn: Any = None

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    def on_disconnect(self, conn: Any) -> None:
        self.registry.drop(conn)

    def exposed_register(self, name: str, info_json: str) -> str:
        return self.registry.add(self._conn, str(name), json.loads(str(info_json)))


def start_rpyc(registry: Registry, address: Address) -> tuple[ThreadedServer, Address]:
    """Start the RPyC listener on a daemon thread; returns the server and its actual address."""
    factory = classpartial(ServerService, registry)
    if address.kind == "unix":
        path = Path(address.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():  # stale, or another server's: the newest server takes the address
            log.warning("replacing existing socket %s", path)
            path.unlink()
        server = ThreadedServer(factory, socket_path=str(path), protocol_config=CONFIG, auto_register=False)
        inode = path.stat().st_ino

        def cleanup() -> None:
            try:
                if path.stat().st_ino == inode:
                    path.unlink()
            except OSError:
                pass

        atexit.register(cleanup)
        actual = address
    else:
        server = ThreadedServer(
            factory, hostname=address.host, port=address.port, protocol_config=CONFIG, auto_register=False
        )
        actual = Address("tcp", host=address.host, port=server.port)
        if address.port == 0:  # clients find an OS-chosen port through the address file
            addr_file().parent.mkdir(parents=True, exist_ok=True)
            addr_file().write_text(str(actual))
    server.logger.setLevel(logging.WARNING)
    threading.Thread(target=server.start, name="rpyc-server", daemon=True).start()
    return server, actual


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------

ClientId = Annotated[str, Field(description="Client id printed by `component-inspect load` (`client id: ...`).")]
Paths = Annotated[
    list[str] | None,
    Field(
        description="Extra overlay paths for this render only, each `[LABEL=]SPEC`: a Python expression with "
        "gf, np and c (the component) in scope, `file.py:func`, or a point list `x0,y0;x1,y1;...`."
    ),
]
HidePaths = Annotated[
    list[str] | None, Field(description="Labels of load-time paths to leave out of this render; ['*'] hides all.")
]
Layers = Annotated[
    list[str] | None, Field(description="Layers to show / analyse, by name or 'L/D' (default: all).")
]
ShowViolations = Annotated[bool, Field(description="Mark the violations of the last `check` on the views.")]
Fill = Annotated[Literal["pdk", "solid", "outline"], Field(description="PDK stipples, solid, or outlines only.")]
Depth = Annotated[
    int | None, Field(ge=0, description="Child levels drawn; deeper cells as frames (0 = own shapes only).")
]
Size = Annotated[int, Field(ge=200, le=4000, description="Long edge of each image in px.")]
ShowPorts = Annotated[bool, Field(description="Draw port arrows and faces.")]
IncludeImages = Annotated[bool, Field(description="Return the PNGs inline (otherwise only their paths).")]
Boxes = Annotated[list[list[float]], Field(description="Views as [x0, y0, x1, y1] in µm.")]


def build_mcp(registry: Registry) -> MCPServer:
    mcp = MCPServer("component-inspect", instructions=INSTRUCTIONS)

    def client(client_id: str) -> Client:
        found = registry.get(client_id)
        if found is None:
            live = [c.client_id for c in registry.all()]
            raise ToolError(
                f"no client {client_id!r}; live clients: {live or 'none'}. "
                "Start one with `pixi run component-inspect load SOURCE` (in the background)."
            )
        return found

    async def call(
        client_id: str, method: str, args: dict[str, Any], ctx: Context, include_images: bool = True
    ) -> list[Any]:
        target = client(client_id)
        loop = asyncio.get_running_loop()

        steps = 0

        def progress(message: str) -> None:
            # Streamed as MCP progress notifications (sent when the caller asked for progress).
            nonlocal steps
            steps += 1
            asyncio.run_coroutine_threadsafe(ctx.report_progress(steps, None, str(message)), loop)

        def run() -> str:
            return str(target.conn.root.call(method, json.dumps(args), progress))

        try:
            raw = await asyncio.to_thread(run)
        except EOFError as exc:
            raise ToolError(f"client {client_id} disconnected") from exc
        result = json.loads(raw)
        if "error" in result:
            raise ToolError(result["error"])
        content: list[Any] = [result["text"]]
        if include_images:
            content += [Image(path=p) for p in result["images"]]
        return content

    def render_args(fill: str, depth: int | None, size: int, show_ports: bool) -> dict[str, Any]:
        return {"fill": fill, "depth": depth, "size": size, "show_ports": show_ports}

    @mcp.tool(structured_output=False)
    def list_clients() -> str:
        """List the live clients (loaded components) with their source and output directory."""
        clients = registry.all()
        if not clients:
            return "no clients; start one with `pixi run component-inspect load SOURCE` (in the background)"
        return "\n".join(
            f"{c.client_id}  source {c.info.get('source')}  out {c.info.get('out')}  pid {c.info.get('pid')}"
            for c in clients
        )

    @mcp.tool(structured_output=False)
    async def summary(client_id: ClientId, ctx: Context) -> list[Any]:
        """Cell bbox, settings, instances, ports (exact), per-layer area/bbox and the load-time paths."""
        return await call(client_id, "summary", {}, ctx)

    @mcp.tool(structured_output=False)
    async def overview(
        client_id: ClientId,
        ctx: Context,
        paths: Paths = None,
        hide_paths: HidePaths = None,
        layers: Layers = None,
        show_violations: ShowViolations = True,
        fill: Fill = "pdk",
        depth: Depth = None,
        size: Size = 1400,
        show_ports: ShowPorts = True,
        include_images: IncludeImages = True,
    ) -> list[Any]:
        """Render the whole cell with port arrows, µm axes, a layer legend and the overlay paths."""
        args = {"paths": paths, "hide_paths": hide_paths or [], "layers": layers, "show_violations": show_violations}
        return await call(
            client_id, "overview", args | render_args(fill, depth, size, show_ports), ctx, include_images
        )

    @mcp.tool(structured_output=False)
    async def zoom(
        client_id: ClientId,
        ctx: Context,
        boxes: Boxes | None = None,
        ports: Annotated[list[str] | None, Field(description="Ports to centre a view on.")] = None,
        all_ports: Annotated[bool, Field(description="One view per port.")] = False,
        radius: Annotated[
            float | None, Field(gt=0, description="Half-size in µm of port views (default max(4*width, 2)).")
        ] = None,
        paths: Paths = None,
        hide_paths: HidePaths = None,
        layers: Layers = None,
        show_violations: ShowViolations = True,
        fill: Fill = "pdk",
        depth: Depth = None,
        size: Size = 1400,
        show_ports: ShowPorts = True,
        include_images: IncludeImages = True,
    ) -> list[Any]:
        """Render close-up views of boxes and/or ports. Prefer several small views to one large one."""
        args = {
            "boxes": boxes or [],
            "ports": ports or [],
            "all_ports": all_ports,
            "radius": radius,
            "paths": paths,
            "hide_paths": hide_paths or [],
            "layers": layers,
            "show_violations": show_violations,
        }
        return await call(client_id, "zoom", args | render_args(fill, depth, size, show_ports), ctx, include_images)

    @mcp.tool(structured_output=False)
    async def cut(
        client_id: ClientId,
        ctx: Context,
        lines: Annotated[list[list[float]], Field(description="Cut lines as [x0, y0, x1, y1] in µm.")],
        layers: Layers = None,
        show_violations: ShowViolations = True,
        fill: Fill = "pdk",
        depth: Depth = None,
        size: Size = 1400,
        show_ports: ShowPorts = True,
        include_images: IncludeImages = True,
    ) -> list[Any]:
        """Measure where each layer starts and stops along each line, and the gaps between spans."""
        args = {"lines": lines, "layers": layers, "show_violations": show_violations}
        return await call(client_id, "cut", args | render_args(fill, depth, size, show_ports), ctx, include_images)

    @mcp.tool(structured_output=False)
    async def check(
        client_id: ClientId,
        ctx: Context,
        min_width: Annotated[float | None, Field(gt=0, description="Min width rule in µm.")] = None,
        min_space: Annotated[float | None, Field(gt=0, description="Min space rule in µm.")] = None,
        grid: Annotated[float | None, Field(gt=0, description="Manufacturing grid in µm.")] = None,
        layers: Layers = None,
        fill: Fill = "pdk",
        depth: Depth = None,
        size: Size = 1400,
        show_ports: ShowPorts = True,
        include_images: IncludeImages = True,
    ) -> list[Any]:
        """Run port and self-intersection checks, plus width/space/grid rules when given.

        Returns close-ups of the first violations; later views mark them until the next check.
        """
        args = {"min_width": min_width, "min_space": min_space, "grid": grid, "layers": layers}
        return await call(client_id, "check", args | render_args(fill, depth, size, show_ports), ctx, include_images)

    @mcp.tool(structured_output=False)
    async def diff(
        client_id: ClientId,
        ctx: Context,
        against: Annotated[
            str, Field(description="'last' (the previous load into the same --out) or a GDS file path.")
        ] = "last",
        fill: Fill = "pdk",
        depth: Depth = None,
        size: Size = 1400,
        show_ports: ShowPorts = True,
        include_images: IncludeImages = True,
    ) -> list[Any]:
        """Added/removed area per layer and port changes, with diff.png (added green, removed red)."""
        args = {"against": against}
        return await call(client_id, "diff", args | render_args(fill, depth, size, show_ports), ctx, include_images)

    @mcp.tool(structured_output=False)
    def close(client_id: ClientId) -> str:
        """Disconnect a client; its process exits."""
        target = client(client_id)
        target.conn.close()
        registry.drop(target.conn)
        return f"closed {client_id}"

    return mcp


def run_server(address: str | None = None) -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="component-inspect: %(message)s")
    registry = Registry()
    _, actual = start_rpyc(registry, server_address(address))
    log.info("listening on %s (pid %d)", actual, os.getpid())
    build_mcp(registry).run()
