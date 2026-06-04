# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for database/schema/preflight.py."""

import os
import sys
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from lp.services.config import config

# Load preflight module without permanently polluting sys.path.
# Save original sys.path to restore after import.
_schema_dir = os.path.join(config.root, "database", "schema")
_orig_sys_path = sys.path.copy()
if _schema_dir not in sys.path:
    sys.path.insert(0, _schema_dir)

# Snapshot sys.modules before loading so tearDownModule can remove exactly
# the modules that the dynamic import pulled in.
_modules_before_load = set(sys.modules.keys())

from preflight import (  # noqa: E402
    BAD_USERS,
    FRAGILE_USERS,
    MAX_LAG,
    SYSTEM_USERS,
    DatabasePreflight,
    KillConnectionsPreflight,
    NoConnectionCheckPreflight,
)

# Restore sys.path to avoid import collisions for the test process lifetime.
sys.path[:] = _orig_sys_path

_modules_injected = set(sys.modules.keys()) - _modules_before_load


def tearDownModule():
    """Remove modules pulled in by the dynamic load from sys.modules."""
    for name in _modules_injected:
        sys.modules.pop(name, None)


def _make_preflight(cls=DatabasePreflight, num_standbys=2, **kwargs):
    """Create a preflight instance with mocked DB connections."""
    with patch("preflight.psycopg2.connect") as mock_connect:
        mock_connect.side_effect = lambda *a, **kw: MagicMock()

        controller = MagicMock()
        controller.primary = "host=localhost dbname=launchpad_prod"
        controller.standbys = MagicMock()
        controller.standbys.values.return_value = set()
        if num_standbys > 0:
            standbys = set()
            for i in range(num_standbys):
                standbys.add("host=standby%d dbname=launchpad_prod" % i)
            controller.standbys.values.return_value = standbys

        log = MagicMock()
        log.debug2 = MagicMock()
        pf = cls(log, controller, **kwargs)
        return pf


class TestCheckStandbyCount(unittest.TestCase):
    """Test check_standby_count compares DB count with provided count."""

    def test_passes_when_counts_match(self):
        pf = _make_preflight()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (2,)
        pf.lpmain_primary_node.con.cursor.return_value = mock_cursor

        self.assertTrue(pf.check_standby_count())

    def test_fails_when_counts_mismatch(self):
        pf = _make_preflight()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (1,)
        pf.lpmain_primary_node.con.cursor.return_value = mock_cursor

        self.assertFalse(pf.check_standby_count())
        pf.log.fatal.assert_called()


class TestCheckIsSuperuser(unittest.TestCase):
    """Test check_is_superuser verifies superuser on all nodes."""

    def test_passes_when_superuser(self):
        pf = _make_preflight()
        for node in pf.nodes:
            node_cursor = MagicMock()
            node_cursor.fetchone.return_value = ("launchpad_prod", True)
            node.con.cursor.return_value = node_cursor

        self.assertTrue(pf.check_is_superuser())

    def test_fails_when_not_superuser(self):
        pf = _make_preflight()
        for node in pf.nodes:
            node_cursor = MagicMock()
            node_cursor.fetchone.return_value = ("launchpad_prod", True)
            node.con.cursor.return_value = node_cursor

        primary_cursor = pf.lpmain_primary_node.con.cursor.return_value
        primary_cursor.fetchone.return_value = (
            "launchpad_prod",
            False,
        )

        self.assertFalse(pf.check_is_superuser())
        pf.log.fatal.assert_called()


class TestCheckOpenConnections(unittest.TestCase):
    """Test check_open_connections detects non-system user connections."""

    def test_passes_with_only_system_users(self):
        pf = _make_preflight()
        results = [
            ("launchpad_prod", system_user, 1) for system_user in SYSTEM_USERS
        ]
        for node in pf.lpmain_nodes:
            node_cursor = MagicMock()
            node_cursor.fetchall.return_value = results
            node.con.cursor.return_value = node_cursor

        self.assertTrue(pf.check_open_connections())

    def test_fails_with_non_system_user(self):
        pf = _make_preflight()
        for node in pf.lpmain_nodes:
            node_cursor = MagicMock()
            node_cursor.fetchall.return_value = []
            node.con.cursor.return_value = node_cursor

        primary_cursor = pf.lpmain_primary_node.con.cursor.return_value
        primary_cursor.fetchall.return_value = [
            ("launchpad_prod", "launchpad_main", 5),
        ]

        self.assertFalse(pf.check_open_connections())
        pf.log.fatal.assert_called()

    def test_fails_with_non_system_user_on_standby(self):
        pf = _make_preflight()
        for node in pf.lpmain_nodes:
            node_cursor = MagicMock()
            node_cursor.fetchall.return_value = []
            node.con.cursor.return_value = node_cursor

        standby_nodes = [
            node
            for node in pf.lpmain_nodes
            if node is not pf.lpmain_primary_node
        ]
        standby_nodes[0].con.cursor.return_value.fetchall.return_value = [
            ("launchpad_prod", "launchpad_main", 5),
        ]

        self.assertFalse(pf.check_open_connections())
        pf.log.fatal.assert_called()


class TestCheckFragileConnections(unittest.TestCase):
    """Test check_fragile_connections fails if fragile users connected."""

    def test_passes_with_no_fragile_users(self):
        pf = _make_preflight()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        pf.lpmain_primary_node.con.cursor.return_value = mock_cursor

        self.assertTrue(pf.check_fragile_connections())

    def test_fails_with_fragile_user(self):
        self.assertTrue(FRAGILE_USERS)
        pf = _make_preflight()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("launchpad_prod", next(iter(FRAGILE_USERS)), 1),
        ]
        pf.lpmain_primary_node.con.cursor.return_value = mock_cursor

        self.assertFalse(pf.check_fragile_connections())
        pf.log.fatal.assert_called()


class TestCheckLongRunningTransactions(unittest.TestCase):
    """Test long running transaction detection."""

    TRANSACTION_TIMEOUT = 60

    def test_passes_with_no_long_transactions(self):
        pf = _make_preflight()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        pf.lpmain_primary_node.con.cursor.return_value = mock_cursor

        self.assertTrue(pf.check_long_running_transactions())

    def test_ignores_bad_users(self):
        self.assertTrue(BAD_USERS)
        pf = _make_preflight()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            (
                "launchpad_prod",
                next(iter(BAD_USERS)),
                timedelta(seconds=self.TRANSACTION_TIMEOUT + 1),
            ),
        ]
        pf.lpmain_primary_node.con.cursor.return_value = mock_cursor

        self.assertTrue(
            pf.check_long_running_transactions(
                max_secs=self.TRANSACTION_TIMEOUT
            )
        )
        pf.log.info.assert_called()

    def test_fails_for_non_bad_user_long_transaction(self):
        pf = _make_preflight()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            (
                "launchpad_prod",
                "launchpad_main",
                timedelta(seconds=self.TRANSACTION_TIMEOUT + 1),
            ),
        ]
        pf.lpmain_primary_node.con.cursor.return_value = mock_cursor

        self.assertFalse(
            pf.check_long_running_transactions(
                max_secs=self.TRANSACTION_TIMEOUT
            )
        )
        pf.log.fatal.assert_called()


class TestCheckReplicationLag(unittest.TestCase):
    """Test replication lag checks across key lag scenarios."""

    @patch("preflight.time.time", side_effect=[100.0, 100.0])
    def test_no_streaming_replication_returns_true(self, mock_time):
        pf = _make_preflight(num_standbys=0)
        primary_cursor = MagicMock()
        primary_cursor.fetchone.return_value = (False, None)
        pf.lpmain_primary_node.con.cursor.return_value = primary_cursor

        self.assertTrue(pf.check_replication_lag())
        pf.log.debug.assert_any_call("No streaming replication")

    @patch("preflight.time.time", side_effect=[100.0, 100.0])
    @patch("preflight.time.sleep")
    def test_low_streaming_lag_returns_true(self, mock_sleep, mock_time):
        pf = _make_preflight(num_standbys=1)
        primary_cursor = MagicMock()
        primary_cursor.fetchone.return_value = (False, None)
        pf.lpmain_primary_node.con.cursor.return_value = primary_cursor

        standby_cursor = MagicMock()
        standby_cursor.fetchone.return_value = (True, timedelta(seconds=5))
        for node in pf.nodes:
            if node is not pf.lpmain_primary_node:
                node.con.cursor.return_value = standby_cursor

        self.assertTrue(pf.check_replication_lag())
        mock_sleep.assert_not_called()

    @patch("preflight.time.time", side_effect=[100.0, 100.0, 131.0])
    @patch("preflight.time.sleep")
    def test_high_streaming_lag_returns_false(self, mock_sleep, mock_time):
        pf = _make_preflight(num_standbys=1)
        primary_cursor = MagicMock()
        primary_cursor.fetchone.return_value = (False, None)
        pf.lpmain_primary_node.con.cursor.return_value = primary_cursor

        standby_cursor = MagicMock()
        standby_cursor.fetchone.return_value = (
            True,
            MAX_LAG + timedelta(seconds=1),
        )
        for node in pf.nodes:
            if node is not pf.lpmain_primary_node:
                node.con.cursor.return_value = standby_cursor

        self.assertFalse(pf.check_replication_lag())
        pf.log.fatal.assert_called()
        mock_sleep.assert_called_once()


class TestCheckCanSync(unittest.TestCase):
    """Test check_can_sync delegates to streaming_sync and logs result."""

    @patch("preflight.streaming_sync", return_value=True)
    def test_returns_true_when_streaming_sync_succeeds(self, mock_sync):
        pf = _make_preflight(num_standbys=0)

        self.assertTrue(pf.check_can_sync())
        mock_sync.assert_called_once_with(pf.lpmain_primary_node.con, 30)
        pf.log.info.assert_called_once_with("Streaming replicas syncing.")

    @patch("preflight.streaming_sync", return_value=False)
    def test_returns_false_when_streaming_sync_fails(self, mock_sync):
        pf = _make_preflight(num_standbys=0)

        self.assertFalse(pf.check_can_sync())
        mock_sync.assert_called_once_with(pf.lpmain_primary_node.con, 30)
        pf.log.fatal.assert_called_once_with("Streaming replicas not syncing.")


class TestNoConnectionCheckPreflight(unittest.TestCase):
    """Test NoConnectionCheckPreflight always passes open connection check."""

    def test_check_open_connections_always_true(self):
        pf = _make_preflight(cls=NoConnectionCheckPreflight)
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("launchpad_prod", "launchpad_main", 5),
        ]
        pf.lpmain_primary_node.con.cursor.return_value = mock_cursor
        self.assertTrue(pf.check_open_connections())


class TestKillConnectionsPreflight(unittest.TestCase):
    """Test KillConnectionsPreflight kills connections and respects
    replication_paused flag."""

    @patch("preflight.time.sleep")
    def test_returns_true_when_no_connections(self, mock_sleep):
        pf = _make_preflight(cls=KillConnectionsPreflight)
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        pf.lpmain_primary_node.con.cursor.return_value = mock_cursor

        self.assertTrue(pf.check_open_connections())
        self.assertIn(
            "pg_terminate_backend", mock_cursor.execute.call_args[0][0]
        )
        mock_sleep.assert_not_called()

    @patch("preflight.time.sleep")
    def test_retries_until_connections_cleared(self, mock_sleep):
        pf = _make_preflight(cls=KillConnectionsPreflight, num_standbys=0)
        mock_cursor = MagicMock()
        mock_cursor.fetchall.side_effect = [
            [(123, "launchpad_prod", "launchpad_main", True)],
            [],
        ]
        pf.lpmain_primary_node.con.cursor.return_value = mock_cursor

        self.assertTrue(pf.check_open_connections())
        self.assertEqual(2, mock_cursor.execute.call_count)
        for call in mock_cursor.execute.call_args_list:
            self.assertIn("pg_terminate_backend", call[0][0])
        mock_sleep.assert_called_once()

    @patch("preflight.time.sleep")
    def test_uses_only_primary_when_replication_paused(self, mock_sleep):
        pf = _make_preflight(
            cls=KillConnectionsPreflight,
            replication_paused=True,
            num_standbys=2,
        )

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        pf.lpmain_primary_node.con.cursor.return_value = mock_cursor

        standby_nodes = [
            node
            for node in pf.lpmain_nodes
            if node is not pf.lpmain_primary_node
        ]

        self.assertTrue(pf.check_open_connections())
        for standby_node in standby_nodes:
            standby_node.con.cursor.assert_not_called()
