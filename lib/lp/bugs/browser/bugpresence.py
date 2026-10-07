# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Bug presence views."""

__all__ = [
    "BugPresenceURL",
]

from zope.interface import implementer

from lp.services.webapp.interfaces import ICanonicalUrlData


@implementer(ICanonicalUrlData)
class BugPresenceURL:
    """Canonical URL rules for a bug presence."""

    rootsite = "bugs"

    def __init__(self, context):
        self.context = context

    @property
    def inside(self):
        return self.context.bug

    @property
    def path(self):
        return "+presence/%d" % self.context.id
