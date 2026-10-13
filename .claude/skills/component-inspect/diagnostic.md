# component-inspect: setup and diagnostics

Read this when `component-inspect` is not found, the `component-inspect` MCP server
is not connected, or the skill is used in a project other than gds-base.

## 1. Which environment manager?

The `component-inspect` command must run inside the project's Python environment.
Look at the project root:

| project has | manager | run commands with |
| --- | --- | --- |
| `pixi.toml`, or `pyproject.toml` with a `[tool.pixi.*]` table | pixi | `pixi run component-inspect …` |
| otherwise: `uv.lock`, or a `pyproject.toml` without `[tool.pixi.*]` | uv | `uv run component-inspect …` |

The first matching row wins, so pixi takes precedence: a `pixi.toml` next to a
`pyproject.toml` (with or without `[tool.pixi.*]`) or a `uv.lock` means pixi. The MCP
server's start script uses the same rule. If the project really is driven by uv
despite a pixi manifest, use `uv run` and force the server with
`COMPONENT_INSPECT_RUNNER=uv` (or `=pixi`) in its environment (the `env` field of its
`.mcp.json` entry).

Everywhere the skill says `pixi run`, use `uv run` in a uv project. Quoting notes
about `pixi run` re-parsing arguments don't apply to `uv run`, but following them is
harmless.

## 2. Is component-inspect installed?

```bash
pixi run component-inspect --help   # or: uv run component-inspect --help
```

A usage message means it is. "command not found" / "Failed to spawn" means the
project doesn't depend on it yet: go to step 3.

## 3. Adding the dependency

This changes the host project's manifest and lock file, so **ask the user first**
(unless they already asked for the skill to be set up). It pulls in gdsfactory
(>=9.51, which brings KLayout), `mcp`, `rpyc`, `matplotlib`, `click` and `pydantic`,
and needs Python >= 3.12. If the project pins conflicting versions, report the
conflict rather than loosening their pins.

The package is not on PyPI. It lives in the gds-base repository at
`.claude/skills/component-inspect` (the directory holding this file), so install it
from git, or from a local path when this skill directory is inside the project.

### pixi

From git (PyPI dependency):

```bash
pixi add --pypi "component-inspect @ git+https://github.com/inwwin/gds-base.git#subdirectory=.claude/skills/component-inspect"
```

or by hand in `pixi.toml` (`[tool.pixi.pypi-dependencies]` in a `pyproject.toml`
manifest), then `pixi install`:

```toml
[pypi-dependencies]
component-inspect = { git = "https://github.com/inwwin/gds-base.git", subdirectory = ".claude/skills/component-inspect" }
```

From a copy of the skill inside the project (editable, so edits to the package apply
without reinstalling; this is how gds-base itself does it):

```toml
[pypi-dependencies]
component-inspect = { path = ".claude/skills/component-inspect", editable = true }
```

pixi resolves gdsfactory and the rest from PyPI unless the project already provides
them from conda-forge.

### uv

As a dev dependency (it is a tool for layout work, not something the project's own
package needs at runtime):

```bash
uv add --dev "component-inspect @ git+https://github.com/inwwin/gds-base.git#subdirectory=.claude/skills/component-inspect"
# or, from a copy of the skill inside the project:
uv add --dev --editable ./.claude/skills/component-inspect
```

`uv add` updates `pyproject.toml` (a `[dependency-groups] dev` entry plus a
`[tool.uv.sources]` entry) and `uv.lock`, and syncs `.venv`. A project without a
`pyproject.toml` needs `uv init --bare` first, which is another thing to check with
the user.

### Notes

- The git form needs read access to `inwwin/gds-base`; pin a commit or tag with
  `@<rev>` before `#subdirectory` (pixi: `rev = "<rev>"`) for reproducible builds.
- The first install downloads gdsfactory and KLayout and can take several minutes:
  run it with a long timeout or in the background.
- Re-run step 2 to confirm.

## 4. Starting the MCP server

The tools (`summary`, `overview`, `zoom`, …) come from the `component-inspect` MCP
server, which Claude Code starts from the project's `.mcp.json` with
`scripts/component-inspect-server.sh` (next to this file). The script runs
`pixi run --locked … component-inspect serve` or
`uv run [--locked] --project … component-inspect serve` in the project directory.

In a project other than gds-base, with this skill directory copied to
`.claude/skills/component-inspect`, register the server (ask the user first) in
`.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "component-inspect": {
      "command": "${CLAUDE_PROJECT_DIR:-.}/.claude/skills/component-inspect/scripts/component-inspect-server.sh"
    }
  }
}
```

and approve it in `.claude/settings.json` with
`"enabledMcpjsonServers": ["component-inspect"]` (otherwise Claude Code asks once).

- If the server was started before the dependency existed, it failed: reconnect it
  from `/mcp` (or restart Claude Code) after step 3. You cannot restart it yourself;
  tell the user.
- `--locked` makes it fail rather than re-solve when the lock file is stale: run
  `pixi install` / `uv lock` after hand-editing the manifest.
- Its stderr (pixi/uv errors, the listening address) is in the MCP logs
  (`claude --debug`, or `/mcp` → the server).

## Troubleshooting

- `cannot reach the component-inspect server`: the MCP server isn't running. Check
  `/mcp` for `component-inspect` (step 4); it listens on
  `build/component-inspect/.server.sock` (TCP on Windows, port in
  `build/component-inspect/.server.addr`; override with `COMPONENT_INSPECT_ADDRESS`,
  `unix:/path` or `tcp:host:port`).
- `AF_UNIX path too long` in the server's log: the project path is too deep for a
  Unix socket. Set a fixed address, e.g. `COMPONENT_INSPECT_ADDRESS=tcp:127.0.0.1:18765`
  or `unix:/tmp/<project>-component-inspect.sock`, in the server's `.mcp.json` `env`
  **and** in front of every `component-inspect load` command (clients don't discover
  an overridden address on their own).
- `no client '<id>'`: the client exited (closed, crashed, or the server restarted);
  load again.
