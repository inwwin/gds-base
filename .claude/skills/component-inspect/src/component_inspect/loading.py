"""Turn a SOURCE (file.py:func, script, GDS, gf expression or object) into a Target."""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any

import click

from .model import Target
from .paths import paths_in

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


def short_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:60] or "cell"


def load_target(source: str, kwargs: dict[str, Any], cell_name: str | None) -> Target:
    import gdsfactory as gf

    path_part, sep, func_name = source.rpartition(":")
    if sep and path_part.endswith(".py"):
        if not Path(path_part).is_file():
            raise click.ClickException(f"{path_part} does not exist")
        return _load_python(Path(path_part), func_name, kwargs)
    if source == "-" or source.endswith(".py"):
        if kwargs:
            raise click.ClickException("--kwargs cannot be used with a script source")
        return _load_script(source)
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
    return as_target(obj, match.group(1) if match else None)


def _load_script(source: str) -> Target:
    """Run a script (stdin for '-') and pick up the `component`/`c` and `paths` it defines."""
    import gdsfactory as gf
    import numpy as np

    if source == "-":
        code, label, name = sys.stdin.read(), "<stdin>", "script"
        if not code.strip():
            raise click.ClickException("SOURCE '-' expects a Python script on stdin, but stdin was empty")
    else:
        path = Path(source).resolve()
        if not path.is_file():
            raise click.ClickException(f"{source} does not exist")
        code, label, name = path.read_text(), str(path), path.stem
        sys.path.insert(0, str(path.parent))
    # __name__ is not "__main__", so a script's `if __name__ == "__main__": c.show()` stays inert.
    namespace: dict[str, Any] = {"__name__": "__inspect_script__", "__file__": label, "gf": gf, "np": np}
    try:
        exec(compile(code, label, "exec"), namespace)  # local developer tool: running the script is the point
    except Exception as exc:
        # Keep only the script's own frames: they are what needs fixing.
        lines = code.splitlines()
        where = "".join(
            f"  line {f.lineno}: {lines[f.lineno - 1].strip() if f.lineno and f.lineno <= len(lines) else ''}\n"
            for f in traceback.extract_tb(exc.__traceback__)
            if f.filename == label
        )
        raise click.ClickException(f"script {label} raised {exc!r}\n{where}".rstrip()) from exc

    component = namespace.get("component", namespace.get("c"))
    raw_paths = namespace.get("paths")
    found: list[tuple[str | None, Any]] = []
    if isinstance(raw_paths, dict):
        bad = [k for k, v in raw_paths.items() if paths_in(v) is None]
        if bad:
            raise click.ClickException(f"script {label}: paths[{bad[0]!r}] is not a path")
        for key, value in raw_paths.items():
            items = paths_in(value) or []
            found += [(str(key) if len(items) == 1 else f"{key}[{i}]", o) for i, o in enumerate(items)]
    elif raw_paths is not None:
        items = paths_in(raw_paths)
        if items is None:
            raise click.ClickException(f"script {label}: `paths` must be a path, a list of paths or a dict of them")
        found = [(None, o) for o in items]
    if component is None:
        if not found:
            raise click.ClickException(f"script {label} must define `component` (or `c`) and/or `paths`")
        component = gf.Component()
        component.name = f"paths_{short_name(name)}"
    if isinstance(component, gf.Component) and (component.name or "").startswith("Unnamed"):
        component.name = short_name(name)
    try:
        target = as_target(component, name)
    except click.ClickException as exc:
        raise click.ClickException(f"script {label}: `component`/`c` is not usable ({exc.message})") from exc
    target.source_paths += found
    target.source_label = f"{label}: paths"
    return target


def import_attr(path: Path, attr: str) -> Any:
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
    obj = import_attr(path, func_name)
    if callable(obj):
        try:
            obj = obj(**kwargs)
        except Exception as exc:
            raise click.ClickException(f"{func_name}(**{kwargs}) raised {exc!r}") from exc
    elif kwargs:
        raise click.ClickException(f"{func_name} is not callable, so --kwargs cannot be applied")
    return as_target(obj, func_name)


def as_target(obj: Any, name: str | None) -> Target:
    import gdsfactory as gf
    import kfactory as kf

    if paths_in(obj) is not None:
        empty = gf.Component()
        empty.name = f"paths_{short_name(name or 'path')}"
        found = [(None, o) for o in paths_in(obj) or []]
        return Target(name=short_name(name or "path"), kcell=empty, source_paths=found)
    if isinstance(obj, gf.ComponentAllAngle):
        flat = gf.Component()
        flat.name = obj.name or f"flat_{short_name(name or 'all_angle')}"  # an unbuilt one has no name
        kf.VInstance(obj).insert_into_flat(flat, levels=0)
        flat.add_ports(obj.ports)
        obj = flat
    if not isinstance(obj, kf.ProtoTKCell):
        raise click.ClickException(f"source did not produce a Component (got {type(obj).__name__})")
    if isinstance(obj, gf.Component):
        obj.insert_vinsts()
    return Target(name=short_name(name or obj.name or "cell"), kcell=obj)


def read_gds(path: Path, cell_name: str | None) -> Any:
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
    kcell = read_gds(path, cell_name)
    return Target(name=short_name(kcell.name), kcell=kcell)

