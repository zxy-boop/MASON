"""Regression tests for Windows-specific launcher safety checks."""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mason_launcher", ROOT / "launcher" / "mason.py")
mason = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mason)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.old = (mason.HOME, mason.DEFAULT_WORKSPACE, mason.WORKSPACE, sys.argv[:], os.environ.get("MASON_WORKSPACE"))
        os.environ.pop("MASON_WORKSPACE", None)

    def tearDown(self):
        mason.HOME, mason.DEFAULT_WORKSPACE, mason.WORKSPACE, argv, env_workspace = self.old
        sys.argv[:] = argv
        if env_workspace is None:
            os.environ.pop("MASON_WORKSPACE", None)
        else:
            os.environ["MASON_WORKSPACE"] = env_workspace

    def test_writable_folder_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path, issue = mason._ensure_workspace(Path(directory) / "workspace")
            self.assertIsNone(issue)
            self.assertTrue(path.is_dir())

    @unittest.skipUnless(mason.WIN, "Windows policy")
    def test_windows_users_root_is_rejected(self):
        issue = mason._workspace_policy_issue(Path.home().parent)
        self.assertIn("user-profile root", issue)

    @unittest.skipUnless(mason.WIN, "Windows policy")
    def test_stale_saved_setting_is_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mason.HOME = root / "config"
            mason.HOME.mkdir()
            mason.DEFAULT_WORKSPACE = root / "Documents" / "MASON"
            mason.WORKSPACE = Path.home().parent
            sys.argv[:] = ["MASON"]
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(mason.prepare_workspace())
            self.assertEqual(mason.WORKSPACE, mason.DEFAULT_WORKSPACE)
            saved = __import__("json").loads((mason.HOME / "settings.json").read_text())
            self.assertEqual(saved["workspace"], str(mason.DEFAULT_WORKSPACE))

    @unittest.skipUnless(mason.WIN, "Windows policy")
    def test_explicit_bad_workspace_fails_without_rewriting_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            mason.HOME = Path(directory) / "config"
            mason.DEFAULT_WORKSPACE = Path(directory) / "default"
            mason.WORKSPACE = Path.home().parent
            sys.argv[:] = ["MASON", "--workspace", str(mason.WORKSPACE)]
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(mason.prepare_workspace())
            self.assertFalse((mason.HOME / "settings.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
