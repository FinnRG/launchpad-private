# Copyright 2009-2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "BugPresence",
    "BugPresenceSet",
]

from storm.databases.postgres import JSON
from storm.locals import Int
from storm.references import Reference
from storm.store import Store
from zope.interface import implementer

from lp.bugs.interfaces.bugpresence import IBugPresence, IBugPresenceSet
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase


@implementer(IBugPresence)
class BugPresence(StormBase):
    """
    Points in the code history of various entities like a product, a
    distribution, or a distribution source package when something was broken
    and/or when it was fixed.
    """

    __storm_table__ = "BugPresence"

    id = Int(primary=True)

    bug_id = Int(name="bug", allow_none=False)
    bug = Reference(bug_id, "Bug.id")

    product_id = Int(name="product", allow_none=True)
    product = Reference(product_id, "Product.id")

    distribution_id = Int(name="distribution", allow_none=True)
    distribution = Reference(distribution_id, "Distribution.id")

    source_package_name_id = Int(name="source_package_name", allow_none=True)
    source_package_name = Reference(
        source_package_name_id, "SourcePackageName.id"
    )

    git_repository_id = Int(name="git_repository", allow_none=True)
    git_repository = Reference(git_repository_id, "GitRepository.id")

    _break_fix_data = JSON(name="break_fix_data", allow_none=False)

    def __init__(
        self,
        bug,
        product,
        distribution,
        source_package_name,
        git_repository,
        break_fix_data,
    ):
        super().__init__()
        self.bug = bug
        self.product = product
        self.distribution = distribution
        self.source_package_name = source_package_name
        self.git_repository = git_repository
        self._break_fix_data = break_fix_data

    @property
    def break_fix_data(self):
        """See `IBugPresence`."""
        return self._break_fix_data or []

    @break_fix_data.setter
    def break_fix_data(self, value):
        """See `IBugPresence`."""
        assert value is None or isinstance(value, list)
        self._break_fix_data = value

    def destroySelf(self):
        """See `IBugPresence`."""
        Store.of(self).remove(self)


@implementer(IBugPresenceSet)
class BugPresenceSet:
    """The set of `IBugPresence` objects."""

    def __getitem__(self, id):
        """See IBugPresenceSet."""
        return IStore(BugPresence).find(BugPresence, id=int(id)).one()

    def create(
        self,
        bug,
        product,
        distribution,
        source_package_name,
        git_repository,
        break_fix_data,
    ):
        """See IBugPresenceSet."""
        bug_presence = BugPresence(
            bug=bug,
            product=product,
            distribution=distribution,
            source_package_name=source_package_name,
            git_repository=git_repository,
            break_fix_data=break_fix_data,
        )

        IStore(BugPresence).add(bug_presence)
        return bug_presence
