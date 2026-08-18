#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: grade_report.py REPORT.json", file=sys.stderr)
        return 2
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    failures = []
    if report.get("format") != "CTXPP-EVAL/1": failures.append("format")
    if len(report.get("results", [])) < 12: failures.append("prompt-count")
    if any(not x.get("resolution_ok") for x in report.get("results", [])): failures.append("resolution")
    if any(not x.get("source_unchanged") for x in report.get("results", [])): failures.append("implicit-mutation")
    if any(not x.get("slice_success") or not x.get("view_success") for x in report.get("results", [])): failures.append("artifacts")
    reduction = report.get("summary", {}).get("median_context_reduction")
    if reduction is None or reduction <= 0: failures.append("context-reduction")
    print(json.dumps({"ok": not failures, "failures": failures}, sort_keys=True, separators=(",", ":")))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
