from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ctxpp_store as storage
from ctxpp_lib import bounded_graph, resolve_symbols


class IndexedStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.records = [
            {'record': 'meta', 'format': 'CTXPP-INDEX/1', 'root': str(self.root), 'semantic': True},
            {'record': 'file', 'path': 'a.cpp', 'hash': 'abc', 'commands': []},
            {'record': 'symbol', 'id': 'same', 'name': 'first', 'file': 'a.cpp',
             'occurrences': [{'file': 'a.cpp', 'start': 1, 'translation_unit': 'a.cpp'}]},
            {'record': 'symbol', 'id': 'same', 'name': 'second', 'file': 'b.cpp',
             'occurrences': [{'file': 'b.cpp', 'start': 2, 'translation_unit': 'b.cpp'}]},
            {'record': 'symbol', 'id': 'other', 'name': 'other', 'file': 'a.cpp'},
            {'record': 'edge', 'from': 'same', 'to': 'other', 'file': 'a.cpp', 'start': 0,
             'type': 'call', 'kind': None, 'end': 0, 'extra': {'unknown': [1, True, None]}},
            {'record': 'edge', 'type': 'include', 'from': '', 'to': None},
        ]

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, records=None):
        return storage.publish(self.root, records or self.records, {'files': {}}, {'files': {}})

    def test_lossless_records_duplicate_semantic_ids_and_absent_null_fields(self):
        self.publish()
        store = storage.open_store(self.root)
        try:
            self.assertEqual(list(store.iter_records()), self.records)
            self.assertEqual(len(store.exact('same', 8)), 2)
            self.assertEqual(store.counts(), (1, 3, 2))
            self.assertEqual(store.edges_for('same'), [self.records[-2]])
        finally:
            store.close()
        output = io.StringIO()
        storage.export_legacy(self.root, output)
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], self.records)
        self.assertFalse((self.root / '.ctxpp/index.jsonl').exists())

    def test_generation_replace_keeps_open_reader_pinned_and_failed_publish_intact(self):
        first = self.publish()
        old = storage.open_store(self.root)
        try:
            changed = [dict(record) for record in self.records]
            changed[0]['semantic'] = False
            second = self.publish(changed)
            self.assertNotEqual(first, second)
            self.assertEqual(old.artifact_identity, first)
            self.assertTrue(old.meta()['semantic'])
            store = storage.open_store(self.root)
            try:
                self.assertEqual(store.artifact_identity, second)
                self.assertFalse(store.meta()['semantic'])
            finally:
                store.close()
            with self.assertRaisesRegex(ValueError, 'multiple index meta'):
                self.publish([*self.records, self.records[0]])
            self.assertEqual(storage.export_descriptor(self.root)['generation'], second)
        finally:
            old.close()

    def test_bounded_retrieval_never_iterates_full_graph(self):
        self.publish()
        with mock.patch.object(storage.IndexedStore, 'iter_records', side_effect=AssertionError('full graph read')), \
             mock.patch.object(storage.IndexedStore, 'all_symbols_edges', side_effect=AssertionError('full graph read')):
            symbols = resolve_symbols(self.root, 'first')
            self.assertEqual([item['name'] for item in symbols], ['first'])
            _, _, symbols, edges, truncated = bounded_graph(self.root, symbols[0])
            self.assertEqual(len(edges), 1)
            self.assertFalse(truncated)
            self.assertEqual({item['id'] for item in symbols}, {'same', 'other'})

    def test_store_corruption_is_reported(self):
        self.publish()
        (self.root / '.ctxpp/index.sqlite').write_bytes(b'corrupt')
        with self.assertRaises(sqlite3.DatabaseError):
            storage.open_store(self.root)

    def test_streaming_migration_validates_and_retains_cache_provenance(self):
        directory = self.root / '.ctxpp'
        (directory / 'cache/tu').mkdir(parents=True)
        source = self.root / 'a.cpp'
        source.write_text('int main() {}\n')
        cache = b'{"record":"symbol","id":"same","file":"a.cpp"}\n'
        (directory / 'cache/tu/a.jsonl').write_bytes(cache)
        payload = ''.join(storage._json(record) + '\n' for record in self.records).encode()
        (directory / 'index.jsonl').write_bytes(payload)
        manifest = {'files': {}, 'index_hash': hashlib.sha256(payload).hexdigest()}
        (directory / 'manifest.json').write_text(json.dumps(manifest))
        (directory / 'cache/freshness.json').write_text('{"files":{}}')
        state = {'jobs': {'a.cpp\0' + '0': {'cache': 'a.jsonl', 'input_fingerprint': 'fingerprint',
                  'dependencies': {'a.cpp': hashlib.sha256(source.read_bytes()).hexdigest()}}}}
        (directory / 'cache/tu-state.json').write_text(json.dumps(state))
        storage.migrate(self.root)
        self.assertEqual((directory / 'index.jsonl').read_bytes(), payload)
        store = storage.open_store(self.root)
        try:
            self.assertEqual(list(store.iter_records()), self.records)
            row = store.connection.execute('SELECT cache_sha256,payload FROM tu_contributions').fetchone()
            self.assertEqual(row[0], hashlib.sha256(cache).hexdigest())
            self.assertEqual(storage.zlib.decompress(row[1]), cache)
        finally:
            store.close()
        compressed = storage.compress_legacy_caches(self.root)
        self.assertEqual(compressed['caches'], 1)
        self.assertEqual(compressed['raw_copies_removed'], 1)
        self.assertFalse((directory / 'cache/tu/a.jsonl').exists())
        self.assertTrue((directory / 'cache/tu/a.jsonl.z').is_file())
        store = storage.open_store(self.root)
        try:
            self.assertEqual(list(store.iter_records()), self.records)
            self.assertEqual(store.metadata('tu_state')['jobs']['a.cpp\0' + '0']['cache'], 'a.jsonl.z')
            self.assertEqual(storage.zlib.decompress(store.connection.execute(
                'SELECT payload FROM tu_contributions').fetchone()[0]), cache)
        finally:
            store.close()
        self.assertEqual(storage.compress_legacy_caches(self.root)['caches'], 0)
        source.write_text('int changed() {}\n')
        with self.assertRaisesRegex(ValueError, 'dependency changed'):
            storage.migrate(self.root)


class IncrementalStoreTests(unittest.TestCase):
    def test_delta_preserves_unaffected_rows_and_rolls_back_incomplete_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / '.ctxpp/cache/tu'
            cache.mkdir(parents=True)
            def symbol(identity, file, name):
                return {'record': 'symbol', 'id': identity, 'name': name, 'qualified_name': name,
                        'file': file, 'definition': True, 'start': 0, 'end': 4}
            raw_a, raw_b = symbol('a', 'a.cpp', 'first'), symbol('b', 'b.cpp', 'second')
            state = {'jobs': {}}
            records = [{'record': 'meta', 'root': str(root), 'format': 'CTXPP-INDEX/1', 'semantic': True}]
            for raw in (raw_a, raw_b):
                file = raw['file']
                (root / file).write_text('int;')
                records.append({'record': 'file', 'path': file, 'hash': 'old'})
                raw['translation_unit'] = file
                raw['configuration_hash'] = 'command'
                records.append({**raw, 'occurrences': [{'file': file, 'start': 0, 'end': 4,
                                                       'translation_unit': file, 'configuration_hash': 'command'}]})
                records.append({'record': 'edge', 'type': 'reference', 'from': raw['id'], 'to': raw['id'],
                                'file': file, 'translation_unit': file})
                name = file + '.jsonl'
                (cache / name).write_text(json.dumps(raw) + '\n')
                state['jobs'][file + '\0' + '0'] = {'cache': name, 'command_hash': 'command',
                    'input_fingerprint': 'input', 'dependencies': {file: 'expected'}}
            storage.publish(root, records, {}, {}, state, {})
            store = storage.open_store(root)
            try:
                self.assertTrue(store.has_incremental_provenance())
                generation = store.artifact_identity
                untouched = tuple(store.connection.execute("SELECT * FROM symbols WHERE id='b'").fetchone())
                self.assertEqual(store.contribution_symbols({'a'}, {'b.cpp'})[0]['name'], 'first')
                self.assertEqual([row['id'] for row in store.symbol_records_for_tus({'a.cpp'})], ['a'])
            finally:
                store.close()
            changed = {**raw_a, 'name': 'changed', 'qualified_name': 'changed',
                       'occurrences': [{'file': 'a.cpp', 'translation_unit': 'a.cpp', 'start': 0, 'end': 4}]}
            (cache / 'a.cpp.jsonl').write_text(json.dumps(changed) + '\n')
            new_generation = storage.apply_delta(root, affected_tus={'a.cpp'}, impacted_symbol_ids={'a'},
                symbols=[changed], edges=[{'record': 'edge', 'type': 'call', 'from': 'a', 'to': 'b',
                                           'translation_unit': 'a.cpp'}], files=[], meta=records[0],
                manifest={}, freshness={}, tu_state=state, readiness={}, expected_generation=generation)
            store = storage.open_store(root)
            try:
                self.assertNotEqual(generation, new_generation)
                self.assertEqual(tuple(store.connection.execute("SELECT * FROM symbols WHERE id='b'").fetchone()), untouched)
                self.assertEqual(store.exact('a', 1)[0]['name'], 'changed')
                self.assertEqual(store.counts(), (2, 2, 2))
                self.assertEqual(store.contribution_symbols({'a'})[0]['name'], 'changed')
                before = list(store.iter_records())
            finally:
                store.close()
            (cache / 'a.cpp.jsonl').unlink()
            with self.assertRaisesRegex(ValueError, 'provenance is incomplete'):
                storage.apply_delta(root, affected_tus={'a.cpp'}, impacted_symbol_ids={'a'}, symbols=[], edges=[],
                    files=[], meta=records[0], manifest={}, freshness={}, tu_state=state, readiness={},
                    expected_generation=new_generation)
            store = storage.open_store(root)
            try:
                self.assertEqual(store.artifact_identity, new_generation)
                self.assertEqual(list(store.iter_records()), before)
                self.assertEqual(store.connection.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            finally:
                store.close()
            with self.assertRaisesRegex(ValueError, 'generation changed'):
                storage.apply_delta(root, affected_tus={'a.cpp'}, impacted_symbol_ids=set(), symbols=[], edges=[],
                    files=[], meta=records[0], manifest={}, freshness={}, tu_state=state, readiness={},
                    expected_generation=generation)


class VerifiedHashesTests(unittest.TestCase):
    def test_reuses_content_until_ctime_changes_even_when_size_and_mtime_are_restored(self):
        from ctxpp_freshness import VerifiedHashes
        import os
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'a.cpp'
            path.write_text('before')
            import time
            time.sleep(2.01)  # Certify outside coarse filesystem timestamp resolution.
            calls = []
            def digest(source):
                calls.append(source)
                return hashlib.sha256(source.read_bytes()).hexdigest()
            def write(destination, data):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            cache = VerifiedHashes(root, 'generation', digest, write)
            original = cache.digest(path)
            cache.flush()
            cache = VerifiedHashes(root, 'generation', digest, write)
            self.assertEqual(cache.digest(path), original)
            self.assertEqual(len(calls), 1)
            prior = path.stat()
            path.write_text('after!')
            os.utime(path, ns=(prior.st_atime_ns, prior.st_mtime_ns))
            self.assertNotEqual(cache.digest(path), original)
            self.assertEqual(len(calls), 2)
            cache.flush()
            other = VerifiedHashes(root, 'different-generation', digest, write)
            other.digest(path)
            self.assertEqual(len(calls), 3)


if __name__ == '__main__':
    unittest.main()
