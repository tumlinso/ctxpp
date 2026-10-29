from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from ctxpp_agents import AgentInstructionsError, prepare_agent_instructions, write_agent_instructions


START = b"<!-- agent-interface:ctxpp:start -->"
END = b"<!-- agent-interface:ctxpp:end -->"


class AgentInstructionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_adds_portable_block_and_is_byte_idempotent(self) -> None:
        path = self.root / "AGENTS.md"
        original = b"# Project\n\nKeep this line exactly.\n"
        path.write_bytes(original)
        update = prepare_agent_instructions(self.root)
        self.assertIsNotNone(update)
        write_agent_instructions(update)
        result = path.read_bytes()
        self.assertTrue(result.startswith(original + b"\n"))
        self.assertEqual(result.count(START), 1)
        self.assertEqual(result.count(END), 1)
        self.assertIn(b"This repository uses ctxpp as its canonical C++ context interface.", result)
        self.assertIn(b"Route C++ symbol lookup, navigation, dependency tracing, and source-context requests through `ctxpp`.", result)
        self.assertIn(b"Canonical source files remain authoritative; `.ctxpp/` contains generated retrieval artifacts.", result)
        self.assertIn(b"https://github.com/tumlinso/ctxpp", result)
        self.assertNotIn(b"/home/", result)
        self.assertIsNone(prepare_agent_instructions(self.root))

    def test_updates_owned_block_and_preserves_suffix_bytes(self) -> None:
        path = self.root / "AGENTS.md"
        original = (b"before\r\n" + START + b"\r\nold instructions\r\n" + END
                    + b"\r\nafter\r\n")
        path.write_bytes(original)
        path.chmod(0o640)
        write_agent_instructions(prepare_agent_instructions(self.root))
        updated = path.read_bytes()
        self.assertTrue(updated.startswith(b"before\r\n" + START))
        self.assertTrue(updated.endswith(b"\r\nafter\r\n"))
        self.assertIn(b"\r\n", updated)
        self.assertEqual(updated.count(START), 1)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertIsNone(prepare_agent_instructions(self.root))

    def test_malformed_duplicate_reversed_and_partial_markers_fail(self) -> None:
        path = self.root / "AGENTS.md"
        malformed = (
            START,
            START + b"\n" + END + b"\n" + START + b"\n" + END,
            END + b"\n" + START,
            b"<!-- agent-interface:ctxpp:start",
        )
        for content in malformed:
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaises(AgentInstructionsError):
                    prepare_agent_instructions(self.root)
                self.assertEqual(path.read_bytes(), content)

    def test_refuses_symlink_and_nonregular_agents_file(self) -> None:
        path = self.root / "AGENTS.md"
        other = self.root / "instructions.md"
        other.write_bytes(b"keep")
        path.symlink_to(other)
        with self.assertRaises(AgentInstructionsError):
            prepare_agent_instructions(self.root)
        path.unlink()
        path.mkdir()
        with self.assertRaises(AgentInstructionsError):
            prepare_agent_instructions(self.root)

    def test_write_detects_concurrent_changes(self) -> None:
        path = self.root / "AGENTS.md"
        path.write_bytes(b"original")
        update = prepare_agent_instructions(self.root)
        path.write_bytes(b"concurrent edit")
        with self.assertRaises(AgentInstructionsError):
            write_agent_instructions(update)
        self.assertEqual(path.read_bytes(), b"concurrent edit")


if __name__ == "__main__":
    unittest.main()
