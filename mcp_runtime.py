from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


Transport = Literal["stdio", "sse", "http"]


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: Transport

    # stdio
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None

    # sse/http
    url: str | None = None
    headers: dict[str, Any] | None = None
    timeout_seconds: float | None = None
    sse_read_timeout_seconds: float | None = None

    # http
    terminate_on_close: bool = True


def _dump(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, list):
        return [_dump(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _dump(v) for k, v in obj.items()}
    return str(obj)


def _json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _resolve_cwd(workspace_root: Path, cwd: str | None) -> str | None:
    if cwd is None:
        return None
    raw = cwd.strip()
    if not raw:
        return None
    p = Path(raw)
    resolved = p.resolve() if p.is_absolute() else (workspace_root / p).resolve()
    return str(resolved)


def _config_search_paths(workspace_root: Path) -> list[Path]:
    # Precedence: lowest to highest, later files override earlier ones.
    return [
        (workspace_root / "mcp_servers.json"),
        (workspace_root / ".mcp" / "servers.json"),
        (workspace_root / ".mcp" / "servers.local.json"),
    ]


def load_mcp_server_configs(workspace_root: Path) -> tuple[list[Path], dict[str, McpServerConfig]]:
    """Load MCP server configs from well-known locations.

    You can override the location entirely with MCP_SERVERS_PATH.
    """
    explicit = os.environ.get("MCP_SERVERS_PATH", "").strip()
    sources: list[Path] = []
    merged: dict[str, Any] = {}

    if explicit:
        p = Path(explicit)
        p = p.resolve() if p.is_absolute() else (workspace_root / p).resolve()
        if not p.exists():
            return [p], {}
        sources = [p]
        merged = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    else:
        for path in _config_search_paths(workspace_root):
            if not path.exists() or not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            sources.append(path)
            # Merge common Claude-style structure: { "mcpServers": { ... } }
            if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
                merged.setdefault("mcpServers", {})
                merged["mcpServers"].update(data["mcpServers"])
            else:
                # If user provides a plain dict, merge top-level.
                if isinstance(data, dict):
                    merged.update(data)

    raw_servers = {}
    if isinstance(merged, dict) and isinstance(merged.get("mcpServers"), dict):
        raw_servers = merged["mcpServers"]
    elif isinstance(merged, dict) and isinstance(merged.get("servers"), dict):
        raw_servers = merged["servers"]
    elif isinstance(merged, dict):
        # Accept top-level mapping { "<name>": { ... } }.
        raw_servers = {k: v for k, v in merged.items() if isinstance(v, dict)}

    configs: dict[str, McpServerConfig] = {}
    for name, raw in raw_servers.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            continue
        server_name = name.strip()
        if not server_name:
            continue

        transport: Transport
        raw_transport = str(raw.get("transport", "")).strip().lower()
        if raw_transport in ("stdio", "sse", "http"):
            transport = raw_transport  # type: ignore[assignment]
        else:
            # Infer transport if not specified.
            transport = "stdio" if raw.get("command") else "sse"

        cfg = McpServerConfig(
            name=server_name,
            transport=transport,
            command=str(raw.get("command", "")).strip() or None,
            args=list(raw.get("args", []) or []),
            env=dict(raw.get("env", {}) or {}) or None,
            cwd=str(raw.get("cwd", "")).strip() or None,
            url=str(raw.get("url", "")).strip() or None,
            headers=dict(raw.get("headers", {}) or {}) or None,
            timeout_seconds=float(raw["timeout_seconds"]) if "timeout_seconds" in raw else None,
            sse_read_timeout_seconds=(
                float(raw["sse_read_timeout_seconds"]) if "sse_read_timeout_seconds" in raw else None
            ),
            terminate_on_close=bool(raw.get("terminate_on_close", True)),
        )

        if cfg.transport == "stdio" and not cfg.command:
            # Invalid entry; skip.
            continue
        if cfg.transport in ("sse", "http") and not cfg.url:
            continue

        configs[server_name] = cfg

    return sources, configs


@dataclass
class _McpConnection:
    config: McpServerConfig
    session: ClientSession
    close: Any  # callable to close the underlying stack
    server_info: Any


class McpRuntime:
    """Manages MCP server discovery and persistent connections for the process."""

    def __init__(self, workspace_root: Path):
        self._workspace_root = workspace_root.resolve()
        self._sources: list[Path] = []
        self._configs: dict[str, McpServerConfig] = {}
        self._connections: dict[str, _McpConnection] = {}
        self._lock = asyncio.Lock()
        self.reload()

    def reload(self) -> dict[str, Any]:
        self._sources, self._configs = load_mcp_server_configs(self._workspace_root)
        # Drop connections for servers that were removed.
        for name in list(self._connections.keys()):
            if name not in self._configs:
                self._connections.pop(name, None)
        return {
            "ok": True,
            "sources": [str(p) for p in self._sources],
            "server_count": len(self._configs),
            "servers": sorted(self._configs.keys()),
        }

    def list_servers(self) -> dict[str, Any]:
        return {
            "ok": True,
            "sources": [str(p) for p in self._sources],
            "servers": [
                {
                    "name": cfg.name,
                    "transport": cfg.transport,
                    "command": cfg.command,
                    "args": cfg.args or [],
                    "url": cfg.url,
                    "connected": cfg.name in self._connections,
                }
                for cfg in self._configs.values()
            ],
        }

    async def _connect(self, cfg: McpServerConfig) -> _McpConnection:
        # Use an async exit stack so the connection stays open until explicitly closed.
        stack = contextlib.AsyncExitStack()
        try:
            if cfg.transport == "stdio":
                params = StdioServerParameters(
                    command=str(cfg.command or "").strip(),
                    args=list(cfg.args or []),
                    env=dict(cfg.env or {}) or None,
                    cwd=_resolve_cwd(self._workspace_root, cfg.cwd),
                    encoding="utf-8",
                    encoding_error_handler="replace",
                )
                read, write = await stack.enter_async_context(stdio_client(params))

            elif cfg.transport == "sse":
                timeout = float(cfg.timeout_seconds) if cfg.timeout_seconds is not None else 5.0
                sse_read_timeout = (
                    float(cfg.sse_read_timeout_seconds)
                    if cfg.sse_read_timeout_seconds is not None
                    else 60.0 * 5
                )
                read, write = await stack.enter_async_context(
                    sse_client(
                        url=str(cfg.url),
                        headers=dict(cfg.headers or {}) or None,
                        timeout=timeout,
                        sse_read_timeout=sse_read_timeout,
                    )
                )

            else:  # http (streamable http)
                timeout = float(cfg.timeout_seconds) if cfg.timeout_seconds is not None else 30.0
                sse_read_timeout = (
                    float(cfg.sse_read_timeout_seconds)
                    if cfg.sse_read_timeout_seconds is not None
                    else 60.0 * 5
                )
                http_client = create_mcp_http_client(
                    headers=dict(cfg.headers or {}) or None,
                    timeout=httpx.Timeout(timeout, read=sse_read_timeout),
                )
                await stack.enter_async_context(http_client)
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(
                        url=str(cfg.url),
                        http_client=http_client,
                        terminate_on_close=bool(cfg.terminate_on_close),
                    )
                )

            session = await stack.enter_async_context(ClientSession(read, write))
            init = await session.initialize()
            return _McpConnection(
                config=cfg,
                session=session,
                close=stack.aclose,
                server_info=getattr(init, "serverInfo", None),
            )
        except Exception:
            await stack.aclose()
            raise

    async def ensure_connected(self, server_name: str) -> dict[str, Any]:
        name = (server_name or "").strip()
        if not name:
            raise ValueError("server_name is required.")

        async with self._lock:
            if name in self._connections:
                return {"ok": True, "server": name, "connected": True, "reused": True}
            cfg = self._configs.get(name)
            if cfg is None:
                raise ValueError(f"Unknown MCP server: {name}")
            conn = await self._connect(cfg)
            self._connections[name] = conn
            return {
                "ok": True,
                "server": name,
                "connected": True,
                "reused": False,
                "server_info": _dump(conn.server_info),
            }

    async def disconnect(self, server_name: str) -> dict[str, Any]:
        name = (server_name or "").strip()
        if not name:
            raise ValueError("server_name is required.")

        async with self._lock:
            conn = self._connections.pop(name, None)
        if conn is None:
            return {"ok": True, "server": name, "disconnected": False, "reason": "not connected"}
        await conn.close()
        return {"ok": True, "server": name, "disconnected": True}

    async def list_tools(self, server_name: str) -> dict[str, Any]:
        name = (server_name or "").strip()
        await self.ensure_connected(name)
        session = self._connections[name].session
        result = await session.list_tools()
        payload = _dump(result)
        return {"ok": True, "server": name, "result": payload}

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        max_result_chars: int = 12_000,
    ) -> dict[str, Any]:
        name = (server_name or "").strip()
        await self.ensure_connected(name)
        session = self._connections[name].session
        safe_max = max(500, min(int(max_result_chars), 100_000))
        result = await session.call_tool(name=tool_name, arguments=arguments or None)
        dumped = _dump(result)

        # Best-effort truncation for very large payloads (especially text content).
        truncated = False
        try:
            raw = _json(dumped)
            if len(raw) > safe_max:
                truncated = True
                if isinstance(dumped, dict) and isinstance(dumped.get("content"), list):
                    clipped: list[Any] = []
                    remaining = safe_max
                    for item in dumped["content"]:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "text" and isinstance(item.get("text"), str):
                            text = item["text"]
                            keep = max(0, min(len(text), remaining))
                            item = dict(item)
                            item["text"] = text[:keep]
                            clipped.append(item)
                            remaining -= keep
                            if remaining <= 0:
                                break
                        else:
                            clipped.append(item)
                    dumped = dict(dumped)
                    dumped["content"] = clipped
        except Exception:
            # If truncation fails, still return the full dumped result.
            pass

        return {
            "ok": True,
            "server": name,
            "tool": tool_name,
            "truncated": truncated,
            "result": dumped,
        }

    async def list_resources(self, server_name: str) -> dict[str, Any]:
        name = (server_name or "").strip()
        await self.ensure_connected(name)
        session = self._connections[name].session
        result = await session.list_resources()
        return {"ok": True, "server": name, "result": _dump(result)}

    async def read_resource(self, server_name: str, uri: str, *, max_result_chars: int = 12_000) -> dict[str, Any]:
        name = (server_name or "").strip()
        await self.ensure_connected(name)
        session = self._connections[name].session
        safe_max = max(500, min(int(max_result_chars), 100_000))
        result = await session.read_resource(uri=uri)
        dumped = _dump(result)
        truncated = False
        try:
            raw = _json(dumped)
            if len(raw) > safe_max:
                truncated = True
                raw = raw[:safe_max]
                dumped = {"_raw_truncated_json": raw}
        except Exception:
            pass
        return {"ok": True, "server": name, "uri": uri, "truncated": truncated, "result": dumped}

    def format_for_system_prompt(self, *, max_chars: int = 1200) -> str:
        header = (
            "\n\nMCP SERVERS\n"
            "MCP servers can provide extra tools/resources. Configure them via:\n"
            "- `.mcp/servers.json` (committable)\n"
            "- `.mcp/servers.local.json` (local overrides)\n"
            "- `mcp_servers.json` (legacy root)\n"
            "Or set `MCP_SERVERS_PATH` to a JSON file.\n"
            "Use `list_mcp_servers()` to see what's configured, then `mcp_connect(name)` and `mcp_call_tool(...)`.\n\n"
            "Configured servers:\n"
        )

        if not self._configs:
            return header + "- (none configured)\n"

        used = 0
        lines: list[str] = []
        for cfg in sorted(self._configs.values(), key=lambda c: c.name.lower()):
            suffix = ""
            if cfg.transport == "stdio":
                suffix = f" cmd={cfg.command}"
            else:
                suffix = f" url={cfg.url}"
            line = f"- {cfg.name}: {cfg.transport}{suffix}\n"
            if used + len(line) > max_chars:
                lines.append("- ... (truncated)\n")
                break
            lines.append(line)
            used += len(line)
        return header + "".join(lines)
