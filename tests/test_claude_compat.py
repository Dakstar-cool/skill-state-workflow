from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"


def frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if match is None:
        raise AssertionError("SKILL.md is missing YAML frontmatter")
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    return values


class ClaudeCompatibilityTests(unittest.TestCase):
    def test_01_frontmatter_supports_discovery_and_direct_invocation(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        metadata = frontmatter(text)
        self.assertEqual(metadata.get("name"), "skill-state-workflow")
        self.assertTrue(metadata.get("description"))
        self.assertNotEqual(metadata.get("disable-model-invocation"), "true")
        self.assertLessEqual(len(text.splitlines()), 500)

    def test_02_supporting_resources_and_skill_dir_are_portable(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("${CLAUDE_SKILL_DIR}", text)
        self.assertNotIn("openai.yaml", text)
        for relative in (
            "references/long-horizon.md",
            "references/persistent-state.md",
            "scripts/state_guard.py",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_03_claude_global_activation_snippet_is_complete(self) -> None:
        snippet = (ROOT / "examples" / "global-CLAUDE-snippet.md").read_text(
            encoding="utf-8"
        )
        for marker in (
            "~/.claude/CLAUDE.md",
            "skill-state-workflow",
            "beginning of every project session",
            "After compaction",
            "ordinary one-shot",
        ):
            self.assertIn(marker, snippet)

    def test_04_readme_documents_personal_and_project_usage(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        guide = (ROOT / "docs" / "claude-code.md").read_text(encoding="utf-8")
        self.assertIn("~/.claude/skills/skill-state-workflow", readme)
        self.assertIn("/skill-state-workflow", readme)
        self.assertIn(".claude/skills/skill-state-workflow/SKILL.md", guide)
        self.assertIn("${CLAUDE_SKILL_DIR}", guide)

    def test_05_internal_markdown_links_resolve(self) -> None:
        markdown_files = [
            path
            for path in ROOT.rglob("*.md")
            if ".git" not in path.parts
        ]
        link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        for markdown in markdown_files:
            text = markdown.read_text(encoding="utf-8")
            for target in link_pattern.findall(text):
                if target.startswith(("http://", "https://", "#")):
                    continue
                relative = target.split("#", 1)[0]
                resolved = (markdown.parent / relative).resolve()
                with self.subTest(markdown=markdown.name, target=target):
                    self.assertTrue(resolved.is_file(), resolved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
