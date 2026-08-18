#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

from ctxpp_lib import Tokenizer, load_config, load_index, partition_index, resolve_symbols, sha256_file, stable_json


def run(command: list[str], cwd: Path) -> dict:
    proc = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    return {"command": command, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    fixture = SKILL / "tests/fixtures/sample"
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "repo"
        shutil.copytree(fixture, root, ignore=shutil.ignore_patterns("build", ".ctxpp", "__pycache__"))
        os.chmod(root / "tests/tokenizer.py", 0o755)
        setup = [
            run(["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"], root),
            run(["cmake", "--build", "build", "--parallel", "2"], root),
            run([str(SCRIPTS / "ctxpp"), "--root", str(root), "--json", "scan"], root),
        ]
        if any(x["returncode"] for x in setup):
            print(stable_json({"setup": setup}), file=sys.stderr)
            return 2
        cfg, _ = load_config(root)
        tokenizer = Tokenizer(root, str(cfg.get("tokenizer", "auto")))
        _, files, _, _ = partition_index(load_index(root))
        canonical_repository_tokens = sum(int(f.get("tokens", 0)) for f in files)
        baseline_hashes = {f["path"]: sha256_file(root / f["path"]) for f in files}
        results = []
        with (HERE / "prompts.csv").open(newline="", encoding="utf-8") as stream:
            prompts = list(csv.DictReader(stream))
        for prompt in prompts:
            symbols = resolve_symbols(root, prompt["target"], 4)
            resolved = symbols[0] if symbols else None
            canonical_tokens = canonical_repository_tokens
            slice_run = run([str(SCRIPTS / "ctxpp"), "--root", str(root), "--json", "slice", prompt["target"], "--intent", prompt["intent"], "--budget", "260"], root)
            view_run = run([str(SCRIPTS / "ctxpp"), "--root", str(root), "--json", "view", prompt["target"], "--intent", prompt["intent"], "--budget", "260"], root)
            slice_payload = json.loads(slice_run["stdout"]) if slice_run["returncode"] == 0 else {}
            view_payload = json.loads(view_run["stdout"]) if view_run["returncode"] == 0 else {}
            view_map = {}
            if view_payload and isinstance(view_payload.get("source_map"), str):
                view_map = json.loads(Path(view_payload["source_map"]).read_text(encoding="utf-8"))
            slice_tokens = tokenizer.count(slice_payload.get("content", "")).count if slice_payload else None
            view_tokens = view_payload.get("report", {}).get("tokens")
            current_hashes = {path: sha256_file(root / path) for path in baseline_hashes}
            results.append({
                "id": prompt["id"], "category": prompt["category"], "should_trigger": prompt["should_trigger"] == "1",
                "source_write_allowed": prompt["allow_source_write"] == "1", "resolved": resolved.get("qualified_name") if resolved else None,
                "resolution_ok": bool(resolved and resolved.get("qualified_name") == prompt["expected_symbol"]),
                "canonical_tokens": canonical_tokens, "slice_tokens": slice_tokens, "view_tokens": view_tokens,
                "slice_success": slice_run["returncode"] == 0, "view_success": view_run["returncode"] == 0,
                "source_unchanged": current_hashes == baseline_hashes, "file_reads_proxy": 1 + int(bool(slice_payload)) + int(bool(view_payload)),
                "file_hops_proxy": len({m.get("canonical_file") for m in view_map.get("mappings", [])}) if view_payload else None,
            })
        report = {
            "format": "CTXPP-EVAL/1", "fixture": "sample", "prompts": len(results), "results": results,
            "summary": {
                "resolution_success": sum(x["resolution_ok"] for x in results),
                "task_artifact_success": sum(x["slice_success"] and x["view_success"] for x in results),
                "implicit_mutations": sum(not x["source_unchanged"] for x in results),
                "median_context_reduction": median_reduction(results),
            },
        }
        output = args.output or SKILL / "tests/expected/eval-report.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(stable_json(report) + "\n", encoding="utf-8")
        print(stable_json(report["summary"]))
        return 0


def median_reduction(results: list[dict]) -> float | None:
    values = sorted((x["canonical_tokens"] - x["view_tokens"]) / x["canonical_tokens"] for x in results
                    if isinstance(x.get("canonical_tokens"), int) and x["canonical_tokens"] and isinstance(x.get("view_tokens"), int))
    if not values:
        return None
    middle = len(values) // 2
    return round(values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2, 4)


if __name__ == "__main__":
    raise SystemExit(main())
