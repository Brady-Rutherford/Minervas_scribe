"""Redaction helpers: signed URLs never reach logs, sensitive keys never reach samples."""

from __future__ import annotations

import re
from typing import Any

# Absolute URLs and bare request paths ("url: /v1/listen?...keyterm=<roster names>" in a
# requests error) both lose their query string.
_URL_QUERY_RE = re.compile(r"(https?://[^\s'\"<>?#]+|(?<![\w/])/[\w.\-/]+)\?[^\s'\"<>#)]*")

# Keys whose values may identify a person or act as a credential.
SENSITIVE_KEY_RE = re.compile(
    r"name|mail|signature|token|secret|cookie|password|phone|avatar|photo|image|url",
    re.IGNORECASE,
)
REDACTED = "[redacted]"


def strip_query(url: str | None) -> str:
    """Drop the query string and fragment (where CloudFront puts Signature/Key-Pair-Id)."""
    if not url:
        return ""
    return str(url).split("?", 1)[0].split("#", 1)[0]


def redact_urls(text: Any) -> str:
    """Replace the query string of every URL inside ``text`` with ``?<redacted>``."""
    if text is None:
        return ""
    return _URL_QUERY_RE.sub(r"\1?<redacted>", str(text))


def redact_keys(obj: Any, pattern: re.Pattern[str] = SENSITIVE_KEY_RE) -> Any:
    """Deep-copy ``obj`` replacing values of keys that match ``pattern``."""
    if isinstance(obj, dict):
        return {
            k: (REDACTED if pattern.search(str(k)) else redact_keys(v, pattern))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_keys(v, pattern) for v in obj]
    return obj
