#!/usr/bin/env python3
"""Capture a bounded stdout response and record raw/delivered byte counts."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CAP_BYTES = int(os.environ.get("CTXPP_EVAL_CAP_BYTES", "8192"))
log = Path(os.environ["CTXPP_EVAL_TRACE"])


def emit(data: bytes) -> tuple[bytes, int]:
    if len(data) <= CAP_BYTES:
        result = data
    else:
        omitted = len(data) - CAP_BYTES
        marker = f"\n[io_probe: {omitted} stdout bytes omitted]\n".encode()
        result = data[:CAP_BYTES] + marker
    sys.stdout.buffer.write(result)
    return result, max(0, len(data) - CAP_BYTES)


mode = sys.argv[1]
if mode == "read":
    path = Path(sys.argv[2])
    start = int(sys.argv[3])
    end = int(sys.argv[4])
    started = time.perf_counter_ns()
    with path.open("rb") as stream:
        for _ in range(start - 1):
            if not stream.readline():
                break
        payload = b"".join(
            stream.readline() for _ in range(max(0, end - start + 1))
        )
    elapsed = time.perf_counter_ns() - started
    delivered, omitted = emit(payload)
    record = {
        "kind": "read",
        "path": str(path),
        "start": start,
        "end": end,
        "raw_output_bytes": len(payload),
        "delivered_bytes": len(delivered),
        "omitted_bytes": omitted,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "elapsed_ns": elapsed,
    }
elif mode == "run":
    argv = sys.argv[3:] if sys.argv[2] == "--" else sys.argv[2:]
    started = time.perf_counter_ns()
    proc = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    elapsed = time.perf_counter_ns() - started
    delivered, omitted = emit(proc.stdout)
    record = {
        "kind": "run",
        "argv": argv,
        "returncode": proc.returncode,
        "raw_output_bytes": len(proc.stdout),
        "delivered_bytes": len(delivered),
        "omitted_bytes": omitted,
        "stderr_bytes": len(proc.stderr),
        "sha256": hashlib.sha256(proc.stdout).hexdigest(),
        "elapsed_ns": elapsed,
    }
    if proc.stderr:
        sys.stderr.buffer.write(proc.stderr)
else:
    raise SystemExit("use read PATH START END or run -- PROGRAM ARG...")

with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record, sort_keys=True) + "\n")
