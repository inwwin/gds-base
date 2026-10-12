"""InspectSession: a loaded cell that answers render / cut / check / diff requests."""

from __future__ import annotations

import json
import math
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from .analysis import (
    cell_section,
    checks_section,
    cut_section,
    diff_section,
    layers_section,
    port_infos,
    present_layers,
    select_layers,
)
from .loading import activate_pdk, as_target, load_target, short_name
from .model import MAX_CHECK_VIEWS, MarkerSet, PathInfo, PortInfo
from .paths import normalize_path_entries, resolve_paths
from .render import Renderer
from .report import format_summary

Progress = Callable[[str], None]
Box = tuple[float, float, float, float]


@dataclass
class Result:
    """What a request produced: summary sections (always including `images`) and their text."""

    data: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return format_summary(self.data)

    @property
    def images(self) -> list[str]:
        return [img["path"] for img in self.data.get("images", [])]


def _box(value: Any, what: str) -> Box:
    """x0,y0,x1,y1 from a 4-sequence or a ``"x0,y0,x1,y1"`` string."""
    try:
        parts = [float(v) for v in (value.split(",") if isinstance(value, str) else value)]
    except (TypeError, ValueError) as exc:
        raise click.ClickException(f"{what} {value!r} is not four numbers x0,y0,x1,y1") from exc
    if len(parts) != 4:
        raise click.ClickException(f"{what} {value!r} needs exactly four numbers x0,y0,x1,y1")
    return parts[0], parts[1], parts[2], parts[3]


def _layer_spec(layers: str | Sequence[str] | None) -> str | None:
    if layers is None or isinstance(layers, str):
        return layers
    return ",".join(layers)


class InspectSession:
    """Loads SOURCE once, then renders, cuts, checks and diffs it on request.

    SOURCE is a CLI source string (``file.py:func``, a script or ``-``, a GDS file, a gf
    expression) or a Component / path object. `paths` are overlays kept for every render
    (a path, a ``[LABEL=]SPEC`` string, a ``{label: path or spec}`` dict, or a list of these).
    Creating a session writes ``layout.gds`` into `out_dir`, keeping the previous one as
    ``layout.prev.gds`` for ``diff("last")``. Errors raise click.ClickException.
    """

    def __init__(
        self,
        source: Any,
        *,
        kwargs: dict[str, Any] | None = None,
        cell: str | None = None,
        pdk: str | None = None,
        paths: Any = None,
        out_dir: Path | str | None = None,
        name: str | None = None,
    ) -> None:
        import gdsfactory as gf

        if pdk:
            activate_pdk(pdk)
        else:
            try:
                gf.get_active_pdk()
            except ValueError:
                activate_pdk(None)

        if isinstance(source, str):
            self.target = load_target(source, kwargs or {}, cell)
            self.source = source
        else:
            if kwargs:
                raise click.ClickException("kwargs only apply to a source string")
            self.target = as_target(source, name)
            self.source = f"<{type(source).__name__} object>"
        self.cell_name = cell
        self.name = short_name(name or self.target.name)
        self.out = Path(out_dir) if out_dir is not None else Path("build") / "component-inspect" / self.name
        self.out.mkdir(parents=True, exist_ok=True)

        self.gds = self.out / "layout.gds"
        self.prev_gds = self.out / "layout.prev.gds"
        # Rotate the previous result so 'last' always means the load before this one.
        if self.gds.exists():
            shutil.copyfile(self.gds, self.prev_gds)
        for stale in self.out.glob("*.png"):
            stale.unlink()
        self.target.kcell.write(str(self.gds))

        self.layers = present_layers(self.target)
        self.ports: list[PortInfo] = port_infos(self.target.kcell)
        self.renderer = Renderer(self.target, self.layers)

        source_paths = resolve_paths(self.target.source_paths, self.target)
        for pi in source_paths:
            pi.spec = self.target.source_label or self.source
        extra = resolve_paths(
            normalize_path_entries(paths),
            self.target,
            start=len(source_paths),
            taken={p.label for p in source_paths},
        )
        self.paths: list[PathInfo] = source_paths + extra
        self.violations: MarkerSet | None = None
        self._seq = 0
        (self.out / "summary.json").write_text(json.dumps(self.summary().data, indent=2))

    # -- helpers ------------------------------------------------------------

    def _image(self, kind: str) -> Path:
        return self.out / f"{self._seq:03d}_{short_name(kind)}.png"

    def _begin(self) -> None:
        self._seq += 1

    def _configure(self, layers: str | Sequence[str] | None, render: dict[str, Any]) -> list[Any]:
        selected = select_layers(self.layers, _layer_spec(layers))
        unknown = set(render) - {"fill", "depth", "size", "show_ports"}
        if unknown:
            raise click.ClickException(f"unknown render option(s) {sorted(unknown)}")
        self.renderer.configure(selected, **render)
        return selected

    def _paths(self, paths: Any, hide_paths: Sequence[str]) -> tuple[list[PathInfo], list[str]]:
        """The load-time paths minus `hide_paths`, plus this call's extra `paths`."""
        labels = [p.label for p in self.paths]
        hide = set(labels) if "*" in hide_paths else set(hide_paths)
        unknown = hide - set(labels)
        if unknown:
            raise click.ClickException(f"hide_paths: no path(s) {sorted(unknown)}; paths are {labels}")
        extra = resolve_paths(normalize_path_entries(paths), self.target, start=len(self.paths), taken=set(labels))
        return [p for p in self.paths if p.label not in hide] + extra, [lb for lb in labels if lb in hide]

    def _markers(self, show_violations: bool) -> list[MarkerSet]:
        return [self.violations] if show_violations and self.violations is not None else []

    def _render(
        self,
        progress: Progress | None,
        kind: str,
        box: Any,
        title: str,
        paths: Sequence[PathInfo] = (),
        markers: Sequence[MarkerSet] = (),
    ) -> dict[str, Any]:
        image = self.renderer.render(self._image(kind), box, title, self.ports, paths, markers)
        if progress is not None:
            progress(f"rendered {image['path']}")
        return image

    def _check_port_names(self, names: Sequence[str]) -> None:
        known = {p.name for p in self.ports}
        unknown = [n for n in names if n not in known]
        if unknown:
            raise click.ClickException(f"unknown port(s) {unknown}; ports are {sorted(known)}")

    # -- requests -----------------------------------------------------------

    def summary(self, progress: Progress | None = None) -> Result:
        """Cell, ports, layers and the load-time paths."""
        return Result(
            {
                "cell": cell_section(self.target),
                "ports": [p.__dict__ for p in self.ports],
                "layers": layers_section(self.target, self.layers),
                "paths": [p.summary() for p in self.paths],
                "gds": str(self.gds.resolve()),
            }
        )

    def overview(
        self,
        paths: Any = None,
        hide_paths: Sequence[str] = (),
        layers: str | Sequence[str] | None = None,
        show_violations: bool = True,
        progress: Progress | None = None,
        **render: Any,
    ) -> Result:
        """The whole cell, with its ports and the drawn paths kept in view."""
        import klayout.db as kdb

        self._begin()
        self._configure(layers, render)
        drawn, hidden = self._paths(paths, hide_paths)
        bbox = self.target.kcell.dbbox()
        for p in self.ports:  # ports and overlay paths may sit off the geometry; keep them in view
            bbox += kdb.DPoint(p.x, p.y)
        for pi in drawn:
            for x, y in pi.points:
                bbox += kdb.DPoint(x, y)
        if bbox.empty():
            bbox = kdb.DBox(-1, -1, 1, 1)
        margin = max(bbox.width(), bbox.height()) * 0.06 + 0.5
        image = self._render(
            progress,
            "overview",
            bbox.enlarged(margin, margin),
            f"{self.target.kcell.name}",
            drawn,
            self._markers(show_violations),
        )
        return Result({"paths": [p.summary() for p in drawn], "hidden_paths": hidden, "images": [image]})

    def zoom(
        self,
        boxes: Sequence[Any] = (),
        ports: Sequence[str] = (),
        all_ports: bool = False,
        radius: float | None = None,
        paths: Any = None,
        hide_paths: Sequence[str] = (),
        layers: str | Sequence[str] | None = None,
        show_violations: bool = True,
        progress: Progress | None = None,
        **render: Any,
    ) -> Result:
        """One view per box (x0,y0,x1,y1 in µm) and per port (half-size `radius`)."""
        import klayout.db as kdb

        parsed = [_box(b, "box") for b in boxes]
        self._check_port_names(ports)
        wanted = [p for p in self.ports if all_ports or p.name in ports]
        if not parsed and not wanted:
            raise click.ClickException("zoom needs at least one box or port (or all_ports)")
        if radius is not None and radius <= 0:
            raise click.ClickException(f"radius must be positive, got {radius}")
        self._begin()
        self._configure(layers, render)
        drawn, hidden = self._paths(paths, hide_paths)
        markers = self._markers(show_violations)
        images = []
        for i, z in enumerate(parsed, 1):
            box = kdb.DBox(min(z[0], z[2]), min(z[1], z[3]), max(z[0], z[2]), max(z[1], z[3]))
            images.append(self._render(progress, f"zoom_{i}", box, f"zoom {list(z)}", drawn, markers))
        for p in wanted:
            r = radius if radius else max(4 * p.width, 2.0)
            box = kdb.DBox(p.x - r, p.y - r, p.x + r, p.y + r)
            title = f"port {p.name} @ ({p.x}, {p.y}) w={p.width} {p.orientation:g}°"
            images.append(self._render(progress, f"port_{p.name}", box, title, drawn, markers))
        return Result({"paths": [p.summary() for p in drawn], "hidden_paths": hidden, "images": images})

    def cut(
        self,
        lines: Sequence[Any],
        layers: str | Sequence[str] | None = None,
        show_violations: bool = True,
        progress: Progress | None = None,
        **render: Any,
    ) -> Result:
        """Where each layer starts and stops along each line x0,y0,x1,y1, with a view of the line."""
        import klayout.db as kdb

        parsed = [_box(c, "cut line") for c in lines]
        if not parsed:
            raise click.ClickException("cut needs at least one line x0,y0,x1,y1")
        self._begin()
        selected = self._configure(layers, render)
        cuts = [cut_section(self.target, selected, c) for c in parsed]
        images = []
        for i, c in enumerate(parsed, 1):
            x0, y0, x1, y1 = c
            line = kdb.DPath([kdb.DPoint(x0, y0), kdb.DPoint(x1, y1)], max(self.target.dbu * 20, 0.002)).polygon()
            markers = [*self._markers(show_violations), MarkerSet(f"cut {i}", 0x0040FF, [line], dither=0)]
            half = math.hypot(x1 - x0, y1 - y0) * 0.65 + 0.2
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            box = kdb.DBox(cx - half, cy - half, cx + half, cy + half)
            images.append(self._render(progress, f"cut_{i}", box, f"cut {list(c)}", self.paths, markers))
        return Result({"cuts": cuts, "images": images})

    def check(
        self,
        min_width: float | None = None,
        min_space: float | None = None,
        grid: float | None = None,
        layers: str | Sequence[str] | None = None,
        progress: Progress | None = None,
        **render: Any,
    ) -> Result:
        """Port, self-intersection and (optional) width / space / grid checks, with close-ups.

        The violations stay marked on later views until the next check.
        """
        self._begin()
        selected = self._configure(layers, render)
        checks, markers = checks_section(self.target, selected, self.ports, grid, min_width, min_space)
        self.violations = markers
        images = []
        for i, (title, vb) in enumerate(markers.focus[:MAX_CHECK_VIEWS], 1):
            pad = max(vb.width(), vb.height()) * 0.15 + 1.0
            images.append(self._render(progress, f"check_{i}", vb.enlarged(pad, pad), title, self.paths, [markers]))
        return Result({"checks": checks, "images": images})

    def diff(
        self,
        against: str = "last",
        progress: Progress | None = None,
        **render: Any,
    ) -> Result:
        """Added / removed geometry and port changes against a GDS, or the previous load ('last')."""
        if against == "last":
            path = self.prev_gds
            if not path.exists():
                raise click.ClickException(f"diff last: no previous load in {self.out}")
        else:
            path = Path(against)
        self._begin()
        self._configure(None, render)
        data, markers, box = diff_section(self.target, path, self.cell_name)
        images = []
        if box is not None:
            pad = max(box.width(), box.height()) * 0.15 + 0.5
            images.append(
                self._render(progress, "diff", box.enlarged(pad, pad), "diff: added / removed", self.paths, markers)
            )
        return Result({"diff": data, "images": images})
