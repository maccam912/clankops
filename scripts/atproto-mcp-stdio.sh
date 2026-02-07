#!/usr/bin/env bash
set -euo pipefail

# Work around a bug in atproto-mcp's bin wrapper: when executed via the usual
# symlink in PATH (or via npx), its "run main if executed directly" check can
# fail and it exits immediately.
#
# Running the resolved JS entrypoint with node makes argv[1] match import.meta.url.

# atproto-mcp currently logs INFO/WARN to stdout, which corrupts MCP stdio JSON-RPC.
# Force ERROR-only logging by default so stdout is reserved for protocol messages.
export LOG_LEVEL="${LOG_LEVEL:-ERROR}"

bin_path="$(command -v atproto-mcp || true)"

if [[ -n "${bin_path}" ]]; then
  real_entry="$(realpath "${bin_path}")"
  export ATPROTO_MCP_CLI_PATH="${real_entry}"
  exec node "./scripts/atproto-mcp-stdio.mjs" "$@"
fi

if [[ -f "./node_modules/atproto-mcp/dist/cli.js" ]]; then
  export ATPROTO_MCP_CLI_PATH="$(realpath "./node_modules/atproto-mcp/dist/cli.js")"
  exec node "./scripts/atproto-mcp-stdio.mjs" "$@"
fi

global_root="$(npm root -g 2>/dev/null || true)"
if [[ -n "${global_root}" && -f "${global_root}/atproto-mcp/dist/cli.js" ]]; then
  export ATPROTO_MCP_CLI_PATH="$(realpath "${global_root}/atproto-mcp/dist/cli.js")"
  exec node "./scripts/atproto-mcp-stdio.mjs" "$@"
fi

echo "Error: atproto-mcp not found. Install it with: npm i -g atproto-mcp" >&2
exit 1
