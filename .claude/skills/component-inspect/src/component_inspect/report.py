"""Text rendering of summary sections."""

from __future__ import annotations

import json
from typing import Any


def _box(box: list[float] | None) -> str:
    return "(empty)" if box is None else str(box)


def format_summary(s: dict[str, Any]) -> str:
    """Human-readable text for whichever sections `s` holds; blocks are separated by blank lines."""
    blocks: list[list[str]] = []

    def block(*lines: str) -> list[str]:
        blocks.append(list(lines))
        return blocks[-1]

    if "client_id" in s:
        block(f"client id: {s['client_id']}")

    if "cell" in s:
        cell = s["cell"]
        out = block(
            f"Cell      : {cell['name']}",
            f"BBox      : {_box(cell['bbox'])}  size {cell['size'][0]} x {cell['size'][1]} µm  (dbu {cell['dbu']})",
        )
        if cell.get("settings"):
            out.append(f"Settings  : {json.dumps(cell['settings'])}")
        if cell.get("info"):
            out.append(f"Info      : {json.dumps(cell['info'])}")
        inst = cell["instances"]
        if inst["count"]:
            out.append(f"Instances : {inst['count']}")
            out += [f"  {i['cell']}  {i['trans']}{'  (array)' if i['array'] else ''}" for i in inst["listed"]]

    if "ports" in s:
        out = block("Ports:")
        if not s["ports"]:
            out.append("  (none)")
        else:
            out.append(f"  {'name':<10} {'x':>10} {'y':>10} {'width':>7} {'angle':>6}  {'layer':<12} type")
            out += [
                f"  {p['name']:<10} {p['x']:>10.4f} {p['y']:>10.4f} {p['width']:>7.4f} {p['orientation']:>6g}"
                f"  {p['layer']:<12} {p['port_type']}"
                for p in s["ports"]
            ]

    if "layers" in s:
        block(
            "Layers:" if s["layers"] else "Layers:\n  (none)",
            *(
                f"  {la['layer']:<24} colour {la['fill']}  polygons {la['polygons']:<5}"
                f" area {la['area_um2']:.4f} µm²  bbox {la['bbox']}"
                for la in s["layers"]
            ),
        )

    for cut in s.get("cuts", []):
        x0, y0, x1, y1 = cut["line"]
        out = block(f"Cut ({x0}, {y0}) -> ({x1}, {y1}), length {cut['length']} µm  [s = distance from start]")
        if not cut["layers"]:
            out.append("  (no geometry crossed)")
        for la in cut["layers"]:
            spans = ", ".join(f"[{sp['s'][0]}, {sp['s'][1]}] ({sp['length']})" for sp in la["spans"])
            out.append(f"  {la['layer']:<24} {spans}")
            if la["gaps"]:
                out.append(f"  {'':<24} gaps: {la['gaps']}")

    if s.get("paths"):
        out = block("Paths:")
        for p in s["paths"]:
            radius = f"min radius {p['min_radius']} µm" if p["min_radius"] is not None else "straight segments only"
            corners = f"  sharp corners: {p['sharp_corners']}" if p["sharp_corners"] else ""
            out.append(
                f"  {p['label']:<7} length {p['length']} µm  start ({p['start'][0]}, {p['start'][1]}) {p['start'][2]:g}°"
                f"  end ({p['end'][0]}, {p['end'][1]}) {p['end'][2]:g}°  {radius}{corners}"
            )
            out.append(f"  {'':<7} {p['spec']}")
    if s.get("hidden_paths"):
        block(f"Hidden paths: {', '.join(s['hidden_paths'])}")

    if "checks" in s:
        ch = s["checks"]
        out = block(f"Checks    : {'PASS' if ch['passed'] else str(len(ch['issues'])) + ' issue(s)'}")
        for issue in ch["issues"]:
            rest = {k: v for k, v in issue.items() if k not in ("check", "layer")}
            out.append(f"  [{issue['check']}] {issue['layer']}: {json.dumps(rest)}")
        for layer, st in ch["edge_stats"].items():
            if st["non_manhattan_edges"]:
                out.append(f"  note: {layer} has {st['non_manhattan_edges']}/{st['edges']} non-Manhattan edges")

    if "diff" in s:
        d = s["diff"]
        out = block(f"Diff vs {d['previous']} (cell {d['previous_cell']}):")
        if d["identical"]:
            out.append("  identical geometry and ports")
        for la in d["layers"]:
            out.append(f"  {la['layer']:<24} +{la['added_um2']:.4f} / -{la['removed_um2']:.4f} µm²  in {la['bbox']}")
        out += [f"  port {json.dumps(p)}" for p in d["ports"]]
        if d["bbox_before"] != d["bbox_after"]:
            out.append(f"  bbox {_box(d['bbox_before'])} -> {_box(d['bbox_after'])}")

    if s.get("images"):
        block(
            "Images:",
            *(f"  {img['path']}  box {img['box']}  ({img['um_per_px']} µm/px)" for img in s["images"]),
        )
    if "gds" in s:
        block(f"GDS       : {s['gds']}")
    return "\n\n".join("\n".join(b) for b in blocks)
