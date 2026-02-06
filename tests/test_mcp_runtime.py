import json
from pathlib import Path

import pytest


def test_load_mcp_server_configs_from_explicit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_path = tmp_path / "mcp.json"
    cfg_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {
                        "transport": "stdio",
                        "command": "python",
                        "args": ["-c", "print('hello')"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_SERVERS_PATH", str(cfg_path))

    import mcp_runtime

    sources, configs = mcp_runtime.load_mcp_server_configs(Path.cwd())
    assert len(sources) == 1
    assert sources[0].name == "mcp.json"
    assert "demo" in configs
    assert configs["demo"].transport == "stdio"


def test_standard_state_includes_mcp_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg_path = tmp_path / "mcp.json"
    cfg_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {
                        "transport": "sse",
                        "url": "http://example.invalid/sse",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_SERVERS_PATH", str(cfg_path))

    from states.standard import create_standard_state

    state = create_standard_state()
    tool_names = set(state.agent._function_toolset.tools.keys())
    assert "list_mcp_servers" in tool_names
    assert "reload_mcp_servers" in tool_names
    assert "mcp_connect" in tool_names
    assert "mcp_disconnect" in tool_names
    assert "mcp_list_tools" in tool_names
    assert "mcp_call_tool" in tool_names
    assert "mcp_list_resources" in tool_names
    assert "mcp_read_resource" in tool_names

    prompt = "\n".join(state.agent._system_prompts).lower()
    assert "mcp servers" in prompt
    assert "demo" in prompt

