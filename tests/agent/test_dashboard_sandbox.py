"""Dashboard path-traversal sandbox + loopback bind (spec dashboard guard).

The only security-relevant surface in M0 is the localhost file viewer. The
sandbox must reject parent traversal, absolute paths outside the workspace, and
symlink escapes; the server binds loopback only.
"""
import os
import tempfile
import unittest
from pathlib import Path

from dashboard.app import HOST, MAX_FILE_BYTES, PathOutsideWorkspace, safe_workspace_path


class TestSandbox(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        (self.root / "ok.txt").write_text("hi")

    def tearDown(self):
        self._dir.cleanup()

    def test_allows_path_within_root(self):
        p = safe_workspace_path(self.root, "ok.txt")
        self.assertTrue(p.is_relative_to(self.root.resolve()))

    def test_rejects_parent_traversal(self):
        with self.assertRaises(PathOutsideWorkspace):
            safe_workspace_path(self.root, "../../etc/passwd")

    def test_rejects_absolute_path_outside(self):
        with self.assertRaises(PathOutsideWorkspace):
            safe_workspace_path(self.root, "/etc/passwd")

    def test_rejects_symlink_escape(self):
        outside = Path(self._dir.name).parent / "outside_target"
        outside.write_text("secret") if not outside.exists() else None
        link = self.root / "escape"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unsupported here")
        try:
            with self.assertRaises(PathOutsideWorkspace):
                safe_workspace_path(self.root, "escape")
        finally:
            outside.unlink(missing_ok=True)


class TestBindHostConstant(unittest.TestCase):
    def test_host_is_loopback(self):
        self.assertEqual(HOST, "127.0.0.1")

    def test_max_file_bytes_is_a_positive_cap(self):
        self.assertIsInstance(MAX_FILE_BYTES, int)
        self.assertGreater(MAX_FILE_BYTES, 0)


if __name__ == "__main__":
    unittest.main()
