# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Verify that LibraryFileContent rows have their files present in Swift.

This script walks the LibraryFileContent table (the
authoritative list of files that should exist) and confirms that each file
is present in Swift.

* It keeps a checkpoint file recording the highest content id verified so
  far, and resumes from there on the next run.
* When it reaches the end of the table it clears the checkpoint so that the
  next run starts a fresh pass.

The librarian-gc job prunes unreferenced files from both the database
and Swift every day.  To avoid spurious "missing file" reports caused by
that pruning racing with this verification, the script:

* re-checks the database (against the primary store) before reporting a
  file as missing, so that a file removed by librarian-gc after we read
  the batch is treated as pruned rather than missing.
"""

__all__ = ["VerifyLibrarianSwift"]

import os
from datetime import datetime, timedelta, timezone

import transaction
from swiftclient import client as swiftclient

from lp.services.database.interfaces import IPrimaryStore, IStandbyStore
from lp.services.features import getFeatureFlag
from lp.services.librarian.model import LibraryFileContent
from lp.services.librarianserver import swift
from lp.services.scripts.base import LaunchpadCronScript

DEFAULT_BATCH_SIZE = 1000
DEFAULT_GRACE_PERIOD_DAYS = 1
DEFAULT_CHECKPOINT_FILE = "librarian-verify-swift.checkpoint"


def _utcnow():
    # Wrapper that is replaced in the test suite.
    return datetime.now(timezone.utc)


def swift_object_exists(content_id):
    """Return True if a file is present in any configured Swift instance."""
    container, name = swift.swift_location(content_id)
    for connection_pool in swift.connection_pools:
        swift_connection = connection_pool.get()
        try:
            swift.quiet_swiftclient(
                swift_connection.head_object, container, name
            )
            connection_pool.put(swift_connection)
            return True
        except swiftclient.ClientException as x:
            if x.http_status == 404:
                connection_pool.put(swift_connection)
            else:
                raise
    return False


class VerifyLibrarianSwift(LaunchpadCronScript):
    """Verify that Librarian files referenced in the db exist in Swift."""

    def add_my_options(self):
        self.parser.add_option(
            "-b",
            "--batch-size",
            action="store",
            type="int",
            default=DEFAULT_BATCH_SIZE,
            dest="batch_size",
            help="Number of rows to process per batch",
        )
        self.parser.add_option(
            "-s",
            "--start-id",
            action="store",
            type="int",
            default=None,
            dest="start_id",
            help=(
                "Start verifying from this content id, ignoring any "
                "checkpoint"
            ),
        )
        self.parser.add_option(
            "--checkpoint-file",
            action="store",
            default=DEFAULT_CHECKPOINT_FILE,
            dest="checkpoint_file",
            metavar="PATH",
            help="File used to record progress so the script can resume",
        )
        self.parser.add_option(
            "--restart",
            action="store_true",
            default=False,
            dest="restart",
            help="Ignore any existing checkpoint and start from the beginning",
        )
        self.parser.add_option(
            "--grace-period-days",
            action="store",
            type="int",
            default=DEFAULT_GRACE_PERIOD_DAYS,
            dest="grace_period_days",
            help=(
                "Skip files created within this many days, as they may not "
                "have been fed into Swift yet"
            ),
        )

    def _read_checkpoint(self):
        """Return the highest verified content id, or None."""
        try:
            with open(self.options.checkpoint_file) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _write_checkpoint(self, last_id):
        """Record the highest verified content id."""
        with open(self.options.checkpoint_file, "w") as f:
            f.write("%d\n" % last_id)

    def _reset_checkpoint(self):
        """Remove the checkpoint so the next run starts a fresh pass."""
        try:
            os.unlink(self.options.checkpoint_file)
        except FileNotFoundError:
            pass

    def _determine_start_id(self):
        if self.options.start_id is not None:
            return self.options.start_id
        if self.options.restart:
            return 1
        checkpoint = self._read_checkpoint()
        if checkpoint is not None:
            self.logger.info("Resuming from checkpoint id %d.", checkpoint)
            return checkpoint + 1
        return 1

    def _still_in_db(self, content_id):
        """True if the content still exists in the primary database.

        Used to distinguish a genuinely missing file from one that
        ``librarian-gc`` pruned after we read the batch.
        """
        # Use a fresh connection so we see the latest committed state.
        store = IPrimaryStore(LibraryFileContent)
        return not store.find(
            LibraryFileContent, LibraryFileContent.id == content_id
        ).is_empty()

    def main(self):
        if not (getFeatureFlag("librarian.swift.enabled") or False):
            self.logger.info(
                "librarian.swift.enabled is not set; nothing to verify."
            )
            return

        store = IStandbyStore(LibraryFileContent)
        batch_size = self.options.batch_size
        grace = timedelta(days=self.options.grace_period_days)
        start_at = self._determine_start_id()

        checked = verified = missing = pruned = skipped = errors = 0

        while True:
            contents = list(
                store.find(
                    LibraryFileContent,
                    LibraryFileContent.id >= start_at,
                ).order_by(LibraryFileContent.id)[:batch_size]
            )
            if not contents:
                break

            cutoff = _utcnow() - grace
            for content in contents:
                checked += 1

                if content.datecreated > cutoff:
                    skipped += 1
                    self.logger.debug(
                        "Skipping recently created LibraryFileContent %d.",
                        content.id,
                    )
                    continue

                try:
                    found = swift_object_exists(content.id)
                except swiftclient.ClientException as x:
                    errors += 1
                    self.logger.error(
                        "ERROR checking LibraryFileContent %d in Swift: %s",
                        content.id,
                        x,
                    )
                    continue

                if not found:
                    container, name = swift.swift_location(content.id)
                    if self._still_in_db(content.id):
                        missing += 1
                        self.logger.error(
                            "MISSING: LibraryFileContent %d not found in "
                            "Swift (container=%s, object=%s, size=%d, "
                            "created=%s).",
                            content.id,
                            container,
                            name,
                            content.filesize,
                            content.datecreated,
                        )
                    else:
                        pruned += 1
                        self.logger.info(
                            "LibraryFileContent %d was pruned from the "
                            "database; skipping.",
                            content.id,
                        )
                    continue

                verified += 1

            last_id = contents[-1].id
            self._write_checkpoint(last_id)
            start_at = last_id + 1
            self.logger.info(
                "Verified up to id %d "
                "(checked=%d verified=%d missing=%d pruned=%d).",
                last_id,
                checked,
                verified,
                missing,
                pruned,
            )
            transaction.commit()

        self._reset_checkpoint()
        self.logger.info(
            "Done. checked=%d verified=%d missing=%d pruned=%d "
            "skipped_recent=%d errors=%d.",
            checked,
            verified,
            missing,
            pruned,
            skipped,
            errors,
        )
