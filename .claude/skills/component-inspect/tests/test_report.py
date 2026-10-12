"""The text each request prints: what the agent actually reads."""

from __future__ import annotations

from pathlib import Path

import gdsfactory as gf
import pytest

from component_inspect.report import format_summary
from component_inspect.session import InspectSession


@pytest.fixture
def straight(tmp_path: Path) -> InspectSession:
    return InspectSession(gf.components.straight(length=10), paths={"r": "0,0;5,0;5,5"}, out_dir=tmp_path / "s")


def lines(text: str) -> list[str]:
    """The non-blank lines (blocks are separated by blank lines)."""
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def test_summary(straight: InspectSession) -> None:
    out = lines(straight.summary().text)
    assert out[1] == "BBox      : [0.0, -0.25, 10.0, 0.25]  size 10.0 x 0.5 µm  (dbu 0.001)"
    assert out[2].startswith('Settings  : {"length": 10')
    assert "  o2            10.0000     0.0000  0.5000      0  WG           optical" in out
    assert "  WG (1/0)                 colour #ff9d9d  polygons 1     area 5.0000 µm²  bbox [0.0, -0.25, 10.0, 0.25]" in out
    assert (
        "  r       length 10.0 µm  start (0.0, 0.0) 0°  end (5.0, 5.0) 90°  straight segments only  sharp corners: 1"
        in out
    )
    assert out[-1] == f"GDS       : {straight.gds.resolve()}"


def test_summary_lists_instances(tmp_path: Path) -> None:
    text = InspectSession(gf.components.ring_single(), out_dir=tmp_path / "r").summary().text
    assert "Instances : 6" in text


def test_summary_of_empty_cell(tmp_path: Path) -> None:
    out = lines(InspectSession(gf.Component(), out_dir=tmp_path / "e").summary().text)
    assert out[1] == "BBox      : (empty)  size 0.0 x 0.0 µm  (dbu 0.001)"
    assert out[out.index("Ports:") + 1] == "  (none)"
    assert out[out.index("Layers:") + 1] == "  (none)"


def test_cut(straight: InspectSession) -> None:
    out = lines(straight.cut([[2, -1, 2, 1], [2, 5, 3, 5]], size=200).text)
    assert out[:4] == [
        "Cut (2.0, -1.0) -> (2.0, 1.0), length 2.0 µm  [s = distance from start]",
        "  WG (1/0)                 [0.75, 1.25] (0.5)",
        "Cut (2.0, 5.0) -> (3.0, 5.0), length 1.0 µm  [s = distance from start]",
        "  (no geometry crossed)",
    ]
    assert out[4] == "Images:"
    assert out[5].endswith("_cut_1.png  box [0.5, -1.5, 3.5, 1.5]  (0.015 µm/px)")


def test_cut_gaps(tmp_path: Path) -> None:
    s = InspectSession(gf.components.coupler_ring(gap=0.2, radius=10), out_dir=tmp_path / "c")
    out = lines(s.cut([[-2, -1, -2, 1.5]], size=200).text)
    assert out[1] == "  WG (1/0)                 [0.75, 1.25] (0.5), [1.45, 1.95] (0.5)"
    assert out[2] == "                           gaps: [0.2]"


def test_check_pass(straight: InspectSession) -> None:
    assert lines(straight.check(size=200).text) == ["Checks    : PASS"]


def test_check_issues(straight: InspectSession) -> None:
    out = lines(straight.check(min_width=1.0, size=200).text)
    assert out[0] == "Checks    : 1 issue(s)"
    assert out[1].startswith('  [min width] WG (1/0): {"rule": 1.0, "edge_pairs": 1, "locations": 1')
    assert '"min_distance": 0.5' in out[1]


def test_check_notes_non_manhattan_edges(tmp_path: Path) -> None:
    s = InspectSession(gf.components.bend_circular(radius=10), out_dir=tmp_path / "b")
    assert any(line.startswith("  note: WG (1/0) has ") for line in lines(s.check(size=200).text))


def test_diff(tmp_path: Path) -> None:
    InspectSession(gf.components.straight(length=10), out_dir=tmp_path / "d")
    after = InspectSession(gf.components.straight(length=12), out_dir=tmp_path / "d")
    out = lines(after.diff(size=200).text)
    assert out[0].startswith(f"Diff vs {after.prev_gds} (cell straight_")
    assert out[1:4] == [
        "  WG (1/0)                 +1.0000 / -0.0000 µm²  in [10.0, -0.25, 12.0, 0.25]",
        '  port {"port": "o2", "change": "modified", "x": [10.0, 12.0]}',
        "  bbox [0.0, -0.25, 10.0, 0.25] -> [0.0, -0.25, 12.0, 0.25]",
    ]


def test_diff_identical(straight: InspectSession) -> None:
    assert lines(straight.diff(against=str(straight.gds)).text)[1] == "  identical geometry and ports"


def test_diff_from_empty(tmp_path: Path) -> None:
    InspectSession(gf.Component(), out_dir=tmp_path / "d")
    after = InspectSession(gf.components.straight(length=2), out_dir=tmp_path / "d")
    out = lines(after.diff(size=200).text)
    assert "  bbox (empty) -> [0.0, -0.25, 2.0, 0.25]" in out
    assert '  port {"port": "o1", "change": "added"}' in out


def test_hidden_paths(straight: InspectSession) -> None:
    assert lines(straight.overview(hide_paths=["r"], size=200).text)[0] == "Hidden paths: r"


def test_client_id_first() -> None:
    assert format_summary({"client_id": "ring-1", "gds": "x.gds"}) == "client id: ring-1\n\nGDS       : x.gds"
