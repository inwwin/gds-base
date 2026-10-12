"""Summary sections: cell, layers, cuts, checks and diffs."""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click

from .loading import read_gds
from .model import COLOR_ADDED, COLOR_REMOVED, COLOR_VIOLATION, IGNORE_ANGLE, MAX_LISTED, MAX_MARKERS
from .model import LayerEntry, MarkerSet, PortInfo, Target

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


def dbox_tuple(box: Any) -> list[float] | None:
    """[left, bottom, right, top] in µm, or None for an empty box (KLayout's is (1,1;-1,-1))."""
    if box.empty():
        return None
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
        "size": [0.0, 0.0] if bbox.empty() else [round(bbox.width(), 4), round(bbox.height(), 4)],
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
    if grid and (grid_dbu < 1 or abs(grid / dbu - grid_dbu) > 1e-6):
        raise click.ClickException(f"grid {grid} µm must be a whole multiple of the database unit {dbu} µm")
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
        # A thin strip just inside the port face, turned to the port's angle, must be
        # fully covered by the port layer. An axis-aligned box would stick out of any
        # port that is not at a multiple of 90 degrees. A rotated face is ragged by about a
        # dbu from grid snapping, so its strip is deeper and slivers under 2 dbu are ignored.
        rotated = bool(p.orientation % 90)
        depth = (6 if rotated else 2) * dbu
        hw = p.width / 2 * 0.98
        to_port = kdb.DCplxTrans(1, p.orientation, False, p.x, p.y)
        probe = to_port * kdb.DPolygon(kdb.DBox(-depth, -hw, 0, hw))
        marker = to_port * kdb.DPolygon(kdb.DBox(-p.width, -p.width, p.width / 2, p.width))
        if entry is None:
            add("port layer empty", p.layer, {"port": p.name}, [marker])
            continue
        probe_region = kdb.Region(probe.to_itype(dbu))
        uncovered = probe_region - layer_region(target, entry.index)
        if rotated and uncovered.sized(-1).is_empty():
            uncovered = kdb.Region()
        if not uncovered.is_empty():
            frac = uncovered.area() / max(probe_region.area(), 1)
            add(
                "port not on geometry",
                p.layer,
                {"port": p.name, "uncovered_fraction": round(frac, 3)},
                [marker],
            )

    return {"issues": issues, "edge_stats": stats, "passed": not issues}, markers


def diff_section(target: Target, prev_path: Path, cell_name: str | None) -> tuple[dict[str, Any], list[MarkerSet], Any]:
    import klayout.db as kdb

    prev = read_gds(prev_path, cell_name)
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
        "changed_bbox": dbox_tuple(changed_box.to_dtype(dbu)),
    }
    return data, [added, removed], changed_box.to_dtype(dbu) if not changed_box.empty() else None

