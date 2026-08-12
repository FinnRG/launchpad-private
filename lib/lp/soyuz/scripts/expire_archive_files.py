#!/usr/bin/python3
#
# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from storm.expr import And, Cast, Except, Is, Not, Or, Select

from lp.registry.model.person import Person
from lp.services.database.constants import UTC_NOW
from lp.services.database.interfaces import IStore
from lp.services.database.stormexpr import Concatenate
from lp.services.librarian.model import LibraryFileAlias
from lp.services.scripts.base import LaunchpadCronScript
from lp.soyuz.enums import ArchivePurpose
from lp.soyuz.model.archive import Archive
from lp.soyuz.model.archivefile import ArchiveFile
from lp.soyuz.model.files import BinaryPackageFile, SourcePackageReleaseFile
from lp.soyuz.model.publishing import (
    BinaryPackagePublishingHistory,
    SourcePackagePublishingHistory,
)

# PPA owners or particular PPAs that we never want to expire.
NEVER_EXPIRE_PPAS = """
bzr
bzr-beta-ppa
bzr-nightly-ppa
canonical-foundations/ubuntu-image
canonical-foundations/uc20-build-ppa
canonical-foundations/uc20-staging-ppa
canonical-kernel-team/uc20-release
canonical-kernel-dgx/release
canonical-nvidia/nvidia-virtualization
canonical-nvidia/nvidia-virtualization-proposed
canonical-nvidia/nvidia-virtualization-staging
checkbox-dev/beta
checkbox-dev/stable
chelsea-team
ci-train-ppa-service/stable-phone-overlay
dennis-team
elvis-team
fluendo-isv
kobuk-team/intel-graphics
kobuk-team/intel-graphics-staging
natick-team
netbook-remix-team
netbook-team
oem-solutions-group
payson
snappy-dev/edge
snappy-dev/image
snappy-dev/tools
transyl
ubuntu-cloud-archive
ubuntu-esm/esm-apps-security
ubuntu-esm/esm-apps-updates
ubuntu-esm/esm-infra-legacy-security
ubuntu-esm/esm-infra-legacy-updates
ubuntu-esm/esm-infra-security
ubuntu-esm/esm-infra-updates
ubuntu-mobile
ubuntu-security/ubuntu-security-staging
ubuntu-security-proposed/ppa
wheelbarrow
""".split()

# Particular PPAs (not owners, unlike the never-expire list) that should be
# expired even if they're private.
ALWAYS_EXPIRE_PPAS = """
adobe-isv/flash64
adobe-isv/ppa
kubuntu-ninjas/ppa
landscape/lds-trunk
moblin/moblin-private-beta
""".split()


class ArchiveExpirer(LaunchpadCronScript):
    """Helper class for expiring old PPA binaries.

    Any PPA binary older than 30 days that is superseded or deleted
    will be marked for immediate expiry.
    """

    never_expire = NEVER_EXPIRE_PPAS
    always_expire = ALWAYS_EXPIRE_PPAS

    def add_my_options(self):
        """Add script command line options."""
        self.parser.add_option(
            "-n",
            "--dry-run",
            action="store_true",
            dest="dryrun",
            metavar="DRY_RUN",
            default=False,
            help="If set, no transactions are committed",
        )
        self.parser.add_option(
            "-e",
            "--expire-after",
            action="store",
            type="int",
            dest="num_days",
            metavar="DAYS",
            default=15,
            help=(
                "The number of days after which to expire binaries. "
                "Must be specified."
            ),
        )

    def _determineExpirables(self, num_days, table):
        """Return expirable LibraryFileAlias IDs based on a given table."""
        stay_of_execution = Cast("%d days" % num_days, "interval")
        archive_types = (ArchivePurpose.PPA, ArchivePurpose.PARTNER)
        eligible_clauses = []
        denied_clauses = []

        LFA = LibraryFileAlias
        if table == BinaryPackageFile:
            BPF = BinaryPackageFile
            BPPH = BinaryPackagePublishingHistory
            lfa_column = BPF.libraryfile_id
            date_removed_column = BPPH.dateremoved
            eligible_clauses.extend(
                [
                    BPF.libraryfile == LFA.id,
                    BPF.binarypackagerelease == BPPH.binarypackagerelease_id,
                    BPPH.archive == Archive.id,
                ]
            )
            denied_clauses.extend(
                [
                    BPF.binarypackagerelease == BPPH.binarypackagerelease_id,
                    BPPH.archive == Archive.id,
                ]
            )
        elif table == SourcePackageReleaseFile:
            SPRF = SourcePackageReleaseFile
            SPPH = SourcePackagePublishingHistory
            lfa_column = SPRF.libraryfile_id
            date_removed_column = SPPH.dateremoved
            eligible_clauses.extend(
                [
                    SPRF.libraryfile == LFA.id,
                    SPRF.sourcepackagerelease == SPPH.sourcepackagerelease_id,
                    SPPH.archive == Archive.id,
                ]
            )
            denied_clauses.extend(
                [
                    SPRF.sourcepackagerelease == SPPH.sourcepackagerelease_id,
                    SPPH.archive == Archive.id,
                ]
            )
        elif table == ArchiveFile:
            lfa_column = ArchiveFile.library_file_id
            date_removed_column = ArchiveFile.date_removed
            eligible_clauses.extend(
                [
                    ArchiveFile.library_file == LFA.id,
                    ArchiveFile.date_removed < UTC_NOW - stay_of_execution,
                    ArchiveFile.archive == Archive.id,
                ]
            )
            # Unlike BPF and SPRF, ArchiveFile rows don't currently share
            # LFAs (although they may share LFCs after deduplication).
            # However, we still use denied_clauses to implement
            # archive-based expiry exceptions.
            denied_clauses.append(ArchiveFile.archive == Archive.id)
        else:
            raise AssertionError("Unknown table: %r" % table)
        full_archive_name = Concatenate(
            Person.name, Concatenate("/", Archive.name)
        )

        # The subquery here has to repeat the checks for privacy and expiry
        # control on *other* publications that are also done in the main
        # loop for the archive being considered.
        eligible = Select(
            LFA.id,
            where=And(
                *eligible_clauses,
                date_removed_column < UTC_NOW - stay_of_execution,
                Archive.purpose.is_in(archive_types),
                LFA.expires == None,
            ),
        )
        denied = Select(
            lfa_column,
            where=And(
                *denied_clauses,
                Archive.owner == Person.id,
                Or(
                    And(
                        Or(
                            Person.name.is_in(self.never_expire),
                            full_archive_name.is_in(self.never_expire),
                        ),
                        Archive.purpose == ArchivePurpose.PPA,
                    ),
                    And(
                        Is(Archive.private, True),
                        Not(full_archive_name.is_in(self.always_expire)),
                    ),
                    Not(Archive.purpose.is_in(archive_types)),
                    date_removed_column > UTC_NOW - stay_of_execution,
                    date_removed_column == None,
                ),
            ),
        )
        return list(self.store.execute(Except(eligible, denied)))

    def main(self):
        self.logger.info("Starting the PPA binary expiration")
        num_days = self.options.num_days
        self.logger.info("Expiring files up to %d days ago" % num_days)

        self.store = IStore(Archive)

        lfa_ids = []
        for table in (
            BinaryPackageFile,
            SourcePackageReleaseFile,
            ArchiveFile,
        ):
            lfa_ids.extend(self._determineExpirables(num_days, table))
        batch_count = 0
        batch_limit = 500
        for id in lfa_ids:
            self.logger.info("Expiring libraryfilealias %s" % id)
            self.store.execute(
                """
                UPDATE libraryfilealias
                SET expires = CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                WHERE id = %s
                """
                % id
            )
            batch_count += 1
            if batch_count % batch_limit == 0:
                if self.options.dryrun:
                    self.logger.info(
                        "%s done, not committing (dryrun mode)" % batch_count
                    )
                    self.txn.abort()
                else:
                    self.logger.info(
                        "%s done, committing transaction" % batch_count
                    )
                    self.txn.commit()

        if self.options.dryrun:
            self.txn.abort()
        else:
            self.txn.commit()

        self.logger.info("Finished PPA binary expiration")
