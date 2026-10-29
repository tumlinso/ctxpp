from __future__ import annotations

import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from ctxpp_lib import _file_metadata_indexes, _merge_core_records, enrich_semantic_edges


SOURCE = "src/model.cc"


def symbol(identifier: str, kind: str, start: int, end: int, file: str = SOURCE) -> dict:
    return {
        "record": "symbol",
        "id": identifier,
        "qualified_name": identifier,
        "kind": kind,
        "file": file,
        "start": start,
        "end": end,
        "definition": True,
    }


def edge(identifier: str, offset: int, *, kind: str = "reference", source: str = "global",
         file: str = SOURCE, target: str = "target") -> dict:
    return {
        "record": "edge",
        "type": kind,
        "from": source,
        "to": target,
        "file": file,
        "start": offset,
        "end": offset + 1,
        "test_id": identifier,
    }


class SemanticGraphReconstructionTests(unittest.TestCase):
    def test_nested_ranges_and_inclusive_boundaries_choose_smallest_callable(self) -> None:
        records = [
            symbol("outer", "FunctionDecl", 10, 100),
            symbol("inner", "CXXMethod", 20, 80),
            symbol("leaf", "Constructor", 30, 40),
            symbol("global", "VarDecl", 0, 5),
            edge("outer-start", 10),
            edge("inner-start", 20),
            edge("leaf-start", 30),
            edge("leaf-end", 40),
            edge("inner-end", 80),
            edge("outer-end", 100),
            edge("outside", 101),
            edge("other-file", 35, file="src/other.cc"),
        ]

        _, edges, _ = _merge_core_records(records)
        by_test = {item["test_id"]: item["from"] for item in edges}

        self.assertEqual(by_test["outer-start"], "outer")
        self.assertEqual(by_test["inner-start"], "inner")
        self.assertEqual(by_test["leaf-start"], "leaf")
        self.assertEqual(by_test["leaf-end"], "leaf")
        self.assertEqual(by_test["inner-end"], "inner")
        self.assertEqual(by_test["outer-end"], "outer")
        self.assertEqual(by_test["outside"], "global")
        self.assertEqual(by_test["other-file"], "global")

    def test_overlapping_and_equal_ranges_preserve_width_then_id_tie_break(self) -> None:
        records = [
            symbol("z_equal", "FunctionDecl", 20, 70),
            symbol("a_equal", "FunctionTemplate", 20, 70),
            symbol("crossing", "Destructor", 50, 90),
            symbol("global", "VarDecl", 0, 1),
            edge("tie", 25),
            edge("narrower-overlap", 55),
        ]

        _, edges, _ = _merge_core_records(records)
        by_test = {item["test_id"]: item["from"] for item in edges}

        self.assertEqual(by_test["tie"], "a_equal")
        self.assertEqual(by_test["narrower-overlap"], "crossing")

    def test_existing_callable_source_is_preserved_and_unrelated_edges_are_untouched(self) -> None:
        records = [
            symbol("outer", "FunctionDecl", 10, 100),
            symbol("inner", "CXXMethod", 20, 80),
            symbol("global", "VarDecl", 0, 1),
            edge("already-owned", 30, kind="call", source="outer"),
            edge("non-call-edge", 30, kind="include"),
        ]

        _, edges, _ = _merge_core_records(records)
        by_test = {item["test_id"]: item["from"] for item in edges}

        self.assertEqual(by_test["already-owned"], "outer")
        self.assertEqual(by_test["non-call-edge"], "global")

    def test_matches_linear_reference_for_deterministic_random_intervals(self) -> None:
        randomizer = random.Random(7319)
        records = [symbol("global", "VarDecl", 0, 1)]
        for index in range(120):
            start = randomizer.randrange(0, 800)
            end = start + randomizer.randrange(0, 160)
            records.append(symbol(f"function_{index:03}", "FunctionDecl", start, end,
                                 file="src/other.cc" if index % 3 == 0 else SOURCE))
        for index in range(500):
            records.append(edge(f"random_{index:03}", randomizer.randrange(0, 1000),
                                file="src/other.cc" if index % 4 == 0 else SOURCE))

        callable_kinds = {"FunctionDecl", "CXXMethod", "Constructor", "Destructor", "FunctionTemplate", "ConversionFunction"}
        symbols = [record for record in records if record["record"] == "symbol"]
        expected = [dict(record) for record in records if record["record"] == "edge"]
        by_id = {item["id"]: item for item in symbols}
        for item in expected:
            source = by_id.get(item.get("from", ""))
            offset = int(item.get("start", -1))
            if (source and source.get("kind") in callable_kinds and source.get("file") == item.get("file")
                    and source.get("start", 0) <= offset <= source.get("end", -1)):
                continue
            enclosing = [candidate for candidate in symbols
                         if candidate.get("kind") in callable_kinds and candidate.get("file") == item.get("file")
                         and candidate.get("start", 0) <= offset <= candidate.get("end", -1)]
            if enclosing:
                item["from"] = min(enclosing, key=lambda candidate: (candidate.get("end", 0) - candidate.get("start", 0), candidate["id"]))["id"]
        expected.sort(key=lambda item: (item.get("type", ""), item.get("from", ""), item.get("to", ""),
                                        item.get("file", ""), item.get("start", 0)))

        _, actual, _ = _merge_core_records(records)
        self.assertEqual(actual, expected)

    def test_per_file_metadata_indexes_match_linear_reference_with_prior_and_new_records(self) -> None:
        symbols = [
            symbol("a_symbol", "FunctionDecl", 0, 10, file="src/a.cc"),
            symbol("a_symbol", "FunctionDecl", 0, 10, file="src/a.cc"),
            symbol("b_symbol", "VarDecl", 0, 1, file="src/b.cc"),
            {"record": "symbol", "id": "missing-file", "kind": "VarDecl"},
        ]
        core_records = [
            {"record": "include", "from": "src/a.cc", "to": "include/shared.hh"},
            {"record": "include", "from": "src/a.cc", "to": "include/shared.hh"},
            {"record": "include", "from": "src/a.cc", "to": ""},
            {"record": "include", "from": "src/a.cc"},
            {"record": "include", "from": "src/b.cc", "to": "include/other.hh"},
            {"record": "include", "to": "include/missing-owner.hh"},
            {"record": "symbol", "from": "src/a.cc", "to": "include/not-an-include.hh"},
        ]
        symbol_ids_by_file, include_targets_by_file = _file_metadata_indexes(symbols, core_records)

        prior_includes = {"src/a.cc": {"include/prior.hh"}, "src/b.cc": {"include/stable.hh"}}
        incremental_pending_tus = {"src/a.cc"}
        for rel in ("src/a.cc", "src/b.cc"):
            expected_ids = sorted(item["id"] for item in symbols if item.get("file") == rel)
            self.assertEqual(sorted(symbol_ids_by_file.get(rel, [])), expected_ids)

            expected_new = {item.get("to") for item in core_records
                            if item.get("record") == "include" and item.get("from") == rel}
            actual_new = set(include_targets_by_file.get(rel, set()))
            self.assertEqual(actual_new, expected_new)
            expected_includes = (expected_new if rel in incremental_pending_tus
                                 else prior_includes.get(rel, set()) | expected_new)
            actual_includes = (actual_new if rel in incremental_pending_tus
                               else prior_includes.get(rel, set()) | actual_new)
            self.assertEqual(actual_includes, expected_includes)
            self.assertEqual(sorted(item for item in actual_includes if item),
                             sorted(item for item in expected_includes if item))

    def test_enrichment_reads_each_source_file_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / SOURCE
            source.parent.mkdir(parents=True)
            source.write_text("void f() { shared; shared; }\n")
            symbols = [
                symbol("function", "FunctionDecl", 0, 40),
                symbol("shared", "VarDecl", 10, 16),
            ]
            edges = [
                {"type": "reference", "from": "function", "to": "shared", "file": SOURCE, "start": 18, "end": 24},
                {"type": "member_access", "from": "function", "to": "shared", "file": SOURCE, "start": 25, "end": 31},
            ]
            original_read_bytes = Path.read_bytes
            reads = []

            def record_read(path: Path) -> bytes:
                reads.append(path)
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", autospec=True, side_effect=record_read):
                enrich_semantic_edges(root, symbols, edges)

            self.assertEqual(reads, [source])

    def test_edges_remain_deterministically_sorted(self) -> None:
        records = [
            symbol("function", "FunctionDecl", 1, 100),
            symbol("global", "VarDecl", 0, 1),
            edge("z", 10, kind="type_use"),
            edge("b", 10, kind="call"),
            edge("a", 10, kind="reference"),
        ]

        _, edges, _ = _merge_core_records(records)
        actual = [(item["type"], item["from"], item["to"], item["file"], item["start"]) for item in edges]
        self.assertEqual(actual, sorted(actual))


if __name__ == "__main__":
    unittest.main()
