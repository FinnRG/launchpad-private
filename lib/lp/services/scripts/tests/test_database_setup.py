# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for non-destructive database configuration updates."""

import os
import subprocess
from pathlib import Path

from lp.services.config import config
from lp.testing import TestCase
from lp.testing.layers import BaseLayer


class TestDatabaseSetup(TestCase):
    layer = BaseLayer

    def setUp(self):
        super().setUp()
        directory = Path(self.makeTemporaryDirectory())
        self.postgresql_conf = directory / "postgresql.conf"
        self.commands = directory / "commands"
        sudo = directory / "sudo"
        sudo.write_text(
            """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$TEST_COMMANDS"
case "$1" in
    grep)
        test "$*" = \
            "grep -qs ^auto /etc/postgresql/$TEST_PGVERSION/main/start.conf"
        ;;
    pg_conftool)
        if [ "${TEST_CONFIG_FAILURE:-0}" = 1 ]; then
            echo "Configuration update failed" >&2
            exit 1
        fi
        shift 3
        exec /usr/bin/pg_conftool "$TEST_CONFIG" "$@"
        ;;
    pg_ctlcluster)
        test "$*" = "pg_ctlcluster $TEST_PGVERSION main reload"
        ;;
    *)
        echo "Unexpected command: $*" >&2
        exit 1
        ;;
esac
"""
        )
        sudo.chmod(0o755)
        self.environment = dict(
            os.environ,
            PATH=str(directory) + os.pathsep + os.environ["PATH"],
            TEST_CONFIG=str(self.postgresql_conf),
            TEST_COMMANDS=str(self.commands),
            TEST_PGVERSION="16",
        )

    def runSetup(self, *args):
        return subprocess.run(
            [
                "/bin/sh",
                os.path.join(
                    config.root, "utilities", "launchpad-database-setup"
                ),
                *args,
            ],
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def assertStringSettings(self):
        result = subprocess.run(
            ["/usr/bin/pg_conftool", str(self.postgresql_conf), "show", "all"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.assertIn("standard_conforming_strings = on\n", result.stdout)
        self.assertNotIn("escape_string_warning =", result.stdout)
        self.assertIn("shared_buffers = 256MB\n", result.stdout)
        self.assertIn(
            "shared_buffers = '256MB'\n", self.postgresql_conf.read_text()
        )
        version = self.environment["TEST_PGVERSION"]
        probes = ["grep -qs ^auto /etc/postgresql/18/main/start.conf"]
        if version == "16":
            probes.append("grep -qs ^auto /etc/postgresql/16/main/start.conf")
        self.assertEqual(
            probes
            + [
                f"pg_conftool {version} main set "
                "standard_conforming_strings on",
                f"pg_conftool {version} main remove escape_string_warning",
                f"pg_ctlcluster {version} main reload",
            ],
            self.commands.read_text().splitlines(),
        )

    def test_update_legacy_config(self):
        self.postgresql_conf.write_text(
            "## Launchpad configuration\n"
            "standard_conforming_strings=off\n"
            "escape_string_warning=off\n"
            "shared_buffers = '256MB'\n"
        )
        result = self.runSetup("--update-config")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Reconnect existing clients", result.stdout)
        self.assertStringSettings()
        updated = self.postgresql_conf.read_bytes()
        self.commands.unlink()
        result = self.runSetup("--update-config")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(updated, self.postgresql_conf.read_bytes())
        self.assertStringSettings()

    def test_update_config_without_explicit_string_settings(self):
        self.postgresql_conf.write_text("shared_buffers = '256MB'\n")
        result = self.runSetup("--update-config")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertStringSettings()

    def test_update_postgresql_18(self):
        self.environment["TEST_PGVERSION"] = "18"
        self.postgresql_conf.write_text("shared_buffers = '256MB'\n")
        result = self.runSetup("--update-config")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Using postgres 18", result.stdout)
        self.assertStringSettings()

    def test_update_failure_does_not_reload(self):
        self.postgresql_conf.write_text("standard_conforming_strings=off\n")
        self.environment["TEST_CONFIG_FAILURE"] = "1"
        result = self.runSetup("--update-config")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Configuration update failed", result.stderr)
        self.assertNotIn("pg_ctlcluster", self.commands.read_text())
        self.assertEqual(
            "standard_conforming_strings=off\n",
            self.postgresql_conf.read_text(),
        )

    def test_invalid_arguments_do_not_change_cluster(self):
        for args in ((), ("--unknown",), ("--update-config", "developer")):
            result = self.runSetup(*args)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("--update-config", result.stdout)
            self.assertFalse(self.commands.exists())
