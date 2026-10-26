"""The settings page and the config store must agree on paths.

The settings API speaks the dotted paths used by ``foxess/config.py`` -
``foxess.user``, ``display.theme`` and so on. ``ConfigStore.save()`` drops
any key that is not in ``EDITABLE``, and it does so silently, so a page that
uses a flat key looks like it saved when nothing was written at all.

These tests parse the page's field maps and check them against the store.
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foxess.config import EDITABLE, SECRET_PATHS  # noqa: E402

PAGE = os.path.join(os.path.dirname(__file__), "..", "static", "settings.html")


def _read_page() -> str:
    with open(PAGE, encoding="utf-8") as fh:
        return fh.read()


def _field_map(source: str, name: str) -> dict[str, str]:
    """Pull ``const NAME = { id:"path", ... };`` out of the page."""
    match = re.search(
        r"const\s+" + re.escape(name) + r"\s*=\s*\{(.*?)\}\s*;",
        source, re.S)
    if not match:
        if re.search(r"const\s+" + re.escape(name) + r"\s*=\s*\[", source):
            raise AssertionError(
                f"{name} is a plain list of field ids. It has to map each id "
                "to its dotted config path, e.g. "
                '{user:"foxess.user"}, otherwise the store cannot tell where '
                "the value belongs and drops it on save.")
        raise AssertionError(f"{name} not found in settings.html")
    body = match.group(1)
    pairs = re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"([^"]+)"', body)
    if not pairs:
        raise AssertionError(f"{name} has no id:\"path\" pairs")
    return dict(pairs)


class TestSettingsPagePaths(unittest.TestCase):

    def setUp(self) -> None:
        self.source = _read_page()
        self.maps = {
            "TEXT_FIELDS": _field_map(self.source, "TEXT_FIELDS"),
            "NUM_FIELDS": _field_map(self.source, "NUM_FIELDS"),
            "BOOL_FIELDS": _field_map(self.source, "BOOL_FIELDS"),
            "SECRET_FIELDS": _field_map(self.source, "SECRET_FIELDS"),
        }

    def test_every_field_path_is_editable(self):
        """A path the store does not know about would be dropped on save."""
        unknown = {}
        for group, fields in self.maps.items():
            for element_id, path in fields.items():
                if path not in EDITABLE:
                    unknown.setdefault(path, []).append(f"{group}.{element_id}")
        self.assertEqual(unknown, {},
                         "these paths are not in ConfigStore.EDITABLE, so "
                         "saving the page would silently discard them")

    def test_every_editable_setting_is_on_the_page(self):
        """Nothing in EDITABLE may be unreachable from the settings page."""
        covered = set()
        for fields in self.maps.values():
            covered.update(fields.values())
        # the slide list is a list, not a field, so it is handled by its own
        # code rather than the generic field map
        covered.add("display.slides")
        # the export meter serial is found by the lookup, not typed in
        covered.add("octopus.export_meter_serial")
        missing = sorted(EDITABLE - covered)
        self.assertEqual(missing, [],
                         "editable settings that the settings page cannot change")

    def test_secret_paths_match_the_store(self):
        self.assertEqual(set(self.maps["SECRET_FIELDS"].values()),
                         set(SECRET_PATHS))

    def test_no_flat_keys(self):
        """Every path must be dotted, except the genuinely top-level ones."""
        top_level = {"api_interval", "live_enabled", "api_enabled"}
        for group, fields in self.maps.items():
            for element_id, path in fields.items():
                if path not in top_level:
                    self.assertIn(".", path,
                                  f"{group}.{element_id} -> {path} should be a "
                                  "dotted path like 'foxess.user'")

    def test_element_ids_exist_in_the_markup(self):
        """Every id the script looks up has to be in the HTML."""
        html = re.sub(r"<script.*?</script>", "", self.source, flags=re.S)
        for group, fields in self.maps.items():
            for element_id in fields:
                self.assertIn(f'id="{element_id}"', html,
                              f"{group}.{element_id} has no matching element")

    def test_secret_placeholder_logic_is_not_inverted(self):
        """A masked value means a secret IS stored.

        The API replaces a stored secret with the mask, so testing
        ``v !== MASK`` would report "not set" for a secret that is there.
        """
        self.assertIn("stored ? ", self.source)
        self.assertNotIn('!== "********") ? "set"', self.source)


class TestDiscardButton(unittest.TestCase):
    """The discard button reverts the form - and says that it did.

    The fields were always restored, but the button gave no sign of having
    done anything, so it looked broken. It also stayed enabled with nothing
    to discard, and a typed secret survived a discard entirely.
    """

    def setUp(self) -> None:
        self.source = _read_page()

    def test_button_exists_and_starts_disabled(self):
        self.assertIn('id="btnDiscard"', self.source)
        self.assertRegex(self.source, r'id="btnDiscard"[^>]*disabled')

    def test_it_reports_what_it_did(self):
        self.assertIn('msg.textContent = "Changes discarded."', self.source)

    def test_it_handles_a_failed_reload(self):
        """A silent no-op is worse than an error message."""
        self.assertIn("Could not reload the settings", self.source)

    def test_dirty_state_is_tracked(self):
        self.assertIn("function formEdits()", self.source)
        self.assertIn("function isDirty()", self.source)
        self.assertIn("function updateDirtyState()", self.source)

    def test_the_button_is_only_live_when_there_is_something_to_discard(self):
        self.assertIn("discard.disabled = !dirty", self.source)

    def test_typed_secrets_are_cleared_on_reload(self):
        """Otherwise a typed secret survives a discard and the form can
        never return to a clean state."""
        secret_block = self.source[self.source.index("SECRET_FIELDS)){"):]
        secret_block = secret_block[:secret_block.index("applyTheme()")]
        self.assertIn('$(id).value = ""', secret_block)

    def test_a_typed_secret_counts_as_a_change(self):
        edits = self.source[self.source.index("function formEdits()"):]
        edits = edits[:edits.index("function isDirty()")]
        self.assertIn('if (el.value !== "") s[path] = el.value;', edits)

    def test_unsaved_changes_warn_before_navigating_away(self):
        self.assertIn("beforeunload", self.source)

    def test_the_theme_is_previewed_live(self):
        self.assertIn("function previewTheme()", self.source)
        self.assertIn('addEventListener("change", previewTheme)', self.source)


class TestSettingsRoundTrip(unittest.TestCase):
    """The store must actually persist what the page sends."""

    def _store(self):
        import tempfile
        from foxess.config import ConfigStore
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        os.unlink(path)
        return ConfigStore(path)

    def test_dotted_paths_are_written(self):
        from foxess.config import ConfigStore
        store = self._store()
        try:
            store.save({
                "foxess.user": "me@example.com",
                "foxess.sn": "SN123",
                "display.theme": "light",
                "display.poll_ms": 2500,
                "api_interval": 600,
            })
            self.assertEqual(store.get("foxess.user"), "me@example.com")
            self.assertEqual(store.get("display.theme"), "light")
            self.assertEqual(store.get("display.poll_ms"), 2500)
            self.assertEqual(store.get("api_interval"), 600)
            # and it really is on disk
            import json
            with open(store.path, encoding="utf-8") as fh:
                on_disk = json.load(fh)
            self.assertEqual(on_disk["foxess"]["user"], "me@example.com")
            self.assertEqual(on_disk["display"]["theme"], "light")
        finally:
            if store.path.exists():
                store.path.unlink()

    def test_flat_keys_are_rejected(self):
        """The failure mode that caused this bug, pinned down."""
        from foxess.config import ConfigStore
        store = self._store()
        try:
            store.save({"user": "me@example.com", "theme": "light"})
            self.assertEqual(store.get("foxess.user"), "",
                             "a flat key should not be silently applied")
            self.assertEqual(store.get("display.theme"), "dark")
        finally:
            if store.path.exists():
                store.path.unlink()

    def test_masked_marks_stored_secrets(self):
        from foxess.config import MASK, ConfigStore
        store = self._store()
        try:
            store.save({"foxess.password": "s3cret", "foxess.api_key": "key-abc"})
            masked = store.masked()
            self.assertEqual(masked["foxess"]["password"], MASK)
            self.assertEqual(masked["foxess"]["api_key"], MASK)
            # non-secret values come through untouched
            self.assertEqual(masked["foxess"]["user"], "")
        finally:
            if store.path.exists():
                store.path.unlink()


if __name__ == "__main__":
    unittest.main()
