from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


_IN_RE = re.compile(
    r"(?is)^\s*in\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\s*$"
)


@dataclass(frozen=True)
class ParsedSchedule:
    deliver_at_utc: int
    interpreted_as: str


def parse_when_to_utc_epoch(when: str) -> ParsedSchedule:
    """Parse a human-ish schedule time into a UTC epoch seconds.

    Supported:
    - "in 10 minutes", "in 2h", "in 1 day"
    - ISO-8601:
      - "2026-02-06T15:04:05Z"
      - "2026-02-06T15:04:05-05:00"
      - "2026-02-06 15:04" (assumed local timezone)
    """
    raw = (when or "").strip()
    if not raw:
        raise ValueError("when is empty.")

    m = _IN_RE.match(raw)
    if m:
        amount = float(m.group(1))
        unit = m.group(2).lower()
        seconds = _to_seconds(amount, unit)
        deliver = int(time.time() + seconds)
        return ParsedSchedule(deliver_at_utc=deliver, interpreted_as=f"relative ({raw})")

    # ISO parsing. datetime.fromisoformat doesn't accept trailing Z; normalize.
    normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        # Allow "YYYY-MM-DD HH:MM:SS" without T and without seconds.
        raise ValueError(
            "Could not parse 'when'. Use ISO-8601 like '2026-02-06T15:04:05Z' "
            "or a relative time like 'in 10 minutes'."
        )

    if dt.tzinfo is None:
        # Assume local timezone for naive datetimes.
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        dt = dt.replace(tzinfo=local_tz)
        interpreted = f"local time ({raw})"
    else:
        interpreted = f"absolute ({raw})"

    deliver_utc = int(dt.astimezone(timezone.utc).timestamp())
    return ParsedSchedule(deliver_at_utc=deliver_utc, interpreted_as=interpreted)


def format_utc_epoch(ts: int) -> str:
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _to_seconds(amount: float, unit: str) -> int:
    if unit in ("second", "seconds", "sec", "secs", "s"):
        return int(round(amount))
    if unit in ("minute", "minutes", "min", "mins", "m"):
        return int(round(amount * 60))
    if unit in ("hour", "hours", "hr", "hrs", "h"):
        return int(round(amount * 60 * 60))
    if unit in ("day", "days", "d"):
        return int(round(amount * 60 * 60 * 24))
    raise ValueError(f"Unsupported time unit: {unit}")

