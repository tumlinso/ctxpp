"""Resolve ctxpp generated state without coupling artifacts to source trees."""
from __future__ import annotations

import os
from pathlib import Path


def state_dir(root: Path) -> Path:
    """Return the configured external state directory or the legacy .ctxpp path."""
    configured = os.environ.get("CTXPP_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return root / ".ctxpp"
