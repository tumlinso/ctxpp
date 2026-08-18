from __future__ import annotations

import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]


class SkillMetadataTests(unittest.TestCase):
    def test_trigger_is_opt_in_or_explicit_and_forbids_implicit_rewrite(self) -> None:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1]
        self.assertIn(".ctxpp.toml", frontmatter)
        self.assertIn("explicitly asks", frontmatter)
        self.assertIn("Never rewrite ordinary C++ implicitly", frontmatter)
        self.assertIn("do not activate implicitly", text)

    def test_references_are_conditionally_routed(self) -> None:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        for name in ("retrieval.md", "authoring.md", "topology.md", "compact-views.md", "source-transforms.md",
                     "cpp-semantic-hazards.md", "comment-contracts.md", "configuration.md", "verification.md", "evaluation.md"):
            self.assertIn(name, text)

    def test_default_configuration_never_writes_source(self) -> None:
        config = (SKILL / "assets/default.ctxpp.toml").read_text(encoding="utf-8")
        self.assertIn("source_write = false", config)


if __name__ == "__main__":
    unittest.main()
