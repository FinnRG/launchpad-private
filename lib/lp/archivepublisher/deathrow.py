# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""
Processes removals of packages that are scheduled for deletion.
"""

import time
from collections import defaultdict
from datetime import datetime, timezone

from storm.expr import And, Exists, Or
from storm.locals import ClassAlias, Not, Select

from lp.archivepublisher.config import getPubConfig
from lp.registry.model.sourcepackagename import SourcePackageName
from lp.services.database import bulk
from lp.services.database.constants import UTC_NOW
from lp.services.database.interfaces import IStore
from lp.services.librarian.model import LibraryFileAlias, LibraryFileContent
from lp.soyuz.enums import ArchivePurpose, ArchiveRepositoryFormat
from lp.soyuz.interfaces.publishing import (
    IBinaryPackagePublishingHistory,
    ISourcePackagePublishingHistory,
    MissingSymlinkInPool,
    NotInPool,
    inactive_publishing_status,
)
from lp.soyuz.model.binarypackagebuild import BinaryPackageBuild
from lp.soyuz.model.binarypackagename import BinaryPackageName
from lp.soyuz.model.binarypackagerelease import BinaryPackageRelease
from lp.soyuz.model.files import BinaryPackageFile, SourcePackageReleaseFile
from lp.soyuz.model.publishing import (
    BinaryPackagePublishingHistory,
    SourcePackagePublishingHistory,
)
from lp.soyuz.model.sourcepackagerelease import SourcePackageRelease

# Cap the number of (filename, sha256) pairs checked by a single
# `_findRemovableFiles` query, both to keep the number of query
# parameters bounded and to avoid one enormous query hogging the database
# when tens of thousands of files are condemned at once (e.g. distro
# series end-of-life).
REMOVABLE_FILES_BATCH_SIZE = 2000

# Cap bulk helper `IN` clauses and bulk UPDATE `IN` clauses for the same
# reason as REMOVABLE_FILES_BATCH_SIZE.
RELATED_OBJECTS_BATCH_SIZE = 2000
MARK_REMOVED_BATCH_SIZE = 2000


def _batches(items, batch_size):
    items = list(items)
    for start in range(0, len(items), batch_size):
        yield items[start : start + batch_size]


def _loadRelatedInBatches(object_type, owning_objects, foreign_keys):
    result = []
    for batch in _batches(owning_objects, RELATED_OBJECTS_BATCH_SIZE):
        result.extend(bulk.load_related(object_type, batch, foreign_keys))
    return result


def _loadReferencingInBatches(object_type, owning_objects, reference_keys):
    result = []
    for batch in _batches(owning_objects, RELATED_OBJECTS_BATCH_SIZE):
        result.extend(
            bulk.load_referencing(object_type, batch, reference_keys)
        )
    return result


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

        start = time.monotonic()
        source_files, binary_files = self._collectCondemned()
        collected = time.monotonic()
        records = self._tryRemovingFromDisk(source_files, binary_files)
        removed = time.monotonic()
        self._markPublicationRemoved(records)
        marked = time.monotonic()
        self.logger.debug(
            "Timing: collect=%.3fs remove=%.3fs mark=%.3fs total=%.3fs"
            % (
                collected - start,
                removed - collected,
                marked - removed,
                marked - start,
            )
        )

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

    def _findRemovableFiles(self, publication_class, file_keys):
        """Check which (filename, sha256) keys are eligible for removal.

        Checking each condemned file's eligibility with a separate query
        does not scale: with tens of thousands of condemned files, the
        per-file round trip to the database dominates the run time. This
        does the equivalent check for every key in `file_keys` with one
        query per batch of `REMOVABLE_FILES_BATCH_SIZE` keys, instead of
        one query per file.

        Check the archive reference-counter implemented in:
        `SourcePackagePublishingHistory` or
        `BinaryPackagePublishingHistory`.

        :return: the subset of `file_keys` that can be removed.
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

        clauses.append(LibraryFileAlias.content == LibraryFileContent.id)

        right_now = datetime.now(timezone.utc)
        file_keys = list(file_keys)
        removable = set()
        for start in range(0, len(file_keys), REMOVABLE_FILES_BATCH_SIZE):
            batch_start = time.monotonic()
            batch = file_keys[start : start + REMOVABLE_FILES_BATCH_SIZE]
            # Match exact (filename, sha256) pairs rather than filtering
            # on each column separately, which would also match a file
            # with the same content but a different name, or vice versa.
            pair_clause = Or(
                *(
                    And(
                        LibraryFileAlias.filename == filename,
                        LibraryFileContent.sha256 == file_sha256,
                    )
                    for filename, file_sha256 in batch
                )
            )
            rows = IStore(publication_class).find(
                (
                    publication_class,
                    LibraryFileAlias.filename,
                    LibraryFileContent.sha256,
                ),
                *clauses,
                pair_clause,
            )

            # Start optimistic: a key with no matching publications at
            # all is removable, exactly as the old per-file check fell
            # through to `return True` when there were no other
            # publications to weigh.
            batch_removable = set(batch)
            for pub, filename, file_sha256 in rows:
                key = (filename, file_sha256)
                if key not in batch_removable:
                    continue
                # Deny removal if any reference is still active, wasn't
                # dominated yet, or is still in 'quarantine'.
                if (
                    pub.status not in inactive_publishing_status
                    or pub.scheduleddeletiondate is None
                    or pub.scheduleddeletiondate > right_now
                ):
                    batch_removable.discard(key)
            removable.update(batch_removable)
            self.logger.debug(
                "Timing: eligibility batch of %d files=%.3fs"
                % (len(batch), time.monotonic() - batch_start)
            )

        return removable

    def _prefetchFiles(self, condemned_source_files, condemned_binary_files):
        """Bulk-fetch the files of the given publications.

        `SourcePackagePublishingHistory.files` and
        `BinaryPackagePublishingHistory.files` each do their own query
        (or two) per publication, which does not scale to tens of
        thousands of condemned publications. Build the same file lists
        from a handful of bulk queries instead.

        This also primes the caches used by `component_name`,
        `pool_name` and `pool_version`, which are read for every
        condemned file further down, and which otherwise each walk a
        chain of references back to a `SourcePackageName`.

        :return: a dict mapping each publication to its list of files.
        """
        pub_files = {}

        if condemned_source_files:
            # Source packages in Conda repositories never have files
            # attached; see SourcePackagePublishingHistory.files.
            if self.archive.repository_format == ArchiveRepositoryFormat.CONDA:
                for pub_record in condemned_source_files:
                    pub_files[pub_record] = []
            else:
                sprs = _loadRelatedInBatches(
                    SourcePackageRelease,
                    condemned_source_files,
                    ["sourcepackagerelease_id"],
                )
                _loadRelatedInBatches(
                    SourcePackageName, sprs, ["sourcepackagename_id"]
                )
                sprfs = _loadReferencingInBatches(
                    SourcePackageReleaseFile,
                    sprs,
                    ["sourcepackagerelease_id"],
                )
                lfas = _loadRelatedInBatches(
                    LibraryFileAlias, sprfs, ["libraryfile_id"]
                )
                _loadRelatedInBatches(LibraryFileContent, lfas, ["content_id"])
                files_by_release = defaultdict(list)
                for sprf in sprfs:
                    files_by_release[sprf.sourcepackagerelease_id].append(sprf)
                for pub_record in condemned_source_files:
                    pub_files[pub_record] = files_by_release[
                        pub_record.sourcepackagerelease_id
                    ]

        if condemned_binary_files:
            bprs = _loadRelatedInBatches(
                BinaryPackageRelease,
                condemned_binary_files,
                ["binarypackagerelease_id"],
            )
            _loadRelatedInBatches(
                BinaryPackageName, bprs, ["binarypackagename_id"]
            )
            bpbs = _loadRelatedInBatches(
                BinaryPackageBuild, bprs, ["build_id"]
            )
            sprs = _loadRelatedInBatches(
                SourcePackageRelease, bpbs, ["source_package_release_id"]
            )
            _loadRelatedInBatches(
                SourcePackageName, sprs, ["sourcepackagename_id"]
            )
            bpfs = _loadReferencingInBatches(
                BinaryPackageFile, bprs, ["binarypackagerelease_id"]
            )
            lfas = _loadRelatedInBatches(
                LibraryFileAlias, bpfs, ["libraryfile_id"]
            )
            _loadRelatedInBatches(LibraryFileContent, lfas, ["content_id"])
            files_by_release = defaultdict(list)
            for bpf in bpfs:
                files_by_release[bpf.binarypackagerelease_id].append(bpf)
            for pub_record in condemned_binary_files:
                pub_files[pub_record] = files_by_release[
                    pub_record.binarypackagerelease_id
                ]

        return pub_files

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

        # Fetch the files for every condemned publication in bulk, and use
        # them to work out which (filename, sha256) keys are removable
        # with one batched query per publication class, rather than one
        # query per file (see `_findRemovableFiles`).
        pub_files = self._prefetchFiles(
            condemned_source_files, condemned_binary_files
        )
        file_keys = {
            SourcePackagePublishingHistory: set(),
            BinaryPackagePublishingHistory: set(),
        }
        for pub_record in condemned_source_files:
            for pub_file in pub_files[pub_record]:
                file_keys[SourcePackagePublishingHistory].add(
                    (
                        pub_file.libraryfile.filename,
                        pub_file.libraryfile.content.sha256,
                    )
                )
        for pub_record in condemned_binary_files:
            for pub_file in pub_files[pub_record]:
                file_keys[BinaryPackagePublishingHistory].add(
                    (
                        pub_file.libraryfile.filename,
                        pub_file.libraryfile.content.sha256,
                    )
                )

        removable = {
            publication_class: self._findRemovableFiles(
                publication_class, keys
            )
            for publication_class, keys in file_keys.items()
            if keys
        }

        def checkPubRecord(pub_record, publication_class):
            """Check if the publishing record can be removed.

            It can only be removed if all files in its context are not
            referred to any other 'published' publishing records.

            See `_findRemovableFiles` for more information.
            """
            files = pub_files[pub_record]
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
                if (filename, file_sha256) not in removable.get(
                    publication_class, ()
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
        # Bulk-update per publication class instead of one UPDATE per
        # record, which is the dominant cost when reaping tens of
        # thousands of publications.
        ids_by_class = {}
        for record in condemned_records:
            ids_by_class.setdefault(record.__class__, []).append(record.id)
        for publication_class, ids in ids_by_class.items():
            for batch in _batches(ids, MARK_REMOVED_BATCH_SIZE):
                IStore(publication_class).find(
                    publication_class, publication_class.id.is_in(batch)
                ).set(dateremoved=UTC_NOW)
