"""Check the distributable's identity, discoverability, licenses and local links."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackageTest(unittest.TestCase):
    def test_manifests_agree_and_marketplace_points_inside_package(self):
        root = json.loads((ROOT / "plugin.json").read_text())
        compat = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        for key in ("name", "version", "description", "license"):
            self.assertEqual(root[key], compat[key])
        self.assertRegex(root["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(root["extensions"]["com.openai"]["interface"], compat["interface"])
        self.assertEqual((ROOT / compat["skills"]).resolve(), ROOT / "skills")
        marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], root["name"])
        self.assertEqual((ROOT / entry["source"]["path"]).resolve(), ROOT)
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")

    def test_skills_have_valid_frontmatter_and_required_resources(self):
        for name in ("watch", "evaluate-video", "learn-from-video"):
            p = ROOT / "skills" / name / "SKILL.md"
            raw = p.read_text()
            frontmatter = raw.split("---", 2)[1]
            self.assertIn(f"name: {name}\n", frontmatter)
            self.assertRegex(frontmatter, r"description: .+Use when")
        self.assertTrue((ROOT / "skills/watch/scripts/watch.py").is_file())
        self.assertIn("MIT License", (ROOT / "LICENSE").read_text())
        self.assertIn("Bradley Bonanno", (ROOT / "LICENSE").read_text())

    def test_documentation_links_resolve_locally(self):
        for path in ROOT.rglob("*.md"):
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text()):
                if "://" in target or target.startswith("#"):
                    continue
                resolved = (path.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(resolved.is_relative_to(ROOT), f"Link escapes package: {path.name}: {target}")
                self.assertTrue(resolved.exists(), f"Missing link: {path.name}: {target}")


if __name__ == "__main__":
    unittest.main()
