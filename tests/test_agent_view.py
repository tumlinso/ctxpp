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

from ctxpp_packet import (render_ambiguous_inspect, render_inspect, render_route, render_task_inspect,
                          _configured_test_build_hints,
                          _route_context_candidates, _route_identifier_reference_targets,
                          _target_declaration_block,
                          _test_intent_blocks)  # noqa: E402


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

    def _route_test_graph(self):
        source_data = (self.root / "src/unit.cc").read_bytes()
        test_data = (self.root / "tests/unit_test.cc").read_bytes()
        call = "const int result = target(input);"
        call_start = test_data.index(call.encode())
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
        main_symbol = {
            "id": "test-main", "name": "main::result", "qualified_name": "main",
            "kind": "FunctionDecl", "file": "tests/unit_test.cc", "start": test_data.index(b"int main"),
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
        return ({"semantic": True, "_stale_relationship_files": [], "_stale_relationship_tus": [],
                 "_relationship_provenance_incomplete": False}, files,
                [target_symbol, type_symbol, main_symbol], edges, False)

    def test_route_returns_target_body_types_test_window_and_alternates_in_one_view(self) -> None:
        state_path = self.root / ".ctxpp"
        state_path.mkdir(exist_ok=True)
        (state_path / "index.sqlite").touch()
        candidate = {
            "id": "target", "name": "target", "qualified_name": "target", "kind": "FunctionDecl",
            "file": "src/unit.cc", "line": 3, "definition": True, "_route_confidence": "high",
        }
        alternate = {
            "id": "other", "name": "other_target", "qualified_name": "other_target", "kind": "FunctionDecl",
            "file": "src/other.cc", "line": 9, "definition": True, "_route_confidence": "medium",
        }
        graph = self._route_test_graph()
        with patch("ctxpp_packet.state_dir", return_value=state_path), \
             patch("ctxpp_packet.bounded_graph", return_value=graph), \
             patch("ctxpp_packet.build_context_packet", return_value=self.packet):
            rendered = render_route(
                self.root, "target input result", [candidate, alternate],
                budget=2400, tokenizer=_ByteTokenizer(),
            )
        self.assertIn("target implementation (canonical source verified; complete range", rendered)
        self.assertIn("return input.value;", rendered)
        self.assertIn("struct Input { int value; };", rendered)
        self.assertIn("semantic indexed callsite", rendered)
        self.assertIn("assert(result == 3);", rendered)
        self.assertIn("other_target [FunctionDecl] — src/other.cc:9", rendered)
        self.assertIn("ctxpp inspect target --intent edit --budget 4800", rendered)
        self.assertLessEqual(_ByteTokenizer.count(rendered).count, 2400)
        self.assertLessEqual(len(rendered.encode()), 6000)

    def test_route_prefers_long_exact_symbol_name_and_promotes_member_hits_to_owner(self) -> None:
        unrelated_member = {
            "id": "missing-status", "name": "missing_timestamp",
            "qualified_name": "cellerator::indexing::occurrence_status_v1::missing_timestamp",
            "kind": "EnumConstantDecl", "file": "src/status.cc", "line": 8,
            "_route_confidence": "medium",
        }
        exact_function = {
            "id": "query-function", "name": "query_temporal_occurrences_v1",
            "qualified_name": "cellerator::indexing::query_temporal_occurrences_v1",
            "kind": "FunctionDecl", "file": "src/query.cc", "line": 20,
            "_route_confidence": "low",
        }
        with patch("ctxpp_packet.resolve_symbols", return_value=[]):
            exact = _route_context_candidates(
                self.root, "query_temporal_occurrences_v1 missing_timestamp",
                [unrelated_member, exact_function],
            )
        self.assertEqual(exact[0][1]["id"], "query-function")

        field = {
            "id": "input-value", "name": "value", "qualified_name": "cellerator::Input::value",
            "kind": "FieldDecl", "file": "src/unit.cc", "line": 1, "_route_confidence": "low",
        }
        owner = dict(
            self.type_symbol, id="input-type", name="Input", qualified_name="cellerator::Input",
            kind="RecordDecl", file="src/unit.cc", line=1, end_line=1, definition=True,
        )
        with patch("ctxpp_packet.resolve_symbols", return_value=[owner]):
            promoted = _route_context_candidates(self.root, "cellerator input value shape", [field])
        self.assertEqual(promoted[0][1]["id"], "input-type")
        self.assertTrue(promoted[0][1]["_route_parent_selection"])
        self.assertEqual(promoted[0][1]["_route_match_candidate"]["id"], "input-value")

        parameter = {
            "id": "freeze-mode", "name": "mode", "qualified_name": "demo::PackingPlan::freeze::mode",
            "kind": "ParmDecl", "file": "src/unit.cc", "line": 3, "_route_confidence": "low",
        }
        method = {
            "id": "freeze", "name": "freeze", "qualified_name": "demo::PackingPlan::freeze",
            "kind": "CXXMethod", "file": "src/unit.cc", "line": 3, "end_line": 6, "definition": True,
        }
        with patch("ctxpp_packet.resolve_symbols", return_value=[method]):
            promoted_method = _route_context_candidates(
                self.root, "demo PackingPlan freeze mode", [parameter],
            )
        self.assertEqual(promoted_method[0][1]["id"], "freeze")
        self.assertEqual(promoted_method[0][1]["kind"], "CXXMethod")

    def test_route_does_not_render_a_weak_lexical_candidate_as_target_context(self) -> None:
        lexical = {
            "id": "lex:tests/device_array.cu:10", "qualified_name": "device_array",
            "kind": "LexicalStructDecl", "file": "tests/device_array.cu", "line": 10,
            "degraded": True, "_route_confidence": "medium",
        }
        with patch("ctxpp_packet.build_context_packet") as build_packet:
            rendered = render_route(
                self.root, "timestamp output array sparse support tests", [lexical],
                budget=1200, tokenizer=_ByteTokenizer(),
            )
        build_packet.assert_not_called()
        self.assertIn("no confident semantic target", rendered)
        self.assertIn("lexical fallback", rendered)
        self.assertIn("ctxpp where lex:tests/device_array.cu:10", rendered)
        self.assertNotIn("target implementation", rendered)
        self.assertLessEqual(_ByteTokenizer.count(rendered).count, 1200)

    def test_route_expands_code_identifier_through_current_incoming_reference_only(self) -> None:
        source = "enum class Status { ok, missing_timestamp };\nint query() { return 1; }\n"
        path = self.root / "src/reference.cc"
        path.parent.mkdir(exist_ok=True)
        path.write_text(source)
        seed = {
            "id": "status-missing", "name": "missing_timestamp",
            "qualified_name": "Status::missing_timestamp", "kind": "EnumConstantDecl",
            "file": "src/reference.cc", "start": source.index("missing_timestamp"),
            "end": source.index("missing_timestamp") + len("missing_timestamp"),
            "line": 1, "end_line": 1, "definition": True,
        }
        function = {
            "id": "query", "name": "query", "qualified_name": "query",
            "kind": "FunctionDecl", "file": "src/reference.cc",
            "start": source.index("int query"), "end": len(source),
            "line": 2, "end_line": 2, "definition": True,
        }
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        graph = (
            {"_stale_relationship_files": [], "_stale_relationship_tus": [],
             "_relationship_provenance_incomplete": False},
            [{"path": "src/reference.cc", "hash": file_hash}],
            [seed, function],
            [{"type": "reference", "from": "query", "to": "status-missing",
              "file": "src/reference.cc", "translation_unit": "src/reference.cc"}],
            False,
        )
        with patch("ctxpp_packet.resolve_symbols", return_value=[seed]), \
             patch("ctxpp_packet.bounded_graph", return_value=graph):
            expanded = _route_identifier_reference_targets(
                self.root, "timestamp window missing_timestamp output", [],
            )
        self.assertEqual([item[1]["id"] for item in expanded], ["query"])
        self.assertEqual(expanded[0][1]["_route_reference_expansion"], "reference")
        self.assertEqual(expanded[0][1]["_route_match_candidate"]["id"], "status-missing")

        stale_graph = ({**graph[0], "_stale_relationship_files": ["src/reference.cc"]}, *graph[1:])
        with patch("ctxpp_packet.resolve_symbols", return_value=[seed]), \
             patch("ctxpp_packet.bounded_graph", return_value=stale_graph):
            stale = _route_identifier_reference_targets(
                self.root, "timestamp window missing_timestamp output", [],
            )
        self.assertEqual(stale, [])

    def test_route_uses_partial_current_body_excerpt_with_shapes_and_test_when_full_body_is_large(self) -> None:
        lines = ["int target(const Input& input) {"]
        lines.extend(f"    const int filler_{index} = {index};" for index in range(1, 75))
        lines.extend(["    if (input.value == 42) return -1;", "    return input.value;", "}"])
        large_text = "\n".join(lines) + "\n"
        (self.root / "src/large.cc").write_text(large_text)
        large_target = _symbol(
            self.root, "src/large.cc", large_text.rstrip(), name="target", kind="FunctionDecl",
        )
        large_target.update(definition=True, signature="int target(const Input& input)")
        packet = dict(self.packet)
        packet["source_identity"] = {"repo_root": str(self.root)}
        packet["target"] = large_target
        packet["request"] = dict(self.packet["request"], target="target", intent="test", budget_tokens=2400)
        packet["coverage"] = dict(self.packet["coverage"], sufficient=False, target_complete=False)
        declaration, error = _target_declaration_block(self.root, large_target, large_target)
        self.assertIsNone(error)
        self.assertIsNotNone(declaration)
        route_blocks = [
            ("target declaration", declaration or ""),
            ("type:Input", "types: Input (indexed range verified; src/unit.cc:1-1)\n    1 | struct Input { int value; };"),
            ("test:tests/unit_test.cc:call", "tests — semantic indexed callsite; partial nearby window; tests/unit_test.cc:4-8\n>    7 | assert(result == 3);"),
        ]
        candidate = {
            "id": "target", "name": "target", "qualified_name": "target", "kind": "FunctionDecl",
            "file": "src/large.cc", "line": 1, "definition": True, "_route_confidence": "high",
        }
        with patch("ctxpp_packet.build_context_packet", return_value=packet), \
             patch("ctxpp_packet._test_intent_blocks", return_value=(route_blocks, [])):
            rendered = render_route(
                self.root, "target input value", [candidate], budget=2400, tokenizer=_ByteTokenizer(),
            )
        self.assertIn("target declaration (canonical source verified; body omitted; src/large.cc:1-1)", rendered)
        self.assertIn("partial query-matched source window; not the full range", rendered)
        self.assertIn("src/large.cc:", rendered)
        self.assertIn("if (input.value == 42) return -1;", rendered)
        self.assertIn("struct Input { int value; };", rendered)
        self.assertIn("assert(result == 3);", rendered)
        self.assertIn("complete implementation body did not fit", rendered)
        self.assertNotIn("filler_1", rendered)
        self.assertLessEqual(len(rendered.encode()), 6000)

    def test_route_test_intent_picks_one_query_relevant_callsite_after_all_shapes(self) -> None:
        test_lines = [
            "#include <cassert>", "int main() {", "    Input input{3};",
            "    const int ordinary = target(input);", "    assert(ordinary == 3);",
            *[f"    int filler_{index} = {index};" for index in range(10)],
            "    const int timestamp_error = target(input);", "    assert(timestamp_error == -1);",
            "    return 0;", "}",
        ]
        test_source = "\n".join(test_lines) + "\n"
        route_graph = self._route_test_graph()
        test_path = self.root / "tests/unit_test.cc"
        test_path.write_text(test_source)
        test_data = test_path.read_bytes()
        call_ranges = []
        for call in ("const int ordinary = target(input);", "const int timestamp_error = target(input);"):
            start = test_data.index(call.encode())
            call_ranges.append((start, start + len(call.encode())))
        meta, files, symbols, edges, truncated = route_graph
        files = [*files]
        for item in files:
            if item["path"] == "tests/unit_test.cc":
                item["hash"] = hashlib.sha256(test_data).hexdigest()
        symbols = [dict(item) for item in symbols]
        main = next(item for item in symbols if item["id"] == "test-main")
        main.update(end=len(test_data), end_line=len(test_lines))
        edges = [item for item in edges if item.get("type") != "test_relationship"]
        edges.extend({
            "type": "test_relationship", "from": "test-main", "to": "target",
            "file": "tests/unit_test.cc", "translation_unit": "tests/unit_test.cc",
            "start": start, "end": end,
        } for start, end in call_ranges)
        route_graph = (meta, files, symbols, edges, truncated)
        state_path = self.root / ".ctxpp"
        state_path.mkdir(exist_ok=True)
        (state_path / "index.sqlite").touch()
        with patch("ctxpp_packet.state_dir", return_value=state_path), \
             patch("ctxpp_packet.bounded_graph", return_value=route_graph):
            blocks, skipped = _test_intent_blocks(
                self.root, self.target, self.packet, "semantic", route_query="timestamp_error",
            )
        test_blocks = [(name, block) for name, block in blocks if name.startswith("test:")]
        type_blocks = [(name, block) for name, block in blocks if name.startswith("type:")]
        self.assertEqual(len(test_blocks), 1)
        self.assertIn("timestamp_error", test_blocks[0][1])
        self.assertNotIn("ordinary", test_blocks[0][1])
        self.assertTrue(type_blocks)
        self.assertLess(blocks.index(type_blocks[0]), blocks.index(test_blocks[0]))
        self.assertIn("partial nearby window; not the full test", test_blocks[0][1])
        self.assertFalse(any("ordinary" in item for item in skipped))

    def test_route_budget_withholds_body_and_stale_source_is_withheld(self) -> None:
        state_path = self.root / ".ctxpp"
        state_path.mkdir(exist_ok=True)
        (state_path / "index.sqlite").touch()
        candidate = {
            "id": "target", "name": "target", "qualified_name": "target", "kind": "FunctionDecl",
            "file": "src/unit.cc", "line": 3, "definition": True, "_route_confidence": "high",
        }
        with patch("ctxpp_packet.state_dir", return_value=state_path), \
             patch("ctxpp_packet.bounded_graph", return_value=self._route_test_graph()), \
             patch("ctxpp_packet.build_context_packet", return_value=self.packet):
            tiny = render_route(self.root, "target", [candidate], budget=50, tokenizer=_ByteTokenizer())
        self.assertIn("insufficient context", tiny)
        self.assertNotIn("return input.value;", tiny)
        self.assertLessEqual(_ByteTokenizer.count(tiny).count, 50)

        (self.root / "src/unit.cc").write_text(self.source.replace("input.value < 0", "input.value <= 0"))
        stale_packet = dict(self.packet)
        stale_packet["trust"] = {**self.packet["trust"], "target_range": "unverified"}
        with patch("ctxpp_packet.build_context_packet", return_value=stale_packet):
            stale = render_route(self.root, "target", [candidate], budget=2400,
                                 tokenizer=_ByteTokenizer(), read_only=True)
        self.assertIn("stale or unverified; code excerpts withheld", stale)
        self.assertIn("refresh current source: ctxpp route target --budget 2400 (omit --read-only)", stale)
        self.assertNotIn("return input.value;", stale)

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
