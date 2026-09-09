# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for merge_people."""

from datetime import datetime, timezone
from operator import attrgetter

import transaction
from testtools.matchers import (
    Equals,
    MatchesListwise,
    MatchesSetwise,
    MatchesStructure,
)
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.charms.interfaces.charmrecipe import (
    CHARM_RECIPE_ALLOW_CREATE,
    ICharmRecipeSet,
)
from lp.code.interfaces.gitrepository import IGitRepositorySet
from lp.crafts.interfaces.craftrecipe import (
    CRAFT_RECIPE_ALLOW_CREATE,
    ICraftRecipeSet,
)
from lp.oci.interfaces.ocirecipe import OCI_RECIPE_ALLOW_CREATE, IOCIRecipeSet
from lp.registry.enums import BugSharingPolicy
from lp.registry.interfaces.accesspolicy import (
    IAccessArtifactGrantSource,
    IAccessPolicyGrantSource,
)
from lp.registry.interfaces.karma import IKarmaCacheManager
from lp.registry.interfaces.mailinglist import MailingListStatus
from lp.registry.interfaces.mailinglistsubscription import (
    MailingListAutoSubscribePolicy,
)
from lp.registry.interfaces.person import IPersonSet, TeamMembershipStatus
from lp.registry.interfaces.personnotification import IPersonNotificationSet
from lp.registry.personmerge import (
    _mergeMailingListSubscriptions,
    merge_people,
)
from lp.registry.tests.test_person import KarmaTestMixin
from lp.rocks.interfaces.rockrecipe import (
    ROCK_RECIPE_ALLOW_CREATE,
    IRockRecipeSet,
)
from lp.services.config import config
from lp.services.database.sqlbase import cursor
from lp.services.features.testing import FeatureFixture
from lp.services.identity.interfaces.emailaddress import (
    EmailAddressStatus,
    IEmailAddressSet,
)
from lp.services.webapp.authorization import check_permission
from lp.services.webauthn.interfaces import IRegisteredAuthenticatorSet
from lp.snappy.interfaces.snap import ISnapSet
from lp.soyuz.enums import ArchiveStatus
from lp.soyuz.interfaces.livefs import LIVEFS_FEATURE_FLAG, ILiveFSSet
from lp.testing import (
    TestCaseWithFactory,
    admin_logged_in,
    celebrity_logged_in,
    login_person,
    person_logged_in,
)
from lp.testing.dbuser import dbuser
from lp.testing.layers import DatabaseFunctionalLayer


class TestMergePeople(TestCaseWithFactory, KarmaTestMixin):
    """Test cases for PersonSet merge."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person_set = getUtility(IPersonSet)

    def _do_premerge(self, from_person, to_person):
        # Do the pre merge work performed by the LoginToken.
        with celebrity_logged_in("admin"):
            email = from_person.preferredemail
            email.status = EmailAddressStatus.NEW
            email.person = to_person
        transaction.commit()

    def _do_merge(self, from_person, to_person, reviewer=None):
        # Perform the merge as the db user that will be used by the jobs.
        with dbuser(config.IPersonMergeJobSource.dbuser):
            merge_people(from_person, to_person, reviewer=reviewer)
        return from_person, to_person

    def test_delete_no_notifications(self):
        team = self.factory.makeTeam()
        owner = team.teamowner
        transaction.commit()
        with dbuser(config.IPersonMergeJobSource.dbuser):
            merge_people(
                team,
                getUtility(ILaunchpadCelebrities).registry_experts,
                owner,
                delete=True,
            )
        notification_set = getUtility(IPersonNotificationSet)
        notifications = notification_set.getNotificationsToSend()
        self.assertEqual(0, notifications.count())

    def test_openid_identifiers(self):
        # Verify that OpenId Identifiers are merged.
        duplicate = self.factory.makePerson()
        duplicate_identifier = (
            removeSecurityProxy(duplicate.account)
            .openid_identifiers.any()
            .identifier
        )
        person = self.factory.makePerson()
        person_identifier = (
            removeSecurityProxy(person.account)
            .openid_identifiers.any()
            .identifier
        )
        self._do_premerge(duplicate, person)
        login_person(person)
        duplicate, person = self._do_merge(duplicate, person)
        self.assertEqual(
            0,
            removeSecurityProxy(duplicate.account).openid_identifiers.count(),
        )

        merged_identifiers = [
            identifier.identifier
            for identifier in removeSecurityProxy(
                person.account
            ).openid_identifiers
        ]

        self.assertIn(duplicate_identifier, merged_identifiers)
        self.assertIn(person_identifier, merged_identifiers)

    def test_karmacache_transferred_to_user_has_no_karma(self):
        # Verify that the merged user has no KarmaCache entries,
        # and the karma total was transferred.
        self.cache_manager = getUtility(IKarmaCacheManager)
        product = self.factory.makeProduct()
        duplicate = self.factory.makePerson()
        self._makeKarmaCache(duplicate, product, [("bugs", 10)])
        self._makeKarmaTotalCache(duplicate, 15)
        # The karma changes invalidated duplicate instance.
        duplicate = self.person_set.get(duplicate.id)
        person = self.factory.makePerson()
        self._do_premerge(duplicate, person)
        login_person(person)
        duplicate, person = self._do_merge(duplicate, person)
        self.assertEqual([], duplicate.karma_category_caches)
        self.assertEqual(0, duplicate.karma)
        self.assertEqual(15, person.karma)

    def test_karmacache_transferred_to_user_has_karma(self):
        # Verify that the merged user has no KarmaCache entries,
        # and the karma total was summed.
        self.cache_manager = getUtility(IKarmaCacheManager)
        product = self.factory.makeProduct()
        duplicate = self.factory.makePerson()
        self._makeKarmaCache(duplicate, product, [("bugs", 10)])
        self._makeKarmaTotalCache(duplicate, 15)
        person = self.factory.makePerson()
        self._makeKarmaCache(person, product, [("bugs", 9)])
        self._makeKarmaTotalCache(person, 13)
        # The karma changes invalidated duplicate and person instances.
        duplicate = self.person_set.get(duplicate.id)
        person = self.person_set.get(person.id)
        self._do_premerge(duplicate, person)
        login_person(person)
        duplicate, person = self._do_merge(duplicate, person)
        self.assertEqual([], duplicate.karma_category_caches)
        self.assertEqual(0, duplicate.karma)
        self.assertEqual(28, person.karma)

    def test_person_date_created_preserved(self):
        # Verify that the oldest datecreated is merged.
        person = self.factory.makePerson()
        duplicate = self.factory.makePerson()
        oldest_date = datetime(2005, 11, 25, 0, 0, 0, 0, timezone.utc)
        removeSecurityProxy(duplicate).datecreated = oldest_date
        self._do_premerge(duplicate, person)
        login_person(person)
        duplicate, person = self._do_merge(duplicate, person)
        self.assertEqual(oldest_date, person.datecreated)

    def test_team_with_active_mailing_list_raises_error(self):
        # A team with an active mailing list cannot be merged.
        target_team = self.factory.makeTeam()
        test_team = self.factory.makeTeam()
        self.factory.makeMailingList(test_team, test_team.teamowner)
        self.assertRaises(
            AssertionError, merge_people, test_team, target_team, None
        )

    def test_team_with_inactive_mailing_list(self):
        # A team with an inactive mailing list can be merged.
        target_team = self.factory.makeTeam()
        test_team = self.factory.makeTeam()
        mailing_list = self.factory.makeMailingList(
            test_team, test_team.teamowner
        )
        mailing_list.deactivate()
        mailing_list.transitionToStatus(MailingListStatus.INACTIVE)
        test_team, target_team = self._do_merge(
            test_team, target_team, test_team.teamowner
        )
        self.assertEqual(target_team, test_team.merged)
        self.assertEqual(
            MailingListStatus.PURGED, test_team.mailing_list.status
        )
        emails = getUtility(IEmailAddressSet).getByPerson(target_team).count()
        self.assertEqual(0, emails)

    def test_team_with_purged_mailing_list(self):
        # A team with a purges mailing list can be merged.
        target_team = self.factory.makeTeam()
        test_team = self.factory.makeTeam()
        mailing_list = self.factory.makeMailingList(
            test_team, test_team.teamowner
        )
        mailing_list.deactivate()
        mailing_list.transitionToStatus(MailingListStatus.INACTIVE)
        mailing_list.purge()
        test_team, target_team = self._do_merge(
            test_team, target_team, test_team.teamowner
        )
        self.assertEqual(target_team, test_team.merged)

    def test_team_with_members(self):
        # Team members are removed before merging.
        target_team = self.factory.makeTeam()
        test_team = self.factory.makeTeam()
        former_member = self.factory.makePerson()
        with person_logged_in(test_team.teamowner):
            test_team.addMember(former_member, test_team.teamowner)
        test_team, target_team = self._do_merge(
            test_team, target_team, test_team.teamowner
        )
        self.assertEqual(target_team, test_team.merged)
        self.assertEqual([], list(former_member.super_teams))

    def test_team_without_super_teams_is_fine(self):
        # A team with no members and no super teams
        # merges without errors.
        test_team = self.factory.makeTeam()
        target_team = self.factory.makeTeam()
        login_person(test_team.teamowner)
        self._do_merge(test_team, target_team, test_team.teamowner)

    def test_team_with_super_teams(self):
        # A team with superteams can be merged, but the memberships
        # are not transferred.
        test_team = self.factory.makeTeam()
        super_team = self.factory.makeTeam()
        target_team = self.factory.makeTeam()
        login_person(test_team.teamowner)
        test_team.join(super_team, test_team.teamowner)
        test_team, target_team = self._do_merge(
            test_team, target_team, test_team.teamowner
        )
        self.assertEqual(target_team, test_team.merged)
        self.assertEqual([], list(target_team.super_teams))

    def test_merge_moves_branches(self):
        # When person/teams are merged, branches owned by the from person
        # are moved.
        person = self.factory.makePerson()
        branch = self.factory.makeBranch()
        duplicate = branch.owner
        self._do_premerge(branch.owner, person)
        login_person(person)
        duplicate, person = self._do_merge(duplicate, person)
        branches = person.getBranches()
        self.assertEqual(1, branches.count())

    def test_merge_with_duplicated_branches(self):
        # If both the from and to people have branches with the same name,
        # merging renames the duplicate from the from person's side.
        product = self.factory.makeProduct()
        from_branch = self.factory.makeBranch(name="foo", product=product)
        to_branch = self.factory.makeBranch(name="foo", product=product)
        mergee = to_branch.owner
        duplicate = from_branch.owner
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        branches = [b.name for b in mergee.getBranches()]
        self.assertEqual(2, len(branches))
        self.assertContentEqual(["foo", "foo-1"], branches)

    def test_merge_moves_git_repositories(self):
        # When person/teams are merged, Git repositories owned by the from
        # person are moved.
        person = self.factory.makePerson()
        repository = self.factory.makeGitRepository()
        duplicate = repository.owner
        self._do_premerge(repository.owner, person)
        login_person(person)
        duplicate, person = self._do_merge(duplicate, person)
        repository_set = getUtility(IGitRepositorySet)
        repositories = repository_set.getRepositories(None, person)
        self.assertEqual(1, repositories.count())

    def test_merge_with_duplicated_git_repositories(self):
        # If both the from and to people have Git repositories with the same
        # name, merging renames the duplicate from the from person's side.
        project = self.factory.makeProduct()
        from_repository = self.factory.makeGitRepository(
            target=project, name="foo"
        )
        to_repository = self.factory.makeGitRepository(
            target=project, name="foo"
        )
        mergee = to_repository.owner
        duplicate = from_repository.owner
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        repository_set = getUtility(IGitRepositorySet)
        repositories = [
            r.name for r in repository_set.getRepositories(None, mergee)
        ]
        self.assertEqual(2, len(repositories))
        self.assertContentEqual(["foo", "foo-1"], repositories)

    def test_merge_moves_recipes(self):
        # When person/teams are merged, recipes owned by the from person are
        # moved.
        person = self.factory.makePerson()
        recipe = self.factory.makeSourcePackageRecipe()
        duplicate = recipe.owner
        # Delete the PPA, which is required for the merge to work.
        with person_logged_in(duplicate):
            recipe.owner.archive.status = ArchiveStatus.DELETED
        self._do_premerge(duplicate, person)
        login_person(person)
        duplicate, person = self._do_merge(duplicate, person)
        self.assertEqual(1, person.recipes.count())

    def test_merge_with_duplicated_recipes(self):
        # If both the from and to people have recipes with the same name,
        # merging renames the duplicate from the from person's side.
        merge_from = self.factory.makeSourcePackageRecipe(
            name="foo", description="FROM"
        )
        merge_to = self.factory.makeSourcePackageRecipe(
            name="foo", description="TO"
        )
        duplicate = merge_from.owner
        mergee = merge_to.owner
        # Delete merge_from's PPA, which is required for the merge to work.
        with person_logged_in(merge_from.owner):
            merge_from.owner.archive.status = ArchiveStatus.DELETED
        self._do_premerge(merge_from.owner, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        recipes = mergee.recipes
        self.assertEqual(2, recipes.count())
        descriptions = [r.description for r in recipes]
        self.assertEqual(["TO", "FROM"], descriptions)
        self.assertEqual("foo-1", recipes[1].name)

    def assertSubscriptionMerges(self, target):
        # Given a subscription target, we want to make sure that subscriptions
        # that the duplicate person made are carried over to the merged
        # account.
        duplicate = self.factory.makePerson()
        with person_logged_in(duplicate):
            target.addSubscription(duplicate, duplicate)
        person = self.factory.makePerson()
        self._do_premerge(duplicate, person)
        login_person(person)
        duplicate, person = self._do_merge(duplicate, person)
        # The merged person has the subscription, and the duplicate person
        # does not.
        self.assertTrue(target.getSubscription(person) is not None)
        self.assertTrue(target.getSubscription(duplicate) is None)

    def assertConflictingSubscriptionDeletes(self, target):
        # Given a subscription target, we want to make sure that subscriptions
        # that the duplicate person made that conflict with existing
        # subscriptions in the merged account are deleted.
        duplicate = self.factory.makePerson()
        person = self.factory.makePerson()
        with person_logged_in(duplicate):
            target.addSubscription(duplicate, duplicate)
        with person_logged_in(person):
            # The description lets us show that we still have the right
            # subscription later.
            target.addBugSubscriptionFilter(person, person).description = (
                "a marker"
            )
        self._do_premerge(duplicate, person)
        login_person(person)
        duplicate, person = self._do_merge(duplicate, person)
        # The merged person still has the original subscription, as shown
        # by the marker name.
        self.assertEqual(
            target.getSubscription(person).bug_filters[0].description,
            "a marker",
        )
        # The conflicting subscription on the duplicate has been deleted.
        self.assertTrue(target.getSubscription(duplicate) is None)

    def test_merge_with_product_subscription(self):
        # See comments in assertSubscriptionMerges.
        self.assertSubscriptionMerges(self.factory.makeProduct())

    def test_merge_with_conflicting_product_subscription(self):
        # See comments in assertConflictingSubscriptionDeletes.
        self.assertConflictingSubscriptionDeletes(self.factory.makeProduct())

    def test_merge_with_project_subscription(self):
        # See comments in assertSubscriptionMerges.
        self.assertSubscriptionMerges(self.factory.makeProject())

    def test_merge_with_conflicting_project_subscription(self):
        # See comments in assertConflictingSubscriptionDeletes.
        self.assertConflictingSubscriptionDeletes(self.factory.makeProject())

    def test_merge_with_distroseries_subscription(self):
        # See comments in assertSubscriptionMerges.
        self.assertSubscriptionMerges(self.factory.makeDistroSeries())

    def test_merge_with_conflicting_distroseries_subscription(self):
        # See comments in assertConflictingSubscriptionDeletes.
        self.assertConflictingSubscriptionDeletes(
            self.factory.makeDistroSeries()
        )

    def test_merge_with_milestone_subscription(self):
        # See comments in assertSubscriptionMerges.
        self.assertSubscriptionMerges(self.factory.makeMilestone())

    def test_merge_with_conflicting_milestone_subscription(self):
        # See comments in assertConflictingSubscriptionDeletes.
        self.assertConflictingSubscriptionDeletes(self.factory.makeMilestone())

    def test_merge_with_productseries_subscription(self):
        # See comments in assertSubscriptionMerges.
        self.assertSubscriptionMerges(self.factory.makeProductSeries())

    def test_merge_with_conflicting_productseries_subscription(self):
        # See comments in assertConflictingSubscriptionDeletes.
        self.assertConflictingSubscriptionDeletes(
            self.factory.makeProductSeries()
        )

    def test_merge_with_distribution_subscription(self):
        # See comments in assertSubscriptionMerges.
        self.assertSubscriptionMerges(self.factory.makeDistribution())

    def test_merge_with_conflicting_distribution_subscription(self):
        # See comments in assertConflictingSubscriptionDeletes.
        self.assertConflictingSubscriptionDeletes(
            self.factory.makeDistribution()
        )

    def test_merge_with_sourcepackage_subscription(self):
        # See comments in assertSubscriptionMerges.
        dsp = self.factory.makeDistributionSourcePackage()
        self.assertSubscriptionMerges(dsp)

    def test_merge_with_conflicting_sourcepackage_subscription(self):
        # See comments in assertConflictingSubscriptionDeletes.
        dsp = self.factory.makeDistributionSourcePackage()
        self.assertConflictingSubscriptionDeletes(dsp)

    def test_merge_accessartifactgrant(self):
        # AccessArtifactGrants are transferred; DB triggers complete.
        dupe = self.factory.makePerson()
        bug = self.factory.makeBug(information_type=InformationType.USERDATA)
        artifact = self.factory.makeAccessArtifact(concrete=bug)
        self.factory.makeAccessArtifactGrant(artifact, dupe)
        person = self.factory.makePerson()
        self._do_premerge(dupe, person)
        with person_logged_in(person):
            self._do_merge(dupe, person)
        source = getUtility(IAccessArtifactGrantSource)
        grantees = [
            grant.grantee
            for grant in source.findByArtifact([artifact])
            if grant.grantee == person
        ]
        self.assertContentEqual([person], grantees)

    def test_merge_accesspolicygrants(self):
        # AccessPolicyGrants are transferred from the duplicate.
        person = self.factory.makePerson()
        grant = self.factory.makeAccessPolicyGrant()
        self._do_premerge(grant.grantee, person)

        source = getUtility(IAccessPolicyGrantSource)
        self.assertEqual(
            grant.grantee, source.findByPolicy([grant.policy]).one().grantee
        )
        with person_logged_in(person):
            self._do_merge(grant.grantee, person)
        self.assertEqual(
            person, source.findByPolicy([grant.policy]).one().grantee
        )

    def test_merge_accesspolicygrants_conflicts(self):
        # Conflicting AccessPolicyGrants are deleted.
        policy = self.factory.makeAccessPolicy()

        person = self.factory.makePerson()
        person_grantor = self.factory.makePerson()
        person_grant = self.factory.makeAccessPolicyGrant(
            grantee=person, grantor=person_grantor, policy=policy
        )
        person_grant_date = person_grant.date_created

        duplicate = self.factory.makePerson()
        duplicate_grantor = self.factory.makePerson()
        self.factory.makeAccessPolicyGrant(
            grantee=duplicate, grantor=duplicate_grantor, policy=policy
        )

        self._do_premerge(duplicate, person)
        with person_logged_in(person):
            self._do_merge(duplicate, person)

        # Only one grant for the policy exists: the retained person's.
        source = getUtility(IAccessPolicyGrantSource)
        self.assertThat(
            source.findByPolicy([policy]).one(),
            MatchesStructure.byEquality(
                policy=policy, grantee=person, date_created=person_grant_date
            ),
        )

    def test_merge_transfers_non_conflicting_gitrulegrants(self):
        # GitRuleGrants are transferred from the duplicate.
        rule = self.factory.makeGitRule()
        person = self.factory.makePerson()
        grant = self.factory.makeGitRuleGrant(rule=rule)
        self._do_premerge(grant.grantee, person)

        self.assertEqual(1, len(rule.grants))
        self.assertEqual(grant.grantee, rule.grants[0].grantee)
        with person_logged_in(person):
            self._do_merge(grant.grantee, person)
        self.assertEqual(1, len(rule.grants))
        self.assertEqual(person, rule.grants[0].grantee)

    def test_merge_conflicting_gitrulegrants(self):
        # Conflicting GitRuleGrants have their permissions merged.
        rule = self.factory.makeGitRule()

        person = self.factory.makePerson()
        person_grant = self.factory.makeGitRuleGrant(
            rule=rule, grantee=person, can_create=True
        )
        person_grant_date = person_grant.date_created

        duplicate = self.factory.makePerson()
        self.factory.makeGitRuleGrant(
            rule=rule, grantee=duplicate, can_push=True
        )

        other_person = self.factory.makePerson()
        self.factory.makeGitRuleGrant(
            rule=rule, grantee=other_person, can_push=True
        )

        other_rule = self.factory.makeGitRule(
            rule.repository, ref_pattern="refs/heads/other/*"
        )
        self.factory.makeGitRuleGrant(
            rule=other_rule, grantee=other_person, can_force_push=True
        )

        self._do_premerge(duplicate, person)
        with person_logged_in(person):
            self._do_merge(duplicate, person)

        # Only two grants for the rule exist: the retained person's, with
        # the union of permissions from the duplicate and target grants, and
        # the grant to an unrelated person.
        self.assertThat(
            rule.grants,
            MatchesSetwise(
                MatchesStructure.byEquality(
                    rule=rule,
                    grantee=person,
                    date_created=person_grant_date,
                    can_create=True,
                    can_push=True,
                    can_force_push=False,
                ),
                MatchesStructure.byEquality(
                    rule=rule,
                    grantee=other_person,
                    can_create=False,
                    can_push=True,
                    can_force_push=False,
                ),
            ),
        )
        # A grant for another rule is untouched.
        self.assertThat(
            other_rule.grants,
            MatchesSetwise(
                MatchesStructure.byEquality(
                    rule=other_rule,
                    grantee=other_person,
                    can_create=False,
                    can_push=False,
                    can_force_push=True,
                )
            ),
        )

    def test_mergeAsync(self):
        # mergeAsync() creates a new `PersonMergeJob`.
        from_person = self.factory.makePerson()
        to_person = self.factory.makePerson()
        login_person(from_person)
        job = self.person_set.mergeAsync(from_person, to_person, from_person)
        self.assertEqual(from_person, job.from_person)
        self.assertEqual(to_person, job.to_person)
        self.assertEqual(from_person, job.requester)

    def test_mergeProposedInvitedTeamMembership(self):
        # Proposed and invited memberships are declined.
        TMS = TeamMembershipStatus
        dupe_team = self.factory.makeTeam()
        test_team = self.factory.makeTeam()
        inviting_team = self.factory.makeTeam()
        proposed_team = self.factory.makeTeam()
        with celebrity_logged_in("admin"):
            # Login as a user who can work with all these teams.
            inviting_team.addMember(dupe_team, inviting_team.teamowner)
            proposed_team.addMember(
                dupe_team, dupe_team.teamowner, status=TMS.PROPOSED
            )
            self._do_merge(dupe_team, test_team, test_team.teamowner)
            self.assertEqual(0, inviting_team.invited_member_count)
            self.assertEqual(0, proposed_team.proposed_member_count)

    def test_merge_moves_livefses(self):
        # When person/teams are merged, live filesystems owned by the from
        # person are moved.
        self.useFixture(FeatureFixture({LIVEFS_FEATURE_FLAG: "on"}))
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        self.factory.makeLiveFS(registrant=duplicate, owner=duplicate)
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        self.assertEqual(1, getUtility(ILiveFSSet).getByPerson(mergee).count())

    def test_merge_with_duplicated_livefses(self):
        # If both the from and to people have live filesystems with the same
        # name, merging renames the duplicate from the from person's side.
        self.useFixture(FeatureFixture({LIVEFS_FEATURE_FLAG: "on"}))
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        self.factory.makeLiveFS(
            registrant=duplicate,
            owner=duplicate,
            name="foo",
            metadata={"project": "FROM"},
        )
        self.factory.makeLiveFS(
            registrant=mergee,
            owner=mergee,
            name="foo",
            metadata={"project": "TO"},
        )
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        livefses = getUtility(ILiveFSSet).getByPerson(mergee)
        self.assertEqual(2, livefses.count())
        project_names = [livefs.metadata["project"] for livefs in livefses]
        self.assertEqual(["TO", "FROM"], project_names)
        self.assertEqual("foo-1", livefses[1].name)

    def test_merge_moves_snaps(self):
        # When person/teams are merged, snap packages owned by the from
        # person are moved.
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        self.factory.makeSnap(registrant=duplicate, owner=duplicate)
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        self.assertEqual(1, getUtility(ISnapSet).findByOwner(mergee).count())

    def test_merge_moves_registered_authenticators(self):
        # When people are merged, WebAuthn authenticators registered by the
        # duplicate person are moved, so that they keep working.
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        authenticator = self.factory.makeRegisteredAuthenticator(
            owner=duplicate
        )
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        authenticator_set = getUtility(IRegisteredAuthenticatorSet)
        self.assertContentEqual(
            [authenticator], authenticator_set.findByOwner(mergee)
        )
        self.assertContentEqual([], authenticator_set.findByOwner(duplicate))

    def test_merge_with_duplicated_snaps(self):
        # If both the from and to people have snap packages with the same
        # name, merging renames the duplicate from the from person's side.
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        branch = self.factory.makeAnyBranch()
        [ref] = self.factory.makeGitRefs()
        self.factory.makeSnap(
            registrant=duplicate, owner=duplicate, name="foo", branch=branch
        )
        self.factory.makeSnap(
            registrant=mergee, owner=mergee, name="foo", git_ref=ref
        )
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        snaps = sorted(
            getUtility(ISnapSet).findByOwner(mergee), key=attrgetter("name")
        )
        self.assertEqual(2, len(snaps))
        self.assertIsNone(snaps[0].branch)
        self.assertEqual(ref.repository, snaps[0].git_repository)
        self.assertEqual(ref.path, snaps[0].git_path)
        self.assertEqual("foo", snaps[0].name)
        self.assertEqual(branch, snaps[1].branch)
        self.assertIsNone(snaps[1].git_repository)
        self.assertIsNone(snaps[1].git_path)
        self.assertEqual("foo-1", snaps[1].name)

    def test_merge_snapsubscription(self):
        # Checks that merging users moves subscriptions.
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        snap = removeSecurityProxy(
            self.factory.makeSnap(
                owner=duplicate, registrant=duplicate, name="foo", private=True
            )
        )

        with admin_logged_in():
            # Owner should have being subscribed automatically on creation.
            self.assertTrue(snap.visibleByUser(duplicate))
            self.assertThat(
                snap.getSubscription(duplicate),
                MatchesStructure(snap=Equals(snap), person=Equals(duplicate)),
            )
            self.assertFalse(snap.visibleByUser(mergee))
            self.assertIsNone(snap.getSubscription(mergee))

        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)

        self.assertTrue(snap.visibleByUser(mergee))
        self.assertThat(
            snap.getSubscription(mergee),
            MatchesStructure(snap=Equals(snap), person=Equals(mergee)),
        )
        self.assertFalse(snap.visibleByUser(duplicate))
        self.assertIsNone(snap.getSubscription(duplicate))

    def test_merge_vulnerability_subscriptions(self):
        # Checks that merging users moves subscriptions.
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        vulnerability = self.factory.makeVulnerability()
        vulnerability.subscribe(duplicate, duplicate)
        self.assertTrue(vulnerability.hasSubscription(duplicate))
        self.assertFalse(vulnerability.hasSubscription(mergee))
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        self.assertFalse(vulnerability.hasSubscription(duplicate))
        self.assertTrue(vulnerability.hasSubscription(mergee))

    def test_merge_vulnerability_subscriptions_mergee_already_subscribed(self):
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        vulnerability = self.factory.makeVulnerability()
        vulnerability.subscribe(duplicate, duplicate)
        vulnerability.subscribe(mergee, mergee)
        mergee_subscription = vulnerability.getSubscription(mergee)
        self.assertTrue(vulnerability.hasSubscription(duplicate))
        self.assertTrue(vulnerability.hasSubscription(mergee))
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        self.assertFalse(vulnerability.hasSubscription(duplicate))
        self.assertTrue(vulnerability.hasSubscription(mergee))
        self.assertEqual(
            mergee_subscription, vulnerability.getSubscription(mergee)
        )

    def test_merge_vulnerability_subscriptions_non_public_vulnerability(self):
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        distribution = self.factory.makeDistribution(
            bug_sharing_policy=BugSharingPolicy.PROPRIETARY
        )
        vulnerability = self.factory.makeVulnerability(
            distribution=distribution,
            information_type=InformationType.PROPRIETARY,
        )
        with person_logged_in(distribution.owner):
            vulnerability.subscribe(duplicate, distribution.owner)
            self.assertTrue(vulnerability.hasSubscription(duplicate))
            self.assertFalse(vulnerability.hasSubscription(mergee))

        with person_logged_in(duplicate):
            self.assertTrue(check_permission("launchpad.View", vulnerability))

        with person_logged_in(mergee):
            self.assertFalse(check_permission("launchpad.View", vulnerability))

        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        with person_logged_in(distribution.owner):
            self.assertFalse(vulnerability.hasSubscription(duplicate))
            self.assertTrue(vulnerability.hasSubscription(mergee))

        # Cannot log in as the duplicate user any more to test that they do not
        # have the permission.
        with person_logged_in(mergee):
            self.assertTrue(check_permission("launchpad.View", vulnerability))

    def test_merge_moves_oci_recipes(self):
        # When person/teams are merged, oci recipes owned by the from
        # person are moved.
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        self.factory.makeOCIRecipe(registrant=duplicate, owner=duplicate)
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        self.assertEqual(
            1, getUtility(IOCIRecipeSet).findByOwner(mergee).count()
        )

    def test_merge_with_duplicated_oci_recipes(self):
        # If both the from and to people have oci recipes with the same
        # name, merging renames the duplicate from the from person's side.
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        [ref] = self.factory.makeGitRefs(paths=["refs/heads/v1.0-20.04"])
        [ref2] = self.factory.makeGitRefs(paths=["refs/heads/v1.0-20.04"])
        self.factory.makeOCIRecipe(
            registrant=duplicate, owner=duplicate, name="foo", git_ref=ref
        )
        self.factory.makeOCIRecipe(
            registrant=mergee, owner=mergee, name="foo", git_ref=ref2
        )
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        oci_recipes = sorted(
            getUtility(IOCIRecipeSet).findByOwner(mergee),
            key=attrgetter("name"),
        )
        self.assertEqual(2, len(oci_recipes))
        self.assertEqual(ref2, oci_recipes[0].git_ref)
        self.assertEqual(ref2.repository, oci_recipes[0].git_repository)
        self.assertEqual(ref2.path, oci_recipes[0].git_path)
        self.assertEqual("foo", oci_recipes[0].name)
        self.assertEqual(ref, oci_recipes[1].git_ref)
        self.assertEqual(ref.repository, oci_recipes[1].git_repository)
        self.assertEqual(ref.path, oci_recipes[1].git_path)
        self.assertEqual("foo-1", oci_recipes[1].name)

    def test_merge_moves_charm_recipes(self):
        # When person/teams are merged, charm recipes owned by the from
        # person are moved.
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        self.factory.makeCharmRecipe(registrant=duplicate, owner=duplicate)
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        self.assertEqual(
            1, getUtility(ICharmRecipeSet).findByOwner(mergee).count()
        )

    def test_merge_with_duplicated_charm_recipes(self):
        # If both the from and to people have charm recipes with the same
        # name, merging renames the duplicate from the from person's side.
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        [ref] = self.factory.makeGitRefs()
        [ref2] = self.factory.makeGitRefs()
        self.factory.makeCharmRecipe(
            registrant=duplicate, owner=duplicate, name="foo", git_ref=ref
        )
        self.factory.makeCharmRecipe(
            registrant=mergee, owner=mergee, name="foo", git_ref=ref2
        )
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        recipes = sorted(
            getUtility(ICharmRecipeSet).findByOwner(mergee),
            key=attrgetter("name"),
        )
        self.assertEqual(2, len(recipes))
        self.assertThat(
            recipes,
            MatchesListwise(
                [
                    MatchesStructure.byEquality(
                        git_repository=ref2.repository,
                        git_path=ref2.path,
                        name="foo",
                    ),
                    MatchesStructure.byEquality(
                        git_repository=ref.repository,
                        git_path=ref.path,
                        name="foo-1",
                    ),
                ]
            ),
        )

    def test_merge_moves_rock_recipes(self):
        # When person/teams are merged, rock recipes owned by the from
        # person are moved.
        self.useFixture(FeatureFixture({ROCK_RECIPE_ALLOW_CREATE: "on"}))
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        self.factory.makeRockRecipe(registrant=duplicate, owner=duplicate)
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        self.assertEqual(
            1, getUtility(IRockRecipeSet).findByOwner(mergee).count()
        )

    def test_merge_with_duplicated_rock_recipes(self):
        # If both the from and to people have rock recipes with the same
        # name, merging renames the duplicate from the from person's side.
        self.useFixture(FeatureFixture({ROCK_RECIPE_ALLOW_CREATE: "on"}))
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        [ref] = self.factory.makeGitRefs()
        [ref2] = self.factory.makeGitRefs()
        self.factory.makeRockRecipe(
            registrant=duplicate, owner=duplicate, name="foo", git_ref=ref
        )
        self.factory.makeRockRecipe(
            registrant=mergee, owner=mergee, name="foo", git_ref=ref2
        )
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        recipes = sorted(
            getUtility(IRockRecipeSet).findByOwner(mergee),
            key=attrgetter("name"),
        )
        self.assertEqual(2, len(recipes))
        self.assertThat(
            recipes,
            MatchesListwise(
                [
                    MatchesStructure.byEquality(
                        git_repository=ref2.repository,
                        git_path=ref2.path,
                        name="foo",
                    ),
                    MatchesStructure.byEquality(
                        git_repository=ref.repository,
                        git_path=ref.path,
                        name="foo-1",
                    ),
                ]
            ),
        )

    def test_merge_moves_craft_recipes(self):
        # When person/teams are merged, craft recipes owned by the from
        # person are moved.
        self.useFixture(FeatureFixture({CRAFT_RECIPE_ALLOW_CREATE: "on"}))
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        self.factory.makeCraftRecipe(registrant=duplicate, owner=duplicate)
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        self.assertEqual(
            1, getUtility(ICraftRecipeSet).findByOwner(mergee).count()
        )

    def test_merge_with_duplicated_craft_recipes(self):
        # If both the from and to people have craft recipes with the same
        # name, merging renames the duplicate from the from person's side.
        self.useFixture(FeatureFixture({CRAFT_RECIPE_ALLOW_CREATE: "on"}))
        duplicate = self.factory.makePerson()
        mergee = self.factory.makePerson()
        [ref] = self.factory.makeGitRefs()
        [ref2] = self.factory.makeGitRefs()
        self.factory.makeCraftRecipe(
            registrant=duplicate, owner=duplicate, name="foo", git_ref=ref
        )
        self.factory.makeCraftRecipe(
            registrant=mergee, owner=mergee, name="foo", git_ref=ref2
        )
        self._do_premerge(duplicate, mergee)
        login_person(mergee)
        duplicate, mergee = self._do_merge(duplicate, mergee)
        recipes = sorted(
            getUtility(ICraftRecipeSet).findByOwner(mergee),
            key=attrgetter("name"),
        )
        self.assertEqual(2, len(recipes))
        self.assertThat(
            recipes,
            MatchesListwise(
                [
                    MatchesStructure.byEquality(
                        git_repository=ref2.repository,
                        git_path=ref2.path,
                        name="foo",
                    ),
                    MatchesStructure.byEquality(
                        git_repository=ref.repository,
                        git_path=ref.path,
                        name="foo-1",
                    ),
                ]
            ),
        )


class TestMergeMailingListSubscriptions(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        TestCaseWithFactory.setUp(self)
        # Use the unsecured PersonSet so that private methods can be tested.
        self.from_person = self.factory.makePerson()
        self.to_person = self.factory.makePerson()
        self.cur = cursor()

    def test__mergeMailingListSubscriptions_no_subscriptions(self):
        _mergeMailingListSubscriptions(
            self.cur, self.from_person.id, self.to_person.id
        )
        self.assertEqual(0, self.cur.rowcount)

    def test__mergeMailingListSubscriptions_with_subscriptions(self):
        naked_person = removeSecurityProxy(self.from_person)
        naked_person.mailing_list_auto_subscribe_policy = (
            MailingListAutoSubscribePolicy.ALWAYS
        )
        self.team, self.mailing_list = self.factory.makeTeamAndMailingList(
            "test-mailinglist", "team-owner"
        )
        with person_logged_in(self.team.teamowner):
            self.team.addMember(self.from_person, reviewer=self.team.teamowner)
        transaction.commit()
        _mergeMailingListSubscriptions(
            self.cur, self.from_person.id, self.to_person.id
        )
        self.assertEqual(1, self.cur.rowcount)
