#!/bin/bash
# Start pyright-langserver inside the project's pixi environment, so it
# resolves imports against the env's Python without the env being activated
# in the shell that launched Claude Code.
set -euo pipefail

# The pixi installer doesn't always put pixi on PATH
PATH="$PATH:$HOME/.pixi/bin"

exec pixi run --locked --manifest-path "$CLAUDE_PROJECT_DIR/pixi.toml" \
  pyright-langserver --stdio
