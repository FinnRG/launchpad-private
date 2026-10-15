# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Internal interfaces for polling external revision checks."""

__all__ = [
    "IRevisionStatusWatch",
    "IRevisionStatusWatchSet",
]

from zope.interface import Attribute, Interface

from lp.services.database.constants import DEFAULT


class IRevisionStatusWatch(Interface):
    """A reminder to collect an external check's revision status."""

    id = Attribute("The watch ID.")
    report = Attribute("The revision status report updated by this watch.")
    remote_system = Attribute("The name of the external system to poll.")
    remote_ref = Attribute("JSON details for the external system's handler.")
    next_check = Attribute("When to poll next, or None to pause polling.")
    lastchecked = Attribute("When last polled, or None if never polled.")
    failure_count = Attribute("Consecutive failures while polling.")
    date_created = Attribute("When this watch was created.")

    def destroySelf():
        """Remove the watch, keeping its revision status report."""


class IRevisionStatusWatchSet(Interface):
    """Create and find internal revision status watches."""

    def new(report, remote_system, remote_ref, next_check=DEFAULT):
        """Create a report's only watch; raise ValueError if it has one.

        A new watch is due now by default; None pauses polling.
        """

    def getByID(watch_id):
        """Return a watch by ID, or None if it does not exist."""

    def findByReport(report):
        """Return a report's watch, or None if it does not have one."""

    def findDue(now=None, remote_system=None, limit=100):
        """Return at most limit watches due at or before now.

        Use the current database time when now is None. Exclude paused
        watches, optionally filter by remote system, and order by next_check
        then ID. limit must be a positive integer. This does not claim or
        lock watches for a worker.
        """
