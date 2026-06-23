# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""ArchivePublishingHistory interface."""

__all__ = [
    "IArchivePublishingHistory",
    "IArchivePublishingHistorySet",
]

from lazr.restful.fields import Reference
from zope.interface import Interface
from zope.schema import Int

from lp import _
from lp.archivepublisher.interfaces.archivepublisherrun import (
    IArchivePublisherRun,
)
from lp.soyuz.interfaces.archive import IArchive


class IArchivePublishingHistory(Interface):
    """An archive that was published in a specific publisher run."""

    id = Int(title=_("ID"), required=True, readonly=True)

    archive = Reference(
        IArchive,
        title=_("Archive"),
        required=True,
        readonly=True,
        description=_("Archive id that was published."),
    )

    publisher_run = Reference(
        IArchivePublisherRun,
        title=_("Archive Publisher Run"),
        required=True,
        readonly=True,
        description=_(
            "Archive publisher run during which the archive was published."
        ),
    )


class IArchivePublishingHistorySet(Interface):
    """A set of archive publishing history records."""

    def new(archive, publisher_run):
        """Create a new `IArchivePublishingHistory`.

        :param archive: The `IArchive`.
        :param publisher_run: The `IArchivePublisherRun` during which the
            archive was published.
        :return: A new `IArchivePublishingHistory`.
        """

    def getById(id):
        """Get a `IArchivePublishingHistory` by its ID.

        :param id: The ID of the publishing history record.
        :return: An `IArchivePublishingHistory` or None if not found.
        """

    def getByArchive(archive):
        """Get all `IArchivePublishingHistory` records for an archive.
        :param archive: An `IArchive`.
        :return: A result set of `IArchivePublishingHistory` objects.
        """

    def getByArchivePublisherRun(publisher_run):
        """Get all `IArchivePublishingHistory` records for a publisher run.
        :param publisher_run: An `IArchivePublisherRun`.
        :return: A result set of `IArchivePublishingHistory` objects.
        """

    def getArchivesPublishedSince(since, status=None, publishing_method=None):
        """Get distinct archives published since the given datetime.

        Returns archives that were included in a publisher run whose
        `date_started` is greater than or equal to `since`.

        :param since: A timezone-aware `datetime` cutoff.
        :param status: An optional `ArchivePublisherRunStatus` to filter by.
            When `None` (the default), archives from runs of any status are
            returned.
        :param publishing_method: An optional `ArchivePublishingMethod` to
            filter by. When `None` (the default), archives with any publishing
            method are returned.
        :return: A result set of distinct `IArchive` objects.
        """
