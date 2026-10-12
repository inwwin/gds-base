"""Every SOURCE form, and the error each bad source gives."""

from __future__ import annotations

import io
from pathlib import Path

import click
import gdsfactory as gf
import pytest

from component_inspect.session import InspectSession


def load(tmp_path: Path, source: str, **kw: object) -> InspectSession:
    return InspectSession(source, out_dir=tmp_path / "out", **kw)  # type: ignore[arg-type]


def area(session: InspectSession) -> float:
    return sum(la["area_um2"] for la in session.summary().data["layers"])


# -- file.py:func ------------------------------------------------------------


def test_python_function_with_kwargs(tmp_path: Path, src: Path) -> None:
    s = load(tmp_path, f"{src}/comp.py:block", kwargs={"w": 2})
    assert area(s) == pytest.approx(10.0)


def test_python_function_returning_a_path(tmp_path: Path, src: Path) -> None:
    s = load(tmp_path, f"{src}/comp.py:euler")
    assert [p.label for p in s.paths] == ["path 1"]
    assert s.paths[0].end[2] == pytest.approx(90)


@pytest.mark.parametrize(
    ("attr", "kwargs", "message"),
    [
        ("nope", {}, "has no attribute 'nope'"),
        ("answer", {}, "did not produce a Component"),
        ("answer", {"w": 1}, "not callable"),
        ("block", {"zz": 1}, "unexpected keyword argument"),
        ("broken", {}, "boom"),
    ],
)
def test_python_function_errors(tmp_path: Path, src: Path, attr: str, kwargs: dict, message: str) -> None:
    with pytest.raises(click.ClickException, match=message):
        load(tmp_path, f"{src}/comp.py:{attr}", kwargs=kwargs)


def test_python_file_missing(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException, match="missing.py does not exist"):
        load(tmp_path, f"{tmp_path}/missing.py:block")


# -- scripts -----------------------------------------------------------------


def test_script_file_with_component_and_paths(tmp_path: Path) -> None:
    script = tmp_path / "make.py"
    script.write_text('c = gf.components.mmi1x2()\npaths = {"route": gf.path.euler(radius=5)}\n')
    s = load(tmp_path, str(script))
    assert {p.name for p in s.ports} == {"o1", "o2", "o3"}
    assert [p.label for p in s.paths] == ["route"]


def test_script_on_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("component = gf.components.straight(length=4)\n"))
    assert area(load(tmp_path, "-")) == pytest.approx(2.0)


def test_script_paths_only(tmp_path: Path) -> None:
    script = tmp_path / "route.py"
    script.write_text("paths = [gf.path.straight(length=3), gf.path.euler(radius=5)]\n")
    s = load(tmp_path, str(script))
    assert [p.label for p in s.paths] == ["path 1", "path 2"]
    assert s.paths[0].length == pytest.approx(3)


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("x = 1\ny = undefined_name + 1\n", r"NameError[\s\S]*line 2: y = undefined_name \+ 1"),
        ("x = 1\n", "must define `component`"),
        ("c = 42\n", "not usable"),
        ("c = gf.components.straight()\npaths = {'r': 3}\n", r"paths\['r'\] is not a path"),
        ("c = gf.components.straight()\npaths = 3\n", "must be a path"),
    ],
)
def test_script_errors(tmp_path: Path, code: str, message: str) -> None:
    script = tmp_path / "bad.py"
    script.write_text(code)
    with pytest.raises(click.ClickException, match=message):
        load(tmp_path, str(script))


def test_script_rejects_kwargs(tmp_path: Path) -> None:
    script = tmp_path / "s.py"
    script.write_text("c = gf.components.straight()\n")
    with pytest.raises(click.ClickException, match="--kwargs cannot be used with a script"):
        load(tmp_path, str(script), kwargs={"a": 1})


def test_empty_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("  \n"))
    with pytest.raises(click.ClickException, match="stdin was empty"):
        load(tmp_path, "-")


# -- gf expressions ----------------------------------------------------------


def test_expression_component(tmp_path: Path) -> None:
    s = load(tmp_path, "gf.components.straight(length=6)")
    assert area(s) == pytest.approx(3.0)
    assert s.name == "straight"


def test_expression_callable_with_kwargs(tmp_path: Path) -> None:
    assert area(load(tmp_path, "gf.components.straight", kwargs={"length": 8})) == pytest.approx(4.0)


def test_expression_path(tmp_path: Path) -> None:
    s = load(tmp_path, "gf.path.euler(radius=10, angle=90)")
    assert s.summary().data["layers"] == []
    assert len(s.paths) == 1


@pytest.mark.parametrize(
    ("source", "kwargs", "message"),
    [
        ("gf.components.mmi2x2(nope=1)", {}, "unexpected keyword argument"),
        ("gf.components.nothing", {}, "has no attribute"),
        ("gf.components.straight()", {"length": 1}, "needs an expression that evaluates to a callable"),
        ("1 + 1", {}, "did not produce a Component"),
    ],
)
def test_expression_errors(tmp_path: Path, source: str, kwargs: dict, message: str) -> None:
    with pytest.raises(click.ClickException, match=message):
        load(tmp_path, source, kwargs=kwargs)


# -- GDS ---------------------------------------------------------------------


@pytest.fixture
def two_tops(tmp_path: Path) -> Path:
    """A GDS with two top cells: 'big' (a 4 µm² box on 1/0) and 'small' (1 µm² on 2/0)."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = 0.001
    for name, size, layer in (("big", 2000, (1, 0)), ("small", 1000, (2, 0))):
        cell = layout.create_cell(name)
        cell.shapes(layout.layer(*layer)).insert(kdb.Box(0, 0, size, size))
    lib = tmp_path / "lib.gds"
    layout.write(str(lib))
    return lib


def test_gds_picks_cell(tmp_path: Path, two_tops: Path) -> None:
    assert area(load(tmp_path, str(two_tops), cell="small")) == pytest.approx(1.0)
    assert area(load(tmp_path, str(two_tops), cell="big")) == pytest.approx(4.0)


def test_gds_several_tops_uses_first_and_says_so(
    tmp_path: Path, two_tops: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    load(tmp_path, str(two_tops))
    assert "several top cells" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("name", "cell", "kwargs", "message"),
    [
        ("lib.gds", "nope", {}, "no cell 'nope'"),
        ("missing.gds", None, {}, "does not exist"),
        ("lib.gds", None, {"a": 1}, "--kwargs cannot be used with a GDS"),
    ],
)
def test_gds_errors(tmp_path: Path, two_tops: Path, name: str, cell: str | None, kwargs: dict, message: str) -> None:
    with pytest.raises(click.ClickException, match=message):
        load(tmp_path, str(tmp_path / name), cell=cell, kwargs=kwargs)


# -- objects and options -----------------------------------------------------


def test_object_rejects_kwargs(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException, match="kwargs only apply"):
        InspectSession(gf.components.straight(), kwargs={"a": 1}, out_dir=tmp_path)


def test_unnamed_all_angle_component_is_flattened(tmp_path: Path) -> None:
    c = gf.ComponentAllAngle()
    ref = c.create_vinst(gf.components.straight(length=10))
    ref.rotate(15)
    s = InspectSession(c, out_dir=tmp_path / "aa")
    assert area(s) == pytest.approx(5.0, rel=1e-3)


def test_bad_pdk(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException, match="cannot load PDK"):
        InspectSession(gf.components.straight(), pdk="no_such_module.PDK", out_dir=tmp_path)


def test_reload_keeps_previous_layout(tmp_path: Path) -> None:
    first = load(tmp_path, "gf.components.straight(length=2)")
    first.overview(size=200)
    second = load(tmp_path, "gf.components.straight(length=3)")
    assert second.prev_gds.exists()
    assert list(second.out.glob("*.png")) == []  # the old views are cleared
