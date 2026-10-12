"""Where the server listens and how both ends connect.

Unix-like systems use a Unix domain socket; Windows uses TCP on localhost with an
OS-chosen port that the server writes to a small address file. Either can be overridden
with ``--address`` or ``COMPONENT_INSPECT_ADDRESS`` (``unix:/path`` or ``tcp:host:port``).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_ADDRESS = "COMPONENT_INSPECT_ADDRESS"

# allow_pickle stays off: only plain values (str, numbers, bytes, tuples) cross the wire.
CONFIG: dict[str, Any] = {
    "sync_request_timeout": 600,  # a large render or check can take a while
    "allow_pickle": False,
    "allow_public_attrs": False,
}


def project_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())


def state_dir() -> Path:
    return project_root() / "build" / "component-inspect"


@dataclass(frozen=True)
class Address:
    kind: str  # "unix" or "tcp"
    path: str = ""
    host: str = "127.0.0.1"
    port: int = 0

    def __str__(self) -> str:
        return f"unix:{self.path}" if self.kind == "unix" else f"tcp:{self.host}:{self.port}"

    @classmethod
    def parse(cls, text: str) -> Address:
        kind, _, rest = text.partition(":")
        if kind == "unix" and rest:
            return cls("unix", path=rest)
        if kind == "tcp":
            host, _, port = rest.rpartition(":")
            if host and port.isdigit():
                return cls("tcp", host=host, port=int(port))
        raise ValueError(f"address must be unix:/path or tcp:host:port, got {text!r}")


def addr_file() -> Path:
    """Where a TCP server records the port it got."""
    return state_dir() / ".server.addr"


def server_address(override: str | None = None) -> Address:
    """The address the server should listen on (port 0 = let the OS pick)."""
    text = override or os.environ.get(ENV_ADDRESS)
    if text:
        return Address.parse(text)
    if sys.platform == "win32":
        return Address("tcp", host="127.0.0.1", port=0)
    return Address("unix", path=str(state_dir() / ".server.sock"))


def client_address(override: str | None = None) -> Address:
    """The address a client should connect to."""
    text = override or os.environ.get(ENV_ADDRESS)
    if text:
        return Address.parse(text)
    if sys.platform == "win32":
        try:
            return Address.parse(addr_file().read_text().strip())
        except (OSError, ValueError) as exc:
            raise ConnectionError(f"no running server found ({addr_file()} unreadable: {exc})") from exc
    return Address("unix", path=str(state_dir() / ".server.sock"))


def connect(address: Address, service: Any) -> Any:
    """Open an rpyc connection that exposes `service` to the other end."""
    import rpyc

    if address.kind == "unix":
        return rpyc.utils.factory.unix_connect(address.path, service=service, config=CONFIG)
    return rpyc.connect(address.host, address.port, service=service, config=CONFIG)
