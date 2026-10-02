# Copyright 2009-2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""BugPresence interfaces"""

__all__ = [
    "IBugPresence",
    "IBugPresenceSet",
]
from lazr.restful.declarations import exported
from zope.interface import Interface
from zope.schema import Dict, Int

from lp import _
from lp.services.fields import BugField


class IBugPresence(Interface):
    """A single `BugPresence` database entry."""

    id = Int(title=_("ID"), required=True, readonly=True)
    bug = exported(BugField(title=_("Bug"), readonly=True))
    product = exported(Int(title=_("Product"), readonly=True))
    distribution = exported(Int(title=_("Distribution"), readonly=True))
    source_package_name = exported(
        Int(title=_("Source Package Name"), readonly=True)
    )
    git_repository = exported(Int(title=_("Git Repository"), readonly=True))
    break_fix_data = exported(Dict(title=_("Break-Fix"), readonly=True))

    def destroySelf(self):
        """Destroy this `IBugPresence` object."""


class IBugPresenceSet(Interface):
    """The set of `IBugPresence` objects."""

    def __getitem__(id):
        """Get a `IBugPresence` by id."""

    def create(
        bug,
        product,
        distribution,
        source_package_name,
        git_repository,
        break_fix_data,
    ):
        """Create a new `IBugPresence`."""
