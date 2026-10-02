# Copyright 2009-2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""BugPresence interfaces"""

__all__ = [
    "IBugPresence",
    "IBugPresenceSet",
]
from lazr.restful.declarations import exported, exported_as_webservice_entry
from lazr.restful.fields import Reference
from zope.interface import Interface
from zope.schema import Dict, Int, List, Text, TextLine

from lp import _
from lp.services.fields import BugField


@exported_as_webservice_entry(as_of="devel")
class IBugPresence(Interface):
    """A single `BugPresence` database entry."""

    id = Int(title=_("ID"), required=True, readonly=True)
    bug = exported(BugField(title=_("Bug"), readonly=True))
    # Really IProduct, patched in lp.bugs.interfaces.webservice.
    product = exported(
        Reference(
            schema=Interface,
            title=_("Product"),
            required=False,
            readonly=True,
        ),
        as_of="devel",
    )
    # Really IDistribution, patched in lp.bugs.interfaces.webservice.
    distribution = exported(
        Reference(
            schema=Interface,
            title=_("Distribution"),
            required=False,
            readonly=True,
        ),
        as_of="devel",
    )
    source_package_name = Int(
        title=_("Source Package Name"), readonly=True, required=False
    )
    api_source_package_name = exported(
        TextLine(
            title=_("Source package name"), required=False, readonly=True
        ),
        as_of="devel",
        exported_as="source_package_name",
    )
    # Really IGitRepository, patched in lp.bugs.interfaces.webservice.
    git_repository = exported(
        Reference(
            schema=Interface,
            title=_("Git repository"),
            required=False,
            readonly=True,
        ),
        as_of="devel",
    )
    break_fix_data = exported(
        List(
            title=_("Break-Fix data"),
            description=_(
                "A list of mappings, each with a 'break' and a 'fix' commit "
                "identifying where the vulnerability was introduced and where "
                "it was fixed."
            ),
            value_type=Dict(key_type=Text(), value_type=Text()),
            required=False,
            readonly=True,
        ),
        as_of="devel",
    )

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
