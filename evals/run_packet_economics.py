#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPTS = SKILL / "scripts"
FIXTURE = SKILL / "tests/fixtures/sample"
sys.path.insert(0, str(SCRIPTS))

from ctxpp_lib import Tokenizer, load_config, load_index, partition_index, resolve_symbols, stable_json
from ctxpp_packet import build_context_packet, render_inspect
from ctxpp_telemetry import BoundedPacketTelemetry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=SKILL / "tests/expected/context-packet-economics.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "repo"
        shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("build", ".ctxpp", "__pycache__"))
        os.chmod(root / "tests/tokenizer.py", 0o755)
        subprocess.run(["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"], cwd=root,
                       check=True, text=True, capture_output=True)
        subprocess.run(["cmake", "--build", "build", "--parallel", "2"], cwd=root,
                       check=True, text=True, capture_output=True)
        subprocess.run([str(SCRIPTS / "ctxpp"), "--root", str(root), "--json", "scan"], cwd=root,
                       check=True, text=True, capture_output=True)

        cfg, _ = load_config(root)
        tokenizer = Tokenizer(root, str(cfg.get("tokenizer", "auto")))
        tokenizer_probe = tokenizer.count("ctxpp packet economics probe")
        _, files, _, _ = partition_index(load_index(root))
        canonical_tokens = sum(int(item.get("tokens", 0)) for item in files)
        target = resolve_symbols(root, "demo::PackingPlan::freeze", 2)[0]
        telemetry = BoundedPacketTelemetry(max_events=8)
        for budget, max_items in ((1200, 4), (2500, 12), (10000, 32)):
            started = time.perf_counter()
            packet = build_context_packet(root, "demo::PackingPlan::freeze", target, intent="edit",
                                          budget=budget, max_items=max_items, tokenizer=tokenizer)
            latency_ms = (time.perf_counter() - started) * 1000
            telemetry.observe(
                packet,
                latency_ms=latency_ms,
                tokenizer=tokenizer,
                compact_text=render_inspect(packet),
                canonical_repository_tokens=canonical_tokens,
            )

        report = telemetry.snapshot()
        report.update({
            "format": "CTXPP-PACKET-ECONOMICS/1",
            "fixture": "sample",
            "tokenizer": {"identity": tokenizer_probe.identity, "exact": tokenizer_probe.exact},
            "fixed_budgets": [1200, 2500, 10000],
            "unavailable_measurements": [
                "local_worker_success",
                "accepted_patch",
                "codex_reinvestigation",
            ],
        })
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(stable_json(report) + "\n", encoding="utf-8")
        print(stable_json(report["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
