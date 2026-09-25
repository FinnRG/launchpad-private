# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Death row processor base script class

This script removes obsolete files from the selected archive(s) pool.
"""

__all__ = [
    "DeathRowProcessor",
]

from zope.component import getUtility

from lp.archivepublisher.deathrow import getDeathRow
from lp.archivepublisher.scripts.base import PublisherScript
from lp.services.limitedlist import LimitedList
from lp.services.webapp.adapter import (
    clear_request_started,
    set_request_started,
)
from lp.soyuz.enums import ArchivePurpose
from lp.soyuz.interfaces.archive import IArchiveSet


class DeathRowProcessor(PublisherScript):
    def add_my_options(self):
        self.parser.add_option(
            "-n",
            "--dry-run",
            action="store_true",
            default=False,
            help="Dry run: goes through the motions but commits to nothing.",
        )

        self.addDistroOptions()

        self.parser.add_option(
            "-p",
            "--pool-root",
            metavar="PATH",
            help="Override the path to the pool folder",
        )

        self.parser.add_option(
            "--ppa",
            action="store_true",
            default=False,
            help="Run only over PPA archives.",
        )

    def getTargetArchives(self, distribution):
        """Find archives to target based on given options."""
        if self.options.ppa:
            return getUtility(IArchiveSet).getArchivesForDistribution(
                distribution,
                purposes=[ArchivePurpose.PPA],
                check_permissions=False,
                exclude_pristine=True,
            )
        else:
            return distribution.all_distro_archives

    def main(self):
        for distribution in self.findDistros():
            for archive in self.getTargetArchives(distribution):
                self.logger.info("Processing %s" % archive.archive_url)
                self.processDeathRow(archive)

    def processDeathRow(self, archive):
        """Process death-row for the given archive.

        It handles the current DB transaction according to the results of
        the operation just executed, i.e, commits successful runs and aborts
        runs with errors. It also respects 'dry-run' command-line option.
        """
        death_row = getDeathRow(archive, self.logger, self.options.pool_root)
        self.logger.debug(
            "Unpublishing death row for %s." % archive.displayname
        )
        set_request_started(
            request_statements=LimitedList(10000),
            txn=self.txn,
            enable_timeout=False,
        )
        try:
            death_row.reap(self.options.dry_run)
        except Exception:
            self.logger.exception(
                "Unexpected exception while doing death-row unpublish"
            )
            self.txn.abort()
        else:
            if self.options.dry_run:
                self.logger.info("Dry run mode; rolling back.")
                self.txn.abort()
            else:
                self.logger.debug("Committing")
                self.txn.commit()
        finally:
            clear_request_started()
