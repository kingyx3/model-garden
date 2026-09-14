from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-release-tag.py"
spec = importlib.util.spec_from_file_location("validate_release_tag", SCRIPT)
release_tag = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(release_tag)


class ReleaseTagTests(unittest.TestCase):
    def _version_file(self, value: str):
        tmp = tempfile.TemporaryDirectory()
        path = pathlib.Path(tmp.name) / "VERSION"
        path.write_text(value, encoding="utf-8")
        return tmp, path

    def test_exact_v_prefixed_version_is_accepted(self):
        tmp, path = self._version_file("1.2.3\n")
        with tmp:
            self.assertEqual(release_tag.validate("v1.2.3", path), "1.2.3")

    def test_mismatched_tag_fails_closed(self):
        tmp, path = self._version_file("1.2.3\n")
        with tmp, self.assertRaisesRegex(ValueError, "does not match VERSION"):
            release_tag.validate("v1.2.4", path)

    def test_non_plain_semver_version_is_rejected(self):
        tmp, path = self._version_file("1.2\n")
        with tmp, self.assertRaisesRegex(ValueError, "plain semantic version"):
            release_tag.validate("v1.2", path)


if __name__ == "__main__":
    unittest.main()
