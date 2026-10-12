"""``component-inspect serve`` (the MCP server) and ``component-inspect load`` (a client)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

ADDRESS_HELP = "Server address, unix:/path or tcp:host:port (default: per platform; env COMPONENT_INSPECT_ADDRESS)."


class JsonDictType(click.ParamType):
    name = "json"

    def convert(self, value: Any, param: Any, ctx: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            self.fail(f"invalid JSON: {exc}", param, ctx)
        if not isinstance(data, dict):
            self.fail("must be a JSON object", param, ctx)
        return data


@click.group(context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100})
def main() -> None:
    """Inspect gdsfactory components with KLayout through an MCP server.

    `serve` runs the MCP server (Claude Code starts it from the project's .mcp.json).
    `load` loads a component in a long-lived client and prints its client id for the MCP tools.
    """


@main.command()
@click.option("--address", help=ADDRESS_HELP)
def serve(address: str | None) -> None:
    """Run the stdio MCP server and the RPyC listener clients register with."""
    from .server import run_server

    run_server(address)


@main.command()
@click.argument("source")
@click.option("--kwargs", "kwargs", type=JsonDictType(), default="{}", help="JSON kwargs for a file.py:func source.")
@click.option("--cell", help="Cell to use from a GDS source (default: top cell).")
@click.option("--pdk", help="PDK to activate, as module.attr (default: gdsfactory generic PDK).")
@click.option(
    "--path",
    "paths",
    multiple=True,
    help="Overlay path kept for every render (repeatable), [LABEL=]SPEC: expression with gf/np/c in scope, "
    "file.py:func, or 'x0,y0;x1,y1;...'.",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Output directory (default: build/component-inspect/<name>).",
)
@click.option("--name", help="Name used for the client id and default output directory.")
@click.option("--address", help=ADDRESS_HELP)
def load(
    source: str,
    kwargs: dict[str, Any],
    cell: str | None,
    pdk: str | None,
    paths: tuple[str, ...],
    out_dir: Path | None,
    name: str | None,
    address: str | None,
) -> None:
    """Load SOURCE (file.py:func, script.py or - for stdin, file.gds, or a gf expression) and serve it.

    Prints `client id: ...` and the summary, then stays alive until the server closes it.
    """
    from .client import serve_component

    serve_component(
        source,
        kwargs=kwargs,
        cell=cell,
        pdk=pdk,
        paths=list(paths),
        out_dir=out_dir,
        name=name,
        address=address,
        block=True,
        echo=click.echo,
    )


if __name__ == "__main__":
    main()
