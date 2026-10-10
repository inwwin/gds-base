#!/usr/bin/env python3
"""Inspect a gdsfactory component (or GDS cell) for an agentic design loop.

Renders views directly with KLayout (no matplotlib), annotates ports, prints a
precise text summary (ports, per-layer statistics, cross-section cuts), runs
lightweight geometry checks and diffs against a previous GDS.

Usage
-----
    pixi run inspect-layout SOURCE [OPTIONS]

SOURCE is one of:

* ``path/to/file.py:func``  - import the file and call ``func(**kwargs)``
  (``--kwargs '{"gap": 0.2}'``). A module-level Component is also accepted.
* ``path/to/file.gds``      - load a GDS cell (``--cell NAME``, default: top cell)
* ``gf.components.mmi1x2()`` - a Python expression evaluated with ``gf`` in scope
* a bare ``gf.Path`` (or list of paths), e.g. ``gf.path.euler(radius=10)`` - drawn on its own

``--path SPEC`` (repeatable) overlays centrelines on the component and reports their
length, end points/headings, minimum bend radius and sharp corners. SPEC is an
expression with ``gf``, ``np`` and ``c`` (the loaded component) in scope, a
``file.py:func`` returning path(s), or a point list ``"x0,y0;x1,y1;..."``.

Every run writes into ``--out`` (default ``build/inspect/<name>/``):

* ``layout.gds``       - the rendered cell (the previous one is kept as
  ``layout.prev.gds`` so ``--diff last`` compares against the last run)
* ``overview.png``     - the whole cell with port arrows, µm axes and a legend
* ``zoom_*.png``       - one image per ``--zoom`` / ``--zoom-port`` view
* ``check_*.png``      - close-ups of the first violations (with ``--check``)
* ``cut_*.png``        - each ``--cut`` line drawn over its neighbourhood
* ``diff.png``         - added (green) / removed (red) geometry (with ``--diff``)
* ``summary.json``     - everything printed to stdout, machine readable

Examples:
--------
    pixi run inspect-layout "gf.components.mmi1x2()" --zoom-ports
    pixi run inspect-layout "gf.components.coupler(gap=0.2)" --cut 0,-2,0,2
    pixi run inspect-layout my_pkg/ring.py:ring --kwargs '{"radius": 10}' \\
        --check --min-width 0.3 --min-space 0.2 --diff last
    pixi run inspect-layout "gf.components.mmi1x2()" \\
        --path 'gf.path.euler(radius=10).move(c.ports["o2"].center)'
"""

from __future__ import annotations

import importlib
import importlib.util
import itertools
import json
import math
import re
import shutil
import sys
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

MAX_LISTED = 20  # violations / instances listed individually in the summary
MAX_MARKERS = 2000  # polygons drawn as markers per category
MAX_CHECK_VIEWS = 4  # close-up images of check violations
# Edges meeting at >= this angle are not checked against each other. KLayout's
# default (90) flags the ~89.9 degree corners where a curved waveguide is cut off.
IGNORE_ANGLE = 80

COLOR_VIOLATION = 0xFF0000
COLOR_ADDED = 0x00B000
COLOR_REMOVED = 0xFF0000
# Overlay paths cycle through these; chosen to differ from the marker and port colours.
PATH_COLORS = (0xD000D0, 0xFF8000, 0x008C8C, 0x7030A0, 0x808000)
SHARP_CORNER_DEG = 5.0  # a turn this large at a single vertex is a corner, not a discretised curve


@dataclass
class PortInfo:
    name: str
    x: float
    y: float
    width: float
    orientation: float
    layer: str
    port_type: str

    def direction(self) -> tuple[float, float]:
        a = math.radians(self.orientation)
        return math.cos(a), math.sin(a)


@dataclass
class LayerEntry:
    index: int
    name: str
    layer: int
    datatype: int
    fill: str = "#808080"
    frame: str = "#808080"

    @property
    def label(self) -> str:
        return f"{self.name} ({self.layer}/{self.datatype})" if self.name else f"{self.layer}/{self.datatype}"


@dataclass
class MarkerSet:
    """Polygons (in µm) drawn over every rendered view."""

    label: str
    color: int
    polygons: list[Any] = field(default_factory=list)  # list[kdb.DPolygon]
    dither: int = 1  # 1 = hollow
    focus: list[tuple[str, Any]] = field(default_factory=list)  # (title, kdb.DBox) worth a close-up


@dataclass
class PathInfo:
    """A path drawn over the layout as a centreline, with its key numbers."""

    label: str
    spec: str
    color: int
    points: list[tuple[float, float]]
    length: float
    start: tuple[float, float, float]  # x, y, heading in degrees
    end: tuple[float, float, float]
    min_radius: float | None
    sharp_corners: int

    def summary(self) -> dict[str, Any]:
        xs = [x for x, _ in self.points]
        ys = [y for _, y in self.points]
        return {
            "label": self.label,
            "spec": self.spec,
            "points": len(self.points),
            "length": round(self.length, 4),
            "start": [round(v, 4) for v in self.start],
            "end": [round(v, 4) for v in self.end],
            "min_radius": round(self.min_radius, 4) if self.min_radius is not None else None,
            "sharp_corners": self.sharp_corners,
            "bbox": [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)],
        }


@dataclass
class Target:
    """The cell under inspection, independent of where it came from."""

    name: str
    kcell: Any  # kfactory ProtoTKCell (gf.Component or kf.KCell)
    source_paths: list[Any] = field(default_factory=list)  # set when SOURCE was a bare path

    @property
    def layout(self) -> Any:
        return self.kcell.kcl.layout

    @property
    def cell(self) -> Any:
        return self.kcell.kdb_cell

    @property
    def dbu(self) -> float:
        return self.layout.dbu


# --------------------------------------------------------------------------
# CLI parameter types
# --------------------------------------------------------------------------


class BoxType(click.ParamType):
    """``x0,y0,x1,y1`` in µm."""

    name = "x0,y0,x1,y1"

    def convert(self, value: Any, param: Any, ctx: Any) -> tuple[float, float, float, float]:
        if isinstance(value, tuple):
            return value  # type: ignore[return-value]
        try:
            parts = [float(v) for v in str(value).split(",")]
        except ValueError:
            self.fail(f"{value!r} is not four comma-separated numbers", param, ctx)
        if len(parts) != 4:
            self.fail(f"{value!r} needs exactly four numbers x0,y0,x1,y1", param, ctx)
        return parts[0], parts[1], parts[2], parts[3]


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


BOX = BoxType()
JSON_DICT = JsonDictType()

# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def activate_pdk(spec: str | None) -> None:
    import gdsfactory as gf

    if not spec:
        gf.gpdk.PDK.activate()
        return
    module_name, _, attr = spec.replace(":", ".").rpartition(".")
    if not module_name:
        raise click.ClickException(f"--pdk must look like 'module.attr', got {spec!r}")
    try:
        pdk = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise click.ClickException(f"cannot load PDK {spec!r}: {exc}") from exc
    pdk.activate()


def _short_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:60] or "cell"


def load_target(source: str, kwargs: dict[str, Any], cell_name: str | None) -> Target:
    import gdsfactory as gf

    path_part, sep, func_name = source.rpartition(":")
    if sep and path_part.endswith(".py") and Path(path_part).is_file():
        return _load_python(Path(path_part), func_name, kwargs)
    if source.lower().endswith((".gds", ".oas", ".gds.gz")):
        if kwargs:
            raise click.ClickException("--kwargs cannot be used with a GDS source")
        return _load_gds(Path(source), cell_name)

    namespace: dict[str, Any] = {"gf": gf}
    try:
        import numpy as np

        namespace["np"] = np
    except ImportError:
        pass
    try:
        obj = eval(source, namespace)  # local developer tool: full eval is intended
    except Exception as exc:
        raise click.ClickException(f"failed to evaluate {source!r}: {exc!r}") from exc
    if callable(obj) and not isinstance(obj, gf.Component):
        obj = obj(**kwargs)
    elif kwargs:
        raise click.ClickException("--kwargs needs an expression that evaluates to a callable")
    match = re.match(r"\s*(?:gf\.)?(?:components\.|path\.)?(\w+)", source)
    return _as_target(obj, match.group(1) if match else None)


def _import_attr(path: Path, attr: str) -> Any:
    path = path.resolve()
    sys.path.insert(0, str(path.parent))
    module_name = f"_inspect_{path.stem}_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise click.ClickException(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise click.ClickException(f"importing {path} failed: {exc!r}") from exc
    if not hasattr(module, attr):
        raise click.ClickException(f"{path} has no attribute {attr!r}")
    return getattr(module, attr)


def _load_python(path: Path, func_name: str, kwargs: dict[str, Any]) -> Target:
    obj = _import_attr(path, func_name)
    if callable(obj):
        try:
            obj = obj(**kwargs)
        except Exception as exc:
            raise click.ClickException(f"{func_name}(**{kwargs}) raised {exc!r}") from exc
    elif kwargs:
        raise click.ClickException(f"{func_name} is not callable, so --kwargs cannot be applied")
    return _as_target(obj, func_name)


def _as_target(obj: Any, name: str | None) -> Target:
    import gdsfactory as gf
    import kfactory as kf

    if _paths_in(obj) is not None:
        empty = gf.Component()
        empty.name = f"paths_{_short_name(name or 'path')}"
        return Target(name=_short_name(name or "path"), kcell=empty, source_paths=_paths_in(obj) or [])
    if isinstance(obj, gf.ComponentAllAngle):
        flat = gf.Component()
        flat.name = obj.name
        kf.VInstance(obj).insert_into_flat(flat, levels=0)
        flat.add_ports(obj.ports)
        obj = flat
    if not isinstance(obj, kf.ProtoTKCell):
        raise click.ClickException(f"source did not produce a Component (got {type(obj).__name__})")
    if isinstance(obj, gf.Component):
        obj.insert_vinsts()
    return Target(name=_short_name(name or obj.name or "cell"), kcell=obj)


def _read_gds(path: Path, cell_name: str | None) -> Any:
    """Read a GDS into its own KCLayout so names never clash with the live one."""
    import kfactory as kf

    if not path.is_file():
        raise click.ClickException(f"{path} does not exist")
    kcl = kf.KCLayout(f"inspect_{uuid.uuid4().hex}")
    kcl.read(str(path))
    if cell_name:
        if kcl.layout.cell(cell_name) is None:
            names = [c.name for c in kcl.layout.top_cells()]
            raise click.ClickException(f"no cell {cell_name!r} in {path} (top cells: {names})")
        return kcl[cell_name]
    tops = kcl.top_kcells()
    if not tops:
        raise click.ClickException(f"{path} contains no cells")
    if len(tops) > 1:
        names = [t.name for t in tops]
        click.echo(f"note: {path} has several top cells {names}; using {names[0]} (pick with --cell)", err=True)
    return tops[0]


def _load_gds(path: Path, cell_name: str | None) -> Target:
    kcell = _read_gds(path, cell_name)
    return Target(name=_short_name(kcell.name), kcell=kcell)


# --------------------------------------------------------------------------
# Overlay paths
# --------------------------------------------------------------------------


def _paths_in(obj: Any) -> list[Any] | None:
    """The path-like objects in obj (a path or a list of them), or None."""
    import kfactory as kf

    def is_path(o: Any) -> bool:
        return hasattr(o, "points") and not isinstance(o, kf.ProtoTKCell)

    if is_path(obj):
        return [obj]
    if isinstance(obj, (list, tuple)) and obj and all(is_path(o) for o in obj):
        return list(obj)
    return None


def _resolve_path_spec(spec: str, target: Target) -> list[Any]:
    import gdsfactory as gf
    import numpy as np

    if ";" in spec and re.fullmatch(r"[\s0-9eE.+\-,;]+", spec):
        try:
            points = [tuple(float(v) for v in pt.split(",")) for pt in spec.split(";") if pt.strip()]
        except ValueError as exc:
            raise click.ClickException(f"--path {spec!r}: bad point list ({exc})") from exc
        if len(points) < 2 or any(len(pt) != 2 for pt in points):
            raise click.ClickException(f"--path {spec!r}: needs at least two x,y points separated by ';'")
        return [gf.Path(points)]

    path_part, sep, attr = spec.rpartition(":")
    if sep and path_part.endswith(".py") and Path(path_part).is_file():
        obj = _import_attr(Path(path_part), attr)
        if callable(obj):
            obj = obj()
    else:
        try:
            obj = eval(spec, {"gf": gf, "np": np, "c": target.kcell})  # local developer tool
        except Exception as exc:
            raise click.ClickException(f"--path {spec!r} failed to evaluate: {exc!r}") from exc
    paths = _paths_in(obj)
    if paths is None:
        raise click.ClickException(f"--path {spec!r} did not produce a path (got {type(obj).__name__})")
    return paths


def path_info(obj: Any, label: str, spec: str, color: int) -> PathInfo:
    """Length, end headings and curvature measured from the path's points."""
    raw = [(float(x), float(y)) for x, y in obj.points]
    # Drop repeated points so every segment has a direction.
    points = [raw[0]] + [b for a, b in itertools.pairwise(raw) if b != a]
    if len(points) < 2:
        raise click.ClickException(f"{label} ({spec}) has fewer than two distinct points")
    segs = [(b[0] - a[0], b[1] - a[1]) for a, b in itertools.pairwise(points)]
    lens = [math.hypot(dx, dy) for dx, dy in segs]
    heads = [math.atan2(dy, dx) for dx, dy in segs]
    min_radius, corners = None, 0
    for i, (h0, h1) in enumerate(itertools.pairwise(heads)):
        turn = abs((h1 - h0 + math.pi) % (2 * math.pi) - math.pi)
        if turn > math.radians(SHARP_CORNER_DEG):
            corners += 1
        elif turn > 1e-9:
            radius = (lens[i] + lens[i + 1]) / 2 / turn
            min_radius = radius if min_radius is None else min(min_radius, radius)
    return PathInfo(
        label=label,
        spec=spec,
        color=color,
        points=points,
        length=sum(lens),
        # Prefer the path's own end angles (exact for gf.Path) over the first/last segment's.
        start=(*points[0], float(getattr(obj, "start_angle", math.degrees(heads[0]))) % 360),
        end=(*points[-1], float(getattr(obj, "end_angle", math.degrees(heads[-1]))) % 360),
        min_radius=min_radius,
        sharp_corners=corners,
    )


def load_paths(specs: Sequence[str], target: Target, source: str) -> list[PathInfo]:
    objs = [(source, o) for o in target.source_paths]
    for spec in specs:
        objs += [(spec, o) for o in _resolve_path_spec(spec, target)]
    return [
        path_info(o, f"path {i}", spec, PATH_COLORS[(i - 1) % len(PATH_COLORS)]) for i, (spec, o) in enumerate(objs, 1)
    ]


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------


def port_infos(kcell: Any) -> list[PortInfo]:
    ports = []
    for p in kcell.ports:
        t = p.dcplx_trans
        li = p.layer_info
        ports.append(
            PortInfo(
                name=str(p.name),
                x=round(t.disp.x, 6),
                y=round(t.disp.y, 6),
                width=round(p.dwidth, 6),
                orientation=round(t.angle % 360, 6),
                layer=(li.name or f"{li.layer}/{li.datatype}") if li is not None else "?",
                port_type=str(p.port_type),
            )
        )
    return ports


def layer_region(target: Target, index: int, merged: bool = True) -> Any:
    import klayout.db as kdb

    region = kdb.Region(target.cell.begin_shapes_rec(index))
    if merged:
        region.merge()
    return region


def present_layers(target: Target) -> list[LayerEntry]:
    layout = target.layout
    entries = []
    for index in layout.layer_indexes():
        if target.cell.bbox(index).empty():
            continue
        info = layout.get_info(index)
        entries.append(LayerEntry(index=index, name=info.name or "", layer=info.layer, datatype=info.datatype))
    entries.sort(key=lambda e: (e.layer, e.datatype))
    return entries


def select_layers(entries: list[LayerEntry], spec: str | None) -> list[LayerEntry]:
    if not spec:
        return entries
    wanted = [w.strip() for w in spec.split(",") if w.strip()]
    chosen = []
    for e in entries:
        keys = {e.name, f"{e.layer}/{e.datatype}", str(e.layer)}
        if keys & set(wanted):
            chosen.append(e)
    missing = [w for w in wanted if not any(w in {e.name, f"{e.layer}/{e.datatype}", str(e.layer)} for e in entries)]
    if missing:
        names = ", ".join(e.label for e in entries)
        raise click.ClickException(f"layers {missing} not present in the cell (present: {names})")
    return chosen


def to_dpolygons(region: Any, dbu: float, limit: int = MAX_MARKERS) -> list[Any]:
    out = []
    for i, poly in enumerate(region.each()):
        if i >= limit:
            break
        out.append(poly.to_dtype(dbu))
    return out


def dbox_tuple(box: Any) -> list[float]:
    return [round(box.left, 4), round(box.bottom, 4), round(box.right, 4), round(box.top, 4)]


# --------------------------------------------------------------------------
# Summary sections
# --------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonable(v) for v in value]
        return str(value)


def cell_section(target: Target) -> dict[str, Any]:
    kc = target.kcell
    bbox = kc.dbbox()
    data: dict[str, Any] = {
        "name": kc.name,
        "bbox": dbox_tuple(bbox),
        "size": [round(bbox.width(), 4), round(bbox.height(), 4)],
        "dbu": target.dbu,
    }
    settings = getattr(kc, "settings", None)
    if settings is not None:
        data["settings"] = _jsonable(settings.model_dump())
    info = getattr(kc, "info", None)
    if info is not None:
        data["info"] = _jsonable(info.model_dump())
    insts = []
    count = 0
    for inst in target.cell.each_inst():
        count += 1
        if len(insts) < MAX_LISTED:
            insts.append({"cell": inst.cell.name, "trans": str(inst.dcplx_trans), "array": inst.is_regular_array()})
    data["instances"] = {"count": count, "listed": insts}
    return data


def layers_section(target: Target, layers: list[LayerEntry]) -> list[dict[str, Any]]:
    dbu = target.dbu
    out = []
    for e in layers:
        raw = layer_region(target, e.index, merged=False)
        merged = raw.merged()
        out.append(
            {
                "layer": e.label,
                "fill": e.fill,
                "polygons": raw.count(),
                "merged_polygons": merged.count(),
                "area_um2": round(merged.area() * dbu * dbu, 6),
                "bbox": dbox_tuple(merged.bbox().to_dtype(dbu)),
            }
        )
    return out


def cut_section(target: Target, layers: list[LayerEntry], cut: tuple[float, float, float, float]) -> dict[str, Any]:
    """Intervals covered by each layer along the line from (x0,y0) to (x1,y1)."""
    import klayout.db as kdb

    x0, y0, x1, y1 = cut
    dbu = target.dbu
    length = math.hypot(x1 - x0, y1 - y0)
    if length == 0:
        raise click.ClickException("--cut needs two distinct points")
    line = kdb.Edges([kdb.DEdge(x0, y0, x1, y1).to_itype(dbu)])
    ux, uy = (x1 - x0) / length, (y1 - y0) / length

    def point(s: float) -> list[float]:
        return [round(x0 + ux * s, 4), round(y0 + uy * s, 4)]

    result: dict[str, Any] = {"line": [x0, y0, x1, y1], "length": round(length, 4), "layers": []}
    for e in layers:
        inside = line & layer_region(target, e.index)
        spans = []
        for edge in inside.merged().each():
            de = edge.to_dtype(dbu)
            s0 = (de.p1.x - x0) * ux + (de.p1.y - y0) * uy
            s1 = (de.p2.x - x0) * ux + (de.p2.y - y0) * uy
            spans.append((min(s0, s1), max(s0, s1)))
        spans.sort()
        if not spans:
            continue
        gaps = [round(b[0] - a[1], 4) for a, b in itertools.pairwise(spans)]
        result["layers"].append(
            {
                "layer": e.label,
                "spans": [
                    {"from": point(a), "to": point(b), "s": [round(a, 4), round(b, 4)], "length": round(b - a, 4)}
                    for a, b in spans
                ],
                "gaps": gaps,
            }
        )
    return result


def _clusters(pairs: Any, dbu: float) -> tuple[list[Any], dict[str, Any]]:
    """Merge touching edge-pair violations into clusters (one per location)."""
    import klayout.db as kdb

    clusters = pairs.polygons(2).merged()
    boxes = [poly.bbox() for poly in clusters.each()]
    worst = [math.inf] * len(boxes)
    for ep in pairs.each():
        bb = ep.bbox()
        for i, box in enumerate(boxes):
            if box.overlaps(bb) or box.contains(bb.center()):
                worst[i] = min(worst[i], ep.distance())
                break
    listed = [
        {"bbox": dbox_tuple(box.to_dtype(dbu)), "min_distance": round(d * dbu, 4) if d < math.inf else None}
        for box, d in list(zip(boxes, worst))[:MAX_LISTED]
    ]
    detail = {"edge_pairs": pairs.count(), "locations": len(boxes), "first": listed}
    polys = [kdb.DPolygon(box.to_dtype(dbu)) for box in boxes[:MAX_MARKERS]]
    return polys, detail


def checks_section(
    target: Target,
    layers: list[LayerEntry],
    ports: list[PortInfo],
    grid: float | None,
    min_width: float | None,
    min_space: float | None,
) -> tuple[dict[str, Any], MarkerSet]:
    import klayout.db as kdb

    dbu = target.dbu
    markers = MarkerSet("violation", COLOR_VIOLATION)
    issues: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}

    def add(kind: str, layer: str, detail: dict[str, Any], polys: Sequence[Any] = ()) -> None:
        issues.append({"check": kind, "layer": layer, **detail})
        markers.polygons.extend(polys)
        if polys:
            markers.focus.append((f"{kind} on {layer}", polys[0].bbox()))

    grid_dbu = round(grid / dbu) if grid else 1
    for e in layers:
        region = layer_region(target, e.index)
        edges = region.edges()
        total = edges.count()
        manhattan = edges.with_angle(0, False).count() + edges.with_angle(90, False).count()
        stats[e.label] = {"edges": total, "non_manhattan_edges": total - manhattan}

        strange = layer_region(target, e.index, merged=False).strange_polygon_check()
        if not strange.is_empty():
            add("self-intersecting polygon", e.label, {"count": strange.count()}, to_dpolygons(strange, dbu))

        if grid_dbu > 1:
            off = region.grid_check(grid_dbu, grid_dbu)
            if not off.is_empty():
                polys, detail = _clusters(off, dbu)
                add("off-grid vertex", e.label, {"grid": grid, **detail}, polys)

        if min_width:
            bad = region.width_check(round(min_width / dbu), ignore_angle=IGNORE_ANGLE)
            if not bad.is_empty():
                polys, detail = _clusters(bad, dbu)
                add("min width", e.label, {"rule": min_width, **detail}, polys)
        if min_space:
            bad = region.space_check(round(min_space / dbu), ignore_angle=IGNORE_ANGLE)
            if not bad.is_empty():
                polys, detail = _clusters(bad, dbu)
                add("min space", e.label, {"rule": min_space, **detail}, polys)

    port_grid = grid if grid else dbu
    by_name = {e.name: e for e in present_layers(target)}
    by_ld = {f"{e.layer}/{e.datatype}": e for e in present_layers(target)}
    for p in ports:
        for value, what in ((p.x, "x"), (p.y, "y")):
            if abs(value / port_grid - round(value / port_grid)) > 1e-6:
                add("port off-grid", p.layer, {"port": p.name, "coord": what, "value": value, "grid": port_grid})
        if abs(p.width / dbu / 2 - round(p.width / dbu / 2)) > 1e-6:
            add("port half-width off dbu", p.layer, {"port": p.name, "width": p.width})
        if p.orientation % 90:
            add("port non-manhattan", p.layer, {"port": p.name, "orientation": p.orientation})

        entry = by_name.get(p.layer) or by_ld.get(p.layer)
        dx, dy = p.direction()
        # A thin box just inside the port face must be fully covered by the port layer.
        depth = 2 * dbu
        cx, cy = p.x - dx * depth / 2, p.y - dy * depth / 2
        hw = p.width / 2 * 0.98
        half_w, half_h = (depth / 2, hw) if abs(dx) > abs(dy) else (hw, depth / 2)
        probe = kdb.DBox(cx - half_w, cy - half_h, cx + half_w, cy + half_h)
        if entry is None:
            add("port layer empty", p.layer, {"port": p.name}, [kdb.DPolygon(probe.enlarged(p.width, p.width))])
            continue
        probe_region = kdb.Region(probe.to_itype(dbu))
        uncovered = probe_region - layer_region(target, entry.index)
        if not uncovered.is_empty():
            frac = uncovered.area() / max(probe_region.area(), 1)
            add(
                "port not on geometry",
                p.layer,
                {"port": p.name, "uncovered_fraction": round(frac, 3)},
                [kdb.DPolygon(probe.enlarged(p.width / 2, p.width / 2))],
            )

    return {"issues": issues, "edge_stats": stats, "passed": not issues}, markers


def diff_section(target: Target, prev_path: Path, cell_name: str | None) -> tuple[dict[str, Any], list[MarkerSet], Any]:
    import klayout.db as kdb

    prev = _read_gds(prev_path, cell_name)
    if cell_name is None and prev.name != target.kcell.name:
        same = prev.kcl.layout.cell(target.kcell.name)
        if same is not None:
            prev = prev.kcl[target.kcell.name]
    dbu = target.dbu
    prev_layout = prev.kcl.layout
    prev_cell = prev.kdb_cell
    added = MarkerSet("added", COLOR_ADDED)
    removed = MarkerSet("removed", COLOR_REMOVED)
    changed_box = kdb.Box()
    layers_out = []

    infos: dict[tuple[int, int], Any] = {}
    for layout, cell in ((target.layout, target.cell), (prev_layout, prev_cell)):
        for index in layout.layer_indexes():
            if not cell.bbox(index).empty():
                info = layout.get_info(index)
                infos.setdefault((info.layer, info.datatype), info)

    for key in sorted(infos):
        info = infos[key]
        cur_idx = target.layout.find_layer(info.layer, info.datatype)
        old_idx = prev_layout.find_layer(info.layer, info.datatype)
        cur = kdb.Region(target.cell.begin_shapes_rec(cur_idx)) if cur_idx is not None else kdb.Region()
        old = kdb.Region(prev_cell.begin_shapes_rec(old_idx)) if old_idx is not None else kdb.Region()
        if abs(prev_layout.dbu - dbu) > 1e-12:
            old = old.transformed(kdb.ICplxTrans(prev_layout.dbu / dbu))
        cur.merge()
        old.merge()
        plus, minus = cur - old, old - cur
        if plus.is_empty() and minus.is_empty():
            continue
        changed_box += plus.bbox() + minus.bbox()
        added.polygons.extend(to_dpolygons(plus, dbu))
        removed.polygons.extend(to_dpolygons(minus, dbu))
        label = f"{info.name} ({info.layer}/{info.datatype})" if info.name else f"{info.layer}/{info.datatype}"
        layers_out.append(
            {
                "layer": label,
                "added_um2": round(plus.area() * dbu * dbu, 6),
                "removed_um2": round(minus.area() * dbu * dbu, 6),
                "bbox": dbox_tuple((plus.bbox() + minus.bbox()).to_dtype(dbu)),
            }
        )

    cur_ports = {p.name: p for p in port_infos(target.kcell)}
    old_ports = {p.name: p for p in port_infos(prev)}
    port_changes = []
    for name in sorted(set(cur_ports) | set(old_ports)):
        a, b = old_ports.get(name), cur_ports.get(name)
        if a is None or b is None:
            port_changes.append({"port": name, "change": "added" if a is None else "removed"})
            continue
        diffs = {
            k: [getattr(a, k), getattr(b, k)]
            for k in ("x", "y", "width", "orientation", "layer", "port_type")
            if getattr(a, k) != getattr(b, k)
        }
        if diffs:
            port_changes.append({"port": name, "change": "modified", **diffs})

    old_bbox = prev.dbbox()
    data = {
        "previous": str(prev_path),
        "previous_cell": prev.name,
        "identical": not layers_out and not port_changes,
        "layers": layers_out,
        "ports": port_changes,
        "bbox_before": dbox_tuple(old_bbox),
        "bbox_after": dbox_tuple(target.kcell.dbbox()),
        "changed_bbox": dbox_tuple(changed_box.to_dtype(dbu)) if not changed_box.empty() else None,
    }
    return data, [added, removed], changed_box.to_dtype(dbu) if not changed_box.empty() else None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _hex(color: int) -> str:
    return f"#{color & 0xFFFFFF:06x}"


class Renderer:
    """Owns a KLayout LayoutView showing the target cell."""

    def __init__(
        self,
        target: Target,
        layers: list[LayerEntry],
        visible: list[LayerEntry],
        depth: int | None,
        fill: str,
        show_ports: bool,
        size: int,
    ) -> None:
        from gdsfactory.pdk import get_layer_views
        from klayout import lay

        self.target = target
        self.layers = layers
        self.show_ports = show_ports
        self.size = size
        self.view = lay.LayoutView()
        index = self.view.create_layout(True)
        self.view.active_cellview_index = index
        cellview = self.view.cellview(index)
        # Share the live layout instead of copying it, as Component.plot does.
        layout = cellview.layout()
        layout.assign(target.layout)
        cellview.cell = layout.cell(target.cell.name)

        with tempfile.TemporaryDirectory() as tmp:
            lyp = Path(tmp) / "layers.lyp"
            try:
                get_layer_views().to_lyp(filepath=lyp)
                self.view.load_layer_props(str(lyp))
            except Exception as exc:  # no layer views in the PDK: KLayout defaults
                click.echo(f"note: could not load PDK layer views ({exc}); using KLayout defaults", err=True)
        self.view.add_missing_layers()

        for key, value in {
            "background-color": "#ffffff",
            "grid-visible": "true",
            "grid-show-ruler": "false",
            "grid-color": "#d0d0d0",
            "text-visible": "true",
            "text-color": "#000000",
            "ruler-color": "#0040ff",
            "ruler-halo": "true",
            "inst-visible": "true",
            "inst-color": "#7f7f7f",
            "markers-visible": "true",
        }.items():
            self.view.set_config(key, value)

        if depth is None:
            self.view.max_hier()
        else:
            self.view.min_hier_levels = 0
            self.view.max_hier_levels = depth + 1  # level 1 = the cell's own shapes

        visible_keys = {(e.layer, e.datatype) for e in visible}
        by_key = {(e.layer, e.datatype): e for e in layers}
        it = self.view.begin_layers()
        while not it.at_end():
            node = it.current()
            key = (node.source_layer, node.source_datatype)
            if key in by_key:
                entry = by_key[key]
                entry.fill = _hex(node.eff_fill_color(True))
                entry.frame = _hex(node.eff_frame_color(True))
                if not entry.name and node.name:
                    entry.name = node.name
            if not node.has_children():
                new = node.dup()
                new.visible = key in visible_keys
                if fill == "solid":
                    new.dither_pattern = 0
                elif fill == "outline":
                    new.dither_pattern = 1
                new.width = 1
                self.view.set_layer_properties(it, new)
            it.next()
        self.visible = [by_key[k] for k in sorted(visible_keys) if k in by_key]
        self.marker_sets: list[MarkerSet] = []
        self.paths: list[PathInfo] = []  # drawn on every view, whatever marker_sets holds
        self._live: list[Any] = []

    # -- scene objects ------------------------------------------------------

    def _annotations(self, ports: list[PortInfo], um_per_px: float, box: Any) -> None:
        import klayout.db as kdb
        from klayout import lay

        self.view.clear_annotations()
        for pi in self.paths:
            # Cross at the start, labelled arrow continuing the path past its end.
            for (x, y, heading), style, length, text in (
                (pi.start, lay.Annotation.StyleCrossStart, 12, ""),
                (pi.end, lay.Annotation.StyleArrowEnd, 30, pi.label),
            ):
                a = math.radians(heading)
                ann = lay.Annotation()
                ann.p1 = kdb.DPoint(x, y)
                ann.p2 = kdb.DPoint(x + math.cos(a) * length * um_per_px, y + math.sin(a) * length * um_per_px)
                ann.style = style
                ann.outline = lay.Annotation.OutlineDiag
                ann.fmt = text
                ann.main_position = lay.Annotation.PositionP2
                self.view.insert_annotation(ann)
        if not self.show_ports:
            return
        arrow = 45 * um_per_px
        for p in ports:
            if not box.contains(kdb.DPoint(p.x, p.y)):
                continue
            dx, dy = p.direction()
            a = lay.Annotation()
            a.p1 = kdb.DPoint(p.x, p.y)
            a.p2 = kdb.DPoint(p.x + dx * arrow, p.y + dy * arrow)
            a.style = lay.Annotation.StyleArrowEnd
            a.outline = lay.Annotation.OutlineDiag
            a.fmt = p.name
            a.main_position = lay.Annotation.PositionP2
            self.view.insert_annotation(a)
            # Port face, so the width is visible against the geometry.
            hx, hy = -dy * p.width / 2, dx * p.width / 2
            face = lay.Annotation()
            face.p1 = kdb.DPoint(p.x - hx, p.y - hy)
            face.p2 = kdb.DPoint(p.x + hx, p.y + hy)
            face.style = lay.Annotation.StyleLine
            face.outline = lay.Annotation.OutlineDiag
            face.fmt = ""
            self.view.insert_annotation(face)

    def _markers(self) -> None:
        import klayout.db as kdb
        from klayout import lay

        for m in self._live:
            m._destroy()
        self._live = []
        for ms in self.marker_sets:
            for poly in ms.polygons:
                marker = lay.Marker(self.view)
                marker.set_polygon(poly)
                marker.color = ms.color
                marker.frame_color = ms.color
                marker.line_width = 2
                marker.dither_pattern = ms.dither
                marker.halo = 0
                self._live.append(marker)
        for pi in self.paths:
            marker = lay.Marker(self.view)
            # Width 0 draws a line, so the stroke stays a constant pixel width at any zoom.
            marker.set_path(kdb.DPath([kdb.DPoint(x, y) for x, y in pi.points], 0))
            marker.color = pi.color
            marker.frame_color = pi.color
            marker.line_width = 2
            marker.vertex_size = 0
            marker.halo = 0
            self._live.append(marker)

    # -- output -------------------------------------------------------------

    def render(self, path: Path, box: Any, title: str, ports: list[PortInfo]) -> dict[str, Any]:
        import klayout.db as kdb

        box = kdb.DBox(box)
        if box.width() <= 0 or box.height() <= 0:
            box = box.enlarged(max(box.width(), box.height(), 1.0) / 2 or 0.5)
        # Pick the image size from the box aspect so no pixels are wasted, then
        # widen the box to the exact pixel grid so the axes are exact.
        aspect = box.width() / box.height()
        if aspect >= 1:
            w, h = self.size, max(160, round(self.size / aspect))
        else:
            w, h = max(160, round(self.size * aspect)), self.size
        um_per_px = max(box.width() / w, box.height() / h)
        c = box.center()
        box = kdb.DBox(
            c.x - w * um_per_px / 2, c.y - h * um_per_px / 2, c.x + w * um_per_px / 2, c.y + h * um_per_px / 2
        )

        self._annotations(ports, um_per_px, box)
        self._markers()
        self.view.zoom_box(box)
        raw = path.with_suffix(".raw.png")
        self.view.save_image_with_options(str(raw), w, h, 0, 1, 0, box, False)  # oversampling greys out stipples
        _decorate(raw, path, box, title, self.visible, self.marker_sets, self.paths)
        raw.unlink(missing_ok=True)
        return {"path": str(path), "box": dbox_tuple(box), "um_per_px": round(um_per_px, 6)}


def _decorate(
    raw: Path,
    out: Path,
    box: Any,
    title: str,
    legend: list[LayerEntry],
    markers: list[MarkerSet],
    paths: list[PathInfo],
) -> None:
    """Frame the KLayout image with µm axes, a title and a legend, keeping its pixels 1:1."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    from matplotlib.ticker import MaxNLocator

    img = plt.imread(raw)
    h, w = img.shape[:2]
    dpi = 100
    left, right, top, bottom, row_h = 72, 16, 28, 40, 20  # px

    handles = [Patch(facecolor=e.fill, edgecolor=e.frame, label=e.label) for e in legend]
    handles += [Patch(facecolor="none", edgecolor=_hex(m.color), label=m.label) for m in markers if m.polygons]
    handles += [Line2D([], [], color=_hex(p.color), lw=2, label=p.label) for p in paths]
    full_w = left + w + right
    ncol, legend_h = 1, 0
    if handles:
        # Rough DejaVu Sans width at 9 pt / 100 dpi; only used to choose a column count.
        item_w = max(34 + 7 * len(str(hd.get_label())) for hd in handles)
        ncol = max(1, min(len(handles), (full_w - 16) // item_w))
        legend_h = 6 + row_h * math.ceil(len(handles) / ncol)
    full_h = top + h + bottom + legend_h

    # Pin the font: the active style may pick one (e.g. Inter) whose contextual
    # alternates render "1x2" in cell names as "1×2".
    with plt.rc_context({"font.family": "DejaVu Sans"}):
        fig = plt.figure(figsize=(full_w / dpi, full_h / dpi), dpi=dpi, facecolor="white")
        # Axes exactly w x h px, so the image is drawn without resampling.
        ax = fig.add_axes((left / full_w, (bottom + legend_h) / full_h, w / full_w, h / full_h))
        ax.imshow(img, extent=(box.left, box.right, box.bottom, box.top), interpolation="none", aspect="auto")
        ax.set_xlim(box.left, box.right)
        ax.set_ylim(box.bottom, box.top)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=max(3, w // 160)))
        ax.yaxis.set_major_locator(MaxNLocator(nbins=max(3, h // 120)))
        ax.tick_params(labelsize=8, length=4)
        ax.set_title(title, loc="left", fontsize=10, pad=6)
        ax.set_xlabel("x (µm)", fontsize=8, labelpad=2)
        ax.set_ylabel("y (µm)", fontsize=8, labelpad=2)
        if handles:
            fig.legend(
                handles=handles,
                loc="lower left",
                bbox_to_anchor=(8 / full_w, 2 / full_h),
                ncol=ncol,
                frameon=False,
                fontsize=9,
                handlelength=1.2,
                handleheight=1.0,
                columnspacing=1.2,
                borderaxespad=0,
            )
        fig.savefig(out, dpi=dpi)  # no bbox_inches="tight": it would rescale the image
        plt.close(fig)


# --------------------------------------------------------------------------
# Text output
# --------------------------------------------------------------------------


def print_summary(s: dict[str, Any]) -> None:
    echo = click.echo
    cell = s["cell"]
    echo(f"Cell      : {cell['name']}")
    echo(f"BBox      : {cell['bbox']}  size {cell['size'][0]} x {cell['size'][1]} µm  (dbu {cell['dbu']})")
    if cell.get("settings"):
        echo(f"Settings  : {json.dumps(cell['settings'])}")
    if cell.get("info"):
        echo(f"Info      : {json.dumps(cell['info'])}")
    inst = cell["instances"]
    if inst["count"]:
        echo(f"Instances : {inst['count']}")
        for i in inst["listed"]:
            echo(f"  {i['cell']}  {i['trans']}{'  (array)' if i['array'] else ''}")

    echo("\nPorts:")
    if not s["ports"]:
        echo("  (none)")
    else:
        echo(f"  {'name':<10} {'x':>10} {'y':>10} {'width':>7} {'angle':>6}  {'layer':<12} type")
        for p in s["ports"]:
            echo(
                f"  {p['name']:<10} {p['x']:>10.4f} {p['y']:>10.4f} {p['width']:>7.4f} {p['orientation']:>6g}"
                f"  {p['layer']:<12} {p['port_type']}"
            )

    echo("\nLayers:")
    for la in s["layers"]:
        echo(
            f"  {la['layer']:<24} colour {la['fill']}  polygons {la['polygons']:<5}"
            f" area {la['area_um2']:.4f} µm²  bbox {la['bbox']}"
        )

    for cut in s.get("cuts", []):
        x0, y0, x1, y1 = cut["line"]
        echo(f"\nCut ({x0}, {y0}) -> ({x1}, {y1}), length {cut['length']} µm  [s = distance from start]")
        if not cut["layers"]:
            echo("  (no geometry crossed)")
        for la in cut["layers"]:
            spans = ", ".join(f"[{sp['s'][0]}, {sp['s'][1]}] ({sp['length']})" for sp in la["spans"])
            echo(f"  {la['layer']:<24} {spans}")
            if la["gaps"]:
                echo(f"  {'':<24} gaps: {la['gaps']}")

    if s.get("paths"):
        echo("\nPaths:")
        for p in s["paths"]:
            radius = f"min radius {p['min_radius']} µm" if p["min_radius"] is not None else "straight segments only"
            corners = f"  sharp corners: {p['sharp_corners']}" if p["sharp_corners"] else ""
            echo(
                f"  {p['label']:<7} length {p['length']} µm  start ({p['start'][0]}, {p['start'][1]}) {p['start'][2]:g}°"
                f"  end ({p['end'][0]}, {p['end'][1]}) {p['end'][2]:g}°  {radius}{corners}"
            )
            echo(f"  {'':<7} {p['spec']}")

    if "checks" in s:
        ch = s["checks"]
        echo(f"\nChecks    : {'PASS' if ch['passed'] else str(len(ch['issues'])) + ' issue(s)'}")
        for issue in ch["issues"]:
            rest = {k: v for k, v in issue.items() if k not in ("check", "layer")}
            echo(f"  [{issue['check']}] {issue['layer']}: {json.dumps(rest)}")
        for layer, st in ch["edge_stats"].items():
            if st["non_manhattan_edges"]:
                echo(f"  note: {layer} has {st['non_manhattan_edges']}/{st['edges']} non-Manhattan edges")

    if "diff" in s:
        d = s["diff"]
        echo(f"\nDiff vs {d['previous']} (cell {d['previous_cell']}):")
        if d["identical"]:
            echo("  identical geometry and ports")
        for la in d["layers"]:
            echo(f"  {la['layer']:<24} +{la['added_um2']:.4f} / -{la['removed_um2']:.4f} µm²  in {la['bbox']}")
        for p in d["ports"]:
            echo(f"  port {json.dumps(p)}")
        if d["bbox_before"] != d["bbox_after"]:
            echo(f"  bbox {d['bbox_before']} -> {d['bbox_after']}")

    echo("\nImages:")
    for img in s["images"]:
        echo(f"  {img['path']}  box {img['box']}  ({img['um_per_px']} µm/px)")
    echo(f"GDS       : {s['gds']}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def _port_box(p: PortInfo, radius: float | None) -> Any:
    import klayout.db as kdb

    r = radius if radius else max(4 * p.width, 2.0)
    return kdb.DBox(p.x - r, p.y - r, p.x + r, p.y + r)


@click.command(context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100})
@click.argument("source")
@click.option("--kwargs", "kwargs", type=JSON_DICT, default="{}", help="JSON kwargs for a file.py:func source.")
@click.option("--cell", "cell_name", help="Cell to use from a GDS source (default: top cell).")
@click.option("--pdk", help="PDK to activate, as module.attr (default: gdsfactory generic PDK).")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Output directory (default: build/inspect/<name>).",
)
@click.option("--zoom", "zooms", type=BOX, multiple=True, help="Extra view x0,y0,x1,y1 in µm (repeatable).")
@click.option("--zoom-port", "zoom_ports", multiple=True, help="Extra view centred on this port (repeatable).")
@click.option("--zoom-ports", "zoom_all_ports", is_flag=True, help="One extra view per port.")
@click.option("--radius", type=float, help="Half-size in µm of --zoom-port views (default: max(4*width, 2)).")
@click.option("--cut", "cuts", type=BOX, multiple=True, help="Cross-section line x0,y0,x1,y1 in µm (repeatable).")
@click.option(
    "--path",
    "path_specs",
    multiple=True,
    help="Path to overlay (repeatable): expression with gf/np/c in scope, file.py:func, or 'x0,y0;x1,y1;...'.",
)
@click.option("--layers", "layer_spec", help="Comma-separated layers to show/analyse, by name or L/D.")
@click.option(
    "--depth", type=click.IntRange(min=0), help="Child levels drawn; deeper cells as frames (0 = own shapes only)."
)
@click.option(
    "--fill",
    type=click.Choice(["pdk", "solid", "outline"]),
    default="pdk",
    show_default=True,
    help="Fill style: PDK stipples, solid, or outlines only.",
)
@click.option("--no-ports", is_flag=True, help="Do not draw port arrows.")
@click.option(
    "--size", type=click.IntRange(min=200, max=4000), default=1400, show_default=True, help="Long edge in px."
)
@click.option("--check", "run_checks", is_flag=True, help="Run geometry checks and mark violations.")
@click.option("--min-width", type=float, help="Min width rule in µm (implies --check).")
@click.option("--min-space", type=float, help="Min space rule in µm (implies --check).")
@click.option("--grid", type=float, help="Manufacturing grid in µm for off-grid checks (implies --check).")
@click.option(
    "--diff",
    "diff_spec",
    help="Previous GDS to diff against, or 'last' for the previous run in --out.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the summary as JSON instead of text.")
def main(
    source: str,
    kwargs: dict[str, Any],
    cell_name: str | None,
    pdk: str | None,
    out_dir: Path | None,
    zooms: tuple[tuple[float, float, float, float], ...],
    zoom_ports: tuple[str, ...],
    zoom_all_ports: bool,
    radius: float | None,
    cuts: tuple[tuple[float, float, float, float], ...],
    path_specs: tuple[str, ...],
    layer_spec: str | None,
    depth: int | None,
    fill: str,
    no_ports: bool,
    size: int,
    run_checks: bool,
    min_width: float | None,
    min_space: float | None,
    grid: float | None,
    diff_spec: str | None,
    as_json: bool,
) -> None:
    """Render and analyse SOURCE (file.py:func, file.gds, or a gf expression)."""
    activate_pdk(pdk)
    target = load_target(source, kwargs, cell_name)
    out = out_dir or Path("build") / "inspect" / target.name
    out.mkdir(parents=True, exist_ok=True)

    gds = out / "layout.gds"
    prev_gds = out / "layout.prev.gds"
    diff_path: Path | None = None
    if diff_spec:
        diff_path = prev_gds if diff_spec == "last" else Path(diff_spec)
        if diff_spec == "last" and not gds.exists() and not prev_gds.exists():
            raise click.ClickException(f"--diff last: no previous run in {out}")
    # Rotate the previous result so 'last' always means the run before this one.
    if gds.exists():
        shutil.copyfile(gds, prev_gds)
    for stale in out.glob("*.png"):
        stale.unlink()
    target.kcell.write(str(gds))

    layers = present_layers(target)
    selected = select_layers(layers, layer_spec)
    ports = port_infos(target.kcell)
    port_names = {p.name for p in ports}
    unknown = [n for n in zoom_ports if n not in port_names]
    if unknown:
        raise click.ClickException(f"unknown port(s) {unknown}; ports are {sorted(port_names)}")

    paths = load_paths(path_specs, target, source)
    renderer = Renderer(target, layers, selected, depth, fill, not no_ports, size)
    renderer.paths = paths
    summary: dict[str, Any] = {"cell": cell_section(target), "ports": [p.__dict__ for p in ports]}
    if paths:
        summary["paths"] = [p.summary() for p in paths]

    if run_checks or min_width or min_space or grid:
        checks, markers = checks_section(target, selected, ports, grid, min_width, min_space)
        summary["checks"] = checks
        renderer.marker_sets.append(markers)

    diff_box = None
    diff_markers: list[MarkerSet] = []
    if diff_path is not None:
        summary["diff"], diff_markers, diff_box = diff_section(target, diff_path, cell_name)

    summary["layers"] = layers_section(target, selected)
    if cuts:
        summary["cuts"] = [cut_section(target, selected, c) for c in cuts]

    import klayout.db as kdb

    images = []
    bbox = target.kcell.dbbox()
    for p in ports:  # ports and overlay paths may sit off the geometry; keep them in view
        bbox += kdb.DPoint(p.x, p.y)
    for pi in paths:
        for x, y in pi.points:
            bbox += kdb.DPoint(x, y)
    if bbox.empty():
        bbox = kdb.DBox(-1, -1, 1, 1)
    margin = max(bbox.width(), bbox.height()) * 0.06 + 0.5
    images.append(renderer.render(out / "overview.png", bbox.enlarged(margin, margin), f"{target.kcell.name}", ports))

    for i, z in enumerate(zooms, 1):
        box = kdb.DBox(min(z[0], z[2]), min(z[1], z[3]), max(z[0], z[2]), max(z[1], z[3]))
        images.append(renderer.render(out / f"zoom_{i}.png", box, f"zoom {list(z)}", ports))
    wanted_ports = [p for p in ports if zoom_all_ports or p.name in zoom_ports]
    for p in wanted_ports:
        title = f"port {p.name} @ ({p.x}, {p.y}) w={p.width} {p.orientation:g}°"
        images.append(renderer.render(out / f"zoom_port_{_short_name(p.name)}.png", _port_box(p, radius), title, ports))
    for i, c in enumerate(cuts, 1):
        # Show the cut line itself on the overview-style view of its neighbourhood.
        x0, y0, x1, y1 = c
        line = kdb.DPath([kdb.DPoint(x0, y0), kdb.DPoint(x1, y1)], max(target.dbu * 20, 0.002)).polygon()
        renderer.marker_sets.append(MarkerSet(f"cut {i}", 0x0040FF, [line], dither=0))
        half = math.hypot(x1 - x0, y1 - y0) * 0.65 + 0.2
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        box = kdb.DBox(cx - half, cy - half, cx + half, cy + half)
        images.append(renderer.render(out / f"cut_{i}.png", box, f"cut {list(c)}", ports))
        renderer.marker_sets.pop()

    if "checks" in summary:
        for i, (title, vb) in enumerate(renderer.marker_sets[0].focus[:MAX_CHECK_VIEWS], 1):
            pad = max(vb.width(), vb.height()) * 0.15 + 1.0
            images.append(renderer.render(out / f"check_{i}.png", vb.enlarged(pad, pad), title, ports))

    if diff_path is not None:
        saved = renderer.marker_sets
        renderer.marker_sets = diff_markers
        if diff_box is not None:
            pad = max(diff_box.width(), diff_box.height()) * 0.15 + 0.5
            images.append(
                renderer.render(out / "diff.png", diff_box.enlarged(pad, pad), "diff: added / removed", ports)
            )
        renderer.marker_sets = saved

    summary["images"] = images
    summary["gds"] = str(gds)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    if as_json:
        click.echo(json.dumps(summary, indent=2))
    else:
        print_summary(summary)


if __name__ == "__main__":
    main()
