# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interfaces for native Soyuz events."""

__all__ = ["IBinaryPackagePublishedEvent"]

from zope.interface.interfaces import IObjectEvent


class IBinaryPackagePublishedEvent(IObjectEvent):
    """A binary publication has first acquired its publication timestamp.

    object is the destination IBinaryPackagePublishingHistory, with its
    status and datepublished updated. Delivery is synchronous within the
    publisher's transaction, not after commit or repository index creation.
    Subscribers must not assume the package is already downloadable and
    must defer external requests until after the transaction commits.
    A rolled-back publication can emit this event again when retried.
    """
