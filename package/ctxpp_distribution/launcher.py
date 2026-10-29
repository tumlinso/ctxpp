"""Launch the source-preserving CLI shipped as ctxpp package data."""

from __future__ import annotations

from pathlib import Path
import runpy
import sysconfig


def main() -> int:
    data_dir = Path(sysconfig.get_path("data"))
    script = data_dir / "share" / "ctxpp" / "scripts" / "ctxpp"
    if not script.is_file():
        raise RuntimeError("ctxpp installation is incomplete: packaged CLI script is missing")
    runpy.run_path(str(script), run_name="__main__")
    return 0
