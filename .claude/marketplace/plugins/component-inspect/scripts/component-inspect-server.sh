#!/bin/bash
# Start the component-inspect MCP server (stdio) inside the project's pixi environment.
# stdout carries MCP; pixi and the server log to stderr.
set -euo pipefail

# The pixi installer doesn't always put pixi on PATH
PATH="$PATH:$HOME/.pixi/bin"

# In a fresh cloud session the SessionStart hook may still be installing pixi.
for _ in $(seq 60); do
  command -v pixi >/dev/null 2>&1 && break
  sleep 1
done

exec pixi run --locked --manifest-path "$CLAUDE_PROJECT_DIR/pixi.toml" \
  component-inspect serve
