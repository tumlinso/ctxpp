#!/usr/bin/env python3
"""Build the optional native parser into ctxpp's writable user cache."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from ctxpp_lib import native_build_directory  # noqa: E402


def main() -> int:
    ctxpp_root = SCRIPT_DIR.parent
    tool_dir = ctxpp_root / "tool"
    build_dir = native_build_directory(ctxpp_root)
    environment = os.environ.copy()
    environment["CTXPP_BUILD_DIR"] = str(build_dir)
    commands = (
        ["cmake", "-S", str(tool_dir), "-B", str(build_dir), "-G", "Unix Makefiles",
         "-DCMAKE_BUILD_TYPE=Release"],
        ["cmake", "--build", str(build_dir), "--parallel",
         environment.get("CTXPP_BUILD_JOBS", "2")],
        [str(build_dir / "ctxpp-core"), "doctor"],
    )
    for command in commands:
        result = subprocess.run(command, env=environment, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
