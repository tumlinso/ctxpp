from __future__ import annotations

import json
import contextlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import ctxpp_incremental as incremental
import ctxpp_lib as lib
import ctxpp_runtime as runtime
import ctxpp_store as storage


class LexicalFileDeltaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = lib.default_config()
        self.cfg["tokenizer"] = "estimate"
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        self.semantic = self.root / "src/core.cpp"
        self.semantic.write_text("int core() { return 1; }\n")
        self.lexical = self.root / "tests/smoke.cu"
        self.lexical.write_text("enum class OldMode { a };\n")
        build = self.root / "build"
        build.mkdir()
        self.command = {"directory": str(self.root), "file": str(self.semantic),
                        "arguments": ["/usr/bin/c++", "-c", str(self.semantic)]}
        (build / "compile_commands.json").write_text(json.dumps([self.command]))
        command_hash = lib.sha256_bytes(lib.stable_json(self.command).encode())
        self.meta = {"record": "meta", "format": "CTXPP-INDEX/1", "root": str(self.root),
                     "backend": "libclang-runtime", "semantic": True, "failures": [],
                     "config_hash": lib.sha256_bytes(lib.stable_json(self.cfg).encode()),
                     "determinants": {"compilation_database_hash": lib.sha256_file(build / "compile_commands.json"),
                                      "compiler_hashes": [command_hash]},
                     "coverage": {"semantic": 2, "lexical_only": 2}, "incomplete": True}
        # Preserve two distinct representations sharing one semantic ID and
        # two enum-class lexical rows sharing another ID.
        self.semantic_rows = [{"record": "symbol", "id": "usr:core", "name": "core", "file": "src/core.cpp",
                               "kind": kind, "occurrences": [{"file": "src/core.cpp", "translation_unit": "src/core.cpp"}]}
                              for kind in ("FunctionDecl", "FunctionTemplate")]
        self.lexical_rows = lib.lexical_symbols(self.root, [self.lexical], [])
        self.assertEqual(len(self.lexical_rows), 2)
        self.edge = {"record": "edge", "type": "reference", "from": "usr:core", "to": "usr:core",
                     "file": "src/core.cpp", "translation_unit": "src/core.cpp", "start": 0, "end": 3}
        cache = self.root / ".ctxpp/cache/tu"
        cache.mkdir(parents=True)
        (cache / "core.jsonl").write_text("".join(lib.stable_json(row) + "\n" for row in self.semantic_rows))
        self.state = {"format": "CTXPP-TU-CACHE/1", "jobs": {"src/core.cpp\0" + "0": {
            "cache": "core.jsonl", "input_fingerprint": "input", "command_hash": "command",
            "dependencies": {"src/core.cpp": lib.sha256_file(self.semantic)}}}}
        self.records = [self.meta, self.file_record(self.semantic, self.semantic_rows, True),
                        self.file_record(self.lexical, self.lexical_rows, False),
                        *self.semantic_rows, *self.lexical_rows, self.edge]
        self.publish()

    def file_record(self, path, symbols, semantic):
        rel = path.relative_to(self.root).as_posix()
        return {"record": "file", "path": rel, "hash": lib.sha256_file(path),
                "symbols": [row["id"] for row in symbols], "translation_units": [rel] if semantic else [],
                "commands": [self.command] if semantic else [], "command_hashes": ["command"] if semantic else []}

    def publish(self):
        files = {row["path"]: row["hash"] for row in self.records if row["record"] == "file"}
        storage.publish(self.root, self.records, {"format": "CTXPP-MANIFEST/2", "files": files},
                        {"format": "CTXPP-FRESHNESS/1", "files": {rel: {"hash": digest} for rel, digest in files.items()}},
                        self.state, {"semantic_stale_tus": [], "read_ready": True, "route_ready": True, "rewrite_ready": False})

    def protected_rows(self):
        with contextlib.closing(sqlite3.connect(storage.database_path(self.root))) as db:
            return {table: db.execute(query).fetchall() for table, query in {
                "symbols": "SELECT * FROM symbols WHERE id='usr:core' ORDER BY symbol_order",
                "occurrences": "SELECT o.* FROM occurrences o JOIN symbols s ON s.symbol_order=o.symbol_row_id WHERE s.id='usr:core' ORDER BY o.symbol_row_id,o.occurrence_order",
                "edges": "SELECT * FROM edges ORDER BY edge_order",
                "tu_contributions": "SELECT * FROM tu_contributions ORDER BY cache_key",
                "provenance_symbols": "SELECT * FROM provenance_symbols ORDER BY cache_key,record_order"}.items()}

    def test_changed_added_deleted_files_preserve_semantic_rows_and_never_read_full_graph(self):
        before = self.protected_rows()
        self.lexical.write_text("enum class NewMode { b };\n")
        added = self.root / "tests/added.cu"
        added.write_text("struct Added { int value; };\n")
        with mock.patch.object(storage.IndexedStore, "iter_records", side_effect=AssertionError("full graph")), \
             mock.patch.object(storage.IndexedStore, "all_symbols_edges", side_effect=AssertionError("full graph")), \
             mock.patch.object(storage.IndexedStore, "files", side_effect=AssertionError("all files")), \
             mock.patch.object(lib, "_read_tu_cache", side_effect=AssertionError("TU payload")), \
             mock.patch.object(lib, "find_core", side_effect=AssertionError("no semantic work")), \
             mock.patch.object(runtime, "_PROFILE_PATH", "enabled"), mock.patch.object(runtime, "_COUNTERS", {}):
            # count uses defaultdict, so keep an isolated real defaultdict.
            from collections import defaultdict
            runtime._COUNTERS = defaultdict(int)
            changed = lib.scan_repository(self.root, self.cfg, SCRIPTS.parent,
                                          ("tests/smoke.cu", "tests/added.cu"))
            self.assertEqual(changed["incremental"], {"translation_units": 0, "parsed": 0, "lexical_files": 2,
                                                     "symbols_replaced": 2, "store_rows_decoded": 4})
            self.assertEqual(changed["files"], 3)
            self.assertEqual(changed["symbols"], 5)
            self.assertEqual(runtime.profile_snapshot()["counters"].get("symbol_records_decoded"), 4)
            self.assertNotIn("tu_cache_payloads_materialized", runtime.profile_snapshot()["counters"])
            self.assertNotIn("tus_parsed", runtime.profile_snapshot()["counters"])
            unchanged_generation = storage.export_descriptor(self.root)["generation"]
            unchanged = lib.scan_repository(self.root, self.cfg, SCRIPTS.parent, ("tests/smoke.cu",))
            self.assertEqual(unchanged["incremental"]["lexical_files"], 0)
            self.assertEqual(storage.export_descriptor(self.root)["generation"], unchanged_generation)
            self.lexical.unlink()
            deleted = lib.scan_repository(self.root, self.cfg, SCRIPTS.parent, ("tests/smoke.cu",))
            self.assertEqual(deleted["files"], 2)
            self.assertEqual(deleted["symbols"], 3)
        self.assertEqual(self.protected_rows(), before)
        store = storage.open_store(self.root)
        try:
            self.assertIsNone(store.file("tests/smoke.cu"))
            self.assertEqual(store.file("tests/added.cu")["hash"], lib.sha256_file(added))
            self.assertEqual(store.meta()["coverage"], {"semantic": 2, "lexical_only": 1})
            self.assertEqual(store.metadata("tu_state"), self.state)
            self.assertNotIn("tests/smoke.cu", store.manifest()["files"])
            self.assertNotIn("tests/smoke.cu", store.freshness()["files"])
        finally:
            store.close()

    def test_dependency_and_semantic_rows_are_ineligible(self):
        self.state["jobs"]["src/core.cpp\0" + "0"]["dependencies"]["tests/smoke.cu"] = lib.sha256_file(self.lexical)
        self.publish()
        generation = storage.export_descriptor(self.root)["generation"]
        self.assertIsNone(incremental.scan_lexical_files(self.root, self.cfg, ("tests/smoke.cu",)))
        self.assertEqual(storage.export_descriptor(self.root)["generation"], generation)
        del self.state["jobs"]["src/core.cpp\0" + "0"]["dependencies"]["tests/smoke.cu"]
        self.records.append({"record": "symbol", "id": "usr:hidden", "file": "tests/smoke.cu"})
        self.publish()
        self.assertIsNone(incremental.scan_lexical_files(self.root, self.cfg, ("tests/smoke.cu",)))

    def test_recipe_headers_unknown_provenance_and_config_changes_fall_back(self):
        for scopes in (("src/core.cpp",), ("tests",), ("include/missing.hpp",)):
            with self.subTest(scopes=scopes):
                self.assertIsNone(incremental.scan_lexical_files(self.root, self.cfg, scopes))
        self.cfg["profile"] = "changed"
        self.assertIsNone(incremental.scan_lexical_files(self.root, self.cfg, ("tests/smoke.cu",)))
        self.cfg["profile"] = lib.default_config()["profile"]
        with contextlib.closing(sqlite3.connect(storage.database_path(self.root))) as db:
            db.execute("UPDATE metadata SET value='false' WHERE key='incremental_provenance'")
            db.commit()
        self.assertIsNone(incremental.scan_lexical_files(self.root, self.cfg, ("tests/smoke.cu",)))

    def test_only_missing_driver_inferred_recipes_allow_lexical_update(self):
        self.lexical.write_text("struct NewMode {};\n")
        generation = storage.export_descriptor(self.root)["generation"]
        with mock.patch.object(incremental, "infer_recipe", return_value=(self.command, "inferred_sibling", .75)), \
             mock.patch.object(incremental, "recipe_preflight", return_value=(True, "")):
            self.assertIsNone(incremental.scan_lexical_files(self.root, self.cfg, ("tests/smoke.cu",)))
            self.assertEqual(storage.export_descriptor(self.root)["generation"], generation)
        with mock.patch.object(incremental, "infer_recipe", return_value=(self.command, "inferred_sibling", .75)), \
             mock.patch.object(incremental, "recipe_preflight", return_value=(False, "Clang driver unavailable")):
            result = incremental.scan_lexical_files(self.root, self.cfg, ("tests/smoke.cu",))
            self.assertEqual(result["incremental"]["lexical_files"], 1)
            self.assertEqual(result["incremental"]["parsed"], 0)

    def test_fatal_inferred_recipe_preflight_falls_back_without_publication(self):
        self.lexical.write_text("struct NewMode {};\n")
        generation = storage.export_descriptor(self.root)["generation"]
        for diagnostic in ("clang: error: unsupported option '--threads'", "", "Clang driver unavailable\nother error"):
            with self.subTest(diagnostic=diagnostic), \
                 mock.patch.object(incremental, "infer_recipe", return_value=(self.command, "inferred_sibling", .75)), \
                 mock.patch.object(incremental, "recipe_preflight", return_value=(False, diagnostic)):
                self.assertIsNone(incremental.scan_lexical_files(self.root, self.cfg, ("tests/smoke.cu",)))
                self.assertEqual(storage.export_descriptor(self.root)["generation"], generation)

    def test_cross_file_colliding_lexical_id_preserves_every_other_representation(self):
        # Even corrupted/noncanonical historical IDs must never remove another
        # file's symbol when apply_delta replaces by nonunique public ID.
        duplicate = dict(self.lexical_rows[0], file="tests/other.cu")
        self.records.append(duplicate)
        self.publish()
        self.lexical.write_text("struct NewMode {};\n")
        result = incremental.scan_lexical_files(self.root, self.cfg, ("tests/smoke.cu",))
        self.assertEqual(result["symbols"], 4)
        store = storage.open_store(self.root)
        try:
            rows = store.symbol_records({duplicate["id"]})
            self.assertIn(duplicate, rows)
            self.assertEqual(len([row for row in rows if row["file"] == "tests/other.cu"]), 1)
        finally:
            store.close()

    def test_source_mutation_before_commit_preserves_generation(self):
        self.lexical.write_text("struct NewMode {};\n")
        original = lib.lexical_symbols
        def race(*args):
            rows = original(*args)
            self.lexical.write_text("struct Raced {};\n")
            return rows
        generation = storage.export_descriptor(self.root)["generation"]
        with mock.patch.object(lib, "lexical_symbols", side_effect=race):
            with self.assertRaisesRegex(lib.CtxppError, "source changed during lexical scan"):
                incremental.scan_lexical_files(self.root, self.cfg, ("tests/smoke.cu",))
        self.assertEqual(storage.export_descriptor(self.root)["generation"], generation)


if __name__ == "__main__":
    unittest.main()
