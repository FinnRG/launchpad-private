# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `lp.registry.model.uservisibility`."""

from storm.expr import Select

from lp.app.enums import InformationType
from lp.registry.model.person import Person
from lp.registry.model.uservisibility import get_user_visibility_cte
from lp.services.database.interfaces import IStore
from lp.testing import TestCaseWithFactory
from lp.testing.layers import DatabaseFunctionalLayer


class TestGetUserVisibilityScopeCte(TestCaseWithFactory):
    """Tests for `get_user_visibility_cte`."""

    layer = DatabaseFunctionalLayer

    def _fetchIds(self, person):
        store = IStore(Person)
        visibility_scope = get_user_visibility_cte(person, store)
        result = store.execute(
            Select(
                (visibility_scope.team_ids, visibility_scope.policy_ids),
                tables=visibility_scope.team_ids.table,
                with_=visibility_scope.with_expr,
            )
        )
        return result.get_one()

    def test_team_with_multiple_policy_grants_is_not_duplicated(self):
        # A team's id must appear only once in team_ids, even if that
        # team has been granted access through more than one
        # AccessPolicyGrant. team_ids and policy_ids must each be
        # computed from their own, independent aggregate, rather than
        # from a single query that joins TeamParticipation to
        # AccessPolicyGrant (which would repeat the team's id once per
        # matching grant).
        person = self.factory.makePerson()
        team = self.factory.makeTeam(members=[person])
        product = self.factory.makeProduct()
        policies = []
        for info_type in (
            InformationType.USERDATA,
            InformationType.PROPRIETARY,
        ):
            policy = self.factory.makeAccessPolicy(
                pillar=product, type=info_type, check_existing=True
            )
            policies.append(policy)
            self.factory.makeAccessPolicyGrant(
                policy=policy, grantee=team, grantor=product.owner
            )

        team_ids, policy_ids = self._fetchIds(person)

        self.assertEqual(len(set(team_ids)), len(team_ids))
        self.assertEqual({person.id, team.id}, set(team_ids))
        self.assertEqual(len(set(policy_ids)), len(policy_ids))
        self.assertEqual({policy.id for policy in policies}, set(policy_ids))
