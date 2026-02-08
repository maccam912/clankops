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


def _build_stdio_env(overrides: dict[str, Any] | None) -> dict[str, str]:
    """Build the environment for stdio MCP servers.

    The upstream MCP stdio transport inherits only a small "safe" set of env vars by
    default. For local dev, clankops expects MCP servers to see the current process
    environment (including variables loaded from `.env`) while still allowing per-server
    overrides.
    """
    merged: dict[str, str] = dict(os.environ)
    if not overrides:
        return merged

    for k, v in overrides.items():
        key = str(k).strip()
        if not key:
            continue
        if v is None:
            merged.pop(key, None)
            continue
        merged[key] = str(v)
    return merged


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
        # NOTE: MCP's stdio transport uses AnyIO cancel scopes that must be exited
        # from the same task they were entered in. pydantic-ai (and asyncio in
        # general) may call these methods from different tasks over the lifetime
        # of a process. To avoid "Attempted to exit cancel scope in a different
        # task than it was entered in" during disconnect/shutdown, all connection
        # lifecycle operations are serialized through a dedicated actor task.
        self._actor_task: asyncio.Task[None] | None = None
        self._actor_queue: asyncio.Queue[tuple[str, tuple[Any, ...], dict[str, Any], asyncio.Future[Any]]] | None = None
        self.reload()

    def reload(self) -> dict[str, Any]:
        self._sources, self._configs = load_mcp_server_configs(self._workspace_root)
        # Intentionally do NOT mutate/close live connections here.
        # Connections are owned by the actor task; tool calls can still use them.
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

    async def _ensure_actor(self) -> None:
        if self._actor_task is not None and not self._actor_task.done():
            return
        self._actor_queue = asyncio.Queue()
        self._actor_task = asyncio.create_task(self._actor_main(), name="mcp-runtime")

    async def _actor_call(self, op: str, *args: Any, **kwargs: Any) -> Any:
        await self._ensure_actor()
        assert self._actor_queue is not None
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._actor_queue.put((op, args, kwargs, fut))
        return await fut

    async def _actor_main(self) -> None:
        assert self._actor_queue is not None
        try:
            while True:
                op, args, kwargs, fut = await self._actor_queue.get()
                if fut.cancelled():
                    continue
                try:
                    if op == "stop":
                        fut.set_result(True)
                        break
                    if op == "ensure_connected":
                        res = await self._ensure_connected_impl(*args, **kwargs)
                    elif op == "disconnect":
                        res = await self._disconnect_impl(*args, **kwargs)
                    elif op == "list_tools":
                        res = await self._list_tools_impl(*args, **kwargs)
                    elif op == "call_tool":
                        res = await self._call_tool_impl(*args, **kwargs)
                    elif op == "list_resources":
                        res = await self._list_resources_impl(*args, **kwargs)
                    elif op == "read_resource":
                        res = await self._read_resource_impl(*args, **kwargs)
                    elif op == "disconnect_all":
                        res = await self._disconnect_all_impl()
                    else:
                        raise ValueError(f"Unknown MCP op: {op}")
                    fut.set_result(res)
                except Exception as exc:
                    fut.set_exception(exc)
        finally:
            # Best-effort cleanup even if the actor is cancelled.
            try:
                await self._disconnect_all_impl()
            except Exception:
                pass

    async def _connect(self, cfg: McpServerConfig) -> _McpConnection:
        # Use an async exit stack so the connection stays open until explicitly closed.
        stack = contextlib.AsyncExitStack()
        try:
            if cfg.transport == "stdio":
                params = StdioServerParameters(
                    command=str(cfg.command or "").strip(),
                    args=list(cfg.args or []),
                    env=_build_stdio_env(cfg.env),
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

    async def _ensure_connected_impl(self, server_name: str) -> dict[str, Any]:
        name = (server_name or "").strip()
        if not name:
            raise ValueError("server_name is required.")

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

    async def ensure_connected(self, server_name: str) -> dict[str, Any]:
        return await self._actor_call("ensure_connected", server_name)

    async def _disconnect_impl(self, server_name: str) -> dict[str, Any]:
        name = (server_name or "").strip()
        if not name:
            raise ValueError("server_name is required.")

        conn = self._connections.pop(name, None)
        if conn is None:
            return {"ok": True, "server": name, "disconnected": False, "reason": "not connected"}
        await conn.close()
        return {"ok": True, "server": name, "disconnected": True}

    async def disconnect(self, server_name: str) -> dict[str, Any]:
        return await self._actor_call("disconnect", server_name)

    async def _list_tools_impl(self, server_name: str) -> dict[str, Any]:
        name = (server_name or "").strip()
        await self._ensure_connected_impl(name)
        session = self._connections[name].session
        result = await session.list_tools()
        payload = _dump(result)
        return {"ok": True, "server": name, "result": payload}

    async def list_tools(self, server_name: str) -> dict[str, Any]:
        return await self._actor_call("list_tools", server_name)

    async def _call_tool_impl(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        max_result_chars: int = 12_000,
    ) -> dict[str, Any]:
        name = (server_name or "").strip()
        await self._ensure_connected_impl(name)
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

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        max_result_chars: int = 12_000,
    ) -> dict[str, Any]:
        return await self._actor_call(
            "call_tool",
            server_name,
            tool_name,
            arguments,
            max_result_chars=max_result_chars,
        )

    async def _list_resources_impl(self, server_name: str) -> dict[str, Any]:
        name = (server_name or "").strip()
        await self._ensure_connected_impl(name)
        session = self._connections[name].session
        result = await session.list_resources()
        return {"ok": True, "server": name, "result": _dump(result)}

    async def list_resources(self, server_name: str) -> dict[str, Any]:
        return await self._actor_call("list_resources", server_name)

    async def _read_resource_impl(
        self, server_name: str, uri: str, *, max_result_chars: int = 12_000
    ) -> dict[str, Any]:
        name = (server_name or "").strip()
        await self._ensure_connected_impl(name)
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

    async def read_resource(self, server_name: str, uri: str, *, max_result_chars: int = 12_000) -> dict[str, Any]:
        return await self._actor_call("read_resource", server_name, uri, max_result_chars=max_result_chars)

    async def _disconnect_all_impl(self) -> None:
        conns = list(self._connections.items())
        self._connections.clear()
        for _, conn in conns:
            try:
                await conn.close()
            except Exception:
                pass

    async def aclose(self) -> None:
        """Close all open connections and stop the actor task."""
        if self._actor_task is None:
            return
        if self._actor_task.done():
            return
        try:
            await self._actor_call("disconnect_all")
            await self._actor_call("stop")
        finally:
            try:
                await self._actor_task
            except Exception:
                pass

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
        notes: list[str] = []
        if "bluesky" in self._configs:
            notes.append(
                "\nNotes:\n"
                "- bluesky (atproto-mcp): auth is automatic from env vars `ATPROTO_IDENTIFIER` and `ATPROTO_PASSWORD` "
                "(Bluesky App Password). If those are set, you generally do not need OAuth tools.\n"
            )
        return header + "".join(lines) + "".join(notes)
