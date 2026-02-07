#!/usr/bin/env bash
set -euo pipefail

# systemd does not run a login shell, so ~/.profile (nvm PATH, etc.) is not loaded.
# Ensure Node.js tooling (node/npm/npx) is available for MCP stdio servers.
if [ -d "/root/.nvm/versions/node/current/bin" ]; then
  case ":${PATH:-}:" in
    *:/root/.nvm/versions/node/current/bin:*)
      ;;
    *)
      export PATH="/root/.nvm/versions/node/current/bin:${PATH:-}"
      ;;
  esac
fi

# uv/uvx are installed here in this environment; prepend if missing.
case ":${PATH:-}:" in
  *:/root/.local/bin:*)
    ;;
  *)
    export PATH="/root/.local/bin:${PATH:-}"
    ;;
esac

cd /root/clankops

uv_bin="${UV_BIN:-/root/.local/bin/uv}"
clankops_args="${CLANKOPS_ARGS:---telegram}"

read -r -a args <<<"$clankops_args"

exec "$uv_bin" run python main.py "${args[@]}"
