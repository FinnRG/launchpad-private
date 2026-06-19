# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Database class for table ArchivePublishingHistory."""

__all__ = [
    "ArchivePublishingHistory",
    "ArchivePublishingHistorySet",
]

from storm.locals import Int, Reference
from zope.interface import implementer

from lp.archivepublisher.interfaces.archivepublishinghistory import (
    IArchivePublishingHistory,
    IArchivePublishingHistorySet,
)
from lp.archivepublisher.model.archivepublisherrun import ArchivePublisherRun
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.soyuz.model.archive import Archive


@implementer(IArchivePublishingHistory)
class ArchivePublishingHistory(StormBase):
    """See `IArchivePublishingHistory`."""

    __storm_table__ = "ArchivePublishingHistory"

    id = Int(primary=True)

    archive_id = Int(name="archive", allow_none=False)
    archive = Reference(archive_id, "Archive.id")

    publisher_run_id = Int(name="publisher_run", allow_none=False)
    publisher_run = Reference(publisher_run_id, "ArchivePublisherRun.id")

    def __init__(self, archive, publisher_run):
        """Construct a `ArchivePublishingHistory`."""
        super().__init__()
        self.archive = archive
        self.publisher_run = publisher_run


@implementer(IArchivePublishingHistorySet)
class ArchivePublishingHistorySet:
    """See `IArchivePublishingHistorySet`."""

    title = "Archive Publishing History Records"

    def new(self, archive, publisher_run):
        """See `IArchivePublishingHistorySet`."""
        store = IStore(ArchivePublishingHistory)
        publishing_history = ArchivePublishingHistory(
            archive=archive, publisher_run=publisher_run
        )
        store.add(publishing_history)
        return publishing_history

    def getById(self, id):
        """See `IArchivePublishingHistorySet`."""
        return IStore(ArchivePublishingHistory).get(
            ArchivePublishingHistory, id
        )

    def getByArchive(self, archive):
        """See `IArchivePublishingHistorySet`."""
        return IStore(ArchivePublishingHistory).find(
            ArchivePublishingHistory,
            ArchivePublishingHistory.archive == archive,
        )

    def getByArchivePublisherRun(self, publisher_run):
        """See `IArchivePublishingHistorySet`."""
        return IStore(ArchivePublishingHistory).find(
            ArchivePublishingHistory,
            ArchivePublishingHistory.publisher_run == publisher_run,
        )

    def getArchivesPublishedSince(
        self, since, status=None, publishing_method=None
    ):
        """See `IArchivePublishingHistorySet`."""
        store = IStore(ArchivePublishingHistory)
        conditions = [
            Archive.id == ArchivePublishingHistory.archive_id,
            ArchivePublishingHistory.publisher_run_id
            == ArchivePublisherRun.id,
            ArchivePublisherRun.date_started >= since,
        ]
        if status is not None:
            conditions.append(ArchivePublisherRun.status == status)
        if publishing_method is not None:
            conditions.append(Archive._publishing_method == publishing_method)
        return store.find(Archive, *conditions).config(distinct=True)
