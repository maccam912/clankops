from __future__ import annotations

import traceback
import os
from typing import Any


def openrouter_api_key_parts() -> tuple[str, str]:
    raw = os.environ.get("OPENROUTER_API_KEY", "")
    return raw, raw.strip()


def get_openrouter_api_key() -> str:
    return _strip_bearer_prefix(openrouter_api_key_parts()[1])


def print_exception_debug(context: str, err: Exception):
    status_code = _extract_status_code(err)
    if status_code is not None:
        print(f"[{context}] HTTP status: {status_code}")
        if status_code == 401:
            print(f"[{context}] 401 Unauthorized. Check OPENROUTER_API_KEY and base URL.")

    print(f"[{context}] {err.__class__.__name__}: {err}")

    tb = traceback.format_exc()
    if tb and tb.strip() != "NoneType: None":
        print(tb)


def _extract_status_code(err: Exception) -> int | None:
    status_code = _safe_getattr(err, "status_code")
    if isinstance(status_code, int):
        return status_code
    response = _safe_getattr(err, "response")
    response_status = _safe_getattr(response, "status_code")
    if isinstance(response_status, int):
        return response_status
    return None


def _safe_getattr(obj: Any, attr: str) -> Any:
    if obj is None:
        return None
    try:
        return getattr(obj, attr)
    except Exception:
        return None


def _strip_bearer_prefix(secret: str) -> str:
    lower = secret.lower()
    if lower.startswith("bearer "):
        return secret[7:].strip()
    return secret
