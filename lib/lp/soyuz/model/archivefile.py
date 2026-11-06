# Copyright 2016-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""A file in an archive."""

__all__ = [
    "ArchiveFile",
    "ArchiveFileSet",
]

import os.path
import re
from datetime import timezone

from storm.locals import (
    SQL,
    And,
    DateTime,
    Desc,
    Int,
    Or,
    Reference,
    Select,
    Unicode,
)
from zope.component import getUtility
from zope.interface import implementer

from lp.app.errors import IncompatibleArguments
from lp.services.database.bulk import load_related
from lp.services.database.constants import UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.stormbase import StormBase
from lp.services.database.stormexpr import ColumnSelect, RegexpMatch, Values
from lp.services.librarian.interfaces import ILibraryFileAliasSet
from lp.services.librarian.model import LibraryFileAlias, LibraryFileContent
from lp.soyuz.interfaces.archivefile import IArchiveFile, IArchiveFileSet

# Must stay identical to the predicate of
# archivefile__archive__container__date_created__index_files__idx.
PACKAGE_INDEX_PATH_PATTERN = r"/(Packages|Sources)(\.(gz|bz2|xz))?$"


def _now():
    """Get the current transaction timestamp.

    Tests can override this with a Storm expression or a `datetime` to
    simulate time changes.
    """
    return UTC_NOW


@implementer(IArchiveFile)
class ArchiveFile(StormBase):
    """See `IArchiveFile`."""

    __storm_table__ = "ArchiveFile"

    id = Int(primary=True)

    archive_id = Int(name="archive", allow_none=False)
    archive = Reference(archive_id, "Archive.id")

    container = Unicode(name="container", allow_none=False)

    path = Unicode(name="path", allow_none=False)

    library_file_id = Int(name="library_file", allow_none=False)
    library_file = Reference(library_file_id, "LibraryFileAlias.id")

    date_created = DateTime(
        name="date_created",
        tzinfo=timezone.utc,
        # XXX cjwatson 2018-04-17: Should be allow_none=False, but we need
        # to backfill existing rows first.
        allow_none=True,
    )

    date_superseded = DateTime(
        name="date_superseded", tzinfo=timezone.utc, allow_none=True
    )

    scheduled_deletion_date = DateTime(
        name="scheduled_deletion_date", tzinfo=timezone.utc, allow_none=True
    )

    date_removed = DateTime(
        name="date_removed", tzinfo=timezone.utc, allow_none=True
    )

    def __init__(self, archive, container, path, library_file):
        """Construct an `ArchiveFile`."""
        super().__init__()
        self.archive = archive
        self.container = container
        self.path = path
        self.library_file = library_file
        self.date_created = _now()
        self.date_superseded = None
        self.scheduled_deletion_date = None
        self.date_removed = None


@implementer(IArchiveFileSet)
class ArchiveFileSet:
    """See `IArchiveFileSet`."""

    @staticmethod
    def getIndexDates(archive, live_at, suites):
        """See `IArchiveFileSet`."""
        containers = Values(
            "suite_containers",
            [("container", "text")],
            [("release:" + suite,) for suite in suites],
        )
        if not containers.values:
            return {}
        container = SQL("suite_containers.container", tables=[containers])
        latest = Select(
            ArchiveFile.date_created,
            tables=ArchiveFile,
            where=And(
                ArchiveFile.archive == archive,
                ArchiveFile.container == SQL("suite_containers.container"),
                ArchiveFile.date_created <= live_at,
                RegexpMatch(ArchiveFile.path, PACKAGE_INDEX_PATH_PATTERN),
            ),
            order_by=Desc(ArchiveFile.date_created),
            limit=1,
        )
        rows = IStore(ArchiveFile).find((container, ColumnSelect(latest)))
        return {
            container[len("release:") :]: date.replace(tzinfo=timezone.utc)
            for container, date in rows
            if date is not None
        }

    @staticmethod
    def new(archive, container, path, library_file):
        """See `IArchiveFileSet`."""
        archive_file = ArchiveFile(archive, container, path, library_file)
        IPrimaryStore(ArchiveFile).add(archive_file)
        return archive_file

    @classmethod
    def newFromFile(
        cls, archive, container, path, fileobj, size, content_type
    ):
        library_file = getUtility(ILibraryFileAliasSet).create(
            os.path.basename(path),
            size,
            fileobj,
            content_type,
            restricted=archive.private,
            allow_zero_length=True,
        )
        return cls.new(archive, container, path, library_file)

    @staticmethod
    def getByArchive(
        archive,
        container=None,
        path=None,
        path_parent=None,
        sha256=None,
        live_at=None,
        existed_at=None,
        only_published=False,
        eager_load=False,
    ):
        """See `IArchiveFileSet`."""
        clauses = [ArchiveFile.archive == archive]
        # XXX cjwatson 2016-03-15: We'll need some more sophisticated way to
        # match containers once we're using them for custom uploads.
        if container is not None:
            clauses.append(ArchiveFile.container == container)
        if path is not None:
            clauses.append(ArchiveFile.path == path)
        if path_parent is not None:
            clauses.append(
                RegexpMatch(
                    ArchiveFile.path, "^%s/[^/]+$" % re.escape(path_parent)
                )
            )
        if sha256 is not None:
            clauses.extend(
                [
                    ArchiveFile.library_file == LibraryFileAlias.id,
                    LibraryFileAlias.content_id == LibraryFileContent.id,
                    LibraryFileContent.sha256 == sha256,
                ]
            )

        if live_at is not None and existed_at is not None:
            raise IncompatibleArguments(
                "You cannot specify both 'live_at' and 'existed_at'."
            )
        if live_at is not None:
            clauses.extend(
                [
                    Or(
                        # Rows predating the introduction of date_created
                        # will have it set to null.
                        ArchiveFile.date_created == None,
                        ArchiveFile.date_created <= live_at,
                    ),
                    Or(
                        ArchiveFile.date_superseded == None,
                        ArchiveFile.date_superseded > live_at,
                    ),
                ]
            )
        elif existed_at is not None:
            clauses.extend(
                [
                    Or(
                        # Rows predating the introduction of date_created
                        # will have it set to null.
                        ArchiveFile.date_created == None,
                        ArchiveFile.date_created <= existed_at,
                    ),
                    Or(
                        ArchiveFile.date_removed == None,
                        ArchiveFile.date_removed > existed_at,
                    ),
                ]
            )

        if only_published:
            clauses.append(ArchiveFile.date_removed == None)
        archive_files = IStore(ArchiveFile).find(ArchiveFile, *clauses)

        def eager_load(rows):
            lfas = load_related(LibraryFileAlias, rows, ["library_file_id"])
            load_related(LibraryFileContent, lfas, ["content_id"])

        if eager_load:
            return DecoratedResultSet(archive_files, pre_iter_hook=eager_load)
        else:
            return archive_files

    @staticmethod
    def scheduleDeletion(archive_files, stay_of_execution):
        """See `IArchiveFileSet`."""
        rows = IPrimaryStore(ArchiveFile).find(
            ArchiveFile,
            ArchiveFile.id.is_in(
                {archive_file.id for archive_file in archive_files}
            ),
        )
        rows.set(
            date_superseded=_now(),
            scheduled_deletion_date=_now() + stay_of_execution,
        )

    @staticmethod
    def getContainersToReap(archive, container_prefix=None):
        clauses = [
            ArchiveFile.archive == archive,
            ArchiveFile.scheduled_deletion_date < _now(),
            ArchiveFile.date_removed == None,
        ]
        if container_prefix is not None:
            clauses.append(ArchiveFile.container.startswith(container_prefix))
        return (
            IStore(ArchiveFile)
            .find(ArchiveFile.container, *clauses)
            .group_by(ArchiveFile.container)
        )

    @staticmethod
    def markDeleted(archive_files):
        """See `IArchiveFileSet`."""
        rows = IPrimaryStore(ArchiveFile).find(
            ArchiveFile,
            ArchiveFile.id.is_in(
                {archive_file.id for archive_file in archive_files}
            ),
            ArchiveFile.date_removed == None,
        )
        rows.set(date_removed=_now())
