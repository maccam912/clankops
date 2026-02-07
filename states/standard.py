import html
import json
import os
import random
import re
import sqlite3
import subprocess
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from debug_auth import get_openrouter_api_key
from machine import SessionContext, StateConfig
import mcp_runtime
import scheduler_utils
import skills_store
from tool_utils import safe_tool

IDLE_TIMEOUT = float(os.environ.get("IDLE_TIMEOUT", "30"))
WORKSPACE_ROOT = Path.cwd().resolve()
MAX_FILE_BYTES = int(os.environ.get("TOOL_MAX_FILE_BYTES", "1048576"))
MAX_TOOL_CHARS = int(os.environ.get("TOOL_MAX_CHARS", "12000"))
WEB_TIMEOUT_SECONDS = float(os.environ.get("WEB_TIMEOUT_SECONDS", "15"))
WEB_USER_AGENT = "clankops-agent/0.1 (+https://example.local)"
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng.k3s.koski.co").strip().rstrip("/")

def _coerce_json_object(value: object | None) -> dict[str, object] | None:
    """Accept either a dict or a JSON object string and return a dict.

    Pydantic tool-arg validation happens before `safe_tool()` can catch errors.
    Some models emit JSON as a string for object-typed tool params; accept and
    parse that here to avoid aborting the whole run.
    """

    if value is None:
        return None
    if isinstance(value, dict):
        # Coerce keys to str to match MCP's expectation.
        return {str(k): v for k, v in value.items()}
    if isinstance(value, str):
        raw = value.strip()
        if not raw or raw.lower() in ("null", "none"):
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as err:
            raise ValueError("arguments must be a JSON object (e.g. {'limit': 5}).") from err
        if parsed is None:
            return None
        if not isinstance(parsed, dict):
            raise ValueError("arguments must be a JSON object (not a list/string/number).")
        return {str(k): v for k, v in parsed.items()}
    raise TypeError("arguments must be an object/dict or a JSON object string.")


class _DuckDuckGoResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_result_link = False
        self._href = ""
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attrs_map = {k: v or "" for k, v in attrs}
        if tag == "a" and "result__a" in attrs_map.get("class", ""):
            self._in_result_link = True
            self._href = attrs_map.get("href", "")
            self._text_parts = []

    def handle_data(self, data: str):
        if self._in_result_link:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str):
        if tag != "a" or not self._in_result_link:
            return

        title = " ".join("".join(self._text_parts).split()).strip()
        url = _decode_ddg_redirect(self._href)
        if title and url:
            self.results.append({"title": title, "url": url})

        self._in_result_link = False
        self._href = ""
        self._text_parts = []


def _json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _decode_ddg_redirect(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        encoded = parse_qs(parsed.query).get("uddg", [""])[0]
        if encoded:
            return unquote(encoded)
    return url


def _resolve_workspace_path(raw_path: str, *, allow_outside_workspace: bool = False) -> Path:
    user_path = Path(raw_path.strip() or ".")
    candidate = user_path.resolve() if user_path.is_absolute() else (WORKSPACE_ROOT / user_path).resolve()
    if allow_outside_workspace:
        return candidate
    if candidate == WORKSPACE_ROOT or WORKSPACE_ROOT in candidate.parents:
        return candidate
    raise ValueError(f"Path must stay inside workspace: {WORKSPACE_ROOT}")


def _to_workspace_relative(path: Path) -> str:
    try:
        return str(path.relative_to(WORKSPACE_ROOT))
    except ValueError:
        return str(path)


def _read_text(path: Path, max_chars: int) -> str:
    if not path.exists():
        raise FileNotFoundError(f"File not found: {_to_workspace_relative(path)}")
    if not path.is_file():
        raise IsADirectoryError(f"Expected file, got directory: {_to_workspace_relative(path)}")

    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(
            f"File too large ({size} bytes). Max supported size is {MAX_FILE_BYTES} bytes."
        )

    text = path.read_text(encoding="utf-8", errors="replace")
    text = text[:max_chars]
    return text


def _fetch_url(
    url: str, max_chars: int, strip_html_content: bool = True
) -> dict[str, str | int | bool]:
    request = Request(url, headers={"User-Agent": WEB_USER_AGENT})
    try:
        with urlopen(request, timeout=WEB_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            content_type = response.headers.get("Content-Type", "")
            charset = response.headers.get_content_charset() or "utf-8"
            data = response.read(MAX_TOOL_CHARS * 4 + 1)
            truncated = len(data) > MAX_TOOL_CHARS * 4
            if truncated:
                data = data[: MAX_TOOL_CHARS * 4]
            text = data.decode(charset, errors="replace")
            error: str | None = None
    except HTTPError as err:
        # HTTPError is also a response-like object (may include a body).
        status = int(getattr(err, "code", 0) or 0)
        headers = getattr(err, "headers", None)
        content_type = headers.get("Content-Type", "") if headers is not None else ""
        try:
            charset = headers.get_content_charset() if headers is not None else None
        except Exception:
            charset = None
        charset = charset or "utf-8"
        try:
            data = err.read(MAX_TOOL_CHARS * 4 + 1) if getattr(err, "fp", None) is not None else b""
        except Exception:
            data = b""
        truncated = len(data) > MAX_TOOL_CHARS * 4
        if truncated:
            data = data[: MAX_TOOL_CHARS * 4]
        text = data.decode(charset, errors="replace")
        reason = getattr(err, "reason", "") or ""
        error = f"HTTPError {status}: {reason}".strip()
    except URLError as err:
        status = 0
        content_type = ""
        truncated = False
        text = ""
        error = f"URLError: {getattr(err, 'reason', err)}"
    except Exception as err:
        status = 0
        content_type = ""
        truncated = False
        text = ""
        error = f"{err.__class__.__name__}: {err}"

    if strip_html_content and "text/html" in content_type.lower():
        text = _strip_html(text)

    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    payload: dict[str, str | int | bool] = {
        "url": url,
        "status": status,
        "content_type": content_type,
        "truncated": truncated,
        "content": text,
    }
    if error:
        payload["error"] = error
    return payload


def _strip_html(raw_html: str) -> str:
    without_scripts = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw_html)
    without_tags = re.sub(r"(?s)<[^>]+>", " ", without_scripts)
    unescaped = html.unescape(without_tags)
    compacted = re.sub(r"[ \t\r\f\v]+", " ", unescaped)
    compacted = re.sub(r"\n\s+", "\n", compacted)
    return compacted.strip()


def _looks_like_query(sql: str) -> bool:
    return bool(re.match(r"(?is)^\s*(select|pragma|with)\b", sql))


def _search_searxng(query: str, max_results: int) -> dict[str, object]:
    if not SEARXNG_URL:
        raise ValueError("SEARXNG_URL is not configured.")

    params = urlencode(
        {
            "q": query,
            "format": "json",
            "language": "en-US",
            "safesearch": "0",
        }
    )
    url = f"{SEARXNG_URL}/search?{params}"
    response = _fetch_url(url, MAX_TOOL_CHARS * 4, strip_html_content=False)
    if response.get("error"):
        raise ValueError(f"SearXNG fetch failed: {response['error']}")
    payload = json.loads(str(response["content"]))

    raw_results = payload.get("results", [])
    results: list[dict[str, object]] = []
    for item in raw_results[:max_results]:
        engines = item.get("engines", [])
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
                "engine": ", ".join(engines) if isinstance(engines, list) else str(engines),
                "score": item.get("score"),
            }
        )

    return {
        "source": "searxng",
        "endpoint": SEARXNG_URL,
        "query": query,
        "results": results,
    }


def create_standard_state(enable_dangerzone: bool = False) -> StateConfig:
    base_url = "https://openrouter.ai/api/v1"
    model_name = os.environ.get("MODEL_NAME", "google/gemini-2.0-flash-exp:free").strip()
    api_key = get_openrouter_api_key()

    provider = OpenAIProvider(
        base_url=base_url,
        api_key=api_key,
    )
    model = OpenAIModel(
        model_name,
        provider=provider,
    )

    allow_outside_workspace = bool(enable_dangerzone)

    skills_section = ""
    try:
        skills_section = skills_store.format_skills_for_system_prompt(WORKSPACE_ROOT)
    except Exception:
        # Skills are optional; prompt should still work if the directory is missing/misconfigured.
        skills_section = (
            "\n\nSKILLS\n"
            "(Skills unavailable: could not load SKILLS_DIR. You can configure SKILLS_DIR to a workspace-relative "
            "path, e.g. 'skills'.)\n"
        )

    mcp = mcp_runtime.McpRuntime(WORKSPACE_ROOT)
    mcp_section = mcp.format_for_system_prompt()

    agent: Agent[SessionContext, str] = Agent(
        model,
        system_prompt=(
            "You are a helpful assistant with broad tools for local files, web research, and SQLite. "
            "Use tools for factual/structured tasks instead of guessing. "
            "When writing files or running SQL, be explicit about what changed. "
            "If the user wants reflection or memory updates, call switch_to_journaling. "
            "Only call fast_forward_to_journaling if the user explicitly asks you to skip waiting and journal now."
            " Only call schedule_self_message if the user explicitly asks you to schedule a message/reminder to yourself."
            + skills_section
            + mcp_section
        ),
        deps_type=SessionContext,
    )

    @agent.tool
    @safe_tool("get_current_time")
    def get_current_time(ctx: RunContext[SessionContext]) -> str:
        """Get the current local time."""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @agent.tool
    @safe_tool("get_random_number")
    def get_random_number(
        ctx: RunContext[SessionContext], min_val: int = 1, max_val: int = 100
    ) -> str:
        """Get a random integer between min_val and max_val."""
        return str(random.randint(min_val, max_val))

    @agent.tool
    @safe_tool("list_directory")
    def list_directory(
        ctx: RunContext[SessionContext],
        path: str = ".",
        recursive: bool = False,
        max_entries: int = 200,
    ) -> str:
        """List files/directories in the workspace (optionally recursive)."""
        target = _resolve_workspace_path(path, allow_outside_workspace=allow_outside_workspace)
        if not target.exists():
            return f"Path not found: {path}"
        if not target.is_dir():
            return f"Path is not a directory: {path}"

        max_entries = max(1, min(max_entries, 1000))
        entries: list[dict[str, str | int]] = []
        iterator = target.rglob("*") if recursive else target.iterdir()
        for item in iterator:
            stat = item.stat()
            entry_type = "dir" if item.is_dir() else "file"
            entries.append(
                {
                    "path": _to_workspace_relative(item),
                    "type": entry_type,
                    "size_bytes": 0 if item.is_dir() else stat.st_size,
                }
            )
            if len(entries) >= max_entries:
                break
        return _json(entries)

    @agent.tool
    @safe_tool("read_text_file")
    def read_text_file(
        ctx: RunContext[SessionContext], path: str, max_chars: int = MAX_TOOL_CHARS
    ) -> str:
        """Read a UTF-8 text file from the workspace."""
        safe_max_chars = max(1, min(max_chars, 50000))
        file_path = _resolve_workspace_path(path, allow_outside_workspace=allow_outside_workspace)
        return _read_text(file_path, safe_max_chars)

    @agent.tool
    @safe_tool("write_text_file")
    def write_text_file(ctx: RunContext[SessionContext], path: str, content: str) -> str:
        """Write UTF-8 text to a file in the workspace (overwrite)."""
        file_path = _resolve_workspace_path(path, allow_outside_workspace=allow_outside_workspace)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {_to_workspace_relative(file_path)}"

    @agent.tool
    @safe_tool("append_text_file")
    def append_text_file(ctx: RunContext[SessionContext], path: str, content: str) -> str:
        """Append UTF-8 text to a file in the workspace."""
        file_path = _resolve_workspace_path(path, allow_outside_workspace=allow_outside_workspace)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended {len(content)} chars to {_to_workspace_relative(file_path)}"

    @agent.tool
    @safe_tool("search_in_files")
    def search_in_files(
        ctx: RunContext[SessionContext], pattern: str, path: str = ".", max_results: int = 50
    ) -> str:
        """Regex-search text files in the workspace and return matching lines."""
        target = _resolve_workspace_path(path, allow_outside_workspace=allow_outside_workspace)
        if not target.exists():
            return f"Path not found: {path}"

        regex = re.compile(pattern)
        max_results = max(1, min(max_results, 200))
        results: list[dict[str, str | int]] = []
        files = [target] if target.is_file() else list(target.rglob("*"))

        for file_path in files:
            if not file_path.is_file():
                continue
            if file_path.stat().st_size > MAX_FILE_BYTES:
                continue
            try:
                with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line_no, line in enumerate(handle, start=1):
                        if regex.search(line):
                            results.append(
                                {
                                    "path": _to_workspace_relative(file_path),
                                    "line": line_no,
                                    "text": line.strip(),
                                }
                            )
                            if len(results) >= max_results:
                                return _json(results)
            except OSError:
                continue

        return _json(results)

    @agent.tool
    @safe_tool("fetch_url")
    def fetch_url(ctx: RunContext[SessionContext], url: str, max_chars: int = 8000) -> str:
        """Fetch and return web content from a URL."""
        safe_max_chars = max(500, min(max_chars, 50000))
        result = _fetch_url(url, safe_max_chars)
        return _json(result)

    @agent.tool
    @safe_tool("search_web")
    def search_web(ctx: RunContext[SessionContext], query: str, max_results: int = 5) -> str:
        """Search the web via SearXNG JSON API, with DuckDuckGo fallback."""
        safe_max_results = max(1, min(max_results, 10))
        try:
            return _json(_search_searxng(query, safe_max_results))
        except Exception as err:
            url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            response = _fetch_url(url, MAX_TOOL_CHARS * 2, strip_html_content=False)
            parser = _DuckDuckGoResultParser()
            parser.feed(str(response["content"]))
            return _json(
                {
                    "source": "duckduckgo_fallback",
                    "fallback_reason": str(err),
                    "fetch_error": response.get("error"),
                    "results": parser.results[:safe_max_results],
                }
            )

    @agent.tool
    @safe_tool("run_sql")
    def run_sql(
        ctx: RunContext[SessionContext], sql: str, max_rows: int = 200
    ) -> str:
        """Run arbitrary SQL against memory/memory.sqlite3."""
        query = sql.strip()
        if not query:
            return "SQL is empty."

        safe_max_rows = max(1, min(max_rows, 1000))
        db_path = ctx.deps.memory_store.sqlite_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                if _looks_like_query(query):
                    cur.execute(query)
                    rows = cur.fetchmany(safe_max_rows + 1)
                    truncated = len(rows) > safe_max_rows
                    rows = rows[:safe_max_rows]
                    payload = {
                        "ok": True,
                        "db_path": str(db_path),
                        "query": query,
                        "row_count": len(rows),
                        "truncated": truncated,
                        "rows": [dict(row) for row in rows],
                    }
                    return _json(payload)

                if ";" in query.strip().rstrip(";"):
                    cur.executescript(query)
                else:
                    cur.execute(query)
                conn.commit()
                payload = {
                    "ok": True,
                    "db_path": str(db_path),
                    "query": query,
                    "total_changes": conn.total_changes,
                    "status": "ok",
                }
                return _json(payload)
        except sqlite3.Error as err:
            return _json(
                {
                    "ok": False,
                    "db_path": str(db_path),
                    "query": query,
                    "error_type": err.__class__.__name__,
                    "error": str(err),
                }
            )

    @agent.tool
    @safe_tool("describe_sql_schema")
    def describe_sql_schema(ctx: RunContext[SessionContext]) -> str:
        """Show SQLite schema objects from memory/memory.sqlite3."""
        db_path = ctx.deps.memory_store.sqlite_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                WHERE type IN ('table', 'index', 'view', 'trigger')
                ORDER BY type, name
                """
            ).fetchall()
        return _json([dict(row) for row in rows])

    @agent.tool
    @safe_tool("list_mcp_servers")
    def list_mcp_servers(ctx: RunContext[SessionContext]) -> str:
        """Discover configured MCP servers from well-known config locations."""
        _ = ctx
        return json.dumps(mcp.list_servers(), indent=2, ensure_ascii=False)

    @agent.tool
    @safe_tool("reload_mcp_servers")
    def reload_mcp_servers(ctx: RunContext[SessionContext]) -> str:
        """Reload MCP server configuration from disk."""
        _ = ctx
        return json.dumps(mcp.reload(), indent=2, ensure_ascii=False)

    @agent.tool
    @safe_tool("mcp_connect")
    async def mcp_connect(ctx: RunContext[SessionContext], server_name: str) -> str:
        """Connect to an MCP server (kept alive for the session)."""
        _ = ctx
        payload = await mcp.ensure_connected(server_name)
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @agent.tool
    @safe_tool("mcp_disconnect")
    async def mcp_disconnect(ctx: RunContext[SessionContext], server_name: str) -> str:
        """Disconnect from an MCP server."""
        _ = ctx
        payload = await mcp.disconnect(server_name)
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @agent.tool
    @safe_tool("mcp_list_tools")
    async def mcp_list_tools(ctx: RunContext[SessionContext], server_name: str) -> str:
        """List tools exposed by an MCP server."""
        _ = ctx
        payload = await mcp.list_tools(server_name)
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @agent.tool
    @safe_tool("mcp_call_tool")
    async def mcp_call_tool(
        ctx: RunContext[SessionContext],
        server_name: str,
        tool_name: str,
        arguments: dict[str, object] | str | None = None,
        max_result_chars: int = MAX_TOOL_CHARS,
    ) -> str:
        """Call a tool on an MCP server."""
        _ = ctx
        safe_max = max(500, min(int(max_result_chars), 100_000))
        parsed_args = _coerce_json_object(arguments)
        payload = await mcp.call_tool(
            server_name=server_name,
            tool_name=tool_name,
            arguments=parsed_args or None,
            max_result_chars=safe_max,
        )
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @agent.tool
    @safe_tool("mcp_list_resources")
    async def mcp_list_resources(ctx: RunContext[SessionContext], server_name: str) -> str:
        """List resources exposed by an MCP server."""
        _ = ctx
        payload = await mcp.list_resources(server_name)
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @agent.tool
    @safe_tool("mcp_read_resource")
    async def mcp_read_resource(
        ctx: RunContext[SessionContext],
        server_name: str,
        uri: str,
        max_result_chars: int = MAX_TOOL_CHARS,
    ) -> str:
        """Read a resource from an MCP server."""
        _ = ctx
        safe_max = max(500, min(int(max_result_chars), 100_000))
        payload = await mcp.read_resource(server_name, uri=uri, max_result_chars=safe_max)
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @agent.tool
    @safe_tool("list_skills")
    def list_skills(ctx: RunContext[SessionContext], max_skills: int = 50) -> str:
        """List available skills (skill_id + description)."""
        _ = ctx
        safe_max = max(1, min(int(max_skills), 200))
        metas = skills_store.list_skills(WORKSPACE_ROOT, max_skills=safe_max)
        return _json(
            [
                {
                    "skill_id": meta.skill_id,
                    "name": meta.name,
                    "description": meta.description,
                    "path": str(meta.path),
                }
                for meta in metas
            ]
        )

    @agent.tool
    @safe_tool("read_skill")
    def read_skill(
        ctx: RunContext[SessionContext], skill_id: str, max_chars: int = MAX_TOOL_CHARS
    ) -> str:
        """Read a skill's SKILL.md markdown (use this to load full instructions)."""
        _ = ctx
        safe_max = max(200, min(int(max_chars), 100_000))
        payload = skills_store.read_skill_markdown(WORKSPACE_ROOT, skill_id, max_chars=safe_max)
        return _json(payload)

    @agent.tool
    @safe_tool("upsert_skill")
    def upsert_skill(
        ctx: RunContext[SessionContext],
        skill_id: str,
        description: str,
        body_markdown: str,
        display_name: str | None = None,
        overwrite: bool = True,
    ) -> str:
        """Create or edit a skill at SKILLS_DIR/<skill_id>/SKILL.md."""
        _ = ctx
        payload = skills_store.upsert_skill(
            WORKSPACE_ROOT,
            skill_id=skill_id,
            description=description,
            body_markdown=body_markdown,
            display_name=display_name,
            overwrite=bool(overwrite),
        )
        return _json(payload)

    @agent.tool
    @safe_tool("delete_skill")
    def delete_skill(ctx: RunContext[SessionContext], skill_id: str, confirm: bool = False) -> str:
        """Delete a skill directory. Requires confirm=True."""
        _ = ctx
        payload = skills_store.delete_skill(WORKSPACE_ROOT, skill_id=skill_id, confirm=bool(confirm))
        return _json(payload)

    if enable_dangerzone:

        @agent.tool
        @safe_tool("run_bash_command")
        def run_bash_command(
            ctx: RunContext[SessionContext],
            command: str,
            timeout_seconds: int = 30,
            max_output_chars: int = MAX_TOOL_CHARS,
        ) -> str:
            """Run a bash command on the host machine."""
            raw_command = command.strip()
            if not raw_command:
                return "Command is empty."

            safe_timeout = max(1, min(timeout_seconds, 300))
            safe_max_output = max(200, min(max_output_chars, 100000))

            def _clip(text: str) -> tuple[str, bool]:
                clipped = text[:safe_max_output]
                return clipped, len(text) > safe_max_output

            try:
                completed = subprocess.run(
                    ["bash", "-lc", raw_command],
                    cwd=str(WORKSPACE_ROOT),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=safe_timeout,
                    check=False,
                )
            except FileNotFoundError:
                return _json(
                    {
                        "command": raw_command,
                        "error": "bash executable not found on host.",
                    }
                )
            except subprocess.TimeoutExpired as err:
                stdout_text = err.stdout if isinstance(err.stdout, str) else ""
                stderr_text = err.stderr if isinstance(err.stderr, str) else ""
                stdout_clipped, stdout_truncated = _clip(stdout_text)
                stderr_clipped, stderr_truncated = _clip(stderr_text)
                return _json(
                    {
                        "command": raw_command,
                        "cwd": str(WORKSPACE_ROOT),
                        "timed_out": True,
                        "timeout_seconds": safe_timeout,
                        "stdout": stdout_clipped,
                        "stderr": stderr_clipped,
                        "stdout_truncated": stdout_truncated,
                        "stderr_truncated": stderr_truncated,
                    }
                )

            stdout_clipped, stdout_truncated = _clip(completed.stdout)
            stderr_clipped, stderr_truncated = _clip(completed.stderr)
            return _json(
                {
                    "command": raw_command,
                    "cwd": str(WORKSPACE_ROOT),
                    "timed_out": False,
                    "exit_code": completed.returncode,
                    "stdout": stdout_clipped,
                    "stderr": stderr_clipped,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                }
            )

    @agent.tool
    @safe_tool("switch_to_journaling")
    def switch_to_journaling(ctx: RunContext[SessionContext]) -> str:
        """Run journaling and memory-updater states."""
        ctx.deps.request_transition("journaling", "user requested reflection and memory updates")
        return "Switching to journaling and memory update flow now."

    @agent.tool
    @safe_tool("fast_forward_to_journaling")
    def fast_forward_to_journaling(ctx: RunContext[SessionContext]) -> str:
        """Immediately transition to journaling (skip idle timeout).

        Do not call this tool unless the user explicitly asks to skip waiting and journal now.
        """
        ctx.deps.request_transition("journaling", "user requested fast-forward to journaling")
        return "Fast-forwarding to journaling now (skipping idle timeout)."

    @agent.tool
    @safe_tool("schedule_self_message")
    def schedule_self_message(ctx: RunContext[SessionContext], when: str, message: str) -> str:
        """Schedule a future message to the agent (persisted in SQLite for restart safety).

        'when' supports ISO-8601 (recommended) or relative strings like "in 10 minutes".
        Do not call this tool unless the user explicitly asks to schedule a message/reminder.
        """
        parsed = scheduler_utils.parse_when_to_utc_epoch(when)
        result = ctx.deps.memory_store.schedule_self_message(
            deliver_at_utc=parsed.deliver_at_utc,
            message=message,
        )
        result = dict(result)
        result["interpreted_as"] = parsed.interpreted_as
        result["deliver_at_utc_human"] = scheduler_utils.format_utc_epoch(parsed.deliver_at_utc)
        return _json(result)

    @agent.tool
    @safe_tool("list_scheduled_self_messages")
    def list_scheduled_self_messages(
        ctx: RunContext[SessionContext], status: str | None = None, limit: int = 50
    ) -> str:
        """List scheduled self-messages from SQLite."""
        rows = ctx.deps.memory_store.list_scheduled_self_messages(status=status, limit=limit)
        return _json({"ok": True, "rows": rows})

    @agent.tool
    @safe_tool("cancel_scheduled_self_message")
    def cancel_scheduled_self_message(ctx: RunContext[SessionContext], message_id: int) -> str:
        """Cancel a scheduled self-message."""
        payload = ctx.deps.memory_store.cancel_scheduled_self_message(message_id=message_id)
        return _json(payload)

    return StateConfig(
        name="standard",
        agent=agent,
        idle_timeout=IDLE_TIMEOUT,
        idle_target="journaling",
        clear_on_enter=True,
    )
