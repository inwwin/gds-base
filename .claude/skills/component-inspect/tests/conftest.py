"""Shared fixtures: the generic PDK, and small source files written per test."""

from __future__ import annotations

from pathlib import Path

import gdsfactory as gf
import pytest

gf.gpdk.PDK.activate()

COMPONENT_PY = """\
import gdsfactory as gf


@gf.cell
def block(w: float = 1.0) -> gf.Component:
    c = gf.Component()
    c.add_polygon([(0, 0), (5, 0), (5, w), (0, w)], layer=(1, 0))
    return c


def euler() -> gf.Path:
    return gf.path.euler(radius=5, angle=90)


answer = 42


def broken() -> gf.Component:
    raise ValueError("boom")
"""


@pytest.fixture
def src(tmp_path: Path) -> Path:
    """A directory holding comp.py (functions to load) for file.py:func sources."""
    d = tmp_path / "src"
    d.mkdir()
    (d / "comp.py").write_text(COMPONENT_PY)
    return d
