"""Time helpers. Uses ``datetime.fromisoformat`` (handles a trailing "Z" on Python 3.11+)."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_iso(value) -> datetime | None:
    """Parse an ISO-8601 string into an aware datetime (naive values are taken as UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip())
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def iso_to_ms(value) -> int | None:
    """ISO-8601 string -> epoch milliseconds (int), or None."""
    dt = parse_iso(value)
    if dt is None:
        return None
    return int(round(dt.timestamp() * 1000))


def epoch_ms(value) -> int | None:
    """Coerce a Forum ``timestamp``-like value to epoch milliseconds.

    Accepts ints/floats/numeric strings (milliseconds; values that look like seconds,
    i.e. < 1e11, are scaled up) and ISO strings.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        num = float(value)
    else:
        text = str(value).strip()
        try:
            num = float(text)
        except ValueError:
            return iso_to_ms(text)
    if num < 1e11:  # looks like epoch seconds
        num *= 1000.0
    return int(round(num))


def ms_to_iso(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def fmt_clock(seconds: float | None) -> str:
    """Seconds -> "m:ss" (or "h:mm:ss" past an hour)."""
    if seconds is None:
        return ""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
