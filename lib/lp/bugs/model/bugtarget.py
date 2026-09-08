# Copyright 2010-2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Components related to IBugTarget."""

__all__ = [
    "BugTargetBase",
    "HasBugsBase",
    "OfficialBugTag",
    "OfficialBugTagTargetMixin",
]


from storm.locals import Int, Reference, Unicode
from zope.component import getUtility
from zope.interface import implementer

from lp.bugs.interfaces.bug import IBugSet
from lp.bugs.interfaces.bugtarget import BugTaskSearchCounts, IOfficialBugTag
from lp.bugs.interfaces.bugtask import (
    DB_INCOMPLETE_BUGTASK_STATUSES,
    UNRESOLVED_BUGTASK_STATUSES,
    BugTaskGroupedCounts,
    BugTaskImportance,
    BugTaskStatusSearch,
)
from lp.bugs.interfaces.bugtaskfilter import simple_weight_calculator
from lp.bugs.interfaces.bugtasksearch import (
    BugTagsSearchCombinator,
    BugTaskSearchParams,
)
from lp.bugs.model.bugtask import BugTaskSet
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.product import IProduct
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase


class HasBugsBase:
    """Standard functionality for IHasBugs.

    All `IHasBugs` implementations should inherit from this class
    or from `BugTargetBase`.
    """

    def _makeSearchParams(self, search_params, user, search_arguments):
        """Build search parameters shared by search operations."""
        if search_arguments.get("status") is None:
            search_arguments["status"] = list(UNRESOLVED_BUGTASK_STATUSES)
        if (
            "order_by" in search_arguments
            and search_arguments["order_by"] is None
        ):
            search_arguments["order_by"] = ["-importance"]
        if search_params is None:
            search_params = BugTaskSearchParams.fromSearchForm(
                user, **search_arguments
            )
        self._customizeSearchParams(search_params)
        return search_params

    def searchTasks(
        self,
        search_params,
        user=None,
        order_by=None,
        search_text=None,
        status=None,
        importance=None,
        assignee=None,
        bug_reporter=None,
        bug_supervisor=None,
        bug_commenter=None,
        bug_subscriber=None,
        owner=None,
        structural_subscriber=None,
        affected_user=None,
        affects_me=False,
        has_patch=None,
        has_cve=None,
        distribution=None,
        tags=None,
        tags_combinator=BugTagsSearchCombinator.ALL,
        omit_duplicates=True,
        omit_targeted=None,
        status_upstream=None,
        milestone=None,
        component=None,
        nominated_for=None,
        sourcepackagename=None,
        has_no_package=None,
        linked_branches=None,
        linked_blueprints=None,
        modified_since=None,
        modified_before=None,
        created_since=None,
        created_before=None,
        information_type=None,
    ):
        """See `IHasBugs`."""
        search_arguments = dict(locals())
        del search_arguments["self"]
        del search_arguments["user"]
        del search_arguments["search_params"]
        search_params = self._makeSearchParams(
            search_params, user, search_arguments
        )
        return BugTaskSet().search(search_params)

    def searchTasksCounts(
        self,
        search_params,
        user=None,
        search_text=None,
        status=None,
        importance=None,
        assignee=None,
        bug_reporter=None,
        bug_supervisor=None,
        bug_commenter=None,
        bug_subscriber=None,
        owner=None,
        affected_user=None,
        affects_me=False,
        has_patch=None,
        has_cve=None,
        distribution=None,
        tags=None,
        tags_combinator=BugTagsSearchCombinator.ALL,
        omit_duplicates=True,
        omit_targeted=None,
        status_upstream=None,
        milestone=None,
        component=None,
        nominated_for=None,
        sourcepackagename=None,
        has_no_package=None,
        linked_branches=None,
        linked_blueprints=None,
        structural_subscriber=None,
        modified_since=None,
        modified_before=None,
        created_since=None,
        created_before=None,
        information_type=None,
    ) -> BugTaskSearchCounts:
        """See `IHasBugs`."""
        search_arguments = dict(locals())
        del search_arguments["self"]
        del search_arguments["user"]
        del search_arguments["search_params"]
        search_params = self._makeSearchParams(
            search_params, user, search_arguments
        )
        counts = BugTaskSet().searchCounts(search_params)
        return self._formatSearchCounts(counts)

    @staticmethod
    def _formatSearchCounts(
        counts: BugTaskGroupedCounts,
    ) -> BugTaskSearchCounts:
        """Convert grouped database counts to the public response shape."""
        by_status = {
            status.name: 0
            for status in BugTaskStatusSearch.items
            if status not in DB_INCOMPLETE_BUGTASK_STATUSES
        }
        by_importance = {
            importance.name: 0 for importance in BugTaskImportance.items
        }
        total = 0
        for (status, importance), count in counts.items():
            by_status[status.name] += count
            by_importance[importance.name] += count
            total += count

        return {
            "total": total,
            "by_status": by_status,
            "by_importance": by_importance,
        }

    def _customizeSearchParams(self, search_params):
        """Customize `search_params` for a specific target."""
        raise NotImplementedError(self._customizeSearchParams)

    def getBugSummaryContextWhereClause(self):
        """Return a storm clause to filter bugsummaries on this context.

        :return: Either a storm clause to filter bugsummaries, or False if
            there cannot be any matching bug summaries.
        """
        raise NotImplementedError(self.getBugSummaryContextWhereClause)

    def getBugTaskWeightFunction(self):
        """Default weight function is the simple one."""
        return simple_weight_calculator


class BugTargetBase(HasBugsBase):
    """Standard functionality for IBugTargets.

    All IBugTargets should inherit from this class.
    """

    # The default implementation of the property, used for
    # IDistribution, IDistroSeries, IProjectGroup.
    enable_bugfiling_duplicate_search = True

    def getUsedBugTagsWithOpenCounts(
        self, user, tag_limit=0, include_tags=None
    ):
        """See IBugTarget."""
        from lp.bugs.model.bug import get_bug_tags_open_count

        return get_bug_tags_open_count(
            self.getBugSummaryContextWhereClause(),
            user,
            tag_limit=tag_limit,
            include_tags=include_tags,
        )

    def createBug(self, params):
        """See IBugTarget."""
        # createBug will raise IllegalTarget for ISeriesBugTargets and
        # IProjectGroup.
        params.target = self
        return getUtility(IBugSet).createBug(params)


class OfficialBugTagTargetMixin:
    """See `IOfficialBugTagTarget`.

    This class is intended to be used as a mixin for the classes
    Distribution, Product and ProjectGroup, which can define official
    bug tags.

    Using this call in ProjectGroup requires a fix of bug 341203, see
    below, class OfficialBugTag.

    See also `Bug.official_bug_tags` which calculates this efficiently for
    a single bug.
    """

    def _getOfficialTagClause(self):
        if IDistribution.providedBy(self):
            return OfficialBugTag.distribution == self
        elif IProduct.providedBy(self):
            return OfficialBugTag.product == self
        else:
            raise AssertionError(
                "%s is not a valid official bug target" % self
            )

    def _getOfficialTags(self):
        """Get the official bug tags as a sorted list of strings."""
        target_clause = self._getOfficialTagClause()
        return list(
            IStore(OfficialBugTag)
            .find(OfficialBugTag.tag, target_clause)
            .order_by(OfficialBugTag.tag)
        )

    def _setOfficialTags(self, tags):
        """Set the official bug tags from a list of strings."""
        new_tags = {tag.lower() for tag in tags}
        old_tags = set(self.official_bug_tags)
        added_tags = new_tags.difference(old_tags)
        removed_tags = old_tags.difference(new_tags)
        for removed_tag in removed_tags:
            self.removeOfficialBugTag(removed_tag)
        for added_tag in added_tags:
            self.addOfficialBugTag(added_tag)

    official_bug_tags = property(_getOfficialTags, _setOfficialTags)

    def _getTag(self, tag):
        """Return the OfficialBugTag record for the given tag, if it exists.

        If the tag is not defined for this target, None is returned.
        """
        target_clause = self._getOfficialTagClause()
        return (
            IStore(OfficialBugTag)
            .find(OfficialBugTag, OfficialBugTag.tag == tag, target_clause)
            .one()
        )

    def addOfficialBugTag(self, tag):
        """See `IOfficialBugTagTarget`."""
        # Tags must be unique per target; adding an existing tag
        # for a second time would lead to an exception.
        if self._getTag(tag) is None:
            new_tag = OfficialBugTag()
            new_tag.tag = tag
            new_tag.target = self
            IStore(OfficialBugTag).add(new_tag)

    def removeOfficialBugTag(self, tag):
        """See `IOfficialBugTagTarget`."""
        tag = self._getTag(tag)
        if tag is not None:
            IStore(OfficialBugTag).remove(tag)


@implementer(IOfficialBugTag)
class OfficialBugTag(StormBase):
    """See `IOfficialBugTag`."""

    # XXX Abel Deuring, 2009-03-11: The SQL table OfficialBugTag has
    # a column "project", while a constraint requires that either "product"
    # or "distribution" must be non-null. Once this is changed, we
    # should add the column "project" here. Bug #341203.

    __storm_table__ = "OfficialBugTag"

    id = Int(primary=True)

    tag = Unicode(allow_none=False)
    distribution_id = Int(name="distribution")
    distribution = Reference(distribution_id, "Distribution.id")

    product_id = Int(name="product")
    product = Reference(product_id, "Product.id")

    @property
    def target(self):
        """See `IOfficialBugTag`."""
        # A database constraint ensures that either distribution or
        # product is not None.
        if self.distribution is not None:
            return self.distribution
        else:
            return self.product

    @target.setter
    def target(self, target):
        """See `IOfficialBugTag`."""
        if IDistribution.providedBy(target):
            self.distribution = target
        elif IProduct.providedBy(target):
            self.product = target
        else:
            raise ValueError(
                "The target of an OfficialBugTag must be either an "
                "IDistribution instance or an IProduct instance."
            )
