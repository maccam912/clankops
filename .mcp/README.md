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

