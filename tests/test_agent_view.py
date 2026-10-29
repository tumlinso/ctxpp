from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from ctxpp_packet import (render_ambiguous_inspect, render_inspect, render_task_inspect,
                          _configured_test_build_hints,
                          _target_declaration_block)  # noqa: E402


def _location(root: Path, relative: str, start: int, end: int) -> dict:
    path = root / relative
    data = path.read_bytes()
    return {
        "path": relative,
        "line": data[:start].count(b"\n") + 1,
        "end_line": data[:end].count(b"\n") + 1,
        "byte_start": start,
        "byte_end": end,
        "content_sha256": hashlib.sha256(data).hexdigest(),
    }


def _symbol(root: Path, relative: str, text: str, *, name: str, kind: str) -> dict:
    data = (root / relative).read_bytes()
    encoded = text.encode()
    start = data.index(encoded)
    end = start + len(encoded)
    return {
        "id": name,
        "name": name,
        "kind": kind,
        "location": _location(root, relative, start, end),
        "text_sha256": hashlib.sha256(encoded).hexdigest(),
        "content": text,
        "canonical": True,
    }


class _ByteTokenizer:
    @staticmethod
    def count(text: str) -> SimpleNamespace:
        return SimpleNamespace(count=(len(text.encode("utf-8")) + 3) // 4)


class AgentViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = (
            "struct Input { int value; };\n"
            "//@ req: value>=0\n"
            "int target(const Input& input) {\n"
            "    if (input.value < 0) return -1;\n"
            "    return input.value;\n"
            "}\n"
        )
        self.test_source = (
            "#include <cassert>\n"
            "int main() {\n"
            "    Input input{3};\n"
            "    const int result = target(input);\n"
            "    assert(result == 3);\n"
            "    assert(input.value == 3);\n"
            "    return 0;\n"
            "}\n"
        )
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src/unit.cc").write_text(self.source)
        (self.root / "tests/unit_test.cc").write_text(self.test_source)
        self.target = _symbol(
            self.root, "src/unit.cc", "int target(const Input& input) {\n"
            "    if (input.value < 0) return -1;\n"
            "    return input.value;\n"
            "}", name="target", kind="FunctionDecl",
        )
        self.type_symbol = _symbol(
            self.root, "src/unit.cc", "struct Input { int value; };",
            name="Input", kind="RecordDecl",
        )
        call = "const int result = target(input);"
        start = self.test_source.index(call)
        self.test_item = {
            "id": "test-main", "name": "main::result", "kind": "VarDecl",
            "relation": "test_relationship", "direction": "incoming", "semantic": True,
            "location": _location(self.root, "tests/unit_test.cc", start, start + len(call)),
        }
        target_line = self.source.splitlines().index("//@ req: value>=0") + 1
        self.packet = {
            "format": "CTXPP-CONTEXT-PACKET/1",
            "source_identity": {"repo_root": str(self.root)},
            "request": {"target": "target", "intent": "edit", "budget_tokens": 2400, "max_items": 12},
            "target": self.target,
            "trust": {"target_range": "hash-verified", "relationships": "semantic"},
            "coverage": {"sufficient": True, "target_complete": True, "budget_exceeded": False, "omitted": {}},
            "estimated_tokens": 450,
            "context": {"types": [self.type_symbol], "dependencies": [], "callers": [], "tests": [self.test_item], "callees": []},
            "invariants": [{"kind": "req", "text": "value>=0", "path": "src/unit.cc", "line": target_line}],
            "expansions": [],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_inspect_shows_verified_target_contract_type_and_nearby_test(self) -> None:
        rendered = render_inspect(self.packet, _ByteTokenizer())
        self.assertIn("canonical source verified; complete range", rendered)
        self.assertIn("indexed range verified; src/unit.cc:", rendered)
        self.assertNotIn("sha256=", rendered)
        self.assertIn("return input.value;", rendered)
        self.assertIn("contracts at src/unit.cc:", rendered)
        self.assertIn("req:value>=0", rendered)
        self.assertIn("struct Input", rendered)
        self.assertIn("partial nearby window; not the full test", rendered)
        self.assertIn("assert(result == 3);", rendered)
        self.assertIn("tests/unit_test.cc", rendered)
        self.assertLessEqual(len(rendered.encode()), 6000)

    def test_stale_target_range_is_withheld_even_when_source_file_hash_is_current(self) -> None:
        changed = self.source.replace("input.value < 0", "input.value <= 0")
        (self.root / "src/unit.cc").write_text(changed)
        stale_range_packet = dict(self.packet)
        stale_range_packet["target"] = dict(self.target)
        stale_range_packet["target"]["location"] = dict(self.target["location"])
        stale_range_packet["target"]["location"]["content_sha256"] = hashlib.sha256(
            changed.encode("utf-8")
        ).hexdigest()

        rendered = render_inspect(stale_range_packet, _ByteTokenizer())
        self.assertIn("stale or unverified; code excerpts withheld", rendered)
        self.assertNotIn("input.value <= 0", rendered)
        self.assertNotIn("sha256=", rendered)

    def test_stale_source_is_not_rendered_and_has_bounded_refresh_route(self) -> None:
        (self.root / "tests/unit_test.cc").write_text(self.test_source.replace("result == 3", "result == 4"))
        rendered = render_inspect(self.packet, _ByteTokenizer())
        self.assertNotIn("assert(result == 3);", rendered)
        self.assertIn("source is missing or changed", rendered)
        self.assertIn("next bounded context: ctxpp packet target", rendered)

    def test_lexical_relationships_are_not_presented_as_semantic(self) -> None:
        lexical = dict(self.packet)
        lexical["trust"] = {**self.packet["trust"], "relationships": "lexical-or-partial"}
        lexical["context"] = dict(self.packet["context"])
        lexical["context"]["tests"] = [dict(self.test_item, semantic=False)]
        rendered = render_inspect(lexical, _ByteTokenizer())
        self.assertIn("candidate reference (not a verified relationship)", rendered)
        self.assertNotIn("semantic indexed relationship", rendered)

    def test_budget_and_hard_output_cap_withhold_an_indivisible_target(self) -> None:
        tiny = dict(self.packet)
        tiny["request"] = dict(self.packet["request"], budget_tokens=50)
        rendered = render_inspect(tiny, _ByteTokenizer())
        self.assertIn("insufficient context", rendered)
        self.assertNotIn("return input.value;", rendered)
        self.assertLessEqual(_ByteTokenizer.count(rendered).count, 50)

        large = dict(self.packet)
        large_target = dict(self.target, content="x" * 4000)
        large_target["text_sha256"] = hashlib.sha256(b"x" * 4000).hexdigest()
        (self.root / "src/large.cc").write_text("x" * 4000)
        large_target["location"] = _location(self.root, "src/large.cc", 0, 4000)
        large["target"] = large_target
        large["request"] = dict(self.packet["request"], target="large")
        large["context"] = {}
        large["invariants"] = []
        large_rendered = render_inspect(large, _ByteTokenizer())
        self.assertIn("indivisible source range", large_rendered)
        self.assertNotIn("x" * 1000, large_rendered)
        self.assertLessEqual(len(large_rendered.encode()), 6000)

    def test_task_inspect_includes_current_target_and_test_source(self) -> None:
        packet = {
            "source_identity": {"repo_root": str(self.root)},
            "request": {"intent": "edit", "budget_tokens": 2400},
            "consumer": "agent",
            "canonical_targets": [self.target],
            "trust": {"confidence": "high", "freshness": "hash-verified", "relationships": "semantic",
                      "sufficient_for": ["edit"], "missing_required": []},
            "invariants": [],
            "semantic_support": {"tests": [self.test_item]},
        }
        rendered = render_task_inspect(packet, _ByteTokenizer())
        self.assertIn("canonical source verified; complete range", rendered)
        self.assertIn("assert(result == 3);", rendered)
        self.assertIn("sufficient_for=edit", rendered)

    def test_test_intent_uses_bounded_types_and_indexed_callsite_without_target_body(self) -> None:
        test_source = (
            "#include <cassert>\n"
            "int main() {\n"
            "    Input input{3};\n"
            "    int before0 = 0;\n"
            "    int before1 = 0;\n"
            "    int before2 = 0;\n"
            "    const int result = target(input);\n"
            "    assert(result == 3);\n"
            "    assert(input.value == 3);\n"
            "    int after0 = 0;\n"
            "    int after1 = 0;\n"
            "    int after2 = 0;\n"
            "    int after3 = 0;\n"
            "    int after4 = 0;\n"
            "    int after5 = 0;\n"
            "    int after6 = 0;\n"
            "    int after7 = 0;\n"
            "    return before0 + before1 + before2;\n"
            "}\n"
        )
        test_path = self.root / "tests/unit_test.cc"
        test_path.write_text(test_source)
        test_data = test_path.read_bytes()
        call = "const int result = target(input);"
        call_start = test_data.index(call.encode())
        source_data = (self.root / "src/unit.cc").read_bytes()
        target_symbol = {
            "id": "target", "name": "target", "qualified_name": "target",
            "kind": "FunctionDecl", "file": "src/unit.cc",
            "start": self.target["location"]["byte_start"],
            "end": self.target["location"]["byte_end"],
            "line": 3, "end_line": 6, "definition": True,
            "signature": "int target(const Input& input)",
        }
        type_symbol = {
            "id": "Input", "name": "Input", "qualified_name": "Input", "kind": "RecordDecl",
            "file": "src/unit.cc", "start": source_data.index(b"struct Input"),
            "end": source_data.index(b"\n", source_data.index(b"struct Input")),
            "line": 1, "end_line": 1, "definition": True,
        }
        main_start = test_data.index(b"int main")
        main_symbol = {
            "id": "test-main", "name": "main::result", "qualified_name": "main",
            "kind": "FunctionDecl", "file": "tests/unit_test.cc", "start": main_start,
            "end": len(test_data) - 1, "line": 2, "end_line": len(test_data.splitlines()),
            "definition": True,
        }
        files = [
            {"path": "src/unit.cc", "hash": hashlib.sha256(source_data).hexdigest()},
            {"path": "tests/unit_test.cc", "hash": hashlib.sha256(test_data).hexdigest()},
        ]
        edges = [
            {"type": "type_use", "from": "target", "to": "Input", "file": "src/unit.cc",
             "translation_unit": "src/unit.cc", "start": target_symbol["start"], "end": target_symbol["start"] + 1},
            {"type": "test_relationship", "from": "test-main", "to": "target",
             "file": "tests/unit_test.cc", "translation_unit": "tests/unit_test.cc",
             "start": call_start, "end": call_start + len(call.encode())},
        ]
        graph = ({"semantic": True, "_stale_relationship_files": [], "_stale_relationship_tus": [],
                  "_relationship_provenance_incomplete": False}, files, [target_symbol, type_symbol, main_symbol],
                 edges, False)
        packet = dict(self.packet)
        packet["request"] = dict(self.packet["request"], intent="test", budget_tokens=2400)
        packet["target"] = dict(self.target, signature="int target(const Input& input)", content="")
        packet["context"] = {"types": [], "dependencies": [], "callers": [], "tests": [], "callees": []}
        packet["coverage"] = dict(self.packet["coverage"], sufficient=False, target_complete=False)
        state_path = self.root / ".ctxpp"
        state_path.mkdir(exist_ok=True)
        (state_path / "index.sqlite").touch()
        with patch("ctxpp_packet.state_dir", return_value=state_path), \
             patch("ctxpp_packet.bounded_graph", return_value=graph):
            rendered = render_inspect(packet, _ByteTokenizer())

        self.assertIn("target declaration (canonical source verified; body omitted; src/unit.cc:3-3)", rendered)
        self.assertIn("machine-packet-coverage=partial test-view=bounded-indexed", rendered)
        self.assertIn("types: Input (indexed range verified; src/unit.cc:", rendered)
        self.assertIn("semantic indexed callsite", rendered)
        self.assertIn("indexed callsite: 7-7", rendered)
        self.assertIn("assert(result == 3);", rendered)
        self.assertNotIn("return input.value;", rendered)
        self.assertNotIn("#include <cassert>", rendered)
        self.assertNotIn("int main()", rendered)
        self.assertNotIn("after7", rendered)
        self.assertLessEqual(len(rendered.encode()), 6000)

    def test_test_callsite_expansion_withholds_stale_test_source(self) -> None:
        (self.root / "tests/unit_test.cc").write_text(self.test_source.replace("result == 3", "result == 4"))
        stale_packet = dict(self.packet)
        stale_packet["request"] = dict(self.packet["request"], intent="test")
        state_path = self.root / ".ctxpp"
        state_path.mkdir(exist_ok=True)
        (state_path / "index.sqlite").touch()
        files = [
            {"path": "src/unit.cc", "hash": self.target["location"]["content_sha256"]},
            {"path": "tests/unit_test.cc", "hash": self.test_item["location"]["content_sha256"]},
        ]
        symbols = [
            {"id": "target", "name": "target", "qualified_name": "target", "kind": "FunctionDecl",
             "file": "src/unit.cc", "start": self.target["location"]["byte_start"],
             "end": self.target["location"]["byte_end"], "line": 3, "end_line": 6,
             "definition": True, "signature": "int target(const Input& input)"},
            {"id": "Input", "name": "Input", "qualified_name": "Input", "kind": "RecordDecl",
             "file": "src/unit.cc", "start": self.type_symbol["location"]["byte_start"],
             "end": self.type_symbol["location"]["byte_end"], "line": 1, "end_line": 1},
            {"id": "test-main", "name": "main", "qualified_name": "main", "kind": "FunctionDecl",
             "file": "tests/unit_test.cc", "start": 0, "end": len(self.test_source), "line": 1, "end_line": 8},
        ]
        edges = [
            {"type": "type_use", "from": "target", "to": "Input", "file": "src/unit.cc",
             "translation_unit": "src/unit.cc", "start": self.target["location"]["byte_start"],
             "end": self.target["location"]["byte_start"] + 1},
            {"type": "test_relationship", "from": "test-main", "to": "target",
             "file": "tests/unit_test.cc", "translation_unit": "tests/unit_test.cc",
             "start": self.test_source.index("target(input)"),
             "end": self.test_source.index("target(input)") + len("target(input)")},
        ]
        graph = ({"semantic": True, "_stale_relationship_files": [], "_stale_relationship_tus": [],
                  "_relationship_provenance_incomplete": False}, files, symbols, edges, False)
        with patch("ctxpp_packet.state_dir", return_value=state_path), \
             patch("ctxpp_packet.bounded_graph", return_value=graph):
            rendered = render_inspect(stale_packet, _ByteTokenizer())
        self.assertNotIn("assert(result == 3);", rendered)
        self.assertIn("stale or provenance-incomplete indexed relationships were withheld", rendered)

    def test_test_intent_target_record_shows_complete_shape_for_supported_kind_names(self) -> None:
        for kind in ("RecordDecl", "CXXRecordDecl"):
            with self.subTest(kind=kind):
                target = dict(self.type_symbol, kind=kind, definition=True)
                rendered, error = _target_declaration_block(self.root, target, target)
                self.assertIsNone(error)
                self.assertIn("complete data shape", rendered)
                self.assertIn("struct Input { int value; };", rendered)

    def test_configured_test_build_hint_is_derived_and_never_reports_a_pass(self) -> None:
        (self.root / ".ctxpp.toml").write_text('[tool]\ncompilation_database = "build/compile_commands.json"\n')
        database = self.root / "build/compile_commands.json"
        database.parent.mkdir()
        (self.root / "tests/unit_test.cc").write_text(self.test_source)
        database.write_text(json.dumps([{
            "directory": str(self.root / "build/tests"),
            "file": str(self.root / "tests/unit_test.cc"),
            "output": "CMakeFiles/unit_test.dir/unit_test.cc.o",
            "command": "c++ -o CMakeFiles/unit_test.dir/unit_test.cc.o -c unit_test.cc",
        }]))
        hints = _configured_test_build_hints(self.root, {"tests/unit_test.cc"})
        self.assertEqual(len(hints), 1)
        self.assertIn("cmake --build build --target unit_test", hints[0])
        self.assertIn("build/test status unknown", hints[0])

    def test_ambiguous_inspect_gives_exact_shell_copyable_bounded_routes(self) -> None:
        rendered = render_ambiguous_inspect(
            "duplicate",
            [{"id": "c:@F@duplicate#I#", "name": "ns::duplicate", "file": "src/a.cc", "line": 12},
             {"id": "c:@F@duplicate#d#", "name": "ns::duplicate", "file": "src/b.cc", "line": 21}],
            intent="edit", budget=900,
        )
        self.assertIn("ambiguous inspect target 'duplicate'", rendered)
        self.assertIn("src/a.cc:12", rendered)
        self.assertIn("ctxpp inspect 'c:@F@duplicate#I#' --intent edit --budget 900", rendered)
        self.assertIn("ctxpp inspect 'c:@F@duplicate#d#' --intent edit --budget 900", rendered)


if __name__ == "__main__":
    unittest.main()
