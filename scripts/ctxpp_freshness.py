"""Disposable content checks keyed by generation and complete local file identity."""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Callable
from ctxpp_paths import state_dir


class VerifiedHashes:
    def __init__(self, root: Path, generation: str, digest: Callable, write: Callable):
        self.root, self.generation, self._digest, self._write = root, generation, digest, write
        self.path = state_dir(root) / 'cache/verified-source-hashes.json'
        self.changed = False
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            self.files = payload.get('files', {}) if payload.get('generation') == generation else {}
        except (OSError, ValueError, TypeError):
            self.files = {}

    @staticmethod
    def identity(path: Path):
        value = path.stat()
        return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]

    def digest(self, path: Path) -> str:
        rel = path.relative_to(self.root).as_posix()
        identity = self.identity(path)
        previous = self.files.get(rel, {})
        # Some filesystems expose millisecond ctimes. A hash taken inside the
        # timestamp-resolution window cannot safely certify a later stat match.
        stable_verification = previous.get('verified_at_ns', 0) >= identity[-1] + 2_000_000_000
        if stable_verification and previous.get('identity') == identity and isinstance(previous.get('hash'), str):
            from ctxpp_runtime import count
            count('verified_hash_hits')
            return previous['hash']
        value = self._digest(path)
        if self.identity(path) == identity:
            self.files[rel] = {'identity': identity, 'hash': value, 'verified_at_ns': time.time_ns()}
            self.changed = True
        return value

    def flush(self):
        if self.changed:
            payload = {'format': 'CTXPP-VERIFIED-HASHES/1', 'generation': self.generation, 'files': self.files}
            self._write(self.path, (json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n').encode())
