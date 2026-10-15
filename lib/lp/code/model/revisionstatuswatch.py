# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Persistent reminders to poll external revision checks."""

__all__ = [
    "RevisionStatusWatch",
    "RevisionStatusWatchSet",
]

from datetime import timezone

from storm.databases.postgres import JSON
from storm.locals import DateTime, Int, Reference, Unicode
from storm.store import Store
from zope.interface import implementer

from lp.code.interfaces.revisionstatuswatch import (
    IRevisionStatusWatch,
    IRevisionStatusWatchSet,
)
from lp.services.database.constants import DEFAULT, UTC_NOW
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase


@implementer(IRevisionStatusWatch)
class RevisionStatusWatch(StormBase):
    """See `IRevisionStatusWatch`."""

    __storm_table__ = "RevisionStatusWatch"

    id = Int(primary=True)
    report_id = Int(name="report", allow_none=False)
    report = Reference(report_id, "RevisionStatusReport.id")
    remote_system = Unicode(allow_none=False)
    remote_ref = JSON("remote_ref", allow_none=False)
    next_check = DateTime(tzinfo=timezone.utc)
    lastchecked = DateTime(tzinfo=timezone.utc)
    failure_count = Int(allow_none=False)
    date_created = DateTime(tzinfo=timezone.utc, allow_none=False)

    def __init__(self, report, remote_system, remote_ref, next_check=DEFAULT):
        super().__init__()
        self.report = report
        self.remote_system = remote_system
        self.remote_ref = remote_ref
        self.next_check = UTC_NOW if next_check is DEFAULT else next_check
        self.lastchecked = None
        self.failure_count = 0
        self.date_created = UTC_NOW

    def destroySelf(self):
        """See `IRevisionStatusWatch`."""
        Store.of(self).remove(self)


@implementer(IRevisionStatusWatchSet)
class RevisionStatusWatchSet:
    """See `IRevisionStatusWatchSet`."""

    def new(self, report, remote_system, remote_ref, next_check=DEFAULT):
        store = IStore(RevisionStatusWatch)
        store.flush()
        store.execute(
            "SELECT id FROM RevisionStatusReport WHERE id = %s FOR UPDATE",
            (report.id,),
        )
        if self.findByReport(report) is not None:
            raise ValueError("A revision status report already has a watch")
        watch = RevisionStatusWatch(
            report, remote_system, remote_ref, next_check=next_check
        )
        store.add(watch)
        return watch

    def getByID(self, watch_id):
        return (
            IStore(RevisionStatusWatch)
            .find(RevisionStatusWatch, id=watch_id)
            .one()
        )

    def findByReport(self, report):
        return (
            IStore(RevisionStatusWatch)
            .find(RevisionStatusWatch, report=report)
            .one()
        )

    def findDue(self, now=None, remote_system=None, limit=100):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if now is None:
            now = UTC_NOW
        clauses = [RevisionStatusWatch.next_check <= now]
        if remote_system is not None:
            clauses.append(RevisionStatusWatch.remote_system == remote_system)
        return (
            IStore(RevisionStatusWatch)
            .find(RevisionStatusWatch, *clauses)
            .order_by(RevisionStatusWatch.next_check, RevisionStatusWatch.id)
            .config(limit=limit)
        )
