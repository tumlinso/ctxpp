from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

from ctxpp_lib import load_config, summarize_diagnostics, verify_commands
from ctxpp_recipe import translate


class RepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        (self.root / "src").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / ".ctxpp.toml").write_text(
            'version=1\nprofile="view"\nsource_write=false\n'
            'sources=["src/**/*.cpp","src/**/*.cu","tests/**/*.cpp"]\n'
            'exclude=["build/**","third_party/**","vendor/**","generated/**",".ctxpp/**"]\n', encoding="utf-8")
        (self.root / "src/base.cpp").write_text("int base_value(){return 1;}\n", encoding="utf-8")
        (self.root / "src/orphan.cpp").write_text(
            "struct PreparedExecution { int backend_capability; int workspace; };\n", encoding="utf-8")
        for number in range(10):
            (self.root / f"tests/noise{number}.cpp").write_text(
                f"int noise_{number}(){{int workspace={number};return workspace;}}\n", encoding="utf-8")
        command = ["/usr/bin/c++", "-std=c++17", "-c", str(self.root / "src/base.cpp"), "-o", "base.o"]
        (self.root / "compile_commands.json").write_text(json.dumps([
            {"directory": str(self.root), "file": str(self.root / "src/base.cpp"), "arguments": command}
        ]), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def ctxpp(self, *args: str, env: dict[str, str] | None = None) -> tuple[dict, subprocess.CompletedProcess[str]]:
        proc = subprocess.run([str(SCRIPTS / "ctxpp"), "--root", str(self.root), "--json", *args],
                              cwd=self.root, env={**os.environ, **(env or {})}, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout), proc

    def test_orphan_inventory_exact_lookup_and_relevant_route(self) -> None:
        self.ctxpp("scan")
        where, _ = self.ctxpp("where", "PreparedExecution")
        self.assertEqual(where["matches"][0]["file"], "src/orphan.cpp")
        self.assertTrue(where["matches"][0]["degraded"])
        route, _ = self.ctxpp("route", "backend capability workspace")
        self.assertEqual(route["matches"][0]["file"], "src/orphan.cpp")
        self.assertLessEqual(len(route["matches"]), 8)
        self.assertFalse(any(match["file"].startswith("tests/noise") for match in route["matches"]))
        (self.root / "src/a_noise.cpp").write_text("int late_prepared_execution = 0;\n", encoding="utf-8")
        late = self.root / "src/late.cpp"
        late.write_text("struct LatePreparedExecution {};\n", encoding="utf-8")
        late_where, _ = self.ctxpp("where", "LatePreparedExecution")
        self.assertEqual(late_where["matches"][0]["file"], "src/late.cpp")

    def test_observed_focused_parse_promotes_and_hot_where_parses_zero(self) -> None:
        self.ctxpp("scan")
        cfg, _ = load_config(self.root)
        cfg["verification"]["build"] = [f"/usr/bin/c++ -std=c++17 -c {self.root / 'src/orphan.cpp'} -o {self.root / 'orphan.o'}"]
        result = verify_commands(self.root, cfg, ["build"])
        self.assertEqual(result[0]["returncode"], 0)
        promoted, _ = self.ctxpp("where", "PreparedExecution")
        self.assertFalse(promoted["matches"][0].get("degraded", False))
        profile = self.root / "where-profile.json"
        where, _ = self.ctxpp("where", "PreparedExecution", env={"CTXPP_PROFILE_PATH": str(profile)})
        self.assertFalse(where["matches"][0].get("degraded", False))
        counters = json.loads(profile.read_text())["counters"]
        self.assertEqual(counters.get("tus_parsed", 0), 0)
        for number, command in enumerate((("route", "backend capability workspace"),
                                          ("slice", "PreparedExecution", "--budget", "500"))):
            path = self.root / f"clean-{number}.json"
            self.ctxpp(*command, env={"CTXPP_PROFILE_PATH": str(path)})
            self.assertEqual(json.loads(path.read_text())["counters"].get("tus_parsed", 0), 0)
        self.ctxpp("scan")
        persisted, _ = self.ctxpp("where", "PreparedExecution")
        self.assertFalse(persisted["matches"][0].get("degraded", False))

    def test_nvcc_translation_filters_codegen_and_maps_architecture(self) -> None:
        source = self.root / "src/kernel.cu"
        source.write_text("__global__ void kernel(){}\n", encoding="utf-8")
        record = {"directory": str(self.root), "file": str(source), "arguments": [
            "/usr/local/cuda/bin/nvcc", "-gencode", "arch=compute_70,code=sm_70", "-Xcompiler", "-fPIC,-Wall",
            "-Xptxas", "-v", "--threads", "8", "-o", "kernel.o", "-MF", "kernel.d", "-I", "include",
            "-D", "FEATURE=1", "-std=c++17", str(source)]}
        recipe = translate(record, source)
        joined = " ".join(recipe["clang_argv"])
        self.assertIn("--cuda-gpu-arch=sm_70", joined)
        self.assertIn("-I include", joined)
        self.assertIn("-D FEATURE=1", joined)
        for option in ("-Xptxas", "--threads", "-o", "-MF", "-Wall"):
            self.assertNotIn(option, recipe["clang_argv"])

    def test_diagnostics_are_grouped_bounded_and_logged(self) -> None:
        failures = [{"file": f"src/k{number}.cu", "configuration": 0,
                     "error": "clang: error: unsupported option '--threads'"} for number in range(30)]
        summary = summarize_diagnostics(self.root, failures, "repair-test")
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["count"], 30)
        self.assertEqual(summary[0]["affected_file_count"], 30)
        self.assertEqual(summary[0]["category"], "command_translation")
        self.assertTrue((self.root / summary[0]["details_log"]).is_file())
        self.assertLess(len(json.dumps(summary)), 8192)

    def test_fatal_recipe_preflight_stops_before_ast_and_compatibility_smoke(self) -> None:
        scan, _ = self.ctxpp("scan")
        self.assertEqual(scan["format"], "CTXPP-SCAN/1")
        fake = self.root / "fake-clang"
        fake.write_text("#!/bin/sh\necho \"clang: error: unsupported option '--threads'\" >&2\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)
        profile = self.root / "preflight-profile.json"
        rescanned, _ = self.ctxpp("scan", "src/orphan.cpp",
                                  env={"CTXPP_CLANG": str(fake), "CTXPP_PROFILE_PATH": str(profile)})
        self.assertGreaterEqual(rescanned["failures"], 1)
        counters = json.loads(profile.read_text())["counters"]
        self.assertEqual(counters.get("asts_constructed", 0), 0)
        status, _ = self.ctxpp("status")
        self.assertEqual(status["format"], "CTXPP-STATUS/1")
        self.assertEqual(status["failures"][0]["category"], "command_translation")
        where, _ = self.ctxpp("where", "PreparedExecution")
        route, _ = self.ctxpp("route", "backend capability workspace")
        lint, _ = self.ctxpp("lint", "src/orphan.cpp")
        self.assertEqual(where["format"], "CTXPP-WHERE/1")
        self.assertEqual(route["format"], "CTXPP-ROUTE/1")
        self.assertEqual(lint["format"], "CTXPP-LINT/1")


if __name__ == "__main__":
    unittest.main()
