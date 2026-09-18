# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Helpers for sharing a person's visibility scope across several checks.

Many artifact visibility filters (for example
`lp.code.model.branch.get_branch_privacy_filter` and
`lp.code.model.gitrepository.get_git_repository_privacy_filter`) each
compute, for a given person, the array of teams they participate in and the
array of access policies they've been granted. When a query needs to apply
that filter to more than one column (for example, the source and target of
a merge proposal), computing those arrays afresh for every column forces
the database to repeat the same aggregate several times. This module
provides a single, shared CTE so the arrays are computed once and reused.
"""

__all__ = [
    "UserVisibilityScope",
    "attach_user_visibility_cte",
    "get_user_visibility_cte",
]

from collections import namedtuple

from storm.expr import Alias, Column, Join, Select, Table

from lp.registry.model.accesspolicy import AccessPolicyGrant
from lp.registry.model.teammembership import TeamParticipation
from lp.services.database.stormexpr import ArrayAgg, WithMaterialized

UserVisibilityScope = namedtuple(
    "UserVisibilityScope", ["with_expr", "team_ids", "policy_ids"]
)


def get_user_visibility_cte(user, store):
    """Return a materialized CTE computing `user`'s teams and policies.

    :param user: The person whose team participations and access policy
        grants should be computed.
    :param store: The `Store` to run the CTE's version check against.
    :return: A `UserVisibilityScope` with the `With` clause to pass to
        `Store.with_`, and the `team_ids`/`policy_ids` column expressions
        to pass to the visibility filter functions that accept them.
    """
    cte_table = Table("visibility_scope")
    # The two aggregates are computed as separate scalar subqueries (rather
    # than from a single joined FROM clause) so that a team with several
    # access policy grants doesn't cause its id to be repeated in team_ids
    # once per matching grant.
    team_ids_query = Select(
        ArrayAgg(TeamParticipation.team_id),
        tables=TeamParticipation,
        where=(TeamParticipation.person == user),
    )
    policy_ids_query = Select(
        ArrayAgg(AccessPolicyGrant.policy_id),
        tables=(
            AccessPolicyGrant,
            Join(
                TeamParticipation,
                TeamParticipation.team_id == AccessPolicyGrant.grantee_id,
            ),
        ),
        where=(TeamParticipation.person == user),
    )
    with_expr = WithMaterialized(
        cte_table.name,
        store,
        Select(
            (
                Alias(team_ids_query, "team_ids"),
                Alias(policy_ids_query, "policy_ids"),
            )
        ),
    )
    return UserVisibilityScope(
        with_expr=with_expr,
        team_ids=Column("team_ids", cte_table),
        policy_ids=Column("policy_ids", cte_table),
    )


def attach_user_visibility_cte(user, store):
    """Attach a shared teams/policies CTE for `user` to `store`, if needed.

    This is the common "does this query need a user's visibility scope,
    and if so, share one materialized computation of them across all the
    filters that will use them" logic needed by every caller that builds
    per-user artifact visibility checks.

    :param user: The person the query is being filtered for, or None for
        an anonymous/public-only query.
    :param store: The `Store` the query will run against.
    :return: A 3-tuple `(store, team_ids, policy_ids)`. If `user` is None,
        `store` is returned unchanged and `team_ids`/`policy_ids` are both
        None. Otherwise `store` is replaced by the `WithSet` returned by
        `Store.with_`, which supports `find`/`using` like a `Store` and
        makes the CTE available to the query; `team_ids`/`policy_ids` are
        the column expressions to pass to the visibility filter functions
        that accept them.
    """
    if user is None:
        return store, None, None
    visibility_scope = get_user_visibility_cte(user, store)
    return (
        store.with_(visibility_scope.with_expr),
        visibility_scope.team_ids,
        visibility_scope.policy_ids,
    )
