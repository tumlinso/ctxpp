from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import sys

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
FIXTURE = SKILL / "tests/fixtures/sample"
sys.path.insert(0, str(SCRIPTS))

from ctxpp_lib import Tokenizer, load_index, partition_index, source_text


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

    def profiled(self, name: str, *args: str) -> dict:
        profile = self.root / f".{name}.profile.json"
        env = {**os.environ, "CTXPP_PROFILE_PATH": str(profile)}
        command = [str(SCRIPTS / "ctxpp"), "--root", str(self.root), "--json", *args]
        proc = subprocess.run(command, cwd=self.root, env=env, text=True, capture_output=True)
        if proc.returncode != 0:
            self.fail(f"ctxpp failed: {' '.join(command)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return json.loads(profile.read_text())

    def scan(self) -> dict:
        return json.loads(self.ctxpp("scan").stdout)

    def test_init_is_agent_owned_idempotent_and_preserves_existing_configuration(self) -> None:
        config = self.root / ".ctxpp.toml"
        config.unlink()
        initialized = json.loads(self.ctxpp("init").stdout)
        self.assertTrue(initialized["ok"])
        self.assertTrue(initialized["configuration"]["created"])
        self.assertTrue(initialized["semantic_core"]["available"])
        self.assertTrue(initialized["ready"]["semantic_routing"])
        self.assertEqual(initialized["scan"]["backend"], "libclang-runtime")
        baseline_config = config.read_bytes()
        baseline_index = (self.root / ".ctxpp/index.jsonl").read_bytes()

        repeated = json.loads(self.ctxpp("init").stdout)
        self.assertTrue(repeated["configuration"]["preserved"])
        self.assertEqual(config.read_bytes(), baseline_config)
        self.assertEqual((self.root / ".ctxpp/index.jsonl").read_bytes(), baseline_index)

    def test_init_reports_agent_action_instead_of_claiming_semantic_readiness_without_compdb(self) -> None:
        (self.root / ".ctxpp.toml").unlink()
        shutil.rmtree(self.root / "build")
        initialized = json.loads(self.ctxpp("init", "--no-build-core").stdout)
        self.assertTrue(initialized["ok"])
        self.assertIsNone(initialized["compilation_database"])
        self.assertFalse(initialized["ready"]["semantic_routing"])
        self.assertTrue(any("compile_commands.json" in action for action in initialized["agent_actions"]))
        self.assertEqual(initialized["external_blockers"], [])

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

    def test_warm_and_incremental_scan_do_not_materialize_unchanged_tu_payloads(self) -> None:
        self.scan()
        warm = self.profiled("warm-scan", "scan")["counters"]
        self.assertEqual(warm.get("tus_parsed", 0), 0)
        self.assertEqual(warm.get("asts_constructed", 0), 0)
        self.assertEqual(warm.get("semantic_records_updated", 0), 0)
        self.assertEqual(warm.get("tu_cache_payloads_materialized", 0), 0)

        source = self.root / "src/other.cpp"
        source.write_bytes(source.read_bytes() + b"\n")
        incremental = self.profiled("incremental-scan", "scan")["counters"]
        self.assertEqual(incremental.get("tus_parsed", 0), 1)
        self.assertEqual(incremental.get("tu_cache_payloads_materialized", 0), 0)
        found = json.loads(self.ctxpp("where", "demo::call_overload").stdout)
        self.assertEqual(found["matches"][0]["file"], "src/other.cpp")

    def test_hot_queries_and_lazy_targeted_refresh_use_minimum_tus(self) -> None:
        self.scan()
        for number, command in enumerate((
            ("where", "demo::PackingPlan::freeze"),
            ("route", "freeze score test"),
            ("slice", "demo::PackingPlan::freeze", "--budget", "500"),
        )):
            counters = self.profiled(f"clean-{number}", *command)["counters"]
            self.assertEqual(counters.get("tus_parsed", 0), 0)
            self.assertEqual(counters.get("workers_started", 0), 0)

        other = self.root / "src/other.cpp"
        other.write_bytes(other.read_bytes() + b"\n")
        unrelated = self.profiled("unrelated", "where", "demo::PackingPlan::freeze")["counters"]
        self.assertEqual(unrelated.get("tus_parsed", 0), 0)

        plan = self.root / "src/plan.cpp"
        plan.write_text(plan.read_text().replace("block_index = initialization_order_anchor;",
                                                 "block_index = initialization_order_anchor + 0;"))
        related = self.profiled("related", "where", "demo::PackingPlan::freeze")["counters"]
        self.assertEqual(related.get("tus_parsed", 0), 1)
        self.assertLessEqual(related.get("peak_concurrent_workers", 0), 1)

        header = self.root / "include/plan.hpp"
        header.write_bytes(header.read_bytes() + b"\n")
        header_related = self.profiled("header", "where", "demo::PackingPlan::freeze")["counters"]
        self.assertEqual(header_related.get("tus_parsed", 0), 1)
        status = json.loads(self.ctxpp("status").stdout)
        self.assertTrue(any(reason.startswith("semantic-stale:") for reason in status["reasons"]))

    def test_same_size_preserved_mtime_edit_is_not_treated_as_clean(self) -> None:
        self.scan()
        source = self.root / "src/plan.cpp"
        prior = source.stat()
        changed = source.read_bytes().replace(b"value + 0.5", b"value + 0.6")
        self.assertEqual(len(changed), prior.st_size)
        source.write_bytes(changed)
        os.utime(source, ns=(prior.st_atime_ns, prior.st_mtime_ns))
        status = json.loads(self.ctxpp("status").stdout)
        self.assertTrue(status["stale"])
        self.assertFalse(status["source_write_safe"])
        counters = self.profiled("preserved-mtime", "where", "demo::overloaded")['counters']
        self.assertEqual(counters.get("tus_parsed", 0), 1)

    def test_new_symbol_in_previously_empty_included_header_routes_via_targeted_refresh(self) -> None:
        header = self.root / "include/empty.hpp"
        header.write_text("", encoding="utf-8")
        source = self.root / "src/plan.cpp"
        source.write_text('#include "empty.hpp"\n' + source.read_text(encoding="utf-8"), encoding="utf-8")
        self.scan()
        header.write_text("#pragma once\nnamespace demo { inline int newly_added_api(){return 7;} }\n", encoding="utf-8")
        counters = self.profiled("empty-header", "where", "demo::newly_added_api")["counters"]
        self.assertEqual(counters.get("tus_parsed", 0), 1)
        found = json.loads(self.ctxpp("where", "demo::newly_added_api").stdout)
        self.assertEqual(found["matches"][0]["file"], "include/empty.hpp")

    def test_external_tokenizer_cache_invalidates_when_adapter_changes(self) -> None:
        adapter = self.root / "token-count.py"
        adapter.write_text("import sys\nprint(len(sys.stdin.read()))\n", encoding="utf-8")
        configured = "external:python3 token-count.py"
        first = Tokenizer(self.root, configured).count("abcd")
        adapter.write_text("import sys\nprint(len(sys.stdin.read())+100)\n", encoding="utf-8")
        second = Tokenizer(self.root, configured).count("abcd")
        self.assertEqual(first.count, 4)
        self.assertEqual(second.count, 104)

    def test_concurrent_identical_views_compute_once(self) -> None:
        self.scan()
        shutil.rmtree(self.root / ".ctxpp/cache/views", ignore_errors=True)
        command = [str(SCRIPTS / "ctxpp"), "--root", str(self.root), "--json", "view",
                   "demo::PackingPlan::freeze", "--budget", "260"]
        profiles = [self.root / ".view-a.json", self.root / ".view-b.json"]
        processes = [subprocess.Popen(command, cwd=self.root, env={**os.environ, "CTXPP_PROFILE_PATH": str(profile)},
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for profile in profiles]
        outputs = [process.communicate() for process in processes]
        for process, (stdout, stderr) in zip(processes, outputs):
            self.assertEqual(process.returncode, 0, stderr)
            json.loads(stdout)
        counters = [json.loads(profile.read_text())["counters"] for profile in profiles]
        self.assertEqual(sum(item.get("compact_view_transforms", 0) for item in counters), 8)
        self.assertEqual(sum(item.get("core_calls", 0) for item in counters), 8)

    def test_private_query_cache_is_disposable_and_command_changes_are_local(self) -> None:
        self.scan()
        expected = self.ctxpp("where", "demo::PackingPlan::freeze").stdout
        (self.root / ".ctxpp/cache/query.sqlite").unlink()
        self.assertEqual(self.ctxpp("where", "demo::PackingPlan::freeze").stdout, expected)
        self.scan()

        database = self.root / "build/compile_commands.json"
        records = json.loads(database.read_text())
        first = records[0]
        if "arguments" in first:
            first["arguments"].insert(1, "-DCTXPP_COMMAND_CHANGED=1")
        else:
            first["command"] += " -DCTXPP_COMMAND_CHANGED=1"
        database.write_text(json.dumps(records, separators=(",", ":")) + "\n")
        profile = self.profiled("command-change", "scan")["counters"]
        self.assertEqual(profile.get("tus_parsed", 0), 1)

    def test_concurrent_scans_publish_only_complete_generations(self) -> None:
        self.scan()
        plan = self.root / "src/plan.cpp"
        plan.write_bytes(plan.read_bytes() + b"\n")
        command = [str(SCRIPTS / "ctxpp"), "--root", str(self.root), "--json", "scan"]
        first = subprocess.Popen(command, cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen(command, cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first_out, first_err = first.communicate()
        second_out, second_err = second.communicate()
        self.assertEqual(first.returncode, 0, first_err)
        self.assertEqual(second.returncode, 0, second_err)
        json.loads(first_out)
        json.loads(second_out)
        for line in (self.root / ".ctxpp/index.jsonl").read_text().splitlines():
            json.loads(line)
        found = json.loads(self.ctxpp("where", "demo::PackingPlan::freeze").stdout)
        self.assertEqual(found["matches"][0]["file"], "src/plan.cpp")

    def test_stale_parse_result_cannot_replace_newer_source_generation(self) -> None:
        real = SKILL / "tool/build/ctxpp-core"
        wrapper = self.root / "slow-core.py"
        log = self.root / "slow-core.log"
        wrapper.write_text(
            "#!/usr/bin/env python3\nimport os,sys,time\n"
            f"real={str(real)!r}; log={str(log)!r}\n"
            "if len(sys.argv)>1 and sys.argv[1]=='scan':\n"
            " open(log,'a').write('scan\\n'); time.sleep(0.5)\n"
            "os.execv(real,[real,*sys.argv[1:]])\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        env = {**os.environ, "CTXPP_CORE": str(wrapper)}
        command = [str(SCRIPTS / "ctxpp"), "--root", str(self.root), "--json", "scan"]
        self.assertEqual(subprocess.run(command, cwd=self.root, env=env, capture_output=True).returncode, 0)
        baseline_index = (self.root / ".ctxpp/index.jsonl").read_bytes()
        log.unlink(missing_ok=True)
        source = self.root / "src/plan.cpp"
        source.write_bytes(source.read_bytes() + b"\n")
        running = subprocess.Popen(command, cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 5
        while not log.exists() and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(log.exists())
        source.write_bytes(source.read_bytes() + b"// newer generation\n")
        _, stderr = running.communicate()
        self.assertNotEqual(running.returncode, 0, stderr.decode())
        self.assertEqual((self.root / ".ctxpp/index.jsonl").read_bytes(), baseline_index)

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
