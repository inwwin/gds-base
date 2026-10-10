---
name: layout-inspect
description: Render and check a gdsfactory component, GDS cell or path with KLayout during layout work, optionally overlaying custom paths (routes, bends) on top. It writes PNG views with port arrows, µm axes and a layer legend, prints a precise summary (ports, per-layer area/bbox, cross-section cuts), runs width/space/port/grid checks, and diffs against the previous run. Use it whenever you create or change a component, to see the geometry, confirm dimensions and verify that an edit did only what you intended.
---

# layout-inspect

`scripts/inspect_layout.py` is the eyes of the component-design loop. It renders with
KLayout directly; matplotlib only adds the axes and legend around those pixels. Every run writes a GDS plus images into one output
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
| bare path(s) | `pixi run inspect-layout "gf.path.euler(radius=10, angle=90)"` |
| script on stdin / script file | `pixi run inspect-layout - <<'EOF' … EOF` or `pixi run inspect-layout build/scratch.py` |

The generic PDK is activated first. Pass `--pdk package.PDK` to use another PDK; a
`file.py` source may also activate its own PDK when it is imported.

## Inline scripts

When building the component or paths takes more than one expression, write it as a
script and pipe it in with `-` (or save it and pass `script.py`). The script runs
first, then the script picks up:

- `component` (or `c`): the cell to inspect;
- `paths` (optional): a `gf.Path`, a list of them, or a `{label: path}` dict, which
  gets drawn and measured like `--path` overlays (dict keys become the labels).

`gf` and `np` are pre-imported and the PDK is already active. `__name__` is not
`"__main__"`, so a trailing `if __name__ == "__main__": c.show()` is harmless.
Every CLI option still applies:

```bash
pixi run inspect-layout - --zoom-port o2 --diff last --out build/inspect/router <<'EOF'
c = gf.Component()
mmi = c << gf.components.mmi1x2()
wg = c << gf.components.straight(length=5)
wg.connect("o1", mmi.ports["o2"])
c.add_port("o1", port=mmi.ports["o1"])
c.add_port("o2", port=wg.ports["o2"])
paths = {"route": gf.path.euler(radius=10).move(c.ports["o2"].center)}
EOF
```

- Use a quoted heredoc (`<<'EOF'`) so the shell passes the script through
  untouched. Quotes inside the script are then safe, unlike in `--path`
  expressions.
- Stdin runs default to `build/inspect/script/`. Pass `--out` to keep separate
  designs, and their `--diff last` history, apart.
- If the script raises, the error lists the script lines involved.

From Python, `inspect()` takes the same options as keyword arguments (e.g.
`path_specs=[route]`, `cuts=[(x0, y0, x1, y1)]`, `quiet=True`) and returns the summary
dict; errors raise `click.ClickException`:

```python
import sys; sys.path.insert(0, ".claude/skills/layout-inspect/scripts")
from inspect_layout import inspect
summary = inspect(c, path_specs=[route], zoom_ports=["o2"], out_dir="build/inspect/dev")
```

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

## Overlaying paths

Use `--path SPEC` (repeatable) to draw centrelines on top of the component. Use it
to plan a route between ports, design an euler/spline bend, or compare against a
reference trajectory. SPEC is one of:

- an expression with `gf`, `np` and `c` (the loaded component) in scope:
  `--path 'gf.path.euler(radius=10).move(c.ports["o2"].center)'`
- `file.py:func` returning a `gf.Path` or a list of them
- a point list: `--path "0,0;5,5;10,0"`

Each path is drawn as a coloured line with a cross at its start and a labelled
arrow at its end. The summary gives its length, start/end point and heading, its
minimum bend radius (estimated from the discretised points), and the number of
sharp corners (turns over 5° at a single vertex).

**Quoting:** `pixi run` re-parses its arguments and drops single quotes *inside*
an argument. So write string literals inside expressions with double quotes and
wrap the whole argument in single quotes, as in `c.ports["o2"]` above. Calling the
script with `pixi run python .claude/skills/layout-inspect/scripts/inspect_layout.py`
avoids the problem altogether.

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
