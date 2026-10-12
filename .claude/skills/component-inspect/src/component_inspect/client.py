"""The client: holds a loaded InspectSession and serves requests from the server over RPyC."""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import rpyc

from .session import InspectSession
from .transport import Address, client_address, connect

METHODS = ("summary", "overview", "zoom", "cut", "check", "diff")


class ClientService(rpyc.Service):
    """Exposed to the server: ``call(method, args_json, progress)`` runs one session request.

    Only a JSON string goes back: ``{"text", "images", "data"}`` or ``{"error"}``. Expected
    input errors (click.ClickException) carry just their message; anything else carries the full
    traceback, which is also printed to this process's stderr.
    """

    def __init__(self, session: InspectSession) -> None:
        super().__init__()
        self.session = session
        self._lock = threading.Lock()  # KLayout's LayoutView is not thread-safe

    def exposed_call(self, method: str, args_json: str, progress: Callable[[str], Any] | None = None) -> str:
        def report(message: str) -> None:
            if progress is not None:
                try:
                    progress(message)
                except Exception:  # progress is best effort; never fail a request over it
                    pass

        with self._lock:
            try:
                method = str(method)
                if method not in METHODS:
                    raise click.ClickException(f"unknown request {method!r}; expected one of {METHODS}")
                kwargs = json.loads(str(args_json))
                result = getattr(self.session, method)(progress=report, **kwargs)
                return json.dumps({"text": result.text, "images": result.images, "data": result.data}, default=str)
            except click.ClickException as exc:
                return json.dumps({"error": exc.format_message()})
            except Exception as exc:
                tb = traceback.format_exc()
                print(tb, file=sys.stderr, flush=True)
                return json.dumps({"error": f"{method} failed with {exc!r}\n\n{tb}"})


@dataclass
class ClientHandle:
    """A session registered with the server under `client_id`."""

    client_id: str
    session: InspectSession
    conn: Any
    address: Address
    thread: Any = None  # rpyc.BgServingThread when serving in the background

    def serve_forever(self) -> None:
        """Serve requests on this thread until the server closes the connection."""
        try:
            self.conn.serve_all()
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    @property
    def closed(self) -> bool:
        return bool(self.conn.closed)

    def close(self) -> None:
        if self.thread is not None:
            try:
                self.thread.stop()
            except Exception:
                pass
            self.thread = None
        if not self.conn.closed:
            self.conn.close()


def serve_component(
    source: Any,
    *,
    kwargs: dict[str, Any] | None = None,
    cell: str | None = None,
    pdk: str | None = None,
    paths: Any = None,
    out_dir: Path | str | None = None,
    name: str | None = None,
    address: str | None = None,
    block: bool = True,
    echo: Callable[[str], Any] | None = print,
) -> ClientHandle:
    """Load SOURCE, register it with the running component-inspect server and serve it.

    SOURCE and the keyword arguments are those of InspectSession. The server assigns a client
    id (printed through `echo`, with the summary, and available as ``handle.client_id``) that
    the MCP tools take. With ``block=True`` this serves until the server closes the connection;
    with ``block=False`` it serves on a background thread and returns at once.
    Errors raise click.ClickException.
    """
    session = InspectSession(source, kwargs=kwargs, cell=cell, pdk=pdk, paths=paths, out_dir=out_dir, name=name)
    try:
        addr = client_address(address)
        conn = connect(addr, ClientService(session))
    except (OSError, ConnectionError, ValueError) as exc:
        raise click.ClickException(
            f"cannot reach the component-inspect server ({exc}). It is the `component-inspect` MCP server, "
            "started by Claude Code from the project's .mcp.json (or run `component-inspect serve`)."
        ) from exc
    info = {"name": session.name, "source": session.source, "pid": os.getpid(), "out": str(session.out.resolve())}
    client_id = str(conn.root.register(session.name, json.dumps(info)))
    handle = ClientHandle(client_id=client_id, session=session, conn=conn, address=addr)
    if echo is not None:
        summary = session.summary()
        summary.data["client_id"] = client_id
        echo(summary.text)
        sys.stdout.flush()
    if block:
        handle.serve_forever()
    else:
        handle.thread = rpyc.BgServingThread(conn)
    return handle
