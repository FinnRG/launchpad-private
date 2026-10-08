# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Native events for Soyuz publications."""

__all__ = ["BinaryPackagePublishedEvent"]

from zope.interface import implementer
from zope.interface.interfaces import ObjectEvent

from lp.soyuz.interfaces.event import IBinaryPackagePublishedEvent


@implementer(IBinaryPackagePublishedEvent)
class BinaryPackagePublishedEvent(ObjectEvent):
    """See `IBinaryPackagePublishedEvent`."""
