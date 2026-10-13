"""Privacy helpers for ``ids`` mode.

Forum user ids are small integers, so a plain hash would be trivially reversible. ids mode
uses an HMAC with a local secret salt: stable across classes (good for datasets), but not
reversible by anyone without the salt. The salt lives in ``SCRIBE_ID_SALT`` or a file in the
(git-ignored) data directory.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

SALT_ENV = "SCRIBE_ID_SALT"
PRIVACY_MODES = ("names", "ids")


def pseudonym(user_id, salt: str) -> str:
    digest = hmac.new(salt.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()
    return f"u_{digest[:10]}"


def load_or_create_salt(path: Path | str | None = None) -> str:
    env = os.environ.get(SALT_ENV, "").strip()
    if env:
        return env
    if path is None:
        # No persistent location: random per process (pseudonyms won't be stable).
        return secrets.token_hex(16)
    p = Path(path)
    if p.exists():
        value = p.read_text(encoding="utf-8").strip()
        if value:
            return value
    p.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(16)
    p.write_text(value + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return value


def ensure_private_dir(path: Path | str) -> Path:
    """Create a data dir that git-ignores itself (so the root .gitignore needn't change)."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    gi = p / ".gitignore"
    if not gi.exists():
        gi.write_text("# Created by Minerva's Scribe v3: everything in here stays local.\n*\n")
    return p
