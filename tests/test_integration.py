from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
FIXTURE = SKILL / "tests/fixtures/sample"
sys.path.insert(0, str(SCRIPTS))

from ctxpp_lib import load_index, partition_index, source_text


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run([str(SKILL / "scripts/build_tool.sh")], cwd=SKILL, check=True, text=True, capture_output=True)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        shutil.copytree(FIXTURE, self.root, ignore=shutil.ignore_patterns("build", ".ctxpp", "__pycache__"))
        os.chmod(self.root / "tests/tokenizer.py", 0o755)
        subprocess.run(["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"], cwd=self.root, check=True, text=True, capture_output=True)
        subprocess.run(["cmake", "--build", "build", "--parallel", "2"], cwd=self.root, check=True, text=True, capture_output=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def ctxpp(self, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
        command = [str(SCRIPTS / "ctxpp"), "--root", str(self.root), "--json", *args]
        proc = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        if ok and proc.returncode != 0:
            self.fail(f"ctxpp failed: {' '.join(command)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return proc

    def scan(self) -> dict:
        return json.loads(self.ctxpp("scan").stdout)

    def test_scan_where_overloads_and_determinism(self) -> None:
        first = self.scan()
        index_first = (self.root / ".ctxpp/index.jsonl").read_bytes()
        second = self.scan()
        self.assertEqual(index_first, (self.root / ".ctxpp/index.jsonl").read_bytes())
        self.assertEqual(first["backend"], "libclang-runtime")
        self.assertGreater(first["symbols"], 20)
        self.assertEqual({k: v for k, v in first.items() if k != "cache_hits"},
                         {k: v for k, v in second.items() if k != "cache_hits"})
        self.assertGreaterEqual(second["cache_hits"], 1)
        found = json.loads(self.ctxpp("where", "demo::PackingPlan::freeze").stdout)
        self.assertEqual(found["matches"][0]["file"], "src/plan.cpp")
        overloads = json.loads(self.ctxpp("where", "overloaded").stdout)
        self.assertEqual(len(overloads["matches"]), 2)

    def test_edit_slice_is_verbatim_whole_and_budget_reports_omissions(self) -> None:
        self.scan()
        records = load_index(self.root)
        _, _, symbols, _ = partition_index(records)
        target = next(s for s in symbols if s.get("qualified_name") == "demo::PackingPlan::freeze" and s.get("definition"))
        canonical = source_text(self.root, target)
        bundle = json.loads(self.ctxpp("slice", target["id"], "--intent", "edit", "--budget", "90").stdout)
        self.assertIn(canonical, bundle["content"])
        self.assertIn("sufficient=", bundle["content"])
        self.assertTrue(canonical.rstrip().endswith("}"))
        self.assertNotIn("//@generated", (self.root / "src/plan.cpp").read_text())

    def test_debug_slice_routes_macro_nonlocal_state_and_test(self) -> None:
        self.scan()
        bundle = json.loads(self.ctxpp("slice", "demo::PackingPlan::freeze", "--intent", "debug", "--budget", "1200").stdout)
        self.assertIn("#define CTXPP_SCALE", bundle["content"])
        self.assertIn("initialization_order_anchor = 3", bundle["content"])
        self.assertIn("sufficient=1", bundle["content"])
        routed = json.loads(self.ctxpp("route", "freeze score test").stdout)
        self.assertTrue(any(x["file"] == "tests/test_plan.cpp" for x in routed["matches"]))

    def test_compact_view_has_exact_tokens_and_source_map(self) -> None:
        self.scan()
        view = json.loads(self.ctxpp("view", "demo::PackingPlan::freeze", "--intent", "understand", "--budget", "260").stdout)
        self.assertTrue(view["readonly"])
        self.assertTrue(view["report"]["token_exact"])
        self.assertTrue(Path(view["view"]).is_file())
        mapping = json.loads(Path(view["source_map"]).read_text())
        self.assertEqual(mapping["format"], "CTXPP-MAP/1")
        self.assertTrue(mapping["mappings"])
        self.assertGreater(view["report"]["selected_source_token_delta"], 0)
        repeated = json.loads(self.ctxpp("view", "demo::PackingPlan::freeze", "--intent", "understand", "--budget", "260").stdout)
        self.assertEqual(view["content"], repeated["content"])
        self.assertEqual(Path(view["source_map"]).read_bytes(), Path(repeated["source_map"]).read_bytes())

    def test_stale_index_and_missing_compdb_degrade_safely(self) -> None:
        self.scan()
        path = self.root / "src/plan.cpp"
        baseline = path.read_bytes()
        path.write_bytes(baseline + b"\n")
        status = json.loads(self.ctxpp("status").stdout)
        self.assertTrue(status["stale"])
        self.assertFalse(status["source_write_safe"])
        path.write_bytes(baseline)
        shutil.rmtree(self.root / "build")
        degraded = self.scan()
        self.assertEqual(degraded["backend"], "degraded-text-routing")
        where = json.loads(self.ctxpp("where", "freeze").stdout)
        self.assertTrue(where["incomplete"])

    def test_semantic_rename_apply_reverse_and_failed_verification_rollback(self) -> None:
        self.scan()
        original = (self.root / "src/plan.cpp").read_bytes()
        planned = json.loads(self.ctxpp("plan", "src/plan.cpp", "--rule", "semantic-local-rename").stdout)
        plan_path = Path(planned["path"])
        plan = json.loads(plan_path.read_text())
        self.assertTrue(plan["edits"])
        self.assertTrue(all(e["proof"] == "P1" for e in plan["edits"]))
        applied = json.loads(self.ctxpp("apply", str(plan_path)).stdout)
        self.assertTrue(applied["success"])
        changed = (self.root / "src/plan.cpp").read_text()
        self.assertIn("int bi = initialization_order_anchor", changed)
        self.assertIn("PackingPlan::freeze", changed)
        idempotent = self.ctxpp("plan", "src/plan.cpp", ok=False)
        self.assertNotEqual(idempotent.returncode, 0)
        reversed_result = json.loads(self.ctxpp("apply", applied["reverse_plan"]).stdout)
        self.assertTrue(reversed_result["success"])
        self.assertEqual((self.root / "src/plan.cpp").read_bytes(), original)

        self.scan()
        failed_plan = json.loads(self.ctxpp("plan", "src/plan.cpp").stdout)
        failed_path = Path(failed_plan["path"])
        payload = json.loads(failed_path.read_text())
        payload["verification"]["build"] = ["false"]
        failed_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        failed = json.loads(self.ctxpp("apply", str(failed_path), ok=False).stdout)
        self.assertFalse(failed["success"])
        self.assertTrue(failed["rolled_back"])
        self.assertEqual((self.root / "src/plan.cpp").read_bytes(), original)

    def test_same_tu_shard_compiles_reverses_and_is_idempotently_refused_after_apply(self) -> None:
        self.scan()
        original = (self.root / "src/plan.cpp").read_bytes()
        before_slice = json.loads(self.ctxpp("slice", "demo::PackingPlan::freeze", "--intent", "edit", "--budget", "500").stdout)["report"]["tokens"]
        planned = json.loads(self.ctxpp("shard", "src/plan.cpp").stdout)
        applied = json.loads(self.ctxpp("apply", planned["path"]).stdout)
        self.assertTrue(applied["success"])
        self.assertIn("#include \"", (self.root / "src/plan.cpp").read_text())
        after_slice = json.loads(self.ctxpp("slice", "demo::PackingPlan::freeze", "--intent", "edit", "--budget", "500").stdout)["report"]["tokens"]
        self.assertLessEqual(after_slice, before_slice)
        self.assertTrue(any(Path(self.root / x).suffix in (".inc", ".cuh") for x in applied["created"]))
        refused = self.ctxpp("apply", planned["path"], ok=False)
        self.assertNotEqual(refused.returncode, 0)
        reverse = json.loads(self.ctxpp("apply", applied["reverse_plan"]).stdout)
        self.assertTrue(reverse["success"])
        self.assertEqual((self.root / "src/plan.cpp").read_bytes(), original)
        for created in applied["created"]:
            self.assertFalse((self.root / created).exists())

    def test_lint_and_generated_path_refusal(self) -> None:
        self.scan()
        lint = json.loads(self.ctxpp("lint").stdout)
        self.assertTrue(lint["ok"], lint)
        refused = self.ctxpp("shard", ".ctxpp/views/generated.cpp", ok=False)
        self.assertNotEqual(refused.returncode, 0)

    def test_verify_plan_v2_runs_semantic_build_and_targeted_tests(self) -> None:
        self.scan()
        planned = json.loads(self.ctxpp("plan", "src/plan.cpp").stdout)
        verified = json.loads(self.ctxpp("verify", planned["path"], "--tier", "V2").stdout)
        self.assertTrue(verified["ok"])
        commands = [x["command"] for x in verified["runs"]]
        self.assertIn("ctxpp scan", commands)
        self.assertTrue(any("cmake --build" in x for x in commands))
        self.assertTrue(any("ctest" in x for x in commands))


if __name__ == "__main__":
    unittest.main()
