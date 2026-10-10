---
name: layout-inspect
description: Render and check a gdsfactory component or GDS cell with KLayout during layout work. It writes PNG views with port arrows, µm axes and a layer legend, prints a precise summary (ports, per-layer area/bbox, cross-section cuts), runs width/space/port/grid checks, and diffs against the previous run. Use it whenever you create or change a component, to see the geometry, confirm dimensions and verify that an edit did only what you intended.
---

# layout-inspect

`scripts/inspect_layout.py` is the eyes of the component-design loop. It talks to
KLayout directly (no matplotlib). Every run writes a GDS plus images into one output
directory and prints a summary you can trust more than the picture.

```bash
pixi run inspect-layout SOURCE [OPTIONS]
```

`SOURCE` is one of:

| form | example |
| --- | --- |
| `file.py:func` (+ `--kwargs` JSON) | `pixi run inspect-layout src/ring.py:ring --kwargs '{"radius": 10}'` |
| `.gds` file (+ `--cell NAME`) | `pixi run inspect-layout build/chip.gds --cell top` |
| gf expression | `pixi run inspect-layout "gf.components.mmi1x2(width_mmi=3)"` |

The generic PDK is activated first. Pass `--pdk package.PDK` to use another PDK; a
`file.py` source may also activate its own PDK when it is imported.

## The loop

1. **Render**: `pixi run inspect-layout src/dev.py:my_comp --zoom-ports`
2. **Read the text summary first.** Port coordinates and widths, layer areas and
   bboxes are exact. Use the images for shape and topology. Read `overview.png`, then
   the zooms.
3. **Measure, don't eyeball.** Use `--cut x0,y0,x1,y1` to print where each layer
   starts and stops along a line, and the gaps between spans (e.g. a coupler gap or a
   slab width).
4. **Check**: add `--min-width W --min-space S` (µm) and `--grid G` as needed, or just
   `--check` for the port and self-intersection checks alone.
5. **Edit the code, then rerun with `--diff last`** to confirm only the intended
   geometry moved. Reruns go to the same output directory, which keeps the
   previous GDS as `layout.prev.gds`. The summary lists added/removed area per layer
   and port changes, and `diff.png` marks added (green) and removed (red) geometry.

## Options worth knowing

- `--zoom x0,y0,x1,y1` (repeatable), `--zoom-port o1` (repeatable), `--zoom-ports`, and
  `--radius R` for the port zoom half-size. Use several small views rather than one
  large image: images are downscaled when read, so tiny features vanish in an
  overview.
- `--layers WG,SLAB` (names or `L/D`) restricts both the drawing and the analysis.
- `--depth N` draws N child levels; deeper cells appear as frames.
- `--fill solid|outline` when the PDK stipples are hard to read. `outline` helps
  with overlapping layers.
- `--no-ports`, `--size PX` (long edge, default 1400), `--out DIR` (default
  `build/inspect/<name>/`), `--json` (machine-readable summary; it is always saved
  as `summary.json` as well).

## Reading the output

- Image axes are in µm and the scale is exact, so you can read coordinates for
  the next `--zoom` or `--cut` straight off the image.
- Blue arrows are ports: the arrow points out of the port, the short bar across it is
  the port face (the width), and the label is the port name.
- Red outlines are check violations. The summary lists each location with its
  bbox and minimum distance, and `check_*.png` are close-ups of the first few.
- The "non-Manhattan edges" note is informational: curved photonic geometry is
  expected to have them.
- "port not on geometry" means the port face is not covered by the port's layer.
  The port is misplaced, points the wrong way, or sits on the wrong layer.
