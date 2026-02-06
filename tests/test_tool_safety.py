import json
from io import BytesIO
from urllib.error import HTTPError


def test_safe_tool_wraps_exceptions_into_json():
    from tool_utils import safe_tool

    @safe_tool("boom")
    def boom(x: int) -> str:
        _ = x
        raise RuntimeError("nope")

    payload = json.loads(boom(123))
    assert payload["ok"] is False
    assert payload["tool"] == "boom"
    assert payload["error_type"] == "RuntimeError"


def test_fetch_url_returns_error_payload_on_http_error(monkeypatch):
    # _fetch_url is used by the fetch_url tool and web search helpers. It should
    # not raise on 4xx/5xx; instead return a dict with status and error.
    from states import standard

    def fake_urlopen(request, timeout=0):  # noqa: ARG001
        body = b"<html><body>not found</body></html>"
        raise HTTPError(
            url=getattr(request, "full_url", "http://example.invalid"),
            code=404,
            msg="Not Found",
            hdrs={"Content-Type": "text/html; charset=utf-8"},
            fp=BytesIO(body),
        )

    monkeypatch.setattr(standard, "urlopen", fake_urlopen)

    result = standard._fetch_url("http://example.invalid/missing", max_chars=200)
    assert result["status"] == 404
    assert "error" in result
    assert isinstance(result["content"], str)

