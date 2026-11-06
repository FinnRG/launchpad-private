# Copyright 2016-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interface for a file in an archive."""

__all__ = [
    "IArchiveFile",
    "IArchiveFileSet",
]

from lazr.restful.fields import Reference
from zope.interface import Interface
from zope.schema import Datetime, Int, TextLine

from lp import _
from lp.services.librarian.interfaces import ILibraryFileAlias
from lp.soyuz.interfaces.archive import IArchive


class IArchiveFile(Interface):
    """A file in an archive.

    This covers files that are not published in the archive's package pool,
    such as the Packages and Sources index files.
    """

    id = Int(title=_("ID"), required=True, readonly=True)

    archive = Reference(
        title=_("The archive containing the index file."),
        schema=IArchive,
        required=True,
        readonly=True,
    )

    container = TextLine(
        title=_("An identifier for the component that manages this file."),
        required=True,
        readonly=True,
    )

    path = TextLine(
        title=_("The path to the index file within the published archive."),
        required=True,
        readonly=True,
    )

    library_file = Reference(
        title=_("The index file in the librarian."),
        schema=ILibraryFileAlias,
        required=True,
        readonly=True,
    )

    date_created = Datetime(
        title=_("The date when this file was created."),
        # XXX cjwatson 2018-04-17: Should be required=True, but we need to
        # backfill existing rows first.
        required=False,
        readonly=False,
    )

    date_superseded = Datetime(
        title=_("The date when this file was scheduled for future deletion."),
        required=False,
        readonly=False,
    )

    scheduled_deletion_date = Datetime(
        title=_("The date when this file should stop being published."),
        required=False,
        readonly=False,
    )

    date_removed = Datetime(
        title=_(
            "The date when this file was entirely removed from the published "
            "archive."
        ),
        required=False,
        readonly=False,
    )


class IArchiveFileSet(Interface):
    """Bulk operations on files in an archive."""

    def getIndexDates(archive, live_at, suites):
        """Return when each suite's package indexes were last recorded.

        :param archive: The archive containing the indexes.
        :param live_at: A datetime or `UTC_NOW`; later records are ignored.
        :param suites: An iterable of suite names.
        :return: A mapping from suite names to datetimes. Suites without a
            dated record are omitted. Only `Sources` and `Packages` files
            count, since other files can change without the package indexes
            being rebuilt.
        """

    def new(archive, container, path, library_file):
        """Create a new `IArchiveFile`.

        :param archive: The `IArchive` containing the new file.
        :param container: An identifier for the component that manages this
            file.
        :param path: The path to the new file within its archive.
        :param library_file: The `ILibraryFileAlias` embodying the new file.
        """

    def newFromFile(archive, container, path, fileobj, size, content_type):
        """Create a new `IArchiveFile` from a file on the file system.

        :param archive: The `IArchive` containing the new file.
        :param container: An identifier for the component that manages this
            file.
        :param path: The path to the new file within its archive.
        :param fileobj: A file-like object to read the data from.
        :param size: The size of the file in bytes.
        :param content_type: The MIME type of the file.
        """

    def getByArchive(
        archive,
        container=None,
        path=None,
        sha256=None,
        live_at=None,
        existed_at=None,
        only_published=False,
        eager_load=False,
    ):
        """Get files in an archive.

        :param archive: Return files in this `IArchive`.
        :param container: Return only files with this container.
        :param path: Return only files with this path.
        :param path_parent: Return only files whose immediate parent
            directory is this path.
        :param sha256: If not None, return only files with this SHA-256
            checksum.
        :param live_at: If not None, return only files that held their path
            in the archive at this `datetime` (or
            `lp.services.database.constants.UTC_NOW`).
        :param existed_at: If not None, return only files that existed in
            the archive at this `datetime` (or
            `lp.services.database.constants.UTC_NOW`).  This includes files
            that did not hold their path (e.g. `dists/jammy/InRelease`) and
            that are merely still published in a `by-hash` directory; it
            should normally be used together with `sha256`.
        :param only_published: If True, return only files without a
            `date_removed` set.
        :param eager_load: If True, preload related `LibraryFileAlias` and
            `LibraryFileContent` rows.
        :raises IncompatibleArguments: if both `live_at` and `existed_at`
            are specified.
        :return: An iterable of matched files.
        """

    def scheduleDeletion(archive_files, stay_of_execution):
        """Schedule these archive files for future deletion.

        :param archive_files: The `IArchiveFile`s to schedule for deletion.
        :param stay_of_execution: A `timedelta`; schedule files for deletion
            this amount of time in the future.
        """

    def getContainersToReap(archive, container_prefix=None):
        """Return containers in this archive with files that should be reaped.

        :param archive: Return containers in this `IArchive`.
        :param container_prefix: Return only containers that start with this
            prefix.
        :return: An iterable of matched container names.
        """

    def markDeleted(archive_files):
        """Mark these archive files as deleted.

        This does not actually delete the rows from the database;
        `lp.soyuz.scripts.expire_archive_files` will expire their
        corresponding `LibraryFileAlias` rows as needed.

        :param archive_files: The `IArchiveFile`s to delete.
        """
