#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
CTXPP = SKILL / "scripts/ctxpp"
FIXTURE = SKILL / "tests/fixtures/sample"


def command(root: Path, *args: str) -> dict:
    proc = subprocess.run([str(CTXPP), "--root", str(root), "--json", *args], cwd=root, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ctxpp {' '.join(args)} failed: {proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout)


def exact_tokens(root: Path, data: bytes) -> int:
    proc = subprocess.run(
        ["python3", "tests/tokenizer.py"], cwd=root, input=data, capture_output=True, check=True
    )
    return int(proc.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=SKILL / "tests/expected/demo-report.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "repo"
        shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("build", ".ctxpp", "__pycache__"))
        os.chmod(root / "tests/tokenizer.py", 0o755)
        subprocess.run(["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"], cwd=root, check=True, capture_output=True)
        subprocess.run(["cmake", "--build", "build", "--parallel", "2"], cwd=root, check=True, capture_output=True)
        scan = command(root, "scan")
        baseline = (root / "src/plan.cpp").read_bytes()
        baseline_tokens = exact_tokens(root, baseline)
        view = command(root, "view", "demo::PackingPlan::freeze", "--intent", "understand", "--budget", "260")

        rename_plan = command(root, "plan", "src/plan.cpp", "--rule", "semantic-local-rename")
        rename_apply = command(root, "apply", rename_plan["path"])
        renamed_source = (root / "src/plan.cpp").read_bytes()
        renamed_contains_bi = b"int bi = initialization_order_anchor" in renamed_source
        renamed_tokens = exact_tokens(root, renamed_source)
        rename_reverse = command(root, "apply", rename_apply["reverse_plan"])
        rename_exact_reverse = (root / "src/plan.cpp").read_bytes() == baseline

        command(root, "scan")
        pre_shard_slice = command(root, "slice", "demo::PackingPlan::freeze", "--intent", "edit", "--budget", "260")
        shard_plan = command(root, "shard", "src/plan.cpp")
        shard_plan_record = json.loads(Path(shard_plan["path"]).read_text())
        shard_apply = command(root, "apply", shard_plan["path"])
        shard_test_ok = all(x["returncode"] == 0 for x in shard_apply["verification"])
        created = list(shard_apply["created"])
        post_shard_slice = command(root, "slice", "demo::PackingPlan::freeze", "--intent", "edit", "--budget", "260")
        shard_reverse = command(root, "apply", shard_apply["reverse_plan"])
        shard_exact_reverse = (root / "src/plan.cpp").read_bytes() == baseline and all(not (root / x).exists() for x in created)

        report = {
            "format": "CTXPP-DEMO/1", "fixture": "sample", "scan": {k: scan[k] for k in ("backend", "files", "symbols", "edges", "failures")},
            "view": {"target": "demo::PackingPlan::freeze", "tokens": view["report"]["tokens"],
                     "token_exact": view["report"]["token_exact"], "tokenizer": view["report"]["tokenizer"],
                     "bytes": view["report"]["bytes"], "glossary": view["report"]["glossary"],
                     "selected_source_tokens_before": view["report"]["selected_source_tokens_before"],
                     "selected_source_tokens_after": view["report"]["selected_source_tokens_after"],
                     "selected_source_token_delta": view["report"]["selected_source_token_delta"],
                     "canonical_plan_cpp_tokens": next(json.loads(x)["tokens"] for x in (root / ".ctxpp/index.jsonl").read_text().splitlines()
                                                       if json.loads(x).get("record") == "file" and json.loads(x).get("path") == "src/plan.cpp")},
            "rename": {"plan": rename_plan["id"], "edits": rename_plan["edits"], "projected_token_delta": rename_plan["projected_token_delta"],
                       "source_tokens_before": baseline_tokens, "source_tokens_after": renamed_tokens,
                       "measured_source_token_delta": baseline_tokens - renamed_tokens,
                       "applied": rename_apply["success"], "verified": all(x["returncode"] == 0 for x in rename_apply["verification"]),
                       "renamed_contains_bi": renamed_contains_bi, "reversed": rename_reverse["success"], "byte_exact_reverse": rename_exact_reverse},
            "shard": {"plan": shard_plan["id"], "created": created, "applied": shard_apply["success"], "verified": shard_test_ok,
                      "edit_slice_tokens_before": pre_shard_slice["report"]["tokens"],
                      "edit_slice_tokens_after": post_shard_slice["report"]["tokens"],
                      "representative_slice_token_delta": shard_plan_record["edits"][0]["representative_slice_token_delta"],
                      "file_open_context_delta": shard_plan_record["edits"][0]["file_open_context_delta"],
                      "reversed": shard_reverse["success"], "byte_exact_reverse": shard_exact_reverse},
            "canonical_source_unchanged_at_end": (root / "src/plan.cpp").read_bytes() == baseline,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
