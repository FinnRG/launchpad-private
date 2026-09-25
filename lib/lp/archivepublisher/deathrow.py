# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""
Processes removals of packages that are scheduled for deletion.
"""

from datetime import datetime, timezone

from storm.expr import Exists
from storm.locals import And, ClassAlias, Not, Select

from lp.archivepublisher.config import getPubConfig
from lp.services.database.constants import UTC_NOW
from lp.services.database.interfaces import IStore
from lp.services.librarian.model import LibraryFileAlias, LibraryFileContent
from lp.soyuz.enums import ArchivePurpose
from lp.soyuz.interfaces.publishing import (
    IBinaryPackagePublishingHistory,
    ISourcePackagePublishingHistory,
    MissingSymlinkInPool,
    NotInPool,
    inactive_publishing_status,
)
from lp.soyuz.model.files import BinaryPackageFile, SourcePackageReleaseFile
from lp.soyuz.model.publishing import (
    BinaryPackagePublishingHistory,
    SourcePackagePublishingHistory,
)


def getDeathRow(archive, log, pool_root_override):
    """Return a Deathrow object for the archive supplied.

    :param archive: Use the publisher config for this archive to derive the
                    DeathRow object.
    :param log: Use this logger for script debug logging.
    :param pool_root_override: Use this pool root for the archive instead of
         the one provided by the publishing-configuration, it will be only
         used for PRIMARY archives.
    """
    log.debug("Grab publisher config.")
    pubconf = getPubConfig(archive)

    if archive.purpose != ArchivePurpose.PRIMARY:
        pool_root_override = None

    dp = pubconf.getDiskPool(log, pool_root_override=pool_root_override)

    log.debug("Preparing death row.")
    return DeathRow(archive, dp, log)


class DeathRow:
    """A Distribution Archive Removal Processor.

    DeathRow will remove archive files from disk if they are marked for
    removal in the publisher tables, and if they are no longer referenced
    by other packages.
    """

    def __init__(self, archive, diskpool, logger):
        self.archive = archive
        self.diskpool = diskpool
        self._removeFile = diskpool.removeFile
        self.logger = logger

    def reap(self, dry_run=False):
        """Reap packages that should be removed from the distribution.

        Looks through all packages that are in condemned states and
        have scheduleddeletiondate is in the past, try to remove their
        files from the archive pool (which may be impossible if they are
        used by other packages which are published), and mark them as
        removed."""
        if dry_run:
            # Don't actually remove the files if we are dry running
            def _mockRemoveFile(
                component_name, pool_name, pool_version, pub_file
            ):
                self.logger.debug(
                    "(Not really!) removing %s %s/%s/%s"
                    % (
                        component_name,
                        pool_name,
                        pool_version,
                        pub_file.libraryfile.filename,
                    )
                )
                fullpath = self.diskpool.pathFor(
                    component_name, pool_name, pool_version, pub_file
                )
                if not fullpath.exists():
                    raise NotInPool
                return fullpath.lstat().st_size

            self._removeFile = _mockRemoveFile

        source_files, binary_files = self._collectCondemned()
        records = self._tryRemovingFromDisk(source_files, binary_files)
        self._markPublicationRemoved(records)

    def _collectCondemned(self):
        """Return the condemned source and binary publications as a tuple.

        Return all the `SourcePackagePublishingHistory` and
        `BinaryPackagePublishingHistory` records that are eligible for
        removal ('condemned') where the source/binary package that they
        refer to is not published somewhere else.

        Both sources and binaries are lists.
        """
        OtherSPPH = ClassAlias(SourcePackagePublishingHistory)
        other_active_spph = Select(
            1,
            tables=[OtherSPPH],
            where=And(
                SourcePackagePublishingHistory.sourcepackagerelease_id
                == OtherSPPH.sourcepackagerelease_id,
                OtherSPPH.archive_id == self.archive.id,
                Not(OtherSPPH.status.is_in(inactive_publishing_status)),
            ),
        )
        sources = list(
            IStore(SourcePackagePublishingHistory)
            .find(
                SourcePackagePublishingHistory,
                SourcePackagePublishingHistory.archive == self.archive,
                SourcePackagePublishingHistory.scheduleddeletiondate < UTC_NOW,
                SourcePackagePublishingHistory.dateremoved == None,
                Not(Exists(other_active_spph)),
            )
            .order_by(SourcePackagePublishingHistory.id)
        )
        self.logger.debug("%d Sources" % len(sources))

        OtherBPPH = ClassAlias(BinaryPackagePublishingHistory)
        other_active_bpph = Select(
            1,
            tables=[OtherBPPH],
            where=And(
                BinaryPackagePublishingHistory.binarypackagerelease_id
                == OtherBPPH.binarypackagerelease_id,
                OtherBPPH.archive_id == self.archive.id,
                Not(OtherBPPH.status.is_in(inactive_publishing_status)),
            ),
        )
        binaries = list(
            IStore(BinaryPackagePublishingHistory)
            .find(
                BinaryPackagePublishingHistory,
                BinaryPackagePublishingHistory.archive == self.archive,
                BinaryPackagePublishingHistory.scheduleddeletiondate < UTC_NOW,
                BinaryPackagePublishingHistory.dateremoved == None,
                Not(Exists(other_active_bpph)),
            )
            .order_by(BinaryPackagePublishingHistory.id)
        )
        self.logger.debug("%d Binaries" % len(binaries))

        return (sources, binaries)

    def canRemove(self, publication_class, filename, file_sha256):
        """Check if given (filename, MD5) can be removed from the pool.

        Check the archive reference-counter implemented in:
        `SourcePackagePublishingHistory` or
        `BinaryPackagePublishingHistory`.

        Only allow removal of unnecessary files.
        """
        clauses = []

        if ISourcePackagePublishingHistory.implementedBy(publication_class):
            clauses.extend(
                [
                    SourcePackagePublishingHistory.archive == self.archive,
                    SourcePackagePublishingHistory.dateremoved == None,
                    SourcePackagePublishingHistory.sourcepackagerelease
                    == SourcePackageReleaseFile.sourcepackagerelease_id,
                    SourcePackageReleaseFile.libraryfile
                    == LibraryFileAlias.id,
                ]
            )
        elif IBinaryPackagePublishingHistory.implementedBy(publication_class):
            clauses.extend(
                [
                    BinaryPackagePublishingHistory.archive == self.archive,
                    BinaryPackagePublishingHistory.dateremoved == None,
                    BinaryPackagePublishingHistory.binarypackagerelease
                    == BinaryPackageFile.binarypackagerelease_id,
                    BinaryPackageFile.libraryfile == LibraryFileAlias.id,
                ]
            )
        else:
            raise AssertionError("%r is not supported." % publication_class)

        clauses.extend(
            [
                LibraryFileAlias.content == LibraryFileContent.id,
                LibraryFileAlias.filename == filename,
                LibraryFileContent.sha256 == file_sha256,
            ]
        )

        all_publications = IStore(publication_class).find(
            publication_class, *clauses
        )

        right_now = datetime.now(timezone.utc)
        for pub in all_publications:
            # Deny removal if any reference is still active.
            if pub.status not in inactive_publishing_status:
                return False
            # Deny removal if any reference wasn't dominated yet.
            if pub.scheduleddeletiondate is None:
                return False
            # Deny removal if any reference is still in 'quarantine'.
            if pub.scheduleddeletiondate > right_now:
                return False

        return True

    def _tryRemovingFromDisk(
        self, condemned_source_files, condemned_binary_files
    ):
        """Take the list of publishing records provided and unpublish them.

        You should only pass in entries you want to be unpublished because
        this will result in the files being removed if they're not otherwise
        in use.
        """
        bytes = 0
        condemned_files = set()
        condemned_records = set()
        considered_files = set()
        details = {}

        def checkPubRecord(pub_record, publication_class):
            """Check if the publishing record can be removed.

            It can only be removed if all files in its context are not
            referred to any other 'published' publishing records.

            See `canRemove` for more information.
            """
            files = pub_record.files
            for pub_file in files:
                filename = pub_file.libraryfile.filename
                file_sha256 = pub_file.libraryfile.content.sha256

                self.logger.debug("Checking %s (%s)" % (filename, file_sha256))

                # Calculating the file path in pool.
                pub_file_details = (
                    pub_record.component_name,
                    pub_record.pool_name,
                    pub_record.pool_version,
                    pub_file,
                )
                file_path = str(self.diskpool.pathFor(*pub_file_details))

                # Check if the LibraryFileAlias in question was already
                # verified. If the verification was already made and the
                # file is condemned queue the publishing record for removal
                # otherwise just continue the iteration.
                if (filename, file_sha256) in considered_files:
                    self.logger.debug("Already verified.")
                    if file_path in condemned_files:
                        condemned_records.add(pub_record)
                    continue
                considered_files.add((filename, file_sha256))

                # Check if the removal is allowed, if not continue.
                if not self.canRemove(
                    publication_class, filename, file_sha256
                ):
                    self.logger.debug("Cannot remove.")
                    continue

                # Update local containers, in preparation to file removal.
                details.setdefault(file_path, pub_file_details)
                condemned_files.add(file_path)
                condemned_records.add(pub_record)

            # A source package with no files at all (which can happen in
            # some cases where the archive's repository format is not
            # ArchiveRepositoryFormat.DEBIAN) cannot have any files which
            # refer to other publishing records, so can always be removed.
            if not files:
                condemned_records.add(pub_record)

        # Check source and binary publishing records.
        for pub_record in condemned_source_files:
            checkPubRecord(pub_record, SourcePackagePublishingHistory)
        for pub_record in condemned_binary_files:
            checkPubRecord(pub_record, BinaryPackagePublishingHistory)

        self.logger.info(
            "Removing %s files marked for reaping" % len(condemned_files)
        )

        for condemned_file in sorted(condemned_files, reverse=True):
            component_name, pool_name, pool_version, pub_file = details[
                condemned_file
            ]
            try:
                bytes += self._removeFile(
                    component_name, pool_name, pool_version, pub_file
                )
            except NotInPool as info:
                # It's safe for us to let this slide because it means that
                # the file is already gone.
                self.logger.debug(str(info))
            except MissingSymlinkInPool as info:
                # This one is a little more worrying, because an expected
                # symlink has vanished from the pool/ (could be a code
                # mistake) but there is nothing we can do about it at this
                # point.
                self.logger.warning(str(info))

        self.logger.info("Total bytes freed: %s" % bytes)

        return condemned_records

    def _markPublicationRemoved(self, condemned_records):
        # Now that the os.remove() calls have been made, simply let every
        # now out-of-date record be marked as removed.
        self.logger.debug(
            "Marking %s condemned packages as removed."
            % len(condemned_records)
        )
        for record in condemned_records:
            record.dateremoved = UTC_NOW
