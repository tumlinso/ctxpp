#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
CTXPP = SKILL / "scripts/ctxpp"
FIXTURE = SKILL / "tests/fixtures/sample"
EXPECTED = SKILL / "tests/expected/compatibility-v1.json"


def normalize(text: str, root: Path) -> str:
    return text.replace(str(root), "<ROOT>").replace(str(SKILL), "<SKILL>")


def invoke(root: Path, *args: str, json_output: bool = True) -> dict:
    command = [str(CTXPP), "--root", str(root)]
    if json_output:
        command.append("--json")
    command.extend(args)
    proc = subprocess.run(command, cwd=root, text=True, capture_output=True)
    return {
        "args": list(args), "json": json_output, "returncode": proc.returncode,
        "stdout": normalize(proc.stdout, root), "stderr": normalize(proc.stderr, root),
    }


def capture() -> dict:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "repo"
        shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("build", ".ctxpp", "__pycache__"))
        os.chmod(root / "tests/tokenizer.py", 0o755)
        subprocess.run(["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"], cwd=root, check=True, capture_output=True)
        subprocess.run(["cmake", "--build", "build", "--parallel", "2"], cwd=root, check=True, capture_output=True)
        records = [
            invoke(root, "scan"),
            invoke(root, "status"),
            invoke(root, "where", "demo::PackingPlan::freeze"),
            invoke(root, "where", "overloaded"),
            invoke(root, "where", "definitely_missing_symbol"),
            invoke(root, "route", "freeze score test"),
            invoke(root, "slice", "demo::PackingPlan::freeze", "--intent", "edit", "--budget", "90"),
            invoke(root, "slice", "overloaded"),
            invoke(root, "slice", "definitely_missing_symbol"),
            invoke(root, "view", "demo::PackingPlan::freeze", "--intent", "understand", "--budget", "260"),
            invoke(root, "expand", "demo::PackingPlan::freeze", json_output=False),
            invoke(root, "audit"),
            invoke(root, "lint"),
            invoke(root, "plan", "src/plan.cpp", "--rule", "semantic-local-rename"),
            invoke(root, "shard", "src/plan.cpp"),
            invoke(root, "shard", ".ctxpp/views/generated.cpp"),
        ]
        source = root / "src/plan.cpp"
        baseline = source.read_bytes()
        source.write_bytes(baseline + b"\n")
        records.append(invoke(root, "status"))
        source.write_bytes(baseline)
        return {"format": "CTXPP-COMPAT/1", "records": records}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()
    actual = capture()
    encoded = json.dumps(actual, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if args.record:
        EXPECTED.write_text(encoded, encoding="utf-8")
        print(EXPECTED)
        return 0
    expected = EXPECTED.read_text(encoding="utf-8")
    if encoded != expected:
        print("compatibility output changed", flush=True)
        return 1
    print("compatibility output unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
