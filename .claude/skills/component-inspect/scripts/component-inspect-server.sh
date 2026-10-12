#!/bin/bash
# Start the component-inspect MCP server (stdio) inside the project's pixi or uv environment.
# stdout carries MCP; pixi/uv and the server log to stderr.
# The project must have component-inspect as a dependency: see
# ../diagnostic.md.
set -euo pipefail

# The pixi and uv installers don't always put them on PATH
PATH="$PATH:$HOME/.pixi/bin:$HOME/.local/bin:$HOME/.cargo/bin"

# Started from the project's .mcp.json, CLAUDE_PROJECT_DIR may be unset: fall back to
# the repo root this script sits in (.claude/skills/component-inspect/scripts).
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../../../.." && pwd)}"
cd "$PROJECT_DIR"  # the server keeps its socket under build/ in the working directory

# Pick the project's environment manager; COMPONENT_INSPECT_RUNNER=pixi|uv overrides.
RUNNER="${COMPONENT_INSPECT_RUNNER:-}"
if [ -z "$RUNNER" ]; then
  if [ -f pixi.toml ] || grep -qs '^\[tool\.pixi' pyproject.toml; then
    RUNNER=pixi
  elif [ -f uv.lock ] || [ -f pyproject.toml ]; then
    RUNNER=uv
  else
    echo "component-inspect: no pixi.toml or pyproject.toml in $PROJECT_DIR;" \
      "set COMPONENT_INSPECT_RUNNER=pixi|uv" >&2
    exit 1
  fi
fi

# In a fresh cloud session the SessionStart hook may still be installing the tool.
for _ in $(seq 60); do
  command -v "$RUNNER" >/dev/null 2>&1 && break
  sleep 1
done

case "$RUNNER" in
  pixi)
    MANIFEST=pixi.toml
    [ -f pixi.toml ] || MANIFEST=pyproject.toml
    exec pixi run --locked --manifest-path "$PROJECT_DIR/$MANIFEST" \
      component-inspect serve
    ;;
  uv)
    LOCKED=()
    [ -f uv.lock ] && LOCKED=(--locked)
    exec uv run "${LOCKED[@]}" --project "$PROJECT_DIR" \
      component-inspect serve
    ;;
  *)
    echo "component-inspect: unknown COMPONENT_INSPECT_RUNNER '$RUNNER' (pixi or uv)" >&2
    exit 1
    ;;
esac
