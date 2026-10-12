"""InspectSession against small components whose geometry is known exactly."""

from __future__ import annotations

from pathlib import Path

import click
import gdsfactory as gf
import pytest

from component_inspect.session import InspectSession


@pytest.fixture
def straight(tmp_path: Path) -> InspectSession:
    return InspectSession(gf.components.straight(length=10), out_dir=tmp_path / "straight")


def rotated_straight(angle: float) -> gf.Component:
    c = gf.Component()
    ref = c << gf.components.straight(length=10)
    ref.drotate(angle)
    c.add_ports(ref.ports)
    return c


def issues(session: InspectSession, **rules: float) -> list[tuple[str, str | None]]:
    found = session.check(**rules).data["checks"]["issues"]
    return [(i["check"], i.get("port")) for i in found]


# -- rendered boxes ----------------------------------------------------------


@pytest.mark.parametrize(
    "box",
    [
        [-11.2, -0.8, -9.6, 0.8],  # sub-µm edges were rounded to whole µm
        [1.25, -0.35, 2.75, 0.35],
        [0.0, 0.0, 3.0, 3.0],
    ],
)
def test_zoom_box_kept(straight: InspectSession, box: list[float]) -> None:
    image = straight.zoom(boxes=[box], size=800).data["images"][0]
    assert image["box"] == pytest.approx(box, abs=image["um_per_px"])


def test_zoom_zero_height_box_keeps_its_width(straight: InspectSession) -> None:
    x0, _, x1, _ = straight.zoom(boxes=[[0, 0, 10, 0]]).data["images"][0]["box"]
    assert (x0, x1) == pytest.approx((0, 10))


def test_zoom_point_gets_a_view(straight: InspectSession) -> None:
    x0, y0, x1, y1 = straight.zoom(boxes=[[1, 0, 1, 0]]).data["images"][0]["box"]
    assert x0 < 1 < x1 and y0 < 0 < y1


def test_cut_view_centred_on_line(straight: InspectSession) -> None:
    x0, y0, x1, y1 = straight.cut([[2, -1, 2, 1.5]]).data["images"][0]["box"]
    assert ((x0 + x1) / 2, (y0 + y1) / 2) == pytest.approx((2, 0.25), abs=1e-3)


# -- cuts --------------------------------------------------------------------


def test_cut_measures_width(straight: InspectSession) -> None:
    (layer,) = straight.cut([[2, -1, 2, 1]]).data["cuts"][0]["layers"]
    assert [sp["s"] for sp in layer["spans"]] == [[0.75, 1.25]]


def test_cut_measures_gap(tmp_path: Path) -> None:
    s = InspectSession(gf.components.coupler_ring(gap=0.2, radius=10), out_dir=tmp_path / "ring")
    (layer,) = s.cut([[-2, -1, -2, 1.5]]).data["cuts"][0]["layers"]
    assert layer["gaps"] == [0.2]


def test_cut_needs_two_points(straight: InspectSession) -> None:
    with pytest.raises(click.ClickException):
        straight.cut([[1, 1, 1, 1]])


# -- checks ------------------------------------------------------------------


def test_check_passes_clean_straight(straight: InspectSession) -> None:
    assert issues(straight, min_width=0.4, grid=0.001) == []


def test_check_min_width(straight: InspectSession) -> None:
    assert issues(straight, min_width=1.0) == [("min width", None)]


@pytest.mark.parametrize("grid", [0.0005, 0.0015])
def test_check_rejects_grid_off_dbu(straight: InspectSession, grid: float) -> None:
    with pytest.raises(click.ClickException, match="multiple of the database unit"):
        straight.check(grid=grid)


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_check_manhattan_rotations_pass(tmp_path: Path, angle: float) -> None:
    assert issues(InspectSession(rotated_straight(angle), out_dir=tmp_path / "r")) == []


def test_check_rotated_port_is_on_geometry(tmp_path: Path) -> None:
    found = issues(InspectSession(rotated_straight(30), out_dir=tmp_path / "r"))
    assert ("port not on geometry", "o1") not in found
    assert ("port not on geometry", "o2") not in found
    assert ("port non-manhattan", "o2") in found


def test_check_ports_off_geometry(tmp_path: Path) -> None:
    c = gf.Component()
    c.add_polygon([(0, 0), (5, 0), (5, 1), (0, 1)], layer=(1, 0))
    c.add_port("on", center=(5, 0.5), width=1, orientation=0, layer=(1, 0))
    c.add_port("away", center=(20, 0.5), width=1, orientation=0, layer=(1, 0))
    c.add_port("half", center=(5, 1), width=1, orientation=0, layer=(1, 0))
    c.add_port("nudged", center=(5.001, 0.5), width=1, orientation=0, layer=(1, 0))
    found = issues(InspectSession(c, out_dir=tmp_path / "p"))
    assert sorted(port for _, port in found) == ["away", "half", "nudged"]


def test_check_rotated_port_off_geometry(tmp_path: Path) -> None:
    c = rotated_straight(30)
    c.add_port("away", center=(30, 30), width=0.5, orientation=30, layer=(1, 0))
    assert ("port not on geometry", "away") in issues(InspectSession(c, out_dir=tmp_path / "r"))


# -- diff --------------------------------------------------------------------


def test_diff_against_last_load(tmp_path: Path) -> None:
    InspectSession(gf.components.straight(length=10), out_dir=tmp_path / "d")
    after = InspectSession(gf.components.straight(length=12), out_dir=tmp_path / "d")
    d = after.diff().data["diff"]
    assert [(la["added_um2"], la["removed_um2"]) for la in d["layers"]] == [(1.0, 0.0)]
    assert d["ports"] == [{"port": "o2", "change": "modified", "x": [10.0, 12.0]}]


def test_diff_against_itself_is_identical(straight: InspectSession) -> None:
    assert straight.diff(against=str(straight.gds)).data["diff"]["identical"]
