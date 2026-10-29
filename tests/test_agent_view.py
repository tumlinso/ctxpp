from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from ctxpp_packet import render_ambiguous_inspect, render_inspect, render_task_inspect  # noqa: E402


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
