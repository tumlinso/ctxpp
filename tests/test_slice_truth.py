from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from ctxpp_lib import (Tokenizer, lexical_api_skeleton, lexical_symbols, refresh_lexical_overlay,
                       render_bundle, resolve_symbols, stable_json)


SMALL_RECORD = """//@own:ctx device context|mut:state,workspace_baseline,run_count,prepared
struct PreparedExecution;
struct PreparedExecution {
  DeviceContext ctx;
  SpMMBackend* backend;
  BackendState state;
  size_t workspace_baseline;
  uint64_t run_count;
  bool prepared;
  PreparedExecution(DeviceContext);
  ~PreparedExecution();
  PreparedExecution(const PreparedExecution&) = delete;
  PreparedExecution& operator=(const PreparedExecution&) = delete;
  PreparedExecution(PreparedExecution&&) = delete;
  PreparedExecution& operator=(PreparedExecution&&) = delete;
  void reset();
};
"""


class SliceTruthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "include").mkdir()
        self.path = self.root / "include/backend.hh"
        self.path.write_text(SMALL_RECORD, encoding="utf-8")
        self.tokenizer = Tokenizer(self.root, "unavailable-test-tokenizer")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def symbols(self) -> list[dict]:
        return lexical_symbols(self.root, [self.path], [])

    def write_index(self, symbols: list[dict] | None = None) -> None:
        records = [{"record": "meta", "format": "CTXPP-INDEX/1", "semantic": False}]
        records.extend(symbols or [])
        target = self.root / ".ctxpp/index.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(stable_json(record) + "\n" for record in records), encoding="utf-8")

    def render(self, target: dict, *, compact: bool = False, budget: int = 5000):
        return render_bundle(self.root, target, "api", budget, self.tokenizer, compact=compact)

    def test_api_prefers_complete_definition_and_counts_it(self) -> None:
        symbols = self.symbols()
        self.write_index(symbols)
        forward = next(symbol for symbol in symbols if not symbol["definition"])
        content, source_map, report = self.render(forward)
        self.assertIn("DeviceContext ctx;", content)
        self.assertIn("PreparedExecution(const PreparedExecution&) = delete;", content)
        self.assertIn("void reset();", content)
        self.assertNotEqual(source_map["target"], forward["id"])
        self.assertGreater(report["mandatory_tokens"], 40)
        self.assertEqual(report["representation_kind"], "canonical-definition")

    def test_name_and_definition_id_recover_equivalent_target(self) -> None:
        symbols = self.symbols()
        self.write_index(symbols)
        definition = next(symbol for symbol in symbols if symbol["definition"])
        by_name = next(symbol for symbol in symbols if symbol["name"] == "PreparedExecution" and not symbol["definition"])
        name_content, _, _ = self.render(by_name)
        id_content, _, _ = self.render(definition)
        self.assertEqual(name_content.split("@target", 1)[1], id_content.split("@target", 1)[1])

    def test_name_only_target_is_never_sufficient(self) -> None:
        self.path.write_text("Broken", encoding="utf-8")
        self.write_index()
        target = {"record": "symbol", "id": "text:include/backend.hh:0", "name": "Broken",
                  "qualified_name": "Broken", "file": "include/backend.hh", "start": 0, "end": 6,
                  "line": 1, "definition": False, "degraded": True}
        content, _, report = self.render(target)
        self.assertFalse(report["sufficient"])
        self.assertEqual(report["sufficiency_reason"], "incomplete-target-representation")
        self.assertIn("sufficient=0", content)

    def test_small_degraded_target_uses_canonical_bytes(self) -> None:
        symbols = self.symbols()
        self.write_index(symbols)
        definition = next(symbol for symbol in symbols if symbol["definition"])
        canonical = self.path.read_bytes()[definition["start"]:definition["end"]].decode()
        content, source_map, _ = self.render(definition, compact=True)
        self.assertIn(canonical, content)
        self.assertEqual(source_map["mappings"][0]["mode"], "verbatim")
        self.assertNotIn("/*...*/", content)

    def test_semantic_target_path_remains_verbatim(self) -> None:
        data = self.path.read_bytes()
        lexical = next(symbol for symbol in self.symbols() if symbol["definition"])
        semantic = {**lexical, "id": "c:@S@PreparedExecution", "kind": "CXXRecordDecl",
                    "degraded": False, "semantic_origin": "compile_database"}
        self.write_index([semantic])
        canonical = data[semantic["start"]:semantic["end"]].decode()
        content, source_map, report = self.render(semantic)
        self.assertIn(canonical, content)
        self.assertEqual(source_map["mappings"][0]["mode"], "verbatim")
        self.assertTrue(report["sufficient"])
        self.assertNotIn("representation_kind", report)

    def test_large_lexical_record_compresses_only_method_bodies(self) -> None:
        methods = "\n".join(
            f"  int method_{index}(int value) const {{ " + "value += 1; " * 50 + "return value; }"
            for index in range(45)
        )
        large = "struct Large {\n  int state;\n" + methods + "\n  Large(const Large&) = delete;\n};\n"
        self.path.write_text(large, encoding="utf-8")
        symbol = next(symbol for symbol in self.symbols() if symbol["definition"])
        canonical = self.path.read_bytes()[symbol["start"]:symbol["end"]].decode()
        skeleton = lexical_api_skeleton(canonical, symbol, self.tokenizer)
        self.assertIsNotNone(skeleton)
        assert skeleton is not None
        self.assertIn("int state;", skeleton)
        self.assertIn("Large(const Large&) = delete;", skeleton)
        self.assertIn("int method_44(int value) const", skeleton)
        self.assertIn("/*...*/", skeleton)
        self.assertLessEqual(self.tokenizer.count(skeleton).count * 100,
                             self.tokenizer.count(canonical).count * 60)


class LocationResolutionTests(unittest.TestCase):
    class Store:
        def __init__(self, matches: list[dict]):
            self.matches = matches

        def location(self, _file: str, _line: int, limit: int) -> list[dict]:
            return self.matches[:limit]

        def close(self) -> None:
            pass

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "include").mkdir()
        self.path = self.root / "include/backend.hh"
        self.path.write_text(SMALL_RECORD, encoding="utf-8")
        self.tokenizer = Tokenizer(self.root, "unavailable-test-tokenizer")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def symbols(self) -> list[dict]:
        return lexical_symbols(self.root, [self.path], [])

    def write_index(self, symbols: list[dict] | None = None) -> None:
        records = [{"record": "meta", "format": "CTXPP-INDEX/1", "semantic": False}]
        records.extend(symbols or [])
        target = self.root / ".ctxpp/index.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(stable_json(record) + "\n" for record in records), encoding="utf-8")

    def prepare_overlay(self) -> list[dict]:
        symbols = self.symbols()
        self.write_index([])
        refresh_lexical_overlay(self.root, [self.path])
        return symbols

    def test_line_inside_lexical_body_resolves_complete_slice_target(self) -> None:
        self.prepare_overlay()
        with patch("ctxpp_lib.open_query_store", return_value=None):
            matches = resolve_symbols(self.root, "include/backend.hh:8")
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0]["target_complete"])
        content, _, _ = render_bundle(self.root, matches[0], "api", 5000, self.tokenizer)
        self.assertIn("PreparedExecution(const PreparedExecution&) = delete;", content)

    def test_empty_semantic_location_result_falls_back_to_lexical_containment(self) -> None:
        self.prepare_overlay()
        with patch("ctxpp_lib.open_query_store", return_value=self.Store([])):
            matches = resolve_symbols(self.root, "include/backend.hh:8")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["name"], "PreparedExecution")
        self.assertTrue(matches[0]["target_complete"])

    def test_exact_semantic_location_keeps_precedence(self) -> None:
        self.prepare_overlay()
        semantic = {"record": "symbol", "id": "c:@S@PreparedExecution@FI@ctx", "name": "ctx",
                    "qualified_name": "PreparedExecution::ctx", "kind": "FieldDecl",
                    "file": "include/backend.hh", "start": 140, "end": 158,
                    "line": 4, "end_line": 4, "definition": True, "degraded": False}
        with patch("ctxpp_lib.open_query_store", return_value=self.Store([semantic])):
            matches = resolve_symbols(self.root, "include/backend.hh:4")
        self.assertEqual([match["id"] for match in matches], [semantic["id"]])

    def test_attached_contract_line_resolves_associated_definition(self) -> None:
        self.path.write_text("//@own:state\nstruct Contracted {\n  int state;\n};\n", encoding="utf-8")
        self.prepare_overlay()
        with patch("ctxpp_lib.open_query_store", return_value=self.Store([])):
            matches = resolve_symbols(self.root, "include/backend.hh:1")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["name"], "Contracted")
        self.assertTrue(matches[0]["target_complete"])

    def test_line_outside_all_ranges_does_not_choose_nearest(self) -> None:
        self.prepare_overlay()
        with patch("ctxpp_lib.open_query_store", return_value=self.Store([])):
            matches = resolve_symbols(self.root, "include/backend.hh:200")
        self.assertEqual(matches, [])


if __name__ == "__main__":
    unittest.main()
