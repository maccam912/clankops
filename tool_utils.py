from __future__ import annotations

import inspect
import json
from functools import wraps
from typing import Any, Callable, TypeVar, overload

TFunc = TypeVar("TFunc", bound=Callable[..., Any])


def _json(value: object) -> str:
    # Keep this local so tool error payloads are consistent even if callers
    # choose to return plain text for success cases.
    return json.dumps(value, indent=2, ensure_ascii=False)


@overload
def safe_tool(name: str) -> Callable[[TFunc], TFunc]: ...


def safe_tool(name: str) -> Callable[[TFunc], TFunc]:
    """Guard a pydantic-ai tool so exceptions don't bubble up and abort a run.

    This returns a wrapped function with the same signature (for tool schema
    generation) that converts exceptions into a JSON error payload.
    """

    def decorator(fn: TFunc) -> TFunc:
        sig = inspect.signature(fn)
        is_async = inspect.iscoroutinefunction(fn)

        if is_async:

            @wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await fn(*args, **kwargs)
                except Exception as err:
                    return _json(
                        {
                            "ok": False,
                            "tool": name,
                            "error_type": err.__class__.__name__,
                            "error": str(err),
                        }
                    )

            async_wrapper.__signature__ = sig  # type: ignore[attr-defined]
            return async_wrapper  # type: ignore[return-value]

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception as err:
                return _json(
                    {
                        "ok": False,
                        "tool": name,
                        "error_type": err.__class__.__name__,
                        "error": str(err),
                    }
                )

        wrapper.__signature__ = sig  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator

