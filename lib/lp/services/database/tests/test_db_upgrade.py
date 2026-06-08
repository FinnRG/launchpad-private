# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for database/schema/upgrade.py."""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from lp.services.config import config

# Load upgrade module without permanently polluting sys.path.
# Save original sys.path to restore after import.
_schema_dir = os.path.join(config.root, "database", "schema")
_orig_sys_path = sys.path.copy()
if _schema_dir not in sys.path:
    sys.path.insert(0, _schema_dir)

# Snapshot sys.modules before loading so tearDownModule can remove exactly
# the modules that the dynamic import pulled in.
_modules_before_load = set(sys.modules.keys())

import upgrade  # noqa: E402

# Restore sys.path to avoid import collisions for the test process lifetime.
sys.path[:] = _orig_sys_path

_modules_injected = set(sys.modules.keys()) - _modules_before_load


def tearDownModule():
    """Remove modules pulled in by the dynamic load from sys.modules."""
    for name in _modules_injected:
        sys.modules.pop(name, None)


class TestAppliedPatches(unittest.TestCase):
    """Test applied_patches queries returns the applied patches from db."""

    def test_returns_tuples_from_db(self):
        mock_con = MagicMock()
        mock_cursor = MagicMock()
        mock_con.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [
            (2211, 0, 0),
            (2211, 1, 0),
            (2211, 2, 0),
        ]

        result = upgrade.applied_patches(mock_con)

        self.assertEqual(result, [(2211, 0, 0), (2211, 1, 0), (2211, 2, 0)])


class TestGetPatchlist(unittest.TestCase):
    """Test get_patchlist filters already-applied patches."""

    def setUp(self):
        self.log_patcher = patch.object(upgrade, "log", create=True)
        self.mock_log = self.log_patcher.start()
        self.addCleanup(self.log_patcher.stop)

    @patch("upgrade.applied_patches")
    @patch("upgrade.glob.glob")
    def test_excludes_already_applied_patches(self, mock_glob, mock_applied):
        mock_applied.return_value = [(2211, 0, 0), (2211, 1, 0)]
        mock_glob.return_value = [
            "/schema/patch-2211-00-0.sql",
            "/schema/patch-2211-01-0.sql",
            "/schema/patch-2211-02-0.sql",
            "/schema/patch-2211-03-0.sql",
        ]

        patches = upgrade.get_patchlist(MagicMock())

        self.assertEqual(len(patches), 2)
        self.assertEqual(patches[0][0], (2211, 2, 0))
        self.assertEqual(patches[1][0], (2211, 3, 0))

    @patch("upgrade.applied_patches")
    @patch("upgrade.glob.glob")
    def test_returns_empty_when_all_applied(self, mock_glob, mock_applied):
        mock_applied.return_value = [(2211, 0, 0)]
        mock_glob.return_value = ["/schema/patch-2211-00-0.sql"]

        patches = upgrade.get_patchlist(MagicMock())
        self.assertEqual(patches, [])

    @patch("upgrade.applied_patches")
    @patch("upgrade.glob.glob")
    def test_returns_sorted_by_version(self, mock_glob, mock_applied):
        mock_applied.return_value = []
        mock_glob.return_value = [
            "/schema/patch-2211-03-0.sql",
            "/schema/patch-2211-01-0.sql",
            "/schema/patch-2211-02-0.sql",
        ]

        patches = upgrade.get_patchlist(MagicMock())

        versions = [p[0] for p in patches]
        self.assertEqual(versions, [(2211, 1, 0), (2211, 2, 0), (2211, 3, 0)])

    @patch("upgrade.applied_patches")
    @patch("upgrade.glob.glob")
    def test_raises_systemexit_on_invalid_filename(
        self, mock_glob, mock_applied
    ):
        mock_applied.return_value = []
        mock_glob.return_value = ["/schema/patch-bad-name.sql"]

        with self.assertRaises(SystemExit) as cm:
            upgrade.get_patchlist(MagicMock())
        self.assertEqual(cm.exception.code, 1)


class _OptionsLogTestBase(unittest.TestCase):
    """Shared setUp for test classes that need options and log mocked."""

    def setUp(self):
        self.options_patcher = patch.object(upgrade, "options", create=True)
        self.mock_options = self.options_patcher.start()
        self.addCleanup(self.options_patcher.stop)
        self.mock_options.commit = False
        self.mock_options.partial = False

        self.log_patcher = patch.object(upgrade, "log", create=True)
        self.mock_log = self.log_patcher.start()
        self.addCleanup(self.log_patcher.stop)


class TestApplyOther(_OptionsLogTestBase):
    """Test apply_other executes SQL from a file."""

    def test_executes_sql_content(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sql", delete=False
        ) as f:
            f.write("CREATE TABLE foo (id int);\n")
            path = f.name

        try:
            mock_con = MagicMock()
            mock_cursor = MagicMock()
            mock_con.cursor.return_value = mock_cursor

            upgrade.apply_other(mock_con, path)

            mock_cursor.execute.assert_called_once_with(
                "CREATE TABLE foo (id int);\n"
            )
        finally:
            os.unlink(path)

    def test_rejects_script_without_trailing_semicolon(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sql", delete=False
        ) as f:
            f.write("SELECT 1")
            path = f.name

        try:
            mock_con = MagicMock()

            with self.assertRaises(SystemExit) as cm:
                upgrade.apply_other(mock_con, path)
            self.assertEqual(cm.exception.code, 3)
        finally:
            os.unlink(path)

    def test_accepts_trailing_whitespace_after_semicolon(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sql", delete=False
        ) as f:
            f.write("SELECT 1;\n\n")
            path = f.name

        try:
            mock_con = MagicMock()
            mock_cursor = MagicMock()
            mock_con.cursor.return_value = mock_cursor

            upgrade.apply_other(mock_con, path)

            mock_cursor.execute.assert_called_once()
        finally:
            os.unlink(path)

    def test_commits_when_partial_and_commit(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sql", delete=False
        ) as f:
            f.write("SELECT 1;")
            path = f.name

        try:
            mock_con = MagicMock()
            mock_cursor = MagicMock()
            mock_con.cursor.return_value = mock_cursor
            upgrade.options.commit = True
            upgrade.options.partial = True

            upgrade.apply_other(mock_con, path)

            mock_con.commit.assert_called_once()
        finally:
            os.unlink(path)

    def test_no_commit_with_no_commit_flag(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sql", delete=False
        ) as f:
            f.write("SELECT 1;")
            path = f.name

        try:
            mock_con = MagicMock()
            mock_cursor = MagicMock()
            mock_con.cursor.return_value = mock_cursor
            upgrade.options.commit = False

            upgrade.apply_other(mock_con, path, no_commit=True)

            mock_con.commit.assert_not_called()
        finally:
            os.unlink(path)


class TestApplyPatch(_OptionsLogTestBase):
    """Test apply_patch verifies LaunchpadDatabaseRevision was updated."""

    @patch("upgrade.applied_patches")
    @patch("upgrade.apply_other")
    def test_succeeds_when_revision_updated(
        self, mock_apply_other, mock_applied
    ):
        mock_applied.return_value = [(2211, 5, 0)]
        mock_con = MagicMock()

        upgrade.apply_patch(mock_con, 2211, 5, 0, "/path/patch-2211-05-0.sql")
        mock_apply_other.assert_called_once()

    @patch("upgrade.applied_patches")
    @patch("upgrade.apply_other")
    def test_fails_when_revision_not_updated(
        self, mock_apply_other, mock_applied
    ):
        mock_applied.return_value = []
        mock_con = MagicMock()

        with self.assertRaises(SystemExit) as cm:
            upgrade.apply_patch(
                mock_con, 2211, 5, 0, "/path/patch-2211-05-0.sql"
            )
        self.assertEqual(cm.exception.code, 2)

    @patch("upgrade.applied_patches")
    @patch("upgrade.apply_other")
    def test_commits_when_commit_and_partial(
        self, mock_apply_other, mock_applied
    ):
        mock_applied.return_value = [(2211, 5, 0)]
        mock_con = MagicMock()
        upgrade.options.commit = True
        upgrade.options.partial = True

        upgrade.apply_patch(mock_con, 2211, 5, 0, "/path/patch-2211-05-0.sql")

        mock_con.commit.assert_called_once()

    @patch("upgrade.applied_patches")
    @patch("upgrade.apply_other")
    def test_no_commit_when_partial_is_false(
        self, mock_apply_other, mock_applied
    ):
        mock_applied.return_value = [(2211, 5, 0)]
        mock_con = MagicMock()
        upgrade.options.commit = True
        upgrade.options.partial = False

        upgrade.apply_patch(mock_con, 2211, 5, 0, "/path/patch-2211-05-0.sql")

        mock_con.commit.assert_not_called()


class TestGetVcsDetails(unittest.TestCase):
    """Test get_vcs_details reads git branch and revision."""

    def setUp(self):
        self.vcs_cache_patcher = patch.object(
            upgrade, "_vcs_details_cache", None, create=True
        )
        self.vcs_cache_patcher.start()
        self.addCleanup(self.vcs_cache_patcher.stop)

    @patch("upgrade.subprocess.check_output")
    @patch("upgrade.os.path.exists", return_value=True)
    def test_returns_branch_and_revision_from_git(
        self, mock_exists, mock_check
    ):
        mock_check.side_effect = [
            "feature/my-branch\n",
            "abcdef1234567890\n",
        ]

        branch, rev = upgrade.get_vcs_details()

        self.assertEqual(branch, "feature/my-branch")
        self.assertEqual(rev, "abcdef1234567890")

    @patch("upgrade.os.path.exists", return_value=False)
    def test_returns_none_when_not_git_repo(self, mock_exists):
        branch, rev = upgrade.get_vcs_details()

        self.assertIsNone(branch)
        self.assertIsNone(rev)

    @patch("upgrade.subprocess.check_output")
    @patch("upgrade.os.path.exists", return_value=True)
    def test_caches_result(self, mock_exists, mock_check):
        mock_check.side_effect = ["main\n", "rev123\n"]

        first = upgrade.get_vcs_details()
        second = upgrade.get_vcs_details()

        self.assertEqual(first, second)

        # get_vcs_details calls check_output twice
        self.assertEqual(mock_check.call_count, 2)
