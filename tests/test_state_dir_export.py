from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from ctxpp_lib import CtxppError, load_export, load_index
from ctxpp_paths import state_dir


class StateDirectoryTests(unittest.TestCase):
    def test_default_remains_repo_local_and_override_is_external(self) -> None:
        root = Path("/tmp/ctxpp-source").resolve()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(state_dir(root), root / ".ctxpp")
        with mock.patch.dict(os.environ, {"CTXPP_STATE_DIR": "/tmp/ctxpp-state"}):
            self.assertEqual(state_dir(root), Path("/tmp/ctxpp-state"))

    def test_export_reads_published_generation_without_touching_source_or_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "source"
            root.mkdir()
            external = base / "state"
            external.mkdir()
            index = b'{"format":"CTXPP-INDEX/1","record":"meta","root":"' + str(root).encode() + b'"}\n'
            (external / "index.jsonl").write_bytes(index)
            (external / "manifest.json").write_text(json.dumps({
                "format": "CTXPP-MANIFEST/1", "index_hash": hashlib.sha256(index).hexdigest(), "files": {}
            }))
            (external / "cache").mkdir()
            freshness = {"format": "CTXPP-FRESHNESS/1", "files": {}}
            (external / "cache/freshness.json").write_text(json.dumps(freshness))
            before = sorted((str(path.relative_to(external)), path.read_bytes())
                            for path in external.rglob("*") if path.is_file())
            result = load_export(root, external)
            after = sorted((str(path.relative_to(external)), path.read_bytes())
                           for path in external.rglob("*") if path.is_file())
            self.assertEqual(result["format"], "CTXPP-EXPORT/1")
            self.assertTrue(result["index_integrity"])
            self.assertEqual(result["freshness_scope"], "as-recorded-at-index-time")
            self.assertEqual(before, after)
            self.assertEqual(list(root.iterdir()), [])

    def test_missing_export_is_a_clear_error_and_does_not_initialize(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            root.mkdir()
            state = Path(temp) / "state"
            with self.assertRaisesRegex(CtxppError, "run ctxpp scan explicitly"):
                load_export(root, state)
            self.assertFalse(state.exists())

    def test_cli_export_with_external_state_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, state = base / "source", base / "state"
            root.mkdir(); state.mkdir(); (state / "cache").mkdir()
            index = (json.dumps({"format": "CTXPP-INDEX/1", "record": "meta", "root": str(root)}, separators=(",", ":")) + "\n").encode()
            (state / "index.jsonl").write_bytes(index)
            (state / "manifest.json").write_text(json.dumps({"index_hash": hashlib.sha256(index).hexdigest(), "files": {}}))
            (state / "cache/freshness.json").write_text(json.dumps({"format": "CTXPP-FRESHNESS/1", "files": {}}))
            command = [str(SCRIPTS / "ctxpp"), "--root", str(root), "--state-dir", str(state),
                       "--read-only", "--json", "export"]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["format"], "CTXPP-EXPORT/1")
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
