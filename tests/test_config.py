"""Tests for the persistent configuration store."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foxess.config import (  # noqa: E402
    DEFAULTS,
    EDITABLE,
    MASK,
    KEEP,
    SECRET_PATHS,
    ConfigStore,
    deep_merge,
    get_path,
    set_path,
)


class TestDeepMerge(unittest.TestCase):
    def test_merges_nested(self):
        out = deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}})
        self.assertEqual(out, {"a": {"b": 1, "c": 3}})

    def test_does_not_mutate_base(self):
        base = {"a": {"b": 1}}
        deep_merge(base, {"a": {"b": 2}})
        self.assertEqual(base["a"]["b"], 1)

    def test_new_keys_are_added(self):
        self.assertEqual(deep_merge({}, {"x": 1}), {"x": 1})


class TestPathHelpers(unittest.TestCase):
    def test_get_path(self):
        cfg = {"a": {"b": {"c": 5}}}
        self.assertEqual(get_path(cfg, "a.b.c"), 5)
        self.assertIsNone(get_path(cfg, "a.x.c"))
        self.assertEqual(get_path(cfg, "a.x.c", "dflt"), "dflt")

    def test_set_path_creates_intermediate(self):
        cfg = {}
        set_path(cfg, "a.b.c", 7)
        self.assertEqual(cfg["a"]["b"]["c"], 7)


class TestConfigStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")
        self.store = ConfigStore(self.path)

    def test_defaults_loaded(self):
        self.assertEqual(self.store.get("server.port"), 8765)
        self.assertEqual(self.store.get("display.units"), "kW")
        self.assertEqual(self.store.get("api_interval"), 300)

    def test_save_and_reload(self):
        self.store.save({"foxess.user": "a@b.c", "display.theme": "light"})
        again = ConfigStore(self.path)
        self.assertEqual(again.get("foxess.user"), "a@b.c")
        self.assertEqual(again.get("display.theme"), "light")

    def test_secret_is_masked(self):
        self.store.save({"foxess.password": "s3cret"})
        self.assertEqual(self.store.masked()["foxess"]["password"], MASK)
        self.assertEqual(self.store.get("foxess.password"), "s3cret")

    def test_empty_secret_keeps_stored_value(self):
        self.store.save({"foxess.password": "s3cret"})
        self.store.save({"foxess.password": ""})
        self.assertEqual(self.store.get("foxess.password"), "s3cret")

    def test_keep_marker_keeps_stored_value(self):
        self.store.save({"foxess.api_key": "key123"})
        self.store.save({"foxess.api_key": KEEP})
        self.assertEqual(self.store.get("foxess.api_key"), "key123")

    def test_masked_value_is_not_written_back(self):
        self.store.save({"foxess.password": "s3cret"})
        self.store.save({"foxess.password": MASK})
        self.assertEqual(self.store.get("foxess.password"), "s3cret")

    def test_non_editable_paths_are_ignored(self):
        self.store.save({"server.port": 9999, "totally.made.up": 1})
        self.assertEqual(self.store.get("server.port"), 8765)
        self.assertIsNone(self.store.get("totally.made.up"))

    def test_every_secret_path_is_editable(self):
        for path in SECRET_PATHS:
            self.assertIn(path, EDITABLE)

    def test_file_permissions_are_owner_only(self):
        self.store.save({"foxess.user": "a@b.c"})
        mode = oct(os.stat(self.path).st_mode & 0o777)
        self.assertEqual(mode, "0o600")

    def test_corrupt_file_falls_back_to_defaults(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json at all")
        store = ConfigStore(self.path)
        self.assertEqual(store.get("server.port"), 8765)

    def test_missing_file_uses_defaults(self):
        store = ConfigStore(os.path.join(self.dir, "nope.json"))
        self.assertEqual(store.get("server.port"), 8765)

    def test_written_file_is_valid_json(self):
        self.store.save({"display.theme": "light"})
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["display"]["theme"], "light")

    def test_defaults_cover_every_editable_path(self):
        for path in EDITABLE:
            self.assertIsNotNone(get_path(DEFAULTS, path),
                                 f"{path} has no default")


if __name__ == "__main__":
    unittest.main(verbosity=2)
