#!/usr/bin/python3 -S
# Copyright 2011-2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Full update process."""

import _pythonpath  # noqa: F401

import sys
from datetime import datetime
from optparse import OptionParser

import psycopg2

import security  # security.py script
import upgrade  # upgrade.py script
from dbcontroller import DBController
from lp.services.scripts import logger, logger_options
from preflight import (
    SYSTEM_USERS,
    KillConnectionsPreflight,
    NoConnectionCheckPreflight,
)


def run_upgrade(options, log, primary_con):
    """Invoke upgrade.py in-process.

    It would be easier to just invoke the script, but this way we save
    several seconds of overhead as the component architecture loads up.
    """
    # Fake expected command line arguments and global log
    upgrade.options = options
    upgrade.log = log
    # upgrade.py doesn't commit, because we are sharing the transaction
    # with security.py. We want schema updates and security changes
    # applied in the same transaction.
    options.commit = False
    options.partial = False
    options.comments = False  # Saves about 1s. Apply comments manually.
    options.separate_sessions = False
    options.dbname = None
    # Invoke the database schema upgrade process.
    try:
        return upgrade.main(primary_con)
    except Exception:
        log.exception("Unhandled exception")
        return 1
    except SystemExit as x:
        log.fatal("upgrade.py failed [%s]", x)


def run_security(options, log, primary_con):
    """Invoke security.py in-process.

    It would be easier to just invoke the script, but this way we save
    several seconds of overhead as the component architecture loads up.
    """
    # Fake expected command line arguments and global log
    options.dryrun = False
    options.revoke = True
    options.owner = "postgres"
    options.dbname = None
    security.options = options
    security.log = log
    # Invoke the database security reset process.
    try:
        return security.main(options, primary_con)
    except Exception:
        log.exception("Unhandled exception")
        return 1
    except SystemExit as x:
        log.fatal("security.py failed [%s]", x)


def main():
    parser = OptionParser()
    parser.add_option(
        "--pgbouncer",
        dest="pgbouncer",
        default="host=localhost port=6432 user=pgbouncer",
        metavar="CONN_STR",
        help="libpq connection string to administer pgbouncer",
    )
    parser.add_option(
        "--dbname",
        dest="dbname",
        default="launchpad_prod",
        metavar="DBNAME",
        help="Database name we are updating.",
    )
    parser.add_option(
        "--dbuser",
        dest="dbuser",
        default="postgres",
        metavar="USERNAME",
        help="Connect as USERNAME to databases",
    )
    parser.add_option(
        "--skip-replication-checks",
        dest="skip_replication_checks",
        default=False,
        action="store_true",
        help="Skip replication lag and sync checks",
    )

    logger_options(parser, milliseconds=True)
    (options, args) = parser.parse_args()
    if args:
        parser.error("Too many arguments")

    # In case we are connected as a non-standard superuser, ensure we
    # don't kill our own connections.
    SYSTEM_USERS.add(options.dbuser)

    log = logger(options)

    controller = DBController(
        log, options.pgbouncer, options.dbname, options.dbuser
    )

    try:
        # Primary connection, not running in autocommit to allow us to
        # rollback changes on failure.
        primary_con = psycopg2.connect(str(controller.primary))
    except Exception as x:
        log.fatal("Unable to open connection to primary db (%s)", str(x))
        return 94

    # Preflight checks. Confirm as best we can that the upgrade will
    # work unattended. Here we ignore open connections, as they
    # will shortly be killed.
    if not options.skip_replication_checks:
        controller.ensure_replication_enabled()
    if not NoConnectionCheckPreflight(
        log,
        controller,
        skip_replication_checks=options.skip_replication_checks,
    ).check_all():
        return 99

    #
    # Start the actual upgrade. Failures beyond this point need to
    # generate informative messages to help with recovery.
    #

    # status flags
    upgrade_run = False
    security_run = False
    replication_paused = False
    primary_disabled = False
    standbys_disabled = False
    outage_start = None

    try:
        # Pause replication.
        if not options.skip_replication_checks:
            replication_paused = controller.pause_replication()
            if not replication_paused:
                return 93

        # Start the outage clock.
        log.info("Outage starts.")
        outage_start = datetime.now()

        # Disable access and kill connections to the primary database.
        primary_disabled = controller.disable_primary()
        if not primary_disabled:
            return 95

        if not KillConnectionsPreflight(
            log,
            controller,
            replication_paused=replication_paused,
            skip_replication_checks=options.skip_replication_checks,
        ).check_all():
            return 100

        log.info("Preflight check succeeded. Starting upgrade.")
        # Does not commit primary_con, even on success.
        upgrade_rc = run_upgrade(options, log, primary_con)
        upgrade_run = upgrade_rc == 0
        if not upgrade_run:
            return upgrade_rc
        log.info("Database patches applied.")

        # Commits primary_con on success.
        security_rc = run_security(options, log, primary_con)
        security_run = security_rc == 0
        if not security_run:
            return security_rc

        primary_disabled = not controller.enable_primary()
        if primary_disabled:
            log.warning("Outage ongoing until pgbouncer bounced.")
            return 96
        else:
            log.info("Outage complete. %s", datetime.now() - outage_start)

        standbys_disabled = controller.disable_standbys()

        # Resume replication.
        if not options.skip_replication_checks:
            replication_paused = not controller.resume_replication()
            if replication_paused:
                log.error(
                    "Failed to resume replication. Run pg_wal_replay_pause() "
                    "on all standbys to manually resume."
                )
            else:
                if controller.sync():
                    log.info("Standbys in sync. Updates replicated.")
                else:
                    log.error(
                        "Standbys failed to sync. Updates may not be "
                        "replicated."
                    )

        if standbys_disabled:
            standbys_disabled = not controller.enable_standbys()
            if standbys_disabled:
                log.warning(
                    "Failed to enable standby databases in pgbouncer. "
                    "Now running in primary-only mode."
                )

        # We will start seeing connections as soon as pgbouncer is
        # reenabled, so ignore them here.
        if not NoConnectionCheckPreflight(
            log,
            controller,
            skip_replication_checks=options.skip_replication_checks,
        ).check_all():
            return 101

        log.info("All good. All done.")
        return 0

    finally:
        if not security_run:
            log.warning("Rolling back all schema and security changes.")
            primary_con.rollback()

        # Recovery if necessary.
        if primary_disabled:
            if controller.enable_primary():
                log.warning(
                    "Primary reenabled despite earlier failures. "
                    "Outage over %s, but we have problems",
                    str(datetime.now() - outage_start),
                )
            else:
                log.warning(
                    "Primary is still disabled in pgbouncer. Outage ongoing."
                )

        if not options.skip_replication_checks and replication_paused:
            controller.resume_replication()

        if standbys_disabled:
            controller.enable_standbys()


if __name__ == "__main__":
    sys.exit(main())
