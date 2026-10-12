"""KLayout rendering: one LayoutView per loaded cell, framed with matplotlib axes."""

from __future__ import annotations

import math
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click

from .analysis import dbox_tuple
from .model import LayerEntry, MarkerSet, PathInfo, PortInfo, Target

FILLS = ("pdk", "solid", "outline")


def _hex(color: int) -> str:
    return f"#{color & 0xFFFFFF:06x}"


class Renderer:
    """Owns a KLayout LayoutView showing the target cell.

    The view is built once; `configure` applies the per-call options (visible layers, fill,
    hierarchy depth, ports, image size) by rewriting the layer properties.
    """

    def __init__(self, target: Target, layers: list[LayerEntry]) -> None:
        from gdsfactory.pdk import get_layer_views
        from klayout import lay

        self.target = target
        self.layers = layers
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

        # Remember the PDK's own properties of every leaf node, so `configure` can start from them.
        by_key = {(e.layer, e.datatype): e for e in layers}
        self._base: list[Any] = []
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
                self._base.append(node.dup())
            it.next()
        self.visible: list[LayerEntry] = list(layers)
        self.show_ports = True
        self.size = 1400
        self._live: list[Any] = []
        self.configure()

    def configure(
        self,
        visible: Sequence[LayerEntry] | None = None,
        fill: str = "pdk",
        depth: int | None = None,
        show_ports: bool = True,
        size: int = 1400,
    ) -> None:
        if fill not in FILLS:
            raise click.ClickException(f"fill must be one of {FILLS}, got {fill!r}")
        if depth is not None and depth < 0:
            raise click.ClickException(f"depth must be >= 0, got {depth}")
        if not 200 <= size <= 4000:
            raise click.ClickException(f"size must be between 200 and 4000 px, got {size}")
        self.show_ports = show_ports
        self.size = size
        if depth is None:
            self.view.max_hier()
        else:
            self.view.min_hier_levels = 0
            self.view.max_hier_levels = depth + 1  # level 1 = the cell's own shapes

        chosen = list(self.layers if visible is None else visible)
        visible_keys = {(e.layer, e.datatype) for e in chosen}
        base = iter(self._base)
        it = self.view.begin_layers()
        while not it.at_end():
            node = it.current()
            if not node.has_children():
                new = next(base).dup()
                new.visible = (new.source_layer, new.source_datatype) in visible_keys
                if fill == "solid":
                    new.dither_pattern = 0
                elif fill == "outline":
                    new.dither_pattern = 1
                new.width = 1
                self.view.set_layer_properties(it, new)
            it.next()
        by_key = {(e.layer, e.datatype): e for e in self.layers}
        self.visible = [by_key[k] for k in sorted(visible_keys) if k in by_key]

    # -- scene objects ------------------------------------------------------

    def _annotations(self, ports: list[PortInfo], paths: Sequence[PathInfo], um_per_px: float, box: Any) -> None:
        import klayout.db as kdb
        from klayout import lay

        self.view.clear_annotations()
        for pi in paths:
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

    def _markers(self, marker_sets: Sequence[MarkerSet], paths: Sequence[PathInfo]) -> None:
        import klayout.db as kdb
        from klayout import lay

        for m in self._live:
            m._destroy()
        self._live = []
        for ms in marker_sets:
            for poly in ms.polygons:
                marker = lay.Marker(self.view)
                marker.set_polygon(poly)
                marker.color = ms.color
                marker.frame_color = ms.color
                marker.line_width = 2
                marker.dither_pattern = ms.dither
                marker.halo = 0
                self._live.append(marker)
        for pi in paths:
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

    def render(
        self,
        path: Path,
        box: Any,
        title: str,
        ports: list[PortInfo],
        paths: Sequence[PathInfo] = (),
        marker_sets: Sequence[MarkerSet] = (),
    ) -> dict[str, Any]:
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

        self._annotations(ports, paths, um_per_px, box)
        self._markers(marker_sets, paths)
        self.view.zoom_box(box)
        raw = path.with_suffix(".raw.png")
        self.view.save_image_with_options(str(raw), w, h, 0, 1, 0, box, False)  # oversampling greys out stipples
        _decorate(raw, path, box, title, self.visible, list(marker_sets), list(paths))
        raw.unlink(missing_ok=True)
        return {"path": str(path.resolve()), "box": dbox_tuple(box), "um_per_px": round(um_per_px, 6)}


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

