# MCP Servers

This project can use MCP (Model Context Protocol) servers from the `standard` state.

## Config Discovery

The standard state loads MCP server definitions from these locations (later overrides earlier):

- `mcp_servers.json`
- `.mcp/servers.json`
- `.mcp/servers.local.json`

Or set `MCP_SERVERS_PATH` to point to a JSON file to override discovery.

## Format

The expected format matches the common Claude Desktop style:

```json
{
  "mcpServers": {
    "my-server": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "some-mcp-server"],
      "env": { "FOO": "bar" },
      "cwd": "."
    }
  }
}
```

Supported transports:

- `stdio`: spawn a local process and talk over stdin/stdout
- `sse`: connect to a remote SSE MCP endpoint
- `http`: connect to a streamable HTTP MCP endpoint

## Agent Tools

From the standard agent you can:

- `list_mcp_servers()`, `reload_mcp_servers()`
- `mcp_connect(server_name)`, `mcp_disconnect(server_name)`
- `mcp_list_tools(server_name)`, `mcp_call_tool(server_name, tool_name, arguments)`
- `mcp_list_resources(server_name)`, `mcp_read_resource(server_name, uri)`

## Bluesky (AT Protocol) MCP

This repo is set up to run a local AT Protocol MCP server over stdio using:

- npm package: `atproto-mcp` (repo: `cameronrye/atproto-mcp`)
- config: `.mcp/servers.local.json` (gitignored)

### What You Need To Provide

In your `.env` (see `.env.example`), set:

- `ATPROTO_IDENTIFIER`: your handle/email/DID (for example `alice.bsky.social`)
- `ATPROTO_PASSWORD`: a Bluesky *App Password* (recommended) rather than your account password

### Install

Install the server once (recommended):

```bash
npm i -g atproto-mcp
```

### How clankops Connects

The `standard` state loads MCP server configs from:

1. `mcp_servers.json`
2. `.mcp/servers.json`
3. `.mcp/servers.local.json`

Then it can spawn stdio servers and talk MCP over stdin/stdout. You can control this from the agent with:

- `list_mcp_servers()`
- `mcp_connect("bluesky")`
- `mcp_list_tools("bluesky")`
- `mcp_call_tool("bluesky", "<tool-name>", { ... })`

Example calls (tool names come from the server; run `mcp_list_tools` to confirm):

```json
{ "server_name": "bluesky", "tool_name": "create_post", "arguments": { "text": "hello from MCP" } }
```

### Troubleshooting

- If the server "exits immediately" when launched via `npx` or a PATH shim, `.mcp/servers.local.json` uses `./scripts/atproto-mcp-stdio.sh` to run the resolved entrypoint with `node`.
- If you see JSON-RPC parse errors, ensure `LOG_LEVEL=ERROR` for the atproto-mcp process (the wrapper script forces this by default so stdout is reserved for MCP messages).
- If tool calls fail with `Invalid literal value, expected "extract_media_from_post"`: this is a known atproto-mcp bug where only one `tools/call` handler is effectively registered. `./scripts/atproto-mcp-stdio.sh` runs `./scripts/atproto-mcp-stdio.mjs`, which patches the server at startup to dispatch `tools/call` by `params.name`.
