from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ctxpp_packet
from ctxpp_lib import Tokenizer


class RelationshipTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target_data = b"int foo() { return 1; }\n"
        self.caller_indexed_data = b"int caller() { foo(); return 1; }\n"
        (self.root / "api.cpp").write_bytes(self.target_data)
        (self.root / "caller.cpp").write_bytes(self.caller_indexed_data)
        start = self.target_data.index(b"int foo")
        self.target = {
            "id": "foo-id", "name": "foo", "qualified_name": "foo", "kind": "FunctionDecl",
            "file": "api.cpp", "start": start, "end": len(self.target_data) - 1,
            "line": 1, "end_line": 1, "definition": True, "body_present": True,
            "degraded": False, "signature": "int foo()", "source_hash": self._hash(self.target_data),
        }
        self.caller = {
            "id": "caller-id", "name": "caller", "qualified_name": "caller", "kind": "FunctionDecl",
            "file": "caller.cpp", "start": 0, "end": len(self.caller_indexed_data) - 1,
            "line": 1, "end_line": 1, "definition": True, "degraded": False,
        }
        self.edge = {
            "record": "edge", "type": "call", "from": "caller-id", "to": "foo-id",
            "file": "caller.cpp", "start": self.caller_indexed_data.index(b"foo()"),
            "end": self.caller_indexed_data.index(b"foo()") + 5, "line": 1,
        }
        self.files = [
            {"record": "file", "path": "api.cpp", "hash": self._hash(self.target_data)},
            {"record": "file", "path": "caller.cpp", "hash": self._hash(self.caller_indexed_data)},
        ]

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _packet(self, meta_extra: dict | None = None) -> dict:
        coverage = {
            "range_valid": True, "target_complete": True,
            "representation_kind": "semantic-definition",
        }
        with patch.object(ctxpp_packet, "recover_target_representation", return_value=(self.target, coverage)), \
                patch.object(ctxpp_packet, "bounded_graph", return_value=(
                    {"semantic": True, "backend": "test", "_generation": "generation-1", **(meta_extra or {})},
                    self.files, [self.target, self.caller], [self.edge], False,
                )):
            return ctxpp_packet.build_context_packet(
                self.root, "foo", self.target, intent="edit", budget=10000,
                max_items=12, tokenizer=Tokenizer(self.root, "none"),
            )

    def test_current_incoming_call_has_verified_location(self) -> None:
        packet = self._packet()
        self.assertEqual(packet["trust"]["relationships"], "semantic")
        self.assertEqual(len(packet["context"]["callers"]), 1)
        location = packet["context"]["callers"][0]["location"]
        self.assertEqual(location["content_sha256"], self._hash(self.caller_indexed_data))

    def test_related_location_keeps_indexed_hash_if_file_changes_after_check(self) -> None:
        expected = self._hash(self.caller_indexed_data)
        changed = "f" * 64
        caller_hash_calls = 0
        real_hash = ctxpp_packet.sha256_file

        def changing_hash(path: Path) -> str:
            nonlocal caller_hash_calls
            if path == self.root / "caller.cpp":
                caller_hash_calls += 1
                return expected if caller_hash_calls == 1 else changed
            return real_hash(path)

        with patch.object(ctxpp_packet, "sha256_file", side_effect=changing_hash):
            packet = self._packet()
        location = packet["context"]["callers"][0]["location"]
        self.assertEqual(location["content_sha256"], expected)
        self.assertNotEqual(location["content_sha256"], changed)

    def test_explicitly_nonsemantic_edge_does_not_inherit_symbol_trust(self) -> None:
        self.edge["semantic"] = False
        packet = self._packet()

        self.assertEqual(packet["trust"]["relationships"], "semantic")
        self.assertFalse(packet["context"]["callers"][0]["semantic"])

    def test_readonly_packet_omits_old_incoming_call_after_last_call_is_removed(self) -> None:
        current_caller = b"int caller() { return 0; }\n"
        (self.root / "caller.cpp").write_bytes(current_caller)
        packet = self._packet()

        self.assertEqual(packet["context"]["callers"], [])
        self.assertEqual(packet["coverage"]["omitted"]["stale_relationships"], 1)
        self.assertFalse(packet["coverage"]["sufficient"])
        self.assertEqual(packet["trust"]["relationships"], "lexical-or-partial")
        rendered = ctxpp_packet.render_inspect(packet)
        self.assertNotIn("semantic indexed relationship", rendered)
        self.assertNotIn("caller.cpp", rendered)

    def test_stale_translation_unit_metadata_suppresses_current_file_edge(self) -> None:
        self.edge["translation_unit"] = "src/caller.cpp"
        packet = self._packet({"_stale_relationship_tus": ["src/caller.cpp"]})

        self.assertEqual(packet["context"]["callers"], [])
        self.assertEqual(packet["coverage"]["omitted"]["stale_relationships"], 1)
        self.assertFalse(packet["coverage"]["sufficient"])

    def test_missing_translation_unit_with_incomplete_provenance_is_unknown(self) -> None:
        packet = self._packet({"_relationship_provenance_incomplete": True})

        self.assertEqual(packet["context"]["callers"], [])
        self.assertEqual(packet["coverage"]["omitted"]["unknown_relationships"], 1)
        self.assertFalse(packet["coverage"]["sufficient"])


if __name__ == "__main__":
    unittest.main()
