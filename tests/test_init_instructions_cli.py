from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ctxpp"
START = b"<!-- agent-interface:ctxpp:start -->"
END = b"<!-- agent-interface:ctxpp:end -->"


class InitInstructionsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def invoke(self, mode: str | None = None) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(SCRIPT), "--root", str(self.root), "--json", "init",
                   "--no-build-core", "--no-scan"]
        if mode:
            command.extend(("--agent-instructions", mode))
        return subprocess.run(command, text=True, capture_output=True,
                              env={**os.environ, "XDG_CACHE_HOME": str(self.root / "cache")})

    def test_default_init_writes_agent_block_and_reports_it(self) -> None:
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["agent_instructions"], {"mode": "write", "written": True})
        content = (self.root / "AGENTS.md").read_bytes()
        self.assertEqual(content.count(START), 1)
        self.assertEqual(content.count(END), 1)
        self.assertTrue((self.root / ".ctxpp.toml").is_file())

    def test_skip_succeeds_without_inspecting_a_symlinked_agents_file(self) -> None:
        target = self.root / "target.md"
        target.write_bytes(b"preserve")
        agents = self.root / "AGENTS.md"
        agents.symlink_to(target)
        result = self.invoke("skip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(agents.is_symlink())
        self.assertEqual(target.read_bytes(), b"preserve")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["agent_instructions"], {"mode": "skip", "written": False})

    def test_malformed_guidance_fails_before_config_or_index_mutations(self) -> None:
        agents = self.root / "AGENTS.md"
        agents.write_bytes(START + b"\nwithout end marker")
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("ctxpp interface markers", result.stderr)
        self.assertFalse((self.root / ".ctxpp.toml").exists())
        self.assertFalse((self.root / ".ctxpp").exists())
        self.assertEqual(agents.read_bytes(), START + b"\nwithout end marker")

    def test_skip_does_not_create_agents_file(self) -> None:
        result = self.invoke("skip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "AGENTS.md").exists())


if __name__ == "__main__":
    unittest.main()
