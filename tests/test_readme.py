"""Tests that keep the README honest.

The README is the first thing anybody reads about this dashboard, and the two
ways it goes wrong are the same: it describes an environment variable, a file
or a default that has since moved on. These tests read the README and check
it against the code, so a stale claim fails the build instead of misleading
somebody at midnight in front of an inverter.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foxess.config import DEFAULTS  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
README = os.path.join(ROOT, "README.md")
EXAMPLE = os.path.join(ROOT, "config.example.json")

#: The files that read the environment, and so the ones a documented variable
#: has to appear in.
ENV_SOURCES = ("run.py", "foxess/config.py", "foxess/server.py")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _readme_section(name: str) -> str:
    """The text of one '## heading' section of the README."""
    text = _read(README)
    start = text.index(f"## {name}")
    rest = text[start + 3:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


class TestDocumentedVariables(unittest.TestCase):

    def test_every_documented_variable_is_read_by_the_code(self):
        """The Configuration table must not list a variable nothing reads."""
        table = _readme_section("Configuration")
        documented = set(re.findall(r"^\|\s*`([A-Z0-9_]+)`\s*\|", table, re.M))
        self.assertTrue(documented, "the Configuration table lost its rows")

        code = set()
        for name in ENV_SOURCES:
            source = _read(os.path.join(ROOT, name))
            code.update(re.findall(r"os\.environ\.get\(\s*\"([A-Z0-9_]+)\"", source))
            # the seeding map: "foxess.user": "FOXESS_USER"
            code.update(re.findall(r"\"[a-z_.]+\":\s*\"([A-Z0-9_]+)\"", source))

        self.assertEqual(
            documented - code, set(),
            "the README documents variables the code never reads")

    def test_the_documented_config_path_matches_the_code(self):
        """FOXESS_DASHBOARD_CONFIG is looked up in three places, in order."""
        source = _read(os.path.join(ROOT, "foxess", "config.py"))
        self.assertIn("FOXESS_DASHBOARD_CONFIG", source)
        self.assertIn(".foxess-dashboard", source)
        self.assertLess(source.index("FOXESS_DASHBOARD_CONFIG"),
                        source.index(".foxess-dashboard"))
        table = _readme_section("Configuration")
        self.assertIn("FOXESS_DASHBOARD_CONFIG", table)
        self.assertIn(".foxess-dashboard/config.json", table)


class TestExampleConfig(unittest.TestCase):

    def test_the_example_agrees_with_the_code_default(self):
        """Copying the example must not quietly change the bind address.

        The code binds 0.0.0.0 by default, so a copy of the example that
        bound localhost instead would leave the Pi screens unable to reach a
        server the documentation says is reachable.
        """
        with open(EXAMPLE, encoding="utf-8") as fh:
            example = json.load(fh)
        self.assertEqual(example["server"]["host"], DEFAULTS["server"]["host"])

    def test_the_example_holds_no_secrets(self):
        with open(EXAMPLE, encoding="utf-8") as fh:
            example = json.load(fh)
        self.assertEqual(example["foxess"]["password"], "")
        self.assertEqual(example["foxess"]["api_key"], "")
        self.assertEqual(example["octopus"]["api_key"], "")


class TestFileTree(unittest.TestCase):

    def test_every_python_file_is_listed(self):
        """The file tree must not quietly fall behind the repository."""
        readme = _read(README)
        start = re.search(r"^foxess-dashboard/$", readme, re.M)
        self.assertIsNotNone(start, "the README has no file tree")
        tree = readme[start.start():]
        tree = tree[:tree.index("```")]

        missing = []
        for folder in ("", "foxess", "tests"):
            directory = os.path.join(ROOT, folder)
            for name in sorted(os.listdir(directory)):
                if not name.endswith(".py"):
                    continue
                if name.startswith("__"):
                    continue
                if name not in tree:
                    missing.append(os.path.join(folder, name).lstrip("/"))
        self.assertEqual(missing, [], "the README's file tree is out of date")

    def test_the_guides_are_listed(self):
        """docs/ holds a Docker guide and a Portainer guide."""
        readme = _read(README)
        for guide in ("docs/DOCKER.md", "docs/PORTAINER.md"):
            self.assertIn(guide, readme)
            self.assertTrue(os.path.exists(os.path.join(ROOT, guide)))


if __name__ == "__main__":
    unittest.main()
