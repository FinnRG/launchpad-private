# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for database/schema/full-update.py."""

import os
import sys
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from unittest.mock import MagicMock, patch

from lp.services.config import config

# Load full-update.py and its schema-local dependencies (upgrade, security,
# etc.) using a temporary sys.path entry that is removed immediately after
# loading so it does not pollute the import namespace for the rest of the
# test process.
_schema_dir = os.path.join(config.root, "database", "schema")

# Save original sys.path to restore after import.
_orig_sys_path = sys.path.copy()
if _schema_dir not in sys.path:
    sys.path.insert(0, _schema_dir)

# Snapshot sys.modules before loading so tearDownModule can remove exactly
# the modules that the dynamic load pulled in (upgrade, security, etc.).
_modules_before_load = set(sys.modules.keys())
path = os.path.join(_schema_dir, "full-update.py")
spec = spec_from_file_location("full_update", path)
full_update = module_from_spec(spec)
spec.loader.exec_module(full_update)
sys.modules["full_update"] = full_update

# Restore sys.path to avoid import collisions for the test process lifetime.
sys.path[:] = _orig_sys_path

_modules_injected = set(sys.modules.keys()) - _modules_before_load


def tearDownModule():
    """Remove modules pulled in by the dynamic load from sys.modules."""
    for name in _modules_injected:
        sys.modules.pop(name, None)


class TestRunUpgrade(unittest.TestCase):
    """Test run_upgrade sets options and delegates to upgrade.main."""

    @patch.object(full_update.upgrade, "main", return_value=0)
    def test_disables_commit_partial_comments_separate_sessions(
        self, mock_main
    ):
        options = MagicMock()
        log = MagicMock()
        con = MagicMock()

        full_update.run_upgrade(options, log, con)

        self.assertFalse(options.commit)
        self.assertFalse(options.partial)
        self.assertFalse(options.comments)
        self.assertFalse(options.separate_sessions)
        self.assertIsNone(options.dbname)
        mock_main.assert_called_once_with(con)

    @patch.object(full_update.upgrade, "main", side_effect=Exception("boom"))
    def test_catches_exception_and_returns_1(self, mock_main):
        options = MagicMock()
        log = MagicMock()
        con = MagicMock()

        result = full_update.run_upgrade(options, log, con)

        self.assertEqual(result, 1)
        log.exception.assert_called_once()

    @patch.object(full_update.upgrade, "main", side_effect=SystemExit(42))
    def test_catches_systemexit_and_logs_fatal(self, mock_main):
        options = MagicMock()
        log = MagicMock()
        con = MagicMock()

        full_update.run_upgrade(options, log, con)
        log.fatal.assert_called()


class _MainTestBase(unittest.TestCase):
    """Shared setUp/tearDown for all main() test classes."""

    def setUp(self):
        self.logger_patcher = patch.object(full_update, "logger")
        self.mock_logger = self.logger_patcher.start()
        self.mock_log = MagicMock()
        self.mock_logger.return_value = self.mock_log
        self.addCleanup(self.logger_patcher.stop)


class TestMainHappyPath(_MainTestBase):
    """Test main() happy path with all steps succeeding."""

    def _run_main(self, argv):
        """Run full_update.main() with mocked dependencies."""
        # Patch external integrations so main() stays on the all-success path.
        with patch(
            "full_update.psycopg2.connect"
        ) as mock_connect, patch.object(
            full_update, "DBController"
        ) as mock_ctrl_cls, patch.object(
            full_update, "NoConnectionCheckPreflight"
        ) as mock_noconn_cls, patch.object(
            full_update, "KillConnectionsPreflight"
        ) as mock_kill_cls, patch.object(
            full_update, "run_upgrade", return_value=0
        ), patch.object(
            full_update, "run_security", return_value=0
        ), patch(
            "sys.argv", argv
        ):

            mock_connect.return_value = MagicMock()

            # Make each controller step succeed so the happy path can run.
            ctrl = MagicMock()
            ctrl.primary = "host=localhost dbname=lp"
            ctrl.pause_replication.return_value = True
            ctrl.disable_primary.return_value = True
            ctrl.enable_primary.return_value = True
            ctrl.disable_standbys.return_value = True
            ctrl.enable_standbys.return_value = True
            ctrl.resume_replication.return_value = True
            ctrl.sync.return_value = True
            mock_ctrl_cls.return_value = ctrl

            # Both preflight checks pass, allowing the upgrade to continue.
            mock_noconn = MagicMock()
            mock_noconn.check_all.return_value = True
            mock_noconn_cls.return_value = mock_noconn

            mock_kill = MagicMock()
            mock_kill.check_all.return_value = True
            mock_kill_cls.return_value = mock_kill

            result = full_update.main()
            return result, ctrl

    def test_full_success_returns_0(self):
        result, _ = self._run_main(["full-update.py"])
        self.assertEqual(result, 0)

    def test_calls_replication_steps_by_default(self):
        _, ctrl = self._run_main(["full-update.py"])
        ctrl.ensure_replication_enabled.assert_called_once()
        ctrl.pause_replication.assert_called_once()
        ctrl.resume_replication.assert_called_once()
        ctrl.sync.assert_called_once()


class TestMainFailurePaths(_MainTestBase):
    """Test main() failure and recovery paths."""

    def test_connection_failure_returns_94(self):
        with patch(
            "full_update.psycopg2.connect"
        ) as mock_connect, patch.object(
            full_update, "DBController"
        ) as mock_ctrl_cls, patch(
            "sys.argv", ["full-update.py"]
        ):

            ctrl = MagicMock()
            ctrl.primary = "host=localhost dbname=lp"
            mock_ctrl_cls.return_value = ctrl
            mock_connect.side_effect = Exception("connection refused")

            result = full_update.main()

        self.assertEqual(result, 94)

    def test_pause_replication_failure_returns_93(self):
        with patch("full_update.psycopg2.connect"), patch.object(
            full_update, "DBController"
        ) as mock_ctrl_cls, patch.object(
            full_update, "NoConnectionCheckPreflight"
        ) as mock_noconn_cls, patch(
            "sys.argv", ["full-update.py"]
        ):

            ctrl = MagicMock()
            ctrl.primary = "host=localhost dbname=lp"
            ctrl.pause_replication.return_value = False
            mock_ctrl_cls.return_value = ctrl

            mock_noconn = MagicMock()
            mock_noconn.check_all.return_value = True
            mock_noconn_cls.return_value = mock_noconn

            result = full_update.main()

        self.assertEqual(result, 93)

    def test_rollback_on_security_failure(self):
        # Keep setup steps green and fail security so the transaction rolls
        # back.
        with patch(
            "full_update.psycopg2.connect"
        ) as mock_connect, patch.object(
            full_update, "DBController"
        ) as mock_ctrl_cls, patch.object(
            full_update, "NoConnectionCheckPreflight"
        ) as mock_noconn_cls, patch.object(
            full_update, "KillConnectionsPreflight"
        ) as mock_kill_cls, patch.object(
            full_update, "run_upgrade", return_value=0
        ), patch.object(
            full_update, "run_security", return_value=1
        ), patch(
            "sys.argv", ["full-update.py"]
        ):

            mock_con = MagicMock()
            mock_connect.return_value = mock_con
            ctrl = MagicMock()
            ctrl.primary = "host=localhost dbname=lp"
            ctrl.disable_primary.return_value = True
            ctrl.enable_primary.return_value = True
            mock_ctrl_cls.return_value = ctrl

            mock_pf = MagicMock()
            mock_pf.check_all.return_value = True
            mock_noconn_cls.return_value = mock_pf
            mock_kill_cls.return_value = mock_pf

            result = full_update.main()

        self.assertEqual(result, 1)
        mock_con.rollback.assert_called_once()

    def test_finally_resumes_replication_on_failure_without_skip(self):
        # Let setup succeed until upgrade fails so finally() performs recovery.
        with patch(
            "full_update.psycopg2.connect"
        ) as mock_connect, patch.object(
            full_update, "DBController"
        ) as mock_ctrl_cls, patch.object(
            full_update, "NoConnectionCheckPreflight"
        ) as mock_noconn_cls, patch.object(
            full_update, "KillConnectionsPreflight"
        ) as mock_kill_cls, patch.object(
            full_update, "run_upgrade", return_value=1
        ), patch.object(
            full_update, "run_security", return_value=0
        ), patch(
            "sys.argv", ["full-update.py"]
        ):

            mock_connect.return_value = MagicMock()
            ctrl = MagicMock()
            ctrl.primary = "host=localhost dbname=lp"
            ctrl.pause_replication.return_value = True
            ctrl.disable_primary.return_value = True
            ctrl.enable_primary.return_value = True
            ctrl.resume_replication.return_value = True
            mock_ctrl_cls.return_value = ctrl

            mock_pf = MagicMock()
            mock_pf.check_all.return_value = True
            mock_noconn_cls.return_value = mock_pf
            mock_kill_cls.return_value = mock_pf

            full_update.main()

        ctrl.resume_replication.assert_called()
        ctrl.enable_primary.assert_called()

    def test_finally_logs_when_primary_reenable_fails(self):
        # Force cleanup to fail when re-enabling the primary and check the log.
        with patch(
            "full_update.psycopg2.connect"
        ) as mock_connect, patch.object(
            full_update, "DBController"
        ) as mock_ctrl_cls, patch.object(
            full_update, "NoConnectionCheckPreflight"
        ) as mock_noconn_cls, patch.object(
            full_update, "KillConnectionsPreflight"
        ) as mock_kill_cls, patch.object(
            full_update, "run_upgrade", return_value=1
        ), patch.object(
            full_update, "run_security", return_value=0
        ), patch(
            "sys.argv", ["full-update.py"]
        ):

            mock_connect.return_value = MagicMock()

            ctrl = MagicMock()
            ctrl.primary = "host=localhost dbname=lp"
            ctrl.pause_replication.return_value = True
            ctrl.disable_primary.return_value = True
            ctrl.enable_primary.return_value = False
            mock_ctrl_cls.return_value = ctrl

            mock_pf = MagicMock()
            mock_pf.check_all.return_value = True
            mock_noconn_cls.return_value = mock_pf
            mock_kill_cls.return_value = mock_pf

            full_update.main()

        self.mock_log.warning.assert_any_call(
            "Primary is still disabled in pgbouncer. Outage ongoing."
        )

    def test_finally_reenables_standbys_if_left_disabled(self):
        # Fail standby re-enable in the main flow so finally() retries it.
        with patch(
            "full_update.psycopg2.connect"
        ) as mock_connect, patch.object(
            full_update, "DBController"
        ) as mock_ctrl_cls, patch.object(
            full_update, "NoConnectionCheckPreflight"
        ) as mock_noconn_cls, patch.object(
            full_update, "KillConnectionsPreflight"
        ) as mock_kill_cls, patch.object(
            full_update, "run_upgrade", return_value=0
        ), patch.object(
            full_update, "run_security", return_value=0
        ), patch(
            "sys.argv", ["full-update.py"]
        ):

            mock_connect.return_value = MagicMock()

            ctrl = MagicMock()
            ctrl.primary = "host=localhost dbname=lp"
            ctrl.pause_replication.return_value = True
            ctrl.disable_primary.return_value = True
            ctrl.enable_primary.return_value = True
            ctrl.disable_standbys.return_value = True
            ctrl.resume_replication.return_value = True
            ctrl.sync.return_value = True
            # First call in try block fails, second call in finally recovers.
            ctrl.enable_standbys.side_effect = [False, True]
            mock_ctrl_cls.return_value = ctrl

            mock_noconn = MagicMock()
            mock_noconn.check_all.return_value = True
            mock_noconn_cls.return_value = mock_noconn

            mock_kill = MagicMock()
            mock_kill.check_all.return_value = True
            mock_kill_cls.return_value = mock_kill

            result = full_update.main()

        self.assertEqual(result, 0)
        self.assertEqual(ctrl.enable_standbys.call_count, 2)
