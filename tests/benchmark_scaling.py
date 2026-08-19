#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
CTXPP = SKILL / "scripts/ctxpp"
REPORT = SKILL / "tests/expected/performance-scaling.json"


def process_tree_rss_kb(pid: int) -> int:
    pending = [pid]
    seen: set[int] = set()
    total = 0
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            for line in Path(f"/proc/{current}/status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])
                    break
            children = Path(f"/proc/{current}/task/{current}/children").read_text(encoding="utf-8").split()
            pending.extend(int(child) for child in children)
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
            continue
    return total


def make_fixture(root: Path, count: int) -> None:
    (root / "include").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "include/shared.hpp").write_text(
        "#pragma once\nnamespace scale { template<class T> constexpr T twice(T x){return x+x;} }\n",
        encoding="utf-8",
    )
    commands = []
    compiler = shutil.which("clang++") or "clang++"
    for index in range(count):
        source = root / f"src/unit_{index:03}.cpp"
        source.write_text(
            f'#include "shared.hpp"\nnamespace scale {{ int work_{index}(int x){{return twice(x)+{index};}} }}\n',
            encoding="utf-8",
        )
        commands.append({"directory": str(root), "file": str(source),
                         "arguments": [compiler, "-std=c++17", "-I", str(root / "include"), "-c", str(source)]})
    (root / "compile_commands.json").write_text(json.dumps(commands, separators=(",", ":")) + "\n", encoding="utf-8")
    (root / ".ctxpp.toml").write_text(
        'version=1\nprofile="view"\ntokenizer="unavailable-base-encoding"\nsource_write=false\n'
        'sources=["include/**/*.hpp","src/**/*.cpp"]\nexclude=[".ctxpp/**"]\n[tool]\nmax_workers="auto"\n',
        encoding="utf-8",
    )


def run(root: Path, workers: int | None) -> dict:
    shutil.rmtree(root / ".ctxpp", ignore_errors=True)
    usage = root / ".usage.json"
    profile = root / ".profile.json"
    env = {**os.environ, "CTXPP_PROFILE_PATH": str(profile)}
    if workers is not None:
        env["CTXPP_MAX_WORKERS"] = str(workers)
    else:
        env.pop("CTXPP_MAX_WORKERS", None)
    command = ["/usr/bin/time", "-f", '{"rss_kb":%M,"major_faults":%F,"swaps":%W}', "-o", str(usage),
               str(CTXPP), "--root", str(root), "--json", "scan"]
    started = time.perf_counter()
    proc = subprocess.Popen(command, cwd=root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    aggregate_peak_rss_kb = 0
    while proc.poll() is None:
        aggregate_peak_rss_kb = max(aggregate_peak_rss_kb, process_tree_rss_kb(proc.pid))
        time.sleep(0.01)
    stdout, stderr = proc.communicate()
    aggregate_peak_rss_kb = max(aggregate_peak_rss_kb, process_tree_rss_kb(proc.pid))
    if proc.returncode != 0:
        raise RuntimeError(stderr)
    elapsed = time.perf_counter() - started
    scan = json.loads(stdout)
    counters = json.loads(profile.read_text())["counters"]
    system = json.loads(usage.read_text())
    aggregate_peak_rss_kb = max(aggregate_peak_rss_kb, int(system.get("rss_kb", 0)))
    parsed = counters.get("tus_parsed", 0)
    return {"requested_workers": workers if workers is not None else "auto", "wall_ms": round(elapsed * 1000, 3),
            "tus_per_second": round(parsed / elapsed, 3), "tu_parses": parsed,
            "worker_cap": counters.get("workers_started", 0), "peak_workers": counters.get("peak_concurrent_workers", 0),
            "aggregate_peak_rss_kb": aggregate_peak_rss_kb,
            **system}


def main() -> int:
    count = 16
    available = max(1, len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1))
    values = sorted({value for value in (1, 2, 4, 8, 16, 24, 32, 40, 48, 64, available) if value <= min(count, available)})
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "repo"
        root.mkdir()
        make_fixture(root, count)
        runs = [run(root, value) for value in values]
        automatic = run(root, None)
    best = max(runs, key=lambda item: item["tus_per_second"])
    report = {"format": "CTXPP-SCALING/1", "translation_units": count, "available_cpus": available,
              "runs": runs, "automatic": automatic, "best": best}
    REPORT.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
