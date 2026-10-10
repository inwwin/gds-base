#!/bin/bash
set -euo pipefail

# Only run in Claude Code cloud sessions
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Install pixi if the container image doesn't ship it
if ! command -v pixi >/dev/null 2>&1; then
  curl -fsSL https://pixi.sh/install.sh | PIXI_NO_PATH_UPDATE=1 bash
  export PATH="$HOME/.pixi/bin:$PATH"
  echo "export PATH=\"$HOME/.pixi/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

# Install the environment exactly as locked in pixi.lock
pixi install --locked

# Activate the default environment for the rest of the session
pixi shell-hook --locked >> "$CLAUDE_ENV_FILE"
