from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from ctxpp_lib import find_core, native_build_directory


class NativeBuildCacheTests(unittest.TestCase):
    def test_default_cache_is_outside_installed_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "share" / "ctxpp"
            (root / "tool" / "src").mkdir(parents=True)
            (root / "tool" / "src" / "main.cpp").write_text("int main() {}\n", encoding="utf-8")
            cache = Path(temp) / "cache"
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}, clear=True):
                build = native_build_directory(root)
                self.assertTrue(build.is_relative_to(cache))
                self.assertFalse(build.is_relative_to(root))
                self.assertIsNone(find_core(root))

    def test_build_dir_override_is_honored_for_build_and_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "ctxpp"
            (root / "tool" / "src").mkdir(parents=True)
            override = Path(temp) / "chosen"
            executable = override / "ctxpp-core"
            executable.parent.mkdir()
            executable.write_text("binary", encoding="utf-8")
            executable.chmod(0o755)
            with patch.dict(os.environ, {"CTXPP_BUILD_DIR": str(override), "XDG_CACHE_HOME": str(Path(temp) / "cache")}):
                self.assertEqual(native_build_directory(root), override.resolve())
                self.assertEqual(find_core(root), executable.resolve())

    def test_native_source_change_selects_a_new_cache_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "ctxpp"
            source_dir = root / "tool" / "src"
            source_dir.mkdir(parents=True)
            source = source_dir / "main.cpp"
            source.write_text("int main() {}\n", encoding="utf-8")
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(Path(temp) / "cache")}, clear=True):
                first = native_build_directory(root)
                source.write_text("int main() { return 1; }\n", encoding="utf-8")
                second = native_build_directory(root)
            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
