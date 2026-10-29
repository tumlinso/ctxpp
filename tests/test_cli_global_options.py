from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ctxpp_store
from ctxpp_lib import default_config, stable_json


class GlobalOptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.root.mkdir()
        self.state = self.base / "external-state"
        source = self.root / "alpha.cpp"
        content = b"int alpha() { return 4; }\n"
        source.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        config_hash = hashlib.sha256(stable_json(default_config()).encode()).hexdigest()
        records = [
            {"record": "meta", "format": "CTXPP-INDEX/1", "root": str(self.root),
             "config_hash": config_hash, "semantic": True, "backend": "libclang-runtime",
             "incomplete": False, "failures": [{"file": "alpha.cpp", "error": "fixture diagnostic"}]},
            {"record": "file", "path": "alpha.cpp", "hash": digest, "parse_status": "ok"},
            {"record": "symbol", "id": "alpha-id", "name": "alpha", "qualified_name": "alpha",
             "kind": "Function", "file": "alpha.cpp", "line": 1, "end_line": 1, "start": 0,
             "end": len(content) - 1, "definition": True, "signature": "int alpha()"},
        ]
        freshness = {"format": "CTXPP-FRESHNESS/1", "config_file_hash": None, "files": {
            "alpha.cpp": {"hash": digest, "size": len(content), "mtime_ns": source.stat().st_mtime_ns}}}
        manifest = {"format": "CTXPP-MANIFEST/2", "files": {"alpha.cpp": digest}}
        readiness = {"format": "CTXPP-READINESS/1", "semantic_stale_tus": []}
        ctxpp_store.publish(self.root, records, manifest, freshness, readiness=readiness,
                            state_path=self.state)

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(SCRIPTS / "ctxpp"), *args], cwd=self.base,
                              capture_output=True, text=True, env=os.environ.copy())

    def state_snapshot(self):
        return {p.relative_to(self.state).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.state.rglob("*") if p.is_file()}

    def test_shared_flags_work_before_and_after_subcommand(self) -> None:
        prefix = self.cli("--root", str(self.root), "--state-dir", str(self.state), "--json", "status")
        self.assertEqual(prefix.returncode, 0, prefix.stderr)
        prefix_status = json.loads(prefix.stdout)
        self.assertTrue(prefix_status["present"])

        readonly_prefix = self.cli("--root", str(self.root), "--read-only", "status", "--json",
                                   "--state-dir", str(self.state))
        self.assertEqual(readonly_prefix.returncode, 0, readonly_prefix.stderr)
        self.assertEqual(json.loads(readonly_prefix.stdout), prefix_status)

        before = self.state_snapshot()
        suffix = self.cli("status", "--read-only", "--json", "--root", str(self.root),
                          "--state-dir", str(self.state))
        self.assertEqual(suffix.returncode, 0, suffix.stderr)
        self.assertEqual(json.loads(suffix.stdout), prefix_status)
        self.assertEqual(self.state_snapshot(), before)

        route = self.cli("route", "alpha", "--json", "--root", str(self.root),
                         "--state-dir", str(self.state), "--read-only")
        self.assertEqual(route.returncode, 0, route.stderr)
        self.assertTrue(json.loads(route.stdout)["matches"])

    def test_read_only_status_does_not_write_diagnostic_logs(self) -> None:
        before = self.state_snapshot()
        result = self.cli("--root", str(self.root), "--state-dir", str(self.state),
                          "--read-only", "status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        failure = json.loads(result.stdout)["failures"][0]
        self.assertNotIn("details_log", failure)
        self.assertEqual(self.state_snapshot(), before)
        self.assertFalse((self.state / "logs").exists())

    def test_terminator_keeps_later_option_like_query_as_data(self) -> None:
        result = self.cli("--root", str(self.root), "--state-dir", str(self.state),
                          "--read-only", "route", "--", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("matches", json.loads(result.stdout))

    def test_read_only_still_rejects_mutating_commands(self) -> None:
        before = self.state_snapshot()
        result = self.cli("scan", "--root", str(self.root), "--state-dir", str(self.state), "--read-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--read-only is supported only", result.stderr)
        self.assertEqual(self.state_snapshot(), before)

    def test_prefix_help_still_works(self) -> None:
        result = self.cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: ctxpp", result.stdout)


if __name__ == "__main__":
    unittest.main()
