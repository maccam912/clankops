import html
import json
import os
import random
import re
import sqlite3
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from debug_auth import get_openrouter_api_key
from machine import SessionContext, StateConfig

IDLE_TIMEOUT = float(os.environ.get("IDLE_TIMEOUT", "30"))
WORKSPACE_ROOT = Path.cwd().resolve()
MAX_FILE_BYTES = int(os.environ.get("TOOL_MAX_FILE_BYTES", "1048576"))
MAX_TOOL_CHARS = int(os.environ.get("TOOL_MAX_CHARS", "12000"))
WEB_TIMEOUT_SECONDS = float(os.environ.get("WEB_TIMEOUT_SECONDS", "15"))
WEB_USER_AGENT = "clankops-agent/0.1 (+https://example.local)"
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng.k3s.koski.co").strip().rstrip("/")


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


def _resolve_workspace_path(raw_path: str) -> Path:
    user_path = Path(raw_path.strip() or ".")
    candidate = user_path.resolve() if user_path.is_absolute() else (WORKSPACE_ROOT / user_path).resolve()
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
    with urlopen(request, timeout=WEB_TIMEOUT_SECONDS) as response:
        status = getattr(response, "status", 200)
        content_type = response.headers.get("Content-Type", "")
        charset = response.headers.get_content_charset() or "utf-8"
        data = response.read(MAX_TOOL_CHARS * 4 + 1)
        truncated = len(data) > MAX_TOOL_CHARS * 4
        if truncated:
            data = data[: MAX_TOOL_CHARS * 4]
        text = data.decode(charset, errors="replace")

    if strip_html_content and "text/html" in content_type.lower():
        text = _strip_html(text)

    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    return {
        "url": url,
        "status": status,
        "content_type": content_type,
        "truncated": truncated,
        "content": text,
    }


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


def create_standard_state() -> StateConfig:
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

    agent: Agent[SessionContext, str] = Agent(
        model,
        system_prompt=(
            "You are a helpful assistant with broad tools for local files, web research, and SQLite. "
            "Use tools for factual/structured tasks instead of guessing. "
            "When writing files or running SQL, be explicit about what changed. "
            "If the user wants reflection or memory updates, call switch_to_journaling."
        ),
        deps_type=SessionContext,
    )

    @agent.tool
    def get_current_time(ctx: RunContext[SessionContext]) -> str:
        """Get the current local time."""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @agent.tool
    def get_random_number(
        ctx: RunContext[SessionContext], min_val: int = 1, max_val: int = 100
    ) -> str:
        """Get a random integer between min_val and max_val."""
        return str(random.randint(min_val, max_val))

    @agent.tool
    def list_directory(
        ctx: RunContext[SessionContext],
        path: str = ".",
        recursive: bool = False,
        max_entries: int = 200,
    ) -> str:
        """List files/directories in the workspace (optionally recursive)."""
        target = _resolve_workspace_path(path)
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
    def read_text_file(
        ctx: RunContext[SessionContext], path: str, max_chars: int = MAX_TOOL_CHARS
    ) -> str:
        """Read a UTF-8 text file from the workspace."""
        safe_max_chars = max(1, min(max_chars, 50000))
        file_path = _resolve_workspace_path(path)
        return _read_text(file_path, safe_max_chars)

    @agent.tool
    def write_text_file(ctx: RunContext[SessionContext], path: str, content: str) -> str:
        """Write UTF-8 text to a file in the workspace (overwrite)."""
        file_path = _resolve_workspace_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {_to_workspace_relative(file_path)}"

    @agent.tool
    def append_text_file(ctx: RunContext[SessionContext], path: str, content: str) -> str:
        """Append UTF-8 text to a file in the workspace."""
        file_path = _resolve_workspace_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended {len(content)} chars to {_to_workspace_relative(file_path)}"

    @agent.tool
    def search_in_files(
        ctx: RunContext[SessionContext], pattern: str, path: str = ".", max_results: int = 50
    ) -> str:
        """Regex-search text files in the workspace and return matching lines."""
        target = _resolve_workspace_path(path)
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
    def fetch_url(ctx: RunContext[SessionContext], url: str, max_chars: int = 8000) -> str:
        """Fetch and return web content from a URL."""
        safe_max_chars = max(500, min(max_chars, 50000))
        result = _fetch_url(url, safe_max_chars)
        return _json(result)

    @agent.tool
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
                    "results": parser.results[:safe_max_results],
                }
            )

    @agent.tool
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

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            if _looks_like_query(query):
                cur.execute(query)
                rows = cur.fetchmany(safe_max_rows + 1)
                truncated = len(rows) > safe_max_rows
                rows = rows[:safe_max_rows]
                payload = {
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
                "db_path": str(db_path),
                "query": query,
                "total_changes": conn.total_changes,
                "status": "ok",
            }
            return _json(payload)

    @agent.tool
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
    def switch_to_journaling(ctx: RunContext[SessionContext]) -> str:
        """Run journaling and memory-updater states."""
        ctx.deps.request_transition("journaling", "user requested reflection and memory updates")
        return "Switching to journaling and memory update flow now."

    return StateConfig(
        name="standard",
        agent=agent,
        idle_timeout=IDLE_TIMEOUT,
        idle_target="journaling",
        clear_on_enter=True,
    )
