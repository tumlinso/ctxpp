#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
CTXPP = SKILL / "scripts/ctxpp"
FIXTURE = SKILL / "tests/fixtures/sample"
BASELINE = SKILL / "tests/expected/performance-baseline.json"
REPORT = SKILL / "tests/expected/performance-optimized.json"


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def command(root: Path, *args: str, env: dict[str, str] | None = None) -> tuple[float, int, int]:
    metric = root / ".time.json"
    argv = ["/usr/bin/time", "-f", '{"rss_kb":%M,"major_faults":%F}', "-o", str(metric),
            str(CTXPP), "--root", str(root), "--json", *args]
    started = time.perf_counter()
    proc = subprocess.run(argv, cwd=root, env=env, text=True, capture_output=True)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if proc.returncode != 0:
        raise RuntimeError(f"ctxpp {' '.join(args)} failed: {proc.stderr}")
    usage = json.loads(metric.read_text())
    return elapsed_ms, int(usage["rss_kb"]), int(usage["major_faults"])


def repeated(root: Path, args: tuple[str, ...], repeats: int, env: dict[str, str]) -> dict:
    samples = [command(root, *args, env=env) for _ in range(repeats)]
    elapsed = [x[0] for x in samples]
    return {
        "p50_ms": round(statistics.median(elapsed), 3), "p95_ms": round(percentile(elapsed, 0.95), 3),
        "peak_rss_kb": max(x[1] for x in samples), "major_faults": sum(x[2] for x in samples),
    }


def core_wrapper(root: Path) -> tuple[Path, Path]:
    real = SKILL / "tool/build/ctxpp-core"
    log = root / "core-calls.jsonl"
    wrapper = root / "core-wrapper.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        f"log={str(log)!r}; real={str(real)!r}\n"
        "with open(log,'a',encoding='utf-8') as out: out.write(json.dumps(sys.argv[1:],separators=(',',':'))+'\\n')\n"
        "os.execv(real,[real,*sys.argv[1:]])\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper, log


def calls(log: Path) -> dict:
    if not log.is_file():
        return {"core_calls": 0, "tu_parses": 0, "compact_calls": 0, "ast_parses": 0}
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    return {
        "core_calls": len(rows),
        "tu_parses": sum(bool(row and row[0] == "scan") for row in rows),
        "compact_calls": sum(bool(row and row[0] == "compact") for row in rows),
        "ast_parses": sum(1 if row and row[0] == "scan" else 2 if row and row[0] == "compact" else 0 for row in rows),
    }


def clear_log(log: Path) -> None:
    if log.exists():
        log.unlink()


def benchmark(repeats: int) -> dict:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "repo"
        shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("build", ".ctxpp", "__pycache__"))
        os.chmod(root / "tests/tokenizer.py", 0o755)
        subprocess.run(["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"], cwd=root, check=True, capture_output=True)
        subprocess.run(["cmake", "--build", "build", "--parallel", "2"], cwd=root, check=True, capture_output=True)
        wrapper, log = core_wrapper(root)
        env = {**os.environ, "CTXPP_CORE": str(wrapper)}

        full_scan = command(root, "scan", env=env)
        full_scan_calls = calls(log)
        clear_log(log)
        incremental_scan = command(root, "scan", env=env)
        incremental_scan_calls = calls(log)

        hot = {}
        cold_view = None
        for name, args in {
            "status": ("status",),
            "where": ("where", "demo::PackingPlan::freeze"),
            "route": ("route", "freeze score test"),
            "slice": ("slice", "demo::PackingPlan::freeze", "--intent", "edit", "--budget", "500"),
            "view": ("view", "demo::PackingPlan::freeze", "--intent", "understand", "--budget", "260"),
        }.items():
            clear_log(log)
            warm = command(root, *args, env=env)  # warm command-local/token caches before hot samples
            if name == "view":
                cold_view = {"wall_ms": round(warm[0], 3), "peak_rss_kb": warm[1], "major_faults": warm[2], **calls(log)}
            clear_log(log)
            hot[name] = repeated(root, args, repeats, env)
            hot[name].update(calls(log))

        source = root / "src/other.cpp"
        source.write_bytes(source.read_bytes() + b"\n")
        clear_log(log)
        unrelated = command(root, "where", "demo::PackingPlan::freeze", env=env)
        unrelated_calls = calls(log)

        return {
            "format": "CTXPP-PERF/1", "repeats": repeats,
            "hot": hot,
            "cold_view": cold_view,
            "full_scan": {"wall_ms": round(full_scan[0], 3), "peak_rss_kb": full_scan[1], "major_faults": full_scan[2], **full_scan_calls},
            "incremental_scan": {"wall_ms": round(incremental_scan[0], 3), "peak_rss_kb": incremental_scan[1], "major_faults": incremental_scan[2], **incremental_scan_calls},
            "changed_cpp_unrelated_where": {"wall_ms": round(unrelated[0], 3), "peak_rss_kb": unrelated[1], "major_faults": unrelated[2], **unrelated_calls},
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record-baseline", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--repeats", type=int, default=12)
    args = parser.parse_args()
    result = benchmark(args.repeats)
    output = BASELINE if args.record_baseline else REPORT
    output.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    if args.check:
        baseline = json.loads(BASELINE.read_text())
        failures = []
        targets = {"status": 250, "where": 200, "route": 400, "slice": 750}
        for name, limit in targets.items():
            if result["hot"][name]["p95_ms"] > limit:
                failures.append(f"{name} p95 {result['hot'][name]['p95_ms']} > {limit}")
        for name in ("where", "route", "slice"):
            if result["hot"][name]["tu_parses"] or result["hot"][name]["ast_parses"]:
                failures.append(f"clean {name} parsed ASTs")
        if result["hot"]["view"]["compact_calls"]:
            failures.append("warm view relaunched compactor")
        if result["incremental_scan"]["tu_parses"] or result["incremental_scan"]["core_calls"]:
            failures.append("clean incremental scan invoked semantic core")
        if result["changed_cpp_unrelated_where"]["tu_parses"]:
            failures.append("unrelated dirty cpp caused TU parse")
        if result["hot"]["view"]["p50_ms"] * 3 >= baseline["hot"]["view"]["p50_ms"]:
            failures.append("warm view speedup below 3x")
        print(json.dumps({"ok": not failures, "failures": failures, "baseline": baseline, "optimized": result}, sort_keys=True, separators=(",", ":")))
        return 1 if failures else 0
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
