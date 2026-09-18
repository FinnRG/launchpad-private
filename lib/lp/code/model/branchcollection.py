# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Implementations of `IBranchCollection`."""

__all__ = [
    "GenericBranchCollection",
]

from collections import defaultdict
from functools import partial
from operator import attrgetter

import six
from lazr.uri import URI, InvalidURIError
from storm.expr import (
    SQL,
    And,
    Asc,
    Count,
    Desc,
    Exists,
    In,
    Join,
    LeftJoin,
    Select,
    With,
)
from storm.info import ClassAlias
from storm.store import EmptyResultSet
from zope.component import getUtility
from zope.interface import implementer

from lp.app.enums import PRIVATE_INFORMATION_TYPES
from lp.bugs.interfaces.bugtask import IBugTaskSet
from lp.bugs.interfaces.bugtaskfilter import filter_bugtasks_by_context
from lp.bugs.interfaces.bugtasksearch import BugTaskSearchParams
from lp.bugs.model.bugbranch import BugBranch
from lp.bugs.model.bugtask import BugTask
from lp.code.enums import BranchListingSort, BranchMergeProposalStatus
from lp.code.interfaces.branch import user_has_special_branch_access
from lp.code.interfaces.branchcollection import (
    IBranchCollection,
    InvalidFilter,
)
from lp.code.interfaces.branchlookup import IBranchLookup
from lp.code.interfaces.codehosting import LAUNCHPAD_SERVICES
from lp.code.interfaces.seriessourcepackagebranch import (
    IFindOfficialBranchLinks,
)
from lp.code.model.branch import Branch, get_branch_privacy_filter
from lp.code.model.branchmergeproposal import BranchMergeProposal
from lp.code.model.branchrevision import BranchRevision
from lp.code.model.branchsubscription import BranchSubscription
from lp.code.model.codeimport import CodeImport
from lp.code.model.codereviewcomment import CodeReviewComment
from lp.code.model.codereviewvote import CodeReviewVoteReference
from lp.code.model.revision import Revision
from lp.code.model.seriessourcepackagebranch import SeriesSourcePackageBranch
from lp.registry.enums import EXCLUSIVE_TEAM_POLICY
from lp.registry.model.distribution import Distribution
from lp.registry.model.distroseries import DistroSeries
from lp.registry.model.person import Person
from lp.registry.model.product import Product
from lp.registry.model.sourcepackagename import SourcePackageName
from lp.registry.model.teammembership import TeamParticipation
from lp.registry.model.uservisibility import attach_user_visibility_cte
from lp.services.database.bulk import load_referencing, load_related
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.interfaces import IStore
from lp.services.database.sqlbase import quote
from lp.services.propertycache import get_property_cache
from lp.services.searchbuilder import any


@implementer(IBranchCollection)
class GenericBranchCollection:
    """See `IBranchCollection`."""

    def __init__(
        self,
        store=None,
        branch_filter_expressions=None,
        tables=None,
        asymmetric_filter_expressions=None,
        asymmetric_tables=None,
    ):
        """Construct a `GenericBranchCollection`.

        :param store: The store to look in for branches. If not specified,
            use the default store.
        :param branch_filter_expressions: A list of Storm expressions to
            restrict the branches in the collection. If unspecified, then
            there will be no restrictions on the result set. That is, all
            branches in the store will be in the collection.
        :param tables: A dict of Storm tables to the Join expression.  If an
            expression in branch_filter_expressions refers to a table, then
            that table *must* be in this list.
        :param asymmetric_filter_expressions: As per branch_filter_expressions
            but only applies to one side of reflexive joins.
        :param asymmetric_tables: As per tables, for
            asymmetric_filter_expressions.
        """
        self._store = store
        if branch_filter_expressions is None:
            branch_filter_expressions = []
        self._branch_filter_expressions = list(branch_filter_expressions)
        if tables is None:
            tables = {}
        self._tables = tables
        if asymmetric_filter_expressions is None:
            asymmetric_filter_expressions = []
        self._asymmetric_filter_expressions = list(
            asymmetric_filter_expressions
        )
        if asymmetric_tables is None:
            asymmetric_tables = {}
        self._asymmetric_tables = asymmetric_tables
        self._user = None

    def count(self):
        """See `IBranchCollection`."""
        return self.getBranches(eager_load=False).count()

    def is_empty(self):
        """See `IBranchCollection`."""
        return self.getBranches(eager_load=False).is_empty()

    def ownerCounts(self):
        """See `IBranchCollection`."""
        is_team = Person.teamowner != None
        branch_owners = self._getBranchSelect((Branch.owner_id,))
        counts = dict(
            self.store.find(
                (is_team, Count(Person.id)), Person.id.is_in(branch_owners)
            ).group_by(is_team)
        )
        return (counts.get(False, 0), counts.get(True, 0))

    @property
    def store(self):
        # Although you might think we could set the default value for store in
        # the constructor, we can't. The IStore utility is not
        # available at the time that the branchcollection.zcml is parsed,
        # which means we get an error if this code is in the constructor.
        # -- JonathanLange 2009-02-17.
        if self._store is None:
            return IStore(BugTask)
        else:
            return self._store

    def _filterBy(self, expressions, table=None, join=None, symmetric=True):
        """Return a subset of this collection, filtered by 'expressions'.

        :param symmetric: If True this filter will apply to both sides
            of merge proposal lookups and any other lookups that join
            Branch back onto Branch.
        """
        # NOTE: JonathanLange 2009-02-17: We might be able to avoid the need
        # for explicit 'tables' by harnessing Storm's table inference system.
        # See http://paste.ubuntu.com/118711/ for one way to do that.
        if table is not None:
            if join is None:
                raise InvalidFilter("Cannot specify a table without a join.")
        if expressions is None:
            expressions = []
        tables = self._tables.copy()
        asymmetric_tables = self._asymmetric_tables.copy()
        if symmetric:
            if table is not None:
                tables[table] = join
            symmetric_expr = self._branch_filter_expressions + expressions
            asymmetric_expr = list(self._asymmetric_filter_expressions)
        else:
            if table is not None:
                asymmetric_tables[table] = join
            symmetric_expr = list(self._branch_filter_expressions)
            asymmetric_expr = self._asymmetric_filter_expressions + expressions
        return self.__class__(
            self.store,
            symmetric_expr,
            tables,
            asymmetric_expr,
            asymmetric_tables,
        )

    def _getBranchSelect(self, columns=(Branch.id,)):
        """Return a Storm 'Select' for columns in this collection."""
        branches = self.getBranches(eager_load=False, find_expr=columns)
        return branches.get_plain_result_set()._get_select()

    def _getBranchExpressions(self, team_ids=None, policy_ids=None):
        """Return the where expressions for this collection."""
        return (
            self._branch_filter_expressions
            + self._asymmetric_filter_expressions
            + self._getBranchVisibilityExpression(
                team_ids=team_ids, policy_ids=policy_ids
            )
        )

    def _getBranchVisibilityExpression(
        self, branch_class=None, team_ids=None, policy_ids=None
    ):
        """Return the where clauses for visibility."""
        return []

    @staticmethod
    def preloadVisibleStackedOnBranches(branches, user=None):
        """Preload the chains of stacked on branches related to the given list
        of branches. Only the branches visible for the given user are
        preloaded/returned.

        """
        if len(branches) == 0:
            return
        store = IStore(Branch)
        result = store.execute(
            """
            WITH RECURSIVE stacked_on_branches_ids AS (
                SELECT column1 as id FROM (VALUES %s) AS temp
                UNION
                SELECT DISTINCT branch.stacked_on
                FROM stacked_on_branches_ids, Branch AS branch
                WHERE
                    branch.id = stacked_on_branches_ids.id AND
                    branch.stacked_on IS NOT NULL
            )
            SELECT id from stacked_on_branches_ids
            """
            % ", ".join(
                ["(%s)" % quote(id) for id in map(attrgetter("id"), branches)]
            )
        )
        branch_ids = [res[0] for res in result.get_all()]
        # Not really sure this is useful: if a given branch is visible by a
        # user, then I think it means that the whole chain of branches on
        # which is is stacked on is visible by this user
        expressions = [Branch.id.is_in(branch_ids)]
        if user is None:
            collection = AnonymousBranchCollection(
                branch_filter_expressions=expressions
            )
        else:
            collection = VisibleBranchCollection(
                user=user, branch_filter_expressions=expressions
            )
        return list(collection.getBranches())

    @staticmethod
    def preloadDataForBranches(branches):
        """Preload branches' cached associated targets, product series, and
        suite source packages."""
        load_related(SourcePackageName, branches, ["sourcepackagename_id"])
        load_related(DistroSeries, branches, ["distroseries_id"])
        load_related(Product, branches, ["product_id"])
        caches = {branch.id: get_property_cache(branch) for branch in branches}
        branch_ids = caches.keys()
        for cache in caches.values():
            cache._associatedProductSeries = []
            cache._associatedSuiteSourcePackages = []
            cache.code_import = None
        # associatedProductSeries
        # Imported here to avoid circular import.
        from lp.registry.model.productseries import ProductSeries

        for productseries in IStore(ProductSeries).find(
            ProductSeries, ProductSeries.branch_id.is_in(branch_ids)
        ):
            cache = caches[productseries.branch_id]
            cache._associatedProductSeries.append(productseries)
        # associatedSuiteSourcePackages
        series_set = getUtility(IFindOfficialBranchLinks)
        # Order by the pocket to get the release one first. If changing
        # this be sure to also change BranchCollection.getBranches.
        links = series_set.findForBranches(branches).order_by(
            SeriesSourcePackageBranch.pocket
        )
        for link in links:
            cache = caches[link.branchID]
            cache._associatedSuiteSourcePackages.append(
                link.suite_sourcepackage
            )
        for code_import in IStore(CodeImport).find(
            CodeImport, CodeImport.branch_id.is_in(branch_ids)
        ):
            cache = caches[code_import.branch_id]
            cache.code_import = code_import

    @staticmethod
    def _convertListingSortToOrderBy(sort_by):
        """Compute a value to pass to `order_by` on a collection of branches.

        :param sort_by: an item from the `BranchListingSort` enumeration.
        """
        DEFAULT_BRANCH_LISTING_SORT = [
            BranchListingSort.PRODUCT,
            BranchListingSort.LIFECYCLE,
            BranchListingSort.OWNER,
            BranchListingSort.NAME,
        ]

        LISTING_SORT_TO_COLUMN = {
            BranchListingSort.PRODUCT: (Asc, Branch.target_suffix),
            BranchListingSort.LIFECYCLE: (Desc, Branch.lifecycle_status),
            BranchListingSort.NAME: (Asc, Branch.name),
            BranchListingSort.OWNER: (Asc, Branch.owner_name),
            BranchListingSort.MOST_RECENTLY_CHANGED_FIRST: (
                Desc,
                Branch.date_last_modified,
            ),
            BranchListingSort.LEAST_RECENTLY_CHANGED_FIRST: (
                Asc,
                Branch.date_last_modified,
            ),
            BranchListingSort.NEWEST_FIRST: (Desc, Branch.date_created),
            BranchListingSort.OLDEST_FIRST: (Asc, Branch.date_created),
        }

        order_by = [
            LISTING_SORT_TO_COLUMN.get(sort)
            for sort in DEFAULT_BRANCH_LISTING_SORT
        ]
        # Stabilise the sort using descending ID as a last resort.
        order_by.append((Desc, Branch.id))

        if sort_by is not None and sort_by != BranchListingSort.DEFAULT:
            direction, column = LISTING_SORT_TO_COLUMN[sort_by]
            order_by = [(direction, column)] + [
                sort for sort in order_by if sort[1] is not column
            ]
        return [direction(column) for direction, column in order_by]

    def getBranches(self, find_expr=Branch, eager_load=False, sort_by=None):
        """See `IBranchCollection`."""
        all_tables = set(self._tables.values()) | set(
            self._asymmetric_tables.values()
        )
        tables = [Branch] + list(all_tables)
        expressions = self._getBranchExpressions()
        resultset = self.store.using(*tables).find(find_expr, *expressions)
        if sort_by is not None:
            resultset = resultset.order_by(
                *self._convertListingSortToOrderBy(sort_by)
            )

        def do_eager_load(rows):
            branch_ids = {branch.id for branch in rows}
            if not branch_ids:
                return
            GenericBranchCollection.preloadDataForBranches(rows)
            # So far have only needed the persons for their canonical_url - no
            # need for validity etc in the /branches API call.
            load_related(
                Person, rows, ["owner_id", "registrant_id", "reviewer_id"]
            )
            load_referencing(BugBranch, rows, ["branch_id"])

        def cache_permission(branch):
            if self._user:
                get_property_cache(branch)._known_viewers = {self._user.id}
            return branch

        eager_load_hook = (
            do_eager_load if eager_load and find_expr == Branch else None
        )
        return DecoratedResultSet(
            resultset,
            pre_iter_hook=eager_load_hook,
            result_decorator=cache_permission,
        )

    def getBranchIds(self):
        """See `IBranchCollection`."""
        return self.getBranches(find_expr=Branch.id).get_plain_result_set()

    def getMergeProposals(
        self,
        statuses=None,
        for_branches=None,
        target_branch=None,
        prerequisite_branch=None,
        merged_revnos=None,
        merged_revision=None,
        eager_load=False,
        created_before=None,
        created_since=None,
    ):
        """See `IBranchCollection`."""
        if for_branches is not None and not for_branches:
            # We have an empty branches list, so we can shortcut.
            return EmptyResultSet()
        elif merged_revnos is not None and not merged_revnos:
            # We have an empty revnos list, so we can shortcut.
            return EmptyResultSet()
        elif (
            self._asymmetric_filter_expressions
            or for_branches is not None
            or target_branch is not None
            or prerequisite_branch is not None
            or merged_revnos is not None
            or merged_revision is not None
            or created_before is not None
            or created_since is not None
        ):
            return self._naiveGetMergeProposals(
                statuses,
                for_branches,
                target_branch,
                prerequisite_branch,
                merged_revnos,
                merged_revision,
                created_before=created_before,
                created_since=created_since,
                eager_load=eager_load,
            )
        else:
            # When examining merge proposals in a scope, this is a moderately
            # effective set of constrained queries. It is not effective when
            # unscoped or when tight constraints on branches are present.
            return self._scopedGetMergeProposals(
                statuses, eager_load=eager_load
            )

    def _naiveGetMergeProposals(
        self,
        statuses=None,
        for_branches=None,
        target_branch=None,
        prerequisite_branch=None,
        merged_revnos=None,
        merged_revision=None,
        eager_load=False,
        created_before=None,
        created_since=None,
    ):
        Target = ClassAlias(Branch, "target")
        extra_tables = list(
            set(self._tables.values()) | set(self._asymmetric_tables.values())
        )
        tables = (
            [Branch]
            + extra_tables
            + [
                Join(
                    BranchMergeProposal,
                    And(
                        Branch.id == BranchMergeProposal.source_branch_id,
                        *(
                            self._branch_filter_expressions
                            + self._asymmetric_filter_expressions
                        ),
                    ),
                ),
                Join(
                    Target, Target.id == BranchMergeProposal.target_branch_id
                ),
            ]
        )
        expressions = self._getBranchVisibilityExpression()
        expressions.extend(self._getBranchVisibilityExpression(Target))
        if for_branches is not None:
            branch_ids = [branch.id for branch in for_branches]
            expressions.append(
                BranchMergeProposal.source_branch_id.is_in(branch_ids)
            )
        if target_branch is not None:
            expressions.append(
                BranchMergeProposal.target_branch == target_branch
            )
        if prerequisite_branch is not None:
            expressions.append(
                BranchMergeProposal.prerequisite_branch == prerequisite_branch
            )
        if merged_revnos is not None:
            expressions.append(
                BranchMergeProposal.merged_revno.is_in(merged_revnos)
            )
        if merged_revision is not None:
            expressions.extend(
                [
                    BranchMergeProposal.merged_revno
                    == BranchRevision.sequence,
                    BranchRevision.revision_id == Revision.id,
                    BranchRevision.branch_id
                    == BranchMergeProposal.target_branch_id,
                    Revision.revision_id == merged_revision,
                ]
            )
            tables.extend([BranchRevision, Revision])
        if statuses is not None:
            expressions.append(
                BranchMergeProposal.queue_status.is_in(statuses)
            )
        if created_before is not None:
            expressions.append(
                BranchMergeProposal.date_created < created_before
            )
        if created_since is not None:
            expressions.append(
                BranchMergeProposal.date_created >= created_since
            )
        resultset = self.store.using(*tables).find(
            BranchMergeProposal, *expressions
        )
        if not eager_load:
            return resultset
        else:
            loader = partial(
                BranchMergeProposal.preloadDataForBMPs, user=self._user
            )
            return DecoratedResultSet(resultset, pre_iter_hook=loader)

    def _scopedGetMergeProposals(self, statuses, eager_load=False):
        expressions = (
            self._branch_filter_expressions
            + self._getBranchVisibilityExpression()
        )
        with_expr = With(
            "candidate_branches",
            Select(
                Branch.id,
                tables=[Branch] + list(self._tables.values()),
                where=And(*expressions) if expressions else True,
            ),
        )
        expressions = [
            SQL(
                """
            source_branch IN (SELECT id FROM candidate_branches) AND
            target_branch IN (SELECT id FROM candidate_branches)"""
            )
        ]
        tables = [BranchMergeProposal]
        if self._asymmetric_filter_expressions:
            # Need to filter on Branch beyond the with constraints.
            expressions += self._asymmetric_filter_expressions
            expressions.append(
                BranchMergeProposal.source_branch_id == Branch.id
            )
            tables.append(Branch)
            tables.extend(self._asymmetric_tables.values())
        if statuses is not None:
            expressions.append(
                BranchMergeProposal.queue_status.is_in(statuses)
            )
        resultset = (
            self.store.with_(with_expr)
            .using(*tables)
            .find(BranchMergeProposal, *expressions)
        )
        if not eager_load:
            return resultset
        else:
            loader = partial(
                BranchMergeProposal.preloadDataForBMPs, user=self._user
            )
            return DecoratedResultSet(resultset, pre_iter_hook=loader)

    def getMergeProposalsForPerson(
        self, person, status=None, eager_load=False
    ):
        """See `IBranchCollection`."""
        # We want to limit the proposals to those where the source branch is
        # limited by the defined collection.
        owned = self.ownedBy(person).getMergeProposals(status)
        reviewing = self.getMergeProposalsForReviewer(person, status)
        resultset = owned.union(reviewing)

        if not eager_load:
            return resultset
        else:
            loader = partial(
                BranchMergeProposal.preloadDataForBMPs, user=self._user
            )
            return DecoratedResultSet(resultset, pre_iter_hook=loader)

    def _getMergeProposalsForReviewerExpressions(
        self, team_ids=None, policy_ids=None
    ):
        """Restrict merge proposals to those reviewable in this collection.

        :param team_ids: An optional precomputed array expression of the
            team ids the requesting user participates in, shared across
            the merge proposal's source and target checks.
        :param policy_ids: An optional precomputed array expression of the
            access policy grants held by the requesting user, shared
            across the merge proposal's source and target checks.
        """
        # Each subquery referencing team_ids/policy_ids must list their
        # CTE in its own FROM clause, otherwise Postgres will reject the
        # subquery with "missing FROM-clause entry".
        cte_tables = [team_ids.table] if team_ids is not None else []
        extra_tables = list(
            set(self._tables.values()) | set(self._asymmetric_tables.values())
        )
        source_expressions = self._getBranchExpressions(
            team_ids=team_ids, policy_ids=policy_ids
        )
        # Use correlated EXISTS checks
        expressions = [
            Exists(
                Select(
                    1,
                    # `extra_tables` must come immediately after Branch
                    # (before `cte_tables`): a `Join(...)` in the tables
                    # list attaches to whichever table precedes it, and
                    # extra_tables' joins reference Branch in their ON
                    # clause (e.g. inProjectGroup's `Branch.product ==
                    # Product.id`), which Postgres can't resolve if
                    # `visibility_scope` (a plain comma-joined table) sits
                    # between them.
                    tables=[Branch] + extra_tables + cte_tables,
                    where=And(
                        Branch.id == BranchMergeProposal.source_branch_id,
                        *source_expressions,
                    ),
                )
            ),
        ]
        # Alias Branch so the target-branch EXISTS below doesn't collide
        # with the source-branch EXISTS above, which also uses Branch.
        Target = ClassAlias(Branch, "reviewer_target_branch")
        visibility = self._getBranchVisibilityExpression(
            Target, team_ids=team_ids, policy_ids=policy_ids
        )
        if visibility:
            expressions.append(
                Exists(
                    Select(
                        1,
                        tables=[Target] + cte_tables,
                        where=And(
                            Target.id == BranchMergeProposal.target_branch_id,
                            *visibility,
                        ),
                    )
                )
            )
        return expressions

    def getMergeProposalsForReviewer(self, reviewer, status=None):
        """See `IBranchCollection`."""
        tables = [
            BranchMergeProposal,
            Join(
                CodeReviewVoteReference,
                CodeReviewVoteReference.branch_merge_proposal
                == BranchMergeProposal.id,
            ),
            LeftJoin(
                CodeReviewComment,
                CodeReviewVoteReference.comment == CodeReviewComment.id,
            ),
        ]

        expressions = [
            CodeReviewVoteReference.reviewer == reviewer,
        ]
        store, team_ids, policy_ids = attach_user_visibility_cte(
            self._user, self.store
        )
        expressions.extend(
            self._getMergeProposalsForReviewerExpressions(
                team_ids=team_ids, policy_ids=policy_ids
            )
        )
        if status is not None:
            expressions.append(BranchMergeProposal.queue_status.is_in(status))
        proposals = store.using(*tables).find(
            BranchMergeProposal, *expressions
        )
        # Apply sorting here as we can't do it in the browser code.  We need
        # to think carefully about the best places to do this, but not here
        # nor now.
        proposals.order_by(Desc(CodeReviewComment.vote))
        return proposals

    def getExtendedRevisionDetails(self, user, revisions):
        """See `IBranchCollection`."""

        if not revisions:
            return []
        branch = revisions[0].branch

        def make_rev_info(
            branch_revision, merge_proposal_revs, linked_bugtasks
        ):
            rev_info = {
                "revision": branch_revision,
                "linked_bugtasks": None,
                "merge_proposal": None,
            }
            merge_proposal = merge_proposal_revs.get(branch_revision.sequence)
            rev_info["merge_proposal"] = merge_proposal
            if merge_proposal is not None:
                rev_info["linked_bugtasks"] = linked_bugtasks.get(
                    merge_proposal.source_branch.id
                )
            return rev_info

        rev_nos = [revision.sequence for revision in revisions]
        merge_proposals = self.getMergeProposals(
            target_branch=branch,
            merged_revnos=rev_nos,
            statuses=[BranchMergeProposalStatus.MERGED],
        )
        merge_proposal_revs = {mp.merged_revno: mp for mp in merge_proposals}
        source_branch_ids = [mp.source_branch.id for mp in merge_proposals]
        linked_bugtasks = defaultdict(list)

        if source_branch_ids:
            # We get the bugtasks for our merge proposal branches

            # First, the bug ids
            params = BugTaskSearchParams(
                user=user, status=None, linked_branches=any(*source_branch_ids)
            )
            bug_ids = getUtility(IBugTaskSet).searchBugIds(params)

            # Then the bug tasks and branches
            store = IStore(BugBranch)
            rs = store.using(
                BugBranch,
                Join(BugTask, BugTask.bug_id == BugBranch.bug_id),
            ).find(
                (BugTask, BugBranch),
                BugBranch.bug_id.is_in(bug_ids),
                BugBranch.branch_id.is_in(source_branch_ids),
            )

            # Build up a collection of bugtasks for each branch
            bugtasks_for_branch = defaultdict(list)
            for bugtask, bugbranch in rs:
                bugtasks_for_branch[bugbranch.branch].append(bugtask)

            # Now filter those down to one bugtask per branch
            for branch, tasks in bugtasks_for_branch.items():
                linked_bugtasks[branch.id].extend(
                    filter_bugtasks_by_context(branch.target.context, tasks)
                )

        return [
            make_rev_info(rev, merge_proposal_revs, linked_bugtasks)
            for rev in revisions
        ]

    def getTeamsWithBranches(self, person):
        """See `IBranchCollection`."""
        # This method doesn't entirely fit with the intent of the
        # BranchCollection conceptual model, but we're not quite sure how to
        # fix it just yet.  Perhaps when bug 337494 is fixed, we'd be able to
        # sensibly be able to move this method to another utility class.
        branch_query = self._getBranchSelect((Branch.owner_id,))
        return self.store.find(
            Person,
            Person.id == TeamParticipation.team_id,
            TeamParticipation.person == person,
            TeamParticipation.team != person,
            Person.id.is_in(branch_query),
        )

    def inProduct(self, product):
        """See `IBranchCollection`."""
        return self._filterBy([Branch.product == product])

    def inProjectGroup(self, projectgroup):
        """See `IBranchCollection`."""
        return self._filterBy(
            [Product.projectgroup == projectgroup.id],
            table=Product,
            join=Join(Product, Branch.product == Product.id),
        )

    def inDistribution(self, distribution):
        """See `IBranchCollection`."""
        return self._filterBy(
            [DistroSeries.distribution == distribution],
            table=Distribution,
            join=Join(DistroSeries, Branch.distroseries == DistroSeries.id),
        )

    def inDistroSeries(self, distro_series):
        """See `IBranchCollection`."""
        return self._filterBy([Branch.distroseries == distro_series])

    def inDistributionSourcePackage(self, distro_source_package):
        """See `IBranchCollection`."""
        distribution = distro_source_package.distribution
        sourcepackagename = distro_source_package.sourcepackagename
        return self._filterBy(
            [
                DistroSeries.distribution == distribution,
                Branch.sourcepackagename == sourcepackagename,
            ],
            table=Distribution,
            join=Join(DistroSeries, Branch.distroseries == DistroSeries.id),
        )

    def officialBranches(self, pocket=None):
        """See `IBranchCollection`"""
        if pocket is None:
            expressions = []
        else:
            expressions = [SeriesSourcePackageBranch.pocket == pocket]
        return self._filterBy(
            expressions,
            table=SeriesSourcePackageBranch,
            join=Join(
                SeriesSourcePackageBranch,
                SeriesSourcePackageBranch.branch == Branch.id,
            ),
        )

    def inSourcePackage(self, source_package):
        """See `IBranchCollection`."""
        return self._filterBy(
            [
                Branch.distroseries == source_package.distroseries,
                Branch.sourcepackagename == source_package.sourcepackagename,
            ]
        )

    def isJunk(self):
        """See `IBranchCollection`."""
        return self._filterBy(
            [Branch.product == None, Branch.sourcepackagename == None]
        )

    def isPrivate(self):
        """See `IBranchCollection`."""
        return self._filterBy(
            [Branch.information_type.is_in(PRIVATE_INFORMATION_TYPES)]
        )

    def isExclusive(self):
        """See `IBranchCollection`."""
        return self._filterBy(
            [Person.membership_policy.is_in(EXCLUSIVE_TEAM_POLICY)],
            table=Person,
            join=Join(Person, Branch.owner_id == Person.id),
        )

    def isSeries(self):
        """See `IBranchCollection`."""
        # Circular imports.
        from lp.registry.model.productseries import ProductSeries

        return self._filterBy(
            [Branch.id == ProductSeries.branch_id],
            table=ProductSeries,
            join=Join(ProductSeries, Branch.id == ProductSeries.branch_id),
        )

    def ownedBy(self, person):
        """See `IBranchCollection`."""
        return self._filterBy([Branch.owner == person], symmetric=False)

    def ownedByTeamMember(self, person):
        """See `IBranchCollection`."""
        subquery = Select(
            TeamParticipation.team_id,
            where=TeamParticipation.person == person,
        )
        return self._filterBy([In(Branch.owner_id, subquery)], symmetric=False)

    def registeredBy(self, person):
        """See `IBranchCollection`."""
        return self._filterBy([Branch.registrant == person], symmetric=False)

    def _getExactMatch(self, term):
        # Try and look up the branch by its URL, which handles lp: shortfom.
        branch_url = getUtility(IBranchLookup).getByUrl(term)
        if branch_url:
            return branch_url
        # Fall back to searching by unique_name, stripping out the path if it's
        # a URI.
        try:
            path = URI(term).path.strip("/")
        except InvalidURIError:
            path = term
        return getUtility(IBranchLookup).getByUniqueName(path)

    def search(self, term):
        """See `IBranchCollection`."""
        branch = self._getExactMatch(term)
        if branch:
            collection = self._filterBy([Branch.id == branch.id])
        else:
            term = six.ensure_text(term)
            # Filter by name.
            field = Branch.name
            # Except if the term contains /, when we use unique_name.
            if "/" in term:
                field = Branch.unique_name
            collection = self._filterBy(
                [field.lower().contains_string(term.lower())]
            )
        return collection.getBranches(eager_load=False).order_by(
            Branch.name, Branch.id
        )

    def scanned(self):
        """See `IBranchCollection`."""
        return self._filterBy([Branch.last_scanned != None])

    def subscribedBy(self, person):
        """See `IBranchCollection`."""
        return self._filterBy(
            [BranchSubscription.person == person],
            table=BranchSubscription,
            join=Join(
                BranchSubscription, BranchSubscription.branch == Branch.id
            ),
            symmetric=False,
        )

    def targetedBy(self, person, since=None):
        """See `IBranchCollection`."""
        clauses = [BranchMergeProposal.registrant == person]
        if since is not None:
            clauses.append(BranchMergeProposal.date_created >= since)
        return self._filterBy(
            clauses,
            table=BranchMergeProposal,
            join=Join(
                BranchMergeProposal,
                BranchMergeProposal.target_branch == Branch.id,
            ),
            symmetric=False,
        )

    def linkedToBugs(self, bugs):
        """See `IBranchCollection`."""
        bug_ids = [bug.id for bug in bugs]
        return self._filterBy(
            [In(BugBranch.bug_id, bug_ids)],
            table=BugBranch,
            join=Join(BugBranch, BugBranch.branch == Branch.id),
            symmetric=False,
        )

    def visibleByUser(self, person):
        """See `IBranchCollection`."""
        if person == LAUNCHPAD_SERVICES or user_has_special_branch_access(
            person
        ):
            return self
        if person is None:
            return AnonymousBranchCollection(
                self._store,
                self._branch_filter_expressions,
                self._tables,
                self._asymmetric_filter_expressions,
                self._asymmetric_tables,
            )
        return VisibleBranchCollection(
            person,
            self._store,
            self._branch_filter_expressions,
            self._tables,
            self._asymmetric_filter_expressions,
            self._asymmetric_tables,
        )

    def withBranchType(self, *branch_types):
        return self._filterBy(
            [Branch.branch_type.is_in(branch_types)], symmetric=False
        )

    def withLifecycleStatus(self, *statuses):
        """See `IBranchCollection`."""
        return self._filterBy(
            [Branch.lifecycle_status.is_in(statuses)], symmetric=False
        )

    def modifiedSince(self, epoch):
        """See `IBranchCollection`."""
        return self._filterBy(
            [Branch.date_last_modified > epoch], symmetric=False
        )

    def scannedSince(self, epoch):
        """See `IBranchCollection`."""
        return self._filterBy([Branch.last_scanned > epoch], symmetric=False)

    def withIds(self, *branch_ids):
        """See `IBranchCollection`."""
        return self._filterBy([Branch.id.is_in(branch_ids)], symmetric=False)


class AnonymousBranchCollection(GenericBranchCollection):
    """Branch collection that only shows public branches."""

    def _getBranchVisibilityExpression(
        self, branch_class=Branch, team_ids=None, policy_ids=None
    ):
        """Return the where clauses for visibility."""
        return get_branch_privacy_filter(None, branch_class=branch_class)


class VisibleBranchCollection(GenericBranchCollection):
    """A branch collection that has special logic for visibility."""

    def __init__(
        self,
        user,
        store=None,
        branch_filter_expressions=None,
        tables=None,
        asymmetric_filter_expressions=None,
        asymmetric_tables=None,
    ):
        super().__init__(
            store=store,
            branch_filter_expressions=branch_filter_expressions,
            tables=tables,
            asymmetric_filter_expressions=asymmetric_filter_expressions,
            asymmetric_tables=asymmetric_tables,
        )
        self._user = user

    def _filterBy(self, expressions, table=None, join=None, symmetric=True):
        """Return a subset of this collection, filtered by 'expressions'.

        :param symmetric: If True this filter will apply to both sides
            of merge proposal lookups and any other lookups that join
            Branch back onto Branch.
        """
        # NOTE: JonathanLange 2009-02-17: We might be able to avoid the need
        # for explicit 'tables' by harnessing Storm's table inference system.
        # See http://paste.ubuntu.com/118711/ for one way to do that.
        if table is not None:
            if join is None:
                raise InvalidFilter("Cannot specify a table without a join.")
        if expressions is None:
            expressions = []
        tables = self._tables.copy()
        asymmetric_tables = self._asymmetric_tables.copy()
        if symmetric:
            if table is not None:
                tables[table] = join
            symmetric_expr = self._branch_filter_expressions + expressions
            asymmetric_expr = list(self._asymmetric_filter_expressions)
        else:
            if table is not None:
                asymmetric_tables[table] = join
            symmetric_expr = list(self._branch_filter_expressions)
            asymmetric_expr = self._asymmetric_filter_expressions + expressions
        return self.__class__(
            self._user,
            self.store,
            symmetric_expr,
            tables,
            asymmetric_expr,
            asymmetric_tables,
        )

    def _getBranchVisibilityExpression(
        self, branch_class=Branch, team_ids=None, policy_ids=None
    ):
        """Return the where clauses for visibility.

        :param branch_class: The Branch class to use - permits using
            ClassAliases.
        :param team_ids: An optional precomputed array expression of the
            team ids the user participates in, shared across several
            visibility checks. See `get_branch_privacy_filter`.
        :param policy_ids: An optional precomputed array expression of the
            access policy grants held by the user, shared across several
            visibility checks. See `get_branch_privacy_filter`.
        """
        return get_branch_privacy_filter(
            self._user,
            branch_class=branch_class,
            team_ids=team_ids,
            policy_ids=policy_ids,
        )

    def visibleByUser(self, person):
        """See `IBranchCollection`."""
        if person == self._user:
            return self
        raise InvalidFilter(
            "Cannot filter for branches visible by user %r, already "
            "filtering for %r" % (person, self._user)
        )
