from __future__ import annotations
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import ctxpp_store as storage

class InterruptedCompressionRecoveryTests(unittest.TestCase):
    def make_legacy_state(self, root: Path):
        source = root / 'src/a.cpp'
        source.parent.mkdir(parents=True)
        source.write_text('int main() {}\n', encoding='utf-8')
        state = root / '.ctxpp'
        cache_dir = state / 'cache/tu'
        cache_dir.mkdir(parents=True)
        cache = b'{"record":"symbol","id":"usr:a","name":"a","file":"src/a.cpp"}\n'
        (cache_dir / 'key.jsonl').write_bytes(cache)
        records = [{'record': 'meta', 'format': 'CTXPP-INDEX/1', 'root': str(root), 'semantic': True},
                   {'record': 'file', 'path': 'src/a.cpp', 'hash': hashlib.sha256(source.read_bytes()).hexdigest()},
                   {'record': 'symbol', 'id': 'usr:a', 'name': 'a', 'file': 'src/a.cpp',
                    'occurrences': [{'file': 'src/a.cpp', 'translation_unit': 'src/a.cpp'}]}]
        legacy = ''.join(storage._json(record) + '\n' for record in records).encode()
        (state / 'index.jsonl').write_bytes(legacy)
        (state / 'manifest.json').write_text(json.dumps({'index_hash': hashlib.sha256(legacy).hexdigest(), 'files': {}}))
        (state / 'cache/freshness.json').write_text(json.dumps({'files': {}}))
        tu_state = {'format': 'CTXPP-TU-CACHE/1', 'jobs': {'src/a.cpp\0' + '0': {
            'cache': 'key.jsonl', 'command_hash': 'command-v1', 'input_fingerprint': 'fingerprint',
            'dependencies': {'src/a.cpp': hashlib.sha256(source.read_bytes()).hexdigest()}}}}
        (state / 'cache/tu-state.json').write_text(json.dumps(tu_state))
        storage.migrate(root)
        return state, cache_dir, cache, records

    def test_retry_recovers_verified_raw_sidecopy_after_projection_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state, cache_dir, raw, records = self.make_legacy_state(root)
            generation_before = storage.export_descriptor(root)['generation']
            with mock.patch.object(storage, '_atomic_json', side_effect=OSError('injected projection failure')):
                with self.assertRaisesRegex(OSError, 'injected projection failure'):
                    storage.compress_legacy_caches(root)
            compressed, legacy = cache_dir / 'key.jsonl.z', cache_dir / 'key.jsonl'
            self.assertTrue(legacy.is_file(), 'raw cache remains until projections succeed')
            self.assertEqual(storage.zlib.decompress(compressed.read_bytes()), raw)
            store = storage.open_store(root)
            try:
                generation = store.artifact_identity
                self.assertNotEqual(generation_before, generation)
                self.assertEqual(list(store.iter_records()), records)
                self.assertEqual(store.metadata('tu_state')['jobs']['src/a.cpp\0' + '0']['cache'], 'key.jsonl.z')
            finally:
                store.close()
            result = storage.compress_legacy_caches(root)
            self.assertEqual(result['caches'], 0)
            self.assertEqual(result['raw_copies_removed'], 1)
            self.assertFalse(legacy.exists())
            descriptor = json.loads((state / 'export.json').read_text(encoding='utf-8'))
            self.assertEqual(descriptor['generation'], storage.export_descriptor(root)['generation'])
            self.assertEqual(descriptor['generation'], generation)
            store = storage.open_store(root)
            try:
                self.assertEqual(list(store.iter_records()), records)
            finally:
                store.close()

    def test_retry_preserves_raw_sidecopy_changed_after_interruption(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, cache_dir, raw, records = self.make_legacy_state(root)
            legacy = cache_dir / 'key.jsonl'
            with mock.patch.object(storage, '_atomic_json', side_effect=OSError('injected projection failure')):
                with self.assertRaisesRegex(OSError, 'injected projection failure'):
                    storage.compress_legacy_caches(root)
            unknown = b'concurrent content that was never captured\n'
            legacy.write_bytes(unknown)
            result = storage.compress_legacy_caches(root)
            self.assertEqual(result['raw_copies_removed'], 0)
            self.assertEqual(legacy.read_bytes(), unknown)
            self.assertEqual(storage.zlib.decompress((cache_dir / 'key.jsonl.z').read_bytes()), raw)
            store = storage.open_store(root)
            try:
                self.assertEqual(list(store.iter_records()), records)
            finally:
                store.close()

if __name__ == '__main__':
    unittest.main()
