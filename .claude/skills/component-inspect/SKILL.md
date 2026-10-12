---
name: component-inspect
description: Render and check a gdsfactory component, GDS cell or path with KLayout during layout work, optionally overlaying custom paths (routes, bends) on top. Load the component once in a background client, then ask the component-inspect MCP tools for overviews, zooms, cross-section cuts, width/space/port/grid checks and diffs, as many times as needed. Images (port arrows, µm axes, layer legend) come back inline with a precise text summary. Use it whenever you create or change a component, to see the geometry, confirm dimensions and verify that an edit did only what you intended.
---

# component-inspect

Two parts, so a component is loaded once and inspected as often as you like:

- **client**: `pixi run component-inspect load SOURCE` loads the component, prints
  `client id: <id>` and its summary, then stays alive holding it. Run it in the
  background (Bash `run_in_background`) and read the id from its output.
- **server**: the `component-inspect` MCP server (started by Claude Code from the
  `component-inspect` plugin). Its tools take that `client_id`.

Rendering is done by KLayout directly; matplotlib only adds the axes and legend.
Each request writes PNGs into the client's output directory (default
`build/component-inspect/<name>/`, plus `layout.gds` and `summary.json`). The tools
also return the images inline, and the text you can trust more than the picture.

## Loading

`SOURCE` is one of:

| form | example |
| --- | --- |
| `file.py:func` (+ `--kwargs` JSON) | `pixi run component-inspect load src/ring.py:ring --kwargs '{"radius": 10}'` |
| `.gds` file (+ `--cell NAME`) | `pixi run component-inspect load build/chip.gds --cell top` |
| gf expression | `pixi run component-inspect load "gf.components.mmi1x2(width_mmi=3)"` |
| bare path(s) | `pixi run component-inspect load "gf.path.euler(radius=10, angle=90)"` |
| script on stdin / script file | `pixi run component-inspect load - <<'EOF' … EOF` or `… load build/scratch.py` |

Other options: `--pdk package.PDK` (default: gdsfactory generic PDK; a `file.py`
source may also activate its own), `--path [LABEL=]SPEC` (repeatable, see below),
`--out DIR`, `--name NAME` (used in the client id and the default `--out`).
Loading errors are printed and the process exits before registering.

From Python (e.g. a notebook) the same thing is a function call:

```python
from component_inspect import serve_component
h = serve_component(c, paths={"route": route}, out_dir="build/component-inspect/dev", block=False)
h.client_id   # pass to the MCP tools
h.close()
```

`InspectSession(c, ...)` gives the same requests (`overview`, `zoom`, `cut`, `check`,
`diff`, `summary`) without any server; each returns a `Result` with `.text`, `.data`,
`.images`.

### Inline scripts

When building the component or paths takes more than one expression, pipe a script
in with `-` (or save it and pass `script.py`). The script runs first, then the client
picks up:

- `component` (or `c`): the cell to inspect;
- `paths` (optional): a `gf.Path`, a list of them, or a `{label: path}` dict, kept as
  overlays (dict keys become the labels).

`gf` and `np` are pre-imported and the PDK is already active. `__name__` is not
`"__main__"`, so a trailing `if __name__ == "__main__": c.show()` is harmless.

```bash
pixi run component-inspect load - --out build/component-inspect/router <<'EOF'
c = gf.Component()
mmi = c << gf.components.mmi1x2()
wg = c << gf.components.straight(length=5)
wg.connect("o1", mmi.ports["o2"])
c.add_port("o1", port=mmi.ports["o1"])
c.add_port("o2", port=wg.ports["o2"])
paths = {"route": gf.path.euler(radius=10).move(c.ports["o2"].center)}
EOF
```

Use a quoted heredoc (`<<'EOF'`) so the shell passes the script through untouched.
If the script raises, the error lists the script lines involved.

## The loop

1. **Load** in the background: `pixi run component-inspect load src/dev.py:my_comp`.
   Read the `client id:` line and the summary it prints.
2. **Look**: `overview`, then `zoom` with `ports` / `boxes` where it matters. Read the
   text first: port coordinates and widths, layer areas and bboxes are exact. Use the
   images for shape and topology.
3. **Measure, don't eyeball**: `cut` with `lines=[[x0, y0, x1, y1]]` prints where each
   layer starts and stops along the line and the gaps between spans (e.g. a coupler
   gap or a slab width).
4. **Check**: `check` with `min_width`, `min_space` (µm) and `grid` as needed, or with
   no rule for the port and self-intersection checks alone. Later views keep the
   violations marked (`show_violations=false` to hide them).
5. **Edit the code, then load again with the same `--out`** (a new client; the old
   `layout.gds` becomes `layout.prev.gds`), call `diff` on the new client (default
   `against="last"`) to confirm only the intended geometry moved, and `close` the old
   client. `diff` lists added/removed area per layer and port changes; its image marks
   added (green) and removed (red) geometry.

`list_clients` shows the live clients. A client exits when closed or when the server
stops.

## Tools

All take `client_id`. Render tools also take `layers` (names or `L/D`),
`fill` (`pdk` | `solid` | `outline`; `outline` helps with overlapping layers),
`depth` (child levels drawn; deeper cells as frames), `size` (long edge, px, default
1400), `show_ports`, and `include_images` (false = paths to the PNGs only).

- `summary`: cell, settings, instances, ports, per-layer stats, load-time paths.
- `overview(paths, hide_paths, ...)`: the whole cell.
- `zoom(boxes, ports, all_ports, radius, paths, hide_paths, ...)`: one view per box
  `[x0, y0, x1, y1]` (µm) and per port (half-size `radius`, default `max(4*width, 2)`).
  Use several small views rather than one large image: images are downscaled when
  read, so tiny features vanish in an overview.
- `cut(lines, ...)`, `check(min_width, min_space, grid, ...)`, `diff(against, ...)`.

## Overlaying paths

Paths are drawn as coloured centrelines with a cross at the start and a labelled
arrow at the end. The text gives each one's length, start/end point and heading, its
minimum bend radius (estimated from the discretised points) and the number of sharp
corners (turns over 5° at a single vertex). Use them to plan a route between ports,
design an euler/spline bend, or compare against a reference trajectory.

- **Load-time paths** are drawn on every view: the source's own (a bare path, a
  script's `paths`), `--path [LABEL=]SPEC` on `load`, or `paths=` in Python.
  Leave some out of one view with `hide_paths=["route"]` (`["*"]` for all).
- **Per-call paths**: `paths=["[LABEL=]SPEC", ...]` on `overview` / `zoom` adds
  overlays to that view only.

SPEC is one of:

- an expression with `gf`, `np` and `c` (the loaded component) in scope:
  `route=gf.path.euler(radius=10).move(c.ports["o2"].center)`
- `file.py:func` returning a `gf.Path` or a list of them
- a point list: `0,0;5,5;10,0`

**Quoting** (for `--path` on the command line): `pixi run` re-parses its arguments
and drops single quotes *inside* an argument. Write string literals inside
expressions with double quotes and wrap the whole argument in single quotes, as in
`c.ports["o2"]` above. MCP tool arguments need no shell quoting.

## Reading the output

- Image axes are in µm and the scale is exact, so you can read coordinates for the
  next `zoom` or `cut` straight off the image.
- Blue arrows are ports: the arrow points out of the port, the short bar across it is
  the port face (the width), and the label is the port name.
- Red outlines are check violations. The text lists each location with its bbox and
  minimum distance, and `check` returns close-ups of the first few.
- The "non-Manhattan edges" note is informational: curved photonic geometry is
  expected to have them.
- "port not on geometry" means the port face is not covered by the port's layer.
  The port is misplaced, points the wrong way, or sits on the wrong layer.
- Tool errors for bad input (unknown port, bad box or path spec) are one-line
  messages; anything unexpected comes with the full traceback, which the client
  also prints to its own stderr. The client stays alive either way.

## Troubleshooting

- `cannot reach the component-inspect server`: the MCP server isn't running. Check
  `/mcp` for `component-inspect`; it listens on
  `build/component-inspect/.server.sock` (TCP on Windows, port in
  `build/component-inspect/.server.addr`; override with `COMPONENT_INSPECT_ADDRESS`).
- `no client '<id>'`: the client exited (closed, crashed, or the server restarted);
  load again.
