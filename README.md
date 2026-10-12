# gds-base

A default base project for new [gdsfactory](https://gdsfactory.github.io/gdsfactory/)
layout projects built with an agentic workflow (Claude Code).

This repository is meant to be **forked**, not developed in directly. It has no
layout code of its own. It provides a locked Python environment and agent tooling,
so a new photonic or electronic layout project starts with an environment that
installs itself and an agent that can see and check the geometry it draws.

## What's included

| Path | Purpose |
| --- | --- |
| `pixi.toml`, `pixi.lock` | [pixi](https://pixi.sh) environment (conda-forge + PyPI) with gdsfactory 9.x and its dependencies, locked for `linux-64` |
| `pyrightconfig.json` | Points pyright at the pixi environment (`.pixi/envs/default`) |
| `.claude/settings.json` | Project settings for Claude Code: the SessionStart hook, the `pixi-pyright` plugin and the `component-inspect` MCP server |
| `.claude/hooks/session-start.sh` | In Claude Code cloud sessions, installs pixi if needed, runs `pixi install --locked` and activates the environment for the session |
| `.claude/cloud-setup.sh` | Optional setup script for a cloud environment. It pre-fills pixi's package cache so sessions start faster |
| `.claude/skills/component-inspect/` | Skill + MCP server that renders gdsfactory components with KLayout and runs width/space/port/grid checks, cross-section cuts and diffs. With it the agent can check its own layout edits |
| `.claude/marketplace/plugins/pixi-pyright/` | Plugin that runs the pyright language server from the pixi environment, giving the agent type information and diagnostics |
| `.mcp.json` | Registers the `component-inspect` MCP server |

## Starting a new project

1. **Fork** this repository (or use it as a template) and rename it.
2. Edit `pixi.toml`: set `name`, `authors` and `version`, and add any PDKs or extra
   dependencies your project needs. Then refresh the lockfile:
   ```bash
   pixi install        # updates pixi.lock
   ```
3. If you use `.claude/cloud-setup.sh`, copy the new `pixi.toml` into it. It embeds
   a copy of the manifest.
4. Add your own code, for example `src/<your_package>/` for components and cells, and
   a `CLAUDE.md` that describes the project's conventions, PDK and design goals for
   the agent.
5. Open the project in Claude Code, locally or at [claude.ai/code](https://claude.ai/code).

## Local setup

```bash
curl -fsSL https://pixi.sh/install.sh | bash   # if pixi isn't installed
pixi install --locked
pixi run python -c "import gdsfactory as gf; print(gf.__version__)"
```

Run the bundled tests (the component-inspect suite) with:

```bash
pixi run test
```

## Agentic workflow

The repository is set up so that a Claude Code session can, with no manual steps:

- **Get a working environment.** In cloud sessions the SessionStart hook installs and
  activates the locked pixi environment. Locally, run `pixi install --locked` once.
- **Understand the code.** The `pixi-pyright` plugin gives the agent language-server
  diagnostics and type information that match the installed gdsfactory version.
- **Check the layout.** After creating or changing a component, the agent loads it with
  `component-inspect` and inspects it through the MCP tools: overviews, zooms, cuts,
  DRC-style checks and before/after diffs. Every result has images plus a precise text
  summary, so dimensions are measured rather than guessed. See
  [`.claude/skills/component-inspect/SKILL.md`](.claude/skills/component-inspect/SKILL.md).

A typical loop: describe the component you want, let the agent write it, have it
inspect and check the result, then review the rendered images and the diff yourself.

## Keeping forks up to date

To pull improvements to the base tooling into a fork:

```bash
git remote add base https://github.com/inwwin/gds-base.git
git fetch base
git merge base/main
```

Conflicts usually come up only in `pixi.toml` and `pixi.lock`. Resolve `pixi.toml` by
hand, then regenerate the lockfile with `pixi install` instead of editing it.
