"""Overlay paths: resolving specs and measuring centrelines."""

from __future__ import annotations

import itertools
import math
import re
from pathlib import Path
from typing import Any

import click

from .model import PATH_COLORS, SHARP_CORNER_DEG, PathInfo, Target

def paths_in(obj: Any) -> list[Any] | None:
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
            raise click.ClickException(f"path {spec!r}: bad point list ({exc})") from exc
        if len(points) < 2 or any(len(pt) != 2 for pt in points):
            raise click.ClickException(f"path {spec!r}: needs at least two x,y points separated by ';'")
        return [gf.Path(points)]

    path_part, sep, attr = spec.rpartition(":")
    if sep and path_part.endswith(".py"):
        if not Path(path_part).is_file():
            raise click.ClickException(f"path {spec!r}: {path_part} does not exist")
        from .loading import import_attr

        obj = import_attr(Path(path_part), attr)
        if callable(obj):
            obj = obj()
    else:
        try:
            obj = eval(spec, {"gf": gf, "np": np, "c": target.kcell})  # local developer tool
        except Exception as exc:
            raise click.ClickException(f"path {spec!r} failed to evaluate: {exc!r}") from exc
    paths = paths_in(obj)
    if paths is None:
        raise click.ClickException(f"path {spec!r} did not produce a path (got {type(obj).__name__})")
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


_LABELLED = re.compile(r"\s*([A-Za-z_][\w.\-]*)\s*=(?!=)(.*)", re.S)


def split_label(spec: str) -> tuple[str | None, str]:
    """``LABEL=SPEC`` -> (LABEL, SPEC); a spec without a label -> (None, spec)."""
    m = _LABELLED.fullmatch(spec)
    return (m.group(1), m.group(2).strip()) if m else (None, spec)


def normalize_path_entries(paths: Any) -> list[tuple[str | None, Any]]:
    """(label, item) pairs from what a caller passed as `paths`.

    `paths` is a path, a spec string (``[LABEL=]SPEC``), a ``{label: path or spec}`` dict, a
    ``{"label": ..., "spec": ...}`` dict, or a list of any of these.
    """
    if paths is None:
        return []
    if isinstance(paths, str):
        return [split_label(paths)]
    if isinstance(paths, dict):
        if "spec" in paths and set(paths) <= {"label", "spec"}:
            return [(paths.get("label"), paths["spec"])]
        return [(str(k), v) for k, v in paths.items()]
    if paths_in(paths) is not None and not isinstance(paths, (list, tuple)):
        return [(None, paths)]
    if isinstance(paths, (list, tuple)):
        if paths_in(paths) is not None:  # a list of path objects
            return [(None, o) for o in paths]
        return [entry for item in paths for entry in normalize_path_entries(item)]
    return [(None, paths)]


def resolve_paths(
    entries: list[tuple[str | None, Any]], target: Target, *, start: int = 0, taken: set[str] | None = None
) -> list[PathInfo]:
    """Measure each (label, spec-or-path) entry; colours and default labels continue from `start`.

    A spec string is resolved with ``c`` (the target) in scope; a path object is used as is.
    """
    objs: list[tuple[str | None, str, Any]] = []
    for label, item in entries:
        if isinstance(item, str):
            spec, items = item, _resolve_path_spec(item, target)
        else:
            found = paths_in(item)
            if found is None:
                raise click.ClickException(f"paths entry {label or item!r} is not a path (got {type(item).__name__})")
            spec, items = f"<{type(found[0]).__name__} object>", found
        objs += [(label if len(items) == 1 or label is None else f"{label}[{i}]", spec, o) for i, o in enumerate(items)]
    infos = []
    seen = set(taken or ())
    for i, (label, spec, o) in enumerate(objs, start + 1):
        name = label or f"path {i}"
        if name in seen:
            raise click.ClickException(f"path label {name!r} is used twice; labels must be unique")
        seen.add(name)
        infos.append(path_info(o, name, spec, PATH_COLORS[(i - 1) % len(PATH_COLORS)]))
    return infos
