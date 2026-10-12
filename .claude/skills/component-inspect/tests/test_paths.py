"""Overlay paths: specs, labels, and the numbers measured from them."""

from __future__ import annotations

import math
from pathlib import Path

import click
import gdsfactory as gf
import pytest

from component_inspect.paths import normalize_path_entries, split_label
from component_inspect.session import InspectSession


@pytest.fixture
def straight(tmp_path: Path) -> InspectSession:
    return InspectSession(gf.components.straight(length=10), out_dir=tmp_path / "s")


def overlay(session: InspectSession, *specs: str) -> list[dict]:
    return session.overview(paths=list(specs), size=200).data["paths"]


def test_point_list(straight: InspectSession) -> None:
    (p,) = overlay(straight, "0,0;5,0;5,5")
    assert p["length"] == 10
    assert (p["start"], p["end"]) == ([0, 0, 0], [5, 5, 90])
    assert (p["min_radius"], p["sharp_corners"]) == (None, 1)


def test_euler_bend_numbers(straight: InspectSession) -> None:
    (p,) = overlay(straight, "bend=gf.path.euler(radius=5, angle=90)")
    assert p["label"] == "bend"
    assert p["end"][2] == pytest.approx(90)
    assert p["min_radius"] == pytest.approx(5, rel=1e-2)
    assert p["sharp_corners"] == 0


def test_arc_length(straight: InspectSession) -> None:
    (p,) = overlay(straight, "gf.path.arc(radius=10, angle=90)")
    assert p["length"] == pytest.approx(10 * math.pi / 2, rel=1e-3)


def test_component_in_scope(straight: InspectSession) -> None:
    (p,) = overlay(straight, "gf.path.straight(length=3).move(c.ports['o2'].center)")
    assert p["start"][:2] == [10, 0]


def test_python_file_path(straight: InspectSession, src: Path) -> None:
    (p,) = overlay(straight, f"r={src}/comp.py:euler")
    assert p["label"] == "r"


def test_default_labels_and_colours_continue(tmp_path: Path) -> None:
    s = InspectSession(gf.components.straight(), paths="gf.path.straight(length=2)", out_dir=tmp_path / "l")
    labels = [p["label"] for p in overlay(s, "gf.path.straight(length=3)")]
    assert labels == ["path 1", "path 2"]


def test_hide_paths(tmp_path: Path) -> None:
    s = InspectSession(gf.components.straight(), paths={"a": "0,0;1,0", "b": "0,0;0,1"}, out_dir=tmp_path / "h")
    assert [p["label"] for p in s.overview(hide_paths=["a"], size=200).data["paths"]] == ["b"]
    data = s.overview(hide_paths=["*"], size=200).data
    assert (data["paths"], data["hidden_paths"]) == ([], ["a", "b"])


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("0,0", "did not produce a path"),
        ("0,0;1", "needs at least two x,y points"),
        ("1,1;1,1", "fewer than two distinct points"),
        ("gf.nope()", "failed to evaluate"),
        ("gf.components.straight()", "did not produce a path"),
        ("missing.py:route", "missing.py does not exist"),
    ],
)
def test_bad_specs(straight: InspectSession, spec: str, message: str) -> None:
    with pytest.raises(click.ClickException, match=message):
        overlay(straight, spec)


def test_duplicate_label(straight: InspectSession) -> None:
    with pytest.raises(click.ClickException, match="used twice"):
        overlay(straight, "a=0,0;1,0", "a=0,0;0,1")


def test_hide_unknown(straight: InspectSession) -> None:
    with pytest.raises(click.ClickException, match="no path"):
        straight.overview(hide_paths=["x"])


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("route=0,0;1,0", ("route", "0,0;1,0")),
        ("gf.path.euler(radius=5)", (None, "gf.path.euler(radius=5)")),
        ("a==b", (None, "a==b")),
    ],
)
def test_split_label(spec: str, expected: tuple[str | None, str]) -> None:
    assert split_label(spec) == expected


def test_normalize_entries() -> None:
    p = gf.path.straight(length=1)
    assert normalize_path_entries(None) == []
    assert normalize_path_entries({"label": "x", "spec": "0,0;1,0"}) == [("x", "0,0;1,0")]
    assert normalize_path_entries({"r": p}) == [("r", p)]
    assert normalize_path_entries([p, p]) == [(None, p), (None, p)]
    assert normalize_path_entries(["a=0,0;1,0", p]) == [("a", "0,0;1,0"), (None, p)]
