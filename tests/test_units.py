from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from ctxpp_lib import Tokenizer, abbreviation, lint_contract, stable_json


class TokenizerTests(unittest.TestCase):
    def test_estimate_is_labeled_and_cached_by_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tokenizer = Tokenizer(root, "unavailable-base-encoding")
            first = tokenizer.count("long_identifier")
            second = tokenizer.count("long_identifier_with_more_bytes!")
            self.assertFalse(first.exact)
            self.assertEqual(first.identity, "utf8-bytes/4")
            self.assertNotEqual(first.count, second.count)
            cache = json.loads((root / ".ctxpp/token-cache.json").read_text())
            self.assertEqual(len(cache), 2)

    def test_unicode_is_measured_not_assumed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tokenizer = Tokenizer(Path(temp), "unavailable-base-encoding")
            unicode_count = tokenizer.count("delta:Δ inequality:≠").count
            ascii_count = tokenizer.count("delta:delta inequality:!=").count
            self.assertIsInstance(unicode_count, int)
            self.assertIsInstance(ascii_count, int)


class ContractTests(unittest.TestCase):
    def test_contract_accepts_canonical_order(self) -> None:
        self.assertEqual(lint_contract("//@in:x|out:y|req:x>0|cost:O(1)", 3), [])

    def test_contract_rejects_unknown_duplicate_and_order(self) -> None:
        problems = lint_contract("//@req:x|in:y|wat:z|in:q", 9)
        self.assertTrue(any("canonical order" in x for x in problems))
        self.assertTrue(any("unknown" in x for x in problems))
        self.assertTrue(any("duplicate" in x for x in problems))


class AbbreviationTests(unittest.TestCase):
    def test_assignment_is_deterministic_and_explainable(self) -> None:
        self.assertEqual(abbreviation("candidate_index"), "ci")
        self.assertEqual(abbreviation("candidate_index"), "ci")
        self.assertNotEqual(abbreviation("accumulated_score"), "")


if __name__ == "__main__":
    unittest.main()
