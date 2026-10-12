"""Render options and the remaining checks, compared on the pixels they produce."""

from __future__ import annotations

from pathlib import Path

import click
import gdsfactory as gf
import numpy as np
import pytest
from matplotlib.image import imread

from component_inspect.model import MAX_CHECK_VIEWS
from component_inspect.session import InspectSession


def pixels(image: dict) -> np.ndarray:
    return imread(image["path"])


def differ(a: dict, b: dict) -> bool:
    pa, pb = pixels(a), pixels(b)
    return pa.shape != pb.shape or not np.array_equal(pa, pb)


@pytest.fixture
def ring(tmp_path: Path) -> InspectSession:
    return InspectSession(gf.components.ring_single(), out_dir=tmp_path / "ring")


@pytest.fixture
def heater(tmp_path: Path) -> InspectSession:
    return InspectSession(gf.components.straight_heater_metal(length=20), out_dir=tmp_path / "heater")


def overview(s: InspectSession, **kw: object) -> dict:
    return s.overview(size=300, **kw).data["images"][0]  # type: ignore[arg-type]


# -- overview ----------------------------------------------------------------


def test_overview_frames_cell_and_ports(heater: InspectSession) -> None:
    x0, y0, x1, y1 = overview(heater)["box"]
    for p in heater.ports:
        assert x0 < p.x < x1 and y0 < p.y < y1


def test_overview_keeps_overlay_paths_in_view(ring: InspectSession) -> None:
    _, _, x1, _ = overview(ring, paths=["0,0;60,0"])["box"]
    assert x1 > 60


def test_render_is_repeatable(ring: InspectSession) -> None:
    assert not differ(overview(ring), overview(ring))


# -- render options ----------------------------------------------------------


@pytest.mark.parametrize("fill", ["solid", "outline"])
def test_fill_changes_pixels(ring: InspectSession, fill: str) -> None:
    assert differ(overview(ring), overview(ring, fill=fill))


def test_depth_zero_draws_frames(ring: InspectSession) -> None:
    full, own = overview(ring), overview(ring, depth=0)
    assert differ(full, own)
    assert not differ(full, overview(ring))  # a later default render draws everything again


def test_layers_select_what_is_drawn(heater: InspectSession) -> None:
    only_wg = overview(heater, layers=["WG"])
    assert differ(overview(heater), only_wg)
    assert not differ(only_wg, overview(heater, layers=["1/0"]))


def test_layers_unknown(heater: InspectSession) -> None:
    with pytest.raises(click.ClickException, match=r"layers \['99/0'\] not present"):
        heater.overview(layers=["99/0"])


def test_show_ports(heater: InspectSession) -> None:
    assert differ(overview(heater), overview(heater, show_ports=False))


@pytest.mark.parametrize("size", [200, 640])
def test_size_sets_long_edge(heater: InspectSession, size: int) -> None:
    image = heater.zoom(boxes=[[0, 0, 4, 2]], size=size, show_ports=False).data["images"][0]
    x0, _, x1, _ = image["box"]
    assert (x1 - x0) / image["um_per_px"] == pytest.approx(size)


@pytest.mark.parametrize(
    ("option", "message"),
    [
        ({"fill": "dots"}, "fill must be one of"),
        ({"depth": -1}, "depth must be >= 0"),
        ({"size": 100}, "size must be between"),
        ({"colour": "red"}, "unknown render option"),
    ],
)
def test_bad_render_options(heater: InspectSession, option: dict, message: str) -> None:
    with pytest.raises(click.ClickException, match=message):
        heater.overview(**option)


# -- zoom on ports -----------------------------------------------------------


def test_zoom_port_radius(heater: InspectSession) -> None:
    (image,) = heater.zoom(ports=["o1"], radius=3, size=300).data["images"]
    assert image["box"] == pytest.approx([-3, -3, 3, 3], abs=image["um_per_px"])


def test_zoom_port_default_radius(heater: InspectSession) -> None:
    (image,) = heater.zoom(ports=["l_e1"], size=300).data["images"]  # 11 µm wide: radius 4 * 11
    x0, _, x1, _ = image["box"]
    assert x1 - x0 == pytest.approx(88, abs=image["um_per_px"])


def test_zoom_all_ports(heater: InspectSession) -> None:
    assert len(heater.zoom(all_ports=True, size=200).data["images"]) == len(heater.ports)


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"ports": ["nope"]}, "unknown port"),
        ({}, "needs at least one box or port"),
        ({"ports": ["o1"], "radius": 0}, "radius must be positive"),
        ({"boxes": [[0, 0, 1]]}, "exactly four numbers"),
        ({"boxes": ["a,b,c,d"]}, "not four numbers"),
    ],
)
def test_zoom_errors(heater: InspectSession, kw: dict, message: str) -> None:
    with pytest.raises(click.ClickException, match=message):
        heater.zoom(**kw)


def test_zoom_box_as_string(heater: InspectSession) -> None:
    (image,) = heater.zoom(boxes=["0,0,4,4"], size=200).data["images"]
    assert image["box"] == pytest.approx([0, 0, 4, 4], abs=image["um_per_px"])


# -- remaining checks --------------------------------------------------------


def blocks(gap: float) -> gf.Component:
    c = gf.Component()
    c.add_polygon([(0, 0), (2, 0), (2, 2), (0, 2)], layer=(1, 0))
    c.add_polygon([(2 + gap, 0), (4 + gap, 0), (4 + gap, 2), (2 + gap, 2)], layer=(1, 0))
    return c


def test_min_space(tmp_path: Path) -> None:
    s = InspectSession(blocks(0.1), out_dir=tmp_path / "sp")
    (issue,) = s.check(min_space=0.2, size=200).data["checks"]["issues"]
    assert (issue["check"], issue["first"][0]["min_distance"]) == ("min space", 0.1)
    assert s.check(min_space=0.1, size=200).data["checks"]["passed"]


def test_self_intersection(tmp_path: Path) -> None:
    c = gf.Component()
    c.add_polygon([(0, 0), (2, 2), (2, 0), (0, 2)], layer=(1, 0))  # a bow tie
    issues = InspectSession(c, out_dir=tmp_path / "bt").check(size=200).data["checks"]["issues"]
    assert [i["check"] for i in issues] == ["self-intersecting polygon"]


def test_off_grid_vertex(tmp_path: Path) -> None:
    c = gf.Component()
    c.add_polygon([(0, 0), (1.003, 0), (1.003, 1), (0, 1)], layer=(1, 0))
    data = InspectSession(c, out_dir=tmp_path / "g").check(grid=0.005, size=200).data
    assert [i["check"] for i in data["checks"]["issues"]] == ["off-grid vertex"]
    assert data["checks"]["issues"][0]["locations"] == 2


def test_check_layers_limit_the_rules(tmp_path: Path) -> None:
    c = blocks(0.1)
    c.add_polygon([(0, 5), (1, 5), (1, 6), (0, 6)], layer=(2, 0))
    s = InspectSession(c, out_dir=tmp_path / "ly")
    assert not s.check(min_space=0.2, layers=["1/0"], size=200).data["checks"]["passed"]
    assert s.check(min_space=0.2, layers=["2/0"], size=200).data["checks"]["passed"]


def test_check_views_are_capped(tmp_path: Path) -> None:
    c = gf.Component()
    for i in range(MAX_CHECK_VIEWS + 2):
        c.add_polygon([(10 * i, 0), (10 * i + 0.1, 0), (10 * i + 0.1, 1), (10 * i, 1)], layer=(1, 0))
    data = InspectSession(c, out_dir=tmp_path / "cap").check(min_width=0.2, size=200).data
    assert data["checks"]["issues"][0]["locations"] == MAX_CHECK_VIEWS + 2
    assert len(data["images"]) == 1  # one close-up per kind of issue


def test_violations_stay_marked(tmp_path: Path) -> None:
    s = InspectSession(blocks(0.1), out_dir=tmp_path / "vm")
    before = overview(s)
    s.check(min_space=0.2, size=200)
    marked = overview(s)
    assert differ(before, marked)
    assert not differ(before, overview(s, show_violations=False))
    s.check(size=200)  # a passing check clears the marks
    assert not differ(before, overview(s))
