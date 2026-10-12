"""Data model shared by loading, analysis and rendering."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

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
    # Paths that came with SOURCE (a bare path, or a script's `paths`): (label or None, path).
    source_paths: list[tuple[str | None, Any]] = field(default_factory=list)
    source_label: str = ""  # shown as the spec of those paths

    @property
    def layout(self) -> Any:
        return self.kcell.kcl.layout

    @property
    def cell(self) -> Any:
        return self.kcell.kdb_cell

    @property
    def dbu(self) -> float:
        return self.layout.dbu

