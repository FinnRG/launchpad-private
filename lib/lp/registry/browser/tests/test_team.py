# Copyright 2010-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from datetime import datetime, timedelta

import soupmatchers
import transaction
from testtools.matchers import Not
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.oci.interfaces.ocirecipe import OCI_RECIPE_ALLOW_CREATE
from lp.registry.browser.team import TeamIndexMenu, TeamOverviewMenu
from lp.registry.enums import (
    EXCLUSIVE_TEAM_POLICY,
    INCLUSIVE_TEAM_POLICY,
    PersonVisibility,
    TeamMembershipPolicy,
    TeamMembershipRenewalPolicy,
)
from lp.registry.interfaces.mailinglist import MailingListStatus
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.persontransferjob import IPersonMergeJobSource
from lp.registry.interfaces.teammembership import (
    ITeamMembershipSet,
    TeamMembershipStatus,
)
from lp.services.features.testing import FeatureFixture
from lp.services.propertycache import get_property_cache
from lp.services.webapp.authorization import check_permission
from lp.services.webapp.escaping import html_escape
from lp.services.webapp.login import OpenIDCallbackView, isFreshLogin
from lp.services.webapp.publisher import canonical_url
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.soyuz.enums import ArchiveStatus
from lp.testing import (
    ANONYMOUS,
    TestCaseWithFactory,
    admin_logged_in,
    login,
    login_celebrity,
    login_person,
    monkey_patch,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.menu import check_menu_links
from lp.testing.pages import extract_text, find_tag_by_id
from lp.testing.views import create_initialized_view, create_view


class TestProposedTeamMembersEditView(TestCaseWithFactory):
    """Tests for ProposedTeamMembersEditView."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.owner = self.factory.makePerson(name="team-owner")
        self.a_team = self.makeTeam("team-a", "A-Team")
        self.b_team = self.makeTeam("team-b", "B-Team")
        transaction.commit()
        login_person(self.owner)

    def makeTeam(self, name, displayname):
        """Make a moderated team."""
        return self.factory.makeTeam(
            name=name,
            owner=self.owner,
            displayname=displayname,
            membership_policy=TeamMembershipPolicy.MODERATED,
        )

    def inviteToJoin(self, joinee, joiner):
        """Invite the joiner team into the joinee team."""
        # Joiner is proposed to join joinee.
        form = {
            "field.teams": joiner.name,
            "field.actions.continue": "Continue",
        }
        view = create_initialized_view(joinee, "+add-my-teams", form=form)
        self.assertEqual([], view.errors)
        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        expected = "%s has been proposed to this team." % (joiner.displayname)
        self.assertEqual(expected, notifications[0].message)

    def acceptTeam(self, joinee, successful, failed):
        """Accept the teams into the joinee team.

        The teams in 'successful' are expected to be allowed.
        The teams in 'failed' are expected to fail.
        """
        failed_names = ", ".join([team.displayname for team in failed])
        if len(failed) == 1:
            failed_message = html_escape(
                "%s is a member of the following team, "
                "so it could not be accepted:  %s.  "
                'You need to "Decline" that team.'
                % (joinee.displayname, failed_names)
            )
        else:
            failed_message = html_escape(
                "%s is a member of the following teams, "
                "so they could not be accepted:  %s.  "
                'You need to "Decline" those teams.'
                % (joinee.displayname, failed_names)
            )

        form = {
            "field.actions.save": "Save changes",
        }
        for team in successful + failed:
            # Construct the team selection field, based on the id of the
            # team.
            selector = "action_%d" % team.id
            form[selector] = "approve"

        view = create_initialized_view(
            joinee, "+editproposedmembers", form=form
        )
        self.assertEqual([], view.errors)
        notifications = view.request.response.notifications
        if len(failed) == 0:
            self.assertEqual(0, len(notifications))
        else:
            self.assertEqual(1, len(notifications))
            self.assertEqual(failed_message, notifications[0].message)

    def test_circular_proposal_acceptance(self):
        """Two teams can invite each other without horrifying results."""

        # Make the criss-cross invitations.

        # Owner proposes Team B join Team A.
        self.inviteToJoin(self.a_team, self.b_team)

        # Owner proposes Team A join Team B.
        self.inviteToJoin(self.b_team, self.a_team)

        # Accept Team B into Team A.
        self.acceptTeam(self.a_team, successful=(self.b_team,), failed=())

        # Accept Team A into Team B, and fail trying.
        self.acceptTeam(self.b_team, successful=(), failed=(self.a_team,))

    def test_circular_proposal_acceptance_with_some_noncircular(self):
        """Accepting a mix of successful and failed teams works."""
        # Create some extra teams.
        self.c_team = self.makeTeam("team-c", "C-Team")
        self.d_team = self.makeTeam("team-d", "D-Team")
        self.super_team = self.makeTeam("super-team", "Super Team")

        # Everyone wants to join Super Team.
        for team in [self.a_team, self.b_team, self.c_team, self.d_team]:
            self.inviteToJoin(self.super_team, team)

        # Super Team joins two teams.
        for team in [self.a_team, self.b_team]:
            self.inviteToJoin(team, self.super_team)

        # Super Team is accepted into both.
        for team in [self.a_team, self.b_team]:
            self.acceptTeam(team, successful=(self.super_team,), failed=())

        # Now Super Team attempts to accept all teams.  Two succeed but the
        # two with that would cause a cycle fail.
        failed = (self.a_team, self.b_team)
        successful = (self.c_team, self.d_team)
        self.acceptTeam(self.super_team, successful, failed)


class TestTeamPersonRenameFormMixin:
    view_name = None

    def test_cannot_rename_team_with_active_ppa(self):
        # A team with an active PPA that contains publications cannot be
        # renamed.
        owner = self.factory.makePerson()
        team = self.factory.makeTeam(owner=owner)
        archive = self.factory.makeArchive(owner=team)
        self.factory.makeSourcePackagePublishingHistory(archive=archive)
        get_property_cache(team).archive = archive
        with person_logged_in(owner):
            view = create_initialized_view(team, name=self.view_name)
            self.assertTrue(view.form_fields["name"].for_display)
            self.assertEqual(
                "This team has an active PPA with packages published and "
                "may not be renamed.",
                view.widgets["name"].hint,
            )

    def test_can_rename_team_with_deleted_ppa(self):
        # A team with a deleted PPA can be renamed.
        owner = self.factory.makePerson()
        team = self.factory.makeTeam(owner=owner)
        archive = self.factory.makeArchive()
        self.factory.makeSourcePackagePublishingHistory(archive=archive)
        removeSecurityProxy(archive).status = ArchiveStatus.DELETED
        get_property_cache(team).archive = archive
        with person_logged_in(owner):
            view = create_initialized_view(team, name=self.view_name)
            self.assertFalse(view.form_fields["name"].for_display)

    def test_can_rename_team_with_purged_mailinglist(self):
        # A team with a mailing list which is purged can be renamed.
        owner = self.factory.makePerson()
        team = self.factory.makeTeam(owner=owner)
        team_list = self.factory.makeMailingList(team, owner)
        team_list.deactivate()
        team_list.transitionToStatus(MailingListStatus.INACTIVE)
        team_list.purge()
        with person_logged_in(owner):
            view = create_initialized_view(team, name=self.view_name)
            self.assertFalse(view.form_fields["name"].for_display)


class TestTeamEditView(TestTeamPersonRenameFormMixin, TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer
    view_name = "+edit"

    def test_edit_team_view_permission(self):
        # Only an administrator or the team owner of a team can
        # change the details of that team.
        person = self.factory.makePerson()
        owner = self.factory.makePerson()
        team = self.factory.makeTeam(owner=owner)
        view = create_view(team, "+edit")
        login_person(person)
        self.assertFalse(check_permission("launchpad.Edit", view))
        login_person(owner)
        self.assertTrue(check_permission("launchpad.Edit", view))

    def test_edit_team_view_data(self):
        # The edit view renders the team's details correctly.
        owner = self.factory.makePerson()
        team = self.factory.makeTeam(
            name="team",
            displayname="A Team",
            description="A great team",
            owner=owner,
            membership_policy=TeamMembershipPolicy.MODERATED,
        )
        with person_logged_in(owner):
            view = create_initialized_view(team, name="+edit")
            self.assertEqual("team", view.widgets["name"]._data)
            self.assertEqual("A Team", view.widgets["display_name"]._data)
            self.assertEqual("A great team", view.widgets["description"]._data)
            self.assertEqual(
                TeamMembershipPolicy.MODERATED,
                view.widgets["membership_policy"]._data,
            )
            self.assertEqual(
                TeamMembershipPolicy,
                view.widgets["membership_policy"].vocabulary,
            )
            self.assertIsNone(view.widgets["membership_policy"].extra_hint)
            self.assertEqual(
                TeamMembershipRenewalPolicy.NONE,
                view.widgets["renewal_policy"]._data,
            )
            self.assertIsNone(view.widgets["defaultrenewalperiod"]._data)

    def _test_edit_team_view_expected_subscription_vocab(
        self, fn_setup, expected_items
    ):
        # The edit view renders only the specified policy choices when
        # the setup performed by fn_setup occurs.
        owner = self.factory.makePerson()
        team = self.factory.makeTeam(
            owner=owner, membership_policy=TeamMembershipPolicy.MODERATED
        )
        fn_setup(team)
        with person_logged_in(owner):
            view = create_initialized_view(team, name="+edit")
            self.assertContentEqual(
                expected_items,
                [
                    term.value
                    for term in view.widgets["membership_policy"].vocabulary
                ],
            )
            self.assertEqual(
                "sprite info",
                view.widgets["membership_policy"].extra_hint_class,
            )
            self.assertIsNotNone(view.widgets["membership_policy"].extra_hint)

    def test_edit_team_view_pillar_owner(self):
        # The edit view renders only closed membership policy choices when
        # the team is a pillar owner.

        def setup_team(team):
            self.factory.makeProduct(owner=team)

        self._test_edit_team_view_expected_subscription_vocab(
            setup_team, EXCLUSIVE_TEAM_POLICY
        )

    def test_edit_team_view_has_ppas(self):
        # The edit view renders only closed membership policy choices when
        # the team has any ppas.

        def setup_team(team):
            with person_logged_in(team.teamowner):
                team.createPPA()

        self._test_edit_team_view_expected_subscription_vocab(
            setup_team, EXCLUSIVE_TEAM_POLICY
        )

    def test_edit_team_view_has_closed_super_team(self):
        # The edit view renders only closed membership policy choices when
        # the team has any closed super teams.

        def setup_team(team):
            super_team = self.factory.makeTeam(
                owner=team.teamowner,
                membership_policy=TeamMembershipPolicy.RESTRICTED,
            )
            with person_logged_in(team.teamowner):
                super_team.addMember(team, team.teamowner, force_team_add=True)

        self._test_edit_team_view_expected_subscription_vocab(
            setup_team, EXCLUSIVE_TEAM_POLICY
        )

    def test_edit_team_view_subscribed_private_bug(self):
        # The edit view renders only closed membership policy choices when
        # the team is subscribed to a private bug.

        def setup_team(team):
            bug = self.factory.makeBug(
                owner=team.teamowner, information_type=InformationType.USERDATA
            )
            with person_logged_in(team.teamowner):
                bug.default_bugtask.transitionToAssignee(team)

        self._test_edit_team_view_expected_subscription_vocab(
            setup_team, EXCLUSIVE_TEAM_POLICY
        )

    def test_edit_team_view_has_open_member(self):
        # The edit view renders open closed membership policy choices when
        # the team has any open sub teams.

        def setup_team(team):
            team_member = self.factory.makeTeam(
                owner=team.teamowner,
                membership_policy=TeamMembershipPolicy.DELEGATED,
            )
            with person_logged_in(team.teamowner):
                team.addMember(
                    team_member, team.teamowner, force_team_add=True
                )

        self._test_edit_team_view_expected_subscription_vocab(
            setup_team, INCLUSIVE_TEAM_POLICY
        )

    def test_edit_team_view_save(self):
        # A team can be edited and saved, including a name change, even if it
        # is a private team and has a purged mailing list.
        owner = self.factory.makePerson()
        team = self.factory.makeTeam(
            name="team",
            displayname="A Team",
            description="A great team",
            owner=owner,
            visibility=PersonVisibility.PRIVATE,
            membership_policy=TeamMembershipPolicy.MODERATED,
        )

        with person_logged_in(owner):
            team_list = self.factory.makeMailingList(team, owner)
            team_list.deactivate()
            team_list.transitionToStatus(MailingListStatus.INACTIVE)
            team_list.purge()
            url = canonical_url(team)
        browser = self.getUserBrowser(url, user=owner)
        browser.getLink("Change details").click()
        browser.getControl("Name", index=0).value = "ubuntuteam"
        browser.getControl("Display Name").value = "Ubuntu Team"
        browser.getControl("Description").value = ""
        browser.getControl("Restricted Team").selected = True
        browser.getControl("Save").click()

        # We're now redirected to the team's home page, which is now on a
        # different URL since we changed its name.
        self.assertEqual("http://launchpad.test/~ubuntuteam", browser.url)

        # Check the values again.
        browser.getLink("Change details").click()
        self.assertEqual(
            "ubuntuteam", browser.getControl("Name", index=0).value
        )
        self.assertEqual(
            "Ubuntu Team", browser.getControl("Display Name", index=0).value
        )
        self.assertEqual("", browser.getControl("Description", index=0).value)
        self.assertTrue(
            browser.getControl("Restricted Team", index=0).selected
        )

    def test_team_name_already_used(self):
        # If we try to use a name which is already in use, we'll get an error
        # message explaining it.

        self.factory.makeTeam(name="existing")
        owner = self.factory.makePerson()
        team = self.factory.makeTeam(name="team", owner=owner)

        form = {
            "field.name": "existing",
            "field.actions.save": "Save",
        }
        login_person(owner)
        view = create_initialized_view(team, "+edit", form=form)
        self.assertEqual(1, len(view.errors))
        self.assertEqual(
            "existing is already in use by another person or team.",
            view.errors[0].doc(),
        )

    def test_expiration_and_renewal(self):
        # The team's membership expiration and renewal rules can be set.
        owner = self.factory.makePerson()
        team = self.factory.makeTeam(name="team", owner=owner)
        form = {
            "field.name": team.name,
            "field.display_name": team.display_name,
            "field.defaultmembershipperiod": "180",
            "field.defaultrenewalperiod": "365",
            "field.membership_policy": "RESTRICTED",
            "field.renewal_policy": "ONDEMAND",
            "field.actions.save": "Save",
        }
        login_person(owner)
        view = create_initialized_view(team, "+edit", form=form)
        self.assertEqual(0, len(view.errors))
        self.assertEqual(
            TeamMembershipPolicy.RESTRICTED, team.membership_policy
        )
        self.assertEqual(180, team.defaultmembershipperiod)
        self.assertEqual(365, team.defaultrenewalperiod)
        self.assertEqual(
            TeamMembershipRenewalPolicy.ONDEMAND, team.renewal_policy
        )


class TestTeamAddView(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer
    view_name = "+newteam"

    def test_team_creation_good_data(self):
        person = self.factory.makePerson()
        form = {
            "field.actions.create": "Create Team",
            "field.display_name": "liberty-land",
            "field.name": "libertyland",
            "field.renewal_policy": "NONE",
            "field.renewal_policy-empty-marker": 1,
            "field.membership_policy": "RESTRICTED",
            "field.membership_policy-empty-marker": 1,
        }
        login_person(person)
        person_set = getUtility(IPersonSet)
        create_initialized_view(person_set, self.view_name, form=form)
        team = person_set.getByName("libertyland")
        self.assertTrue(team is not None)
        self.assertEqual("libertyland", team.name)

    def test_random_does_not_see_visibility_field(self):
        personset = getUtility(IPersonSet)
        person = self.factory.makePerson()
        view = create_initialized_view(
            personset, name=self.view_name, principal=person
        )
        self.assertNotIn(
            "visibility", [field.__name__ for field in view.form_fields]
        )

    def test_admin_sees_visibility_field(self):
        personset = getUtility(IPersonSet)
        admin = login_celebrity("admin")
        view = create_initialized_view(
            personset, name=self.view_name, principal=admin
        )
        self.assertIn(
            "visibility", [field.__name__ for field in view.form_fields]
        )

    def test_person_with_cs_sees_visibility_field(self):
        personset = getUtility(IPersonSet)
        team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.MODERATED
        )
        self.factory.grantCommercialSubscription(team)
        with person_logged_in(team.teamowner):
            view = create_initialized_view(
                personset, name=self.view_name, principal=team.teamowner
            )
            self.assertIn(
                "visibility", [field.__name__ for field in view.form_fields]
            )

    def test_person_with_cs_can_create_private_team(self):
        personset = getUtility(IPersonSet)
        team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.MODERATED
        )
        self.factory.grantCommercialSubscription(team)
        team_name = self.factory.getUniqueString()
        form = {
            "field.name": team_name,
            "field.display_name": "New Team",
            "field.membership_policy": "RESTRICTED",
            "field.visibility": "PRIVATE",
            "field.actions.create": "Create",
        }
        with person_logged_in(team.teamowner):
            create_initialized_view(
                personset,
                name=self.view_name,
                principal=team.teamowner,
                form=form,
            )
            team = personset.getByName(team_name)
            self.assertIsNotNone(team)
            self.assertEqual(PersonVisibility.PRIVATE, team.visibility)

    def test_person_with_expired_cs_does_not_see_visibility(self):
        personset = getUtility(IPersonSet)
        team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.MODERATED
        )
        product = self.factory.makeProduct(owner=team)
        self.factory.makeCommercialSubscription(product, expired=True)
        with person_logged_in(team.teamowner):
            view = create_initialized_view(
                personset, name=self.view_name, principal=team.teamowner
            )
            self.assertNotIn(
                "visibility", [field.__name__ for field in view.form_fields]
            )

    def test_visibility_is_correct_during_edit(self):
        owner = self.factory.makePerson()
        team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.RESTRICTED,
            visibility=PersonVisibility.PRIVATE,
            owner=owner,
        )
        product = self.factory.makeProduct(owner=owner)
        self.factory.makeCommercialSubscription(product)
        with person_logged_in(owner):
            url = canonical_url(team)
        browser = self.getUserBrowser(url, user=owner)
        browser.getLink("Change details").click()
        self.assertEqual(
            ["PRIVATE"], browser.getControl(name="field.visibility").value
        )


class TestSimpleTeamAddView(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer
    view_name = "+simplenewteam"

    def test_create_team(self):
        personset = getUtility(IPersonSet)
        team_name = self.factory.getUniqueString()
        form = {
            "field.name": team_name,
            "field.display_name": "New Team",
            "field.visibility": "PRIVATE",
            "field.membership_policy": "RESTRICTED",
            "field.actions.create": "Create",
        }
        login_celebrity("admin")
        create_initialized_view(personset, name=self.view_name, form=form)
        team = personset.getByName(team_name)
        self.assertIsNotNone(team)
        self.assertEqual("New Team", team.displayname)
        self.assertEqual(PersonVisibility.PRIVATE, team.visibility)
        self.assertEqual(
            TeamMembershipPolicy.RESTRICTED, team.membership_policy
        )


class TestTeamMenu(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.team = self.factory.makeTeam()

    def test_TeamIndexMenu(self):
        view = create_view(self.team, "+index")
        menu = TeamIndexMenu(view)
        self.assertEqual(
            ("edit", "administer", "delete", "join", "add_my_teams", "leave"),
            menu.links,
        )

    def test_TeamIndexMenu_anonymous(self):
        view = create_view(self.team, "+index")
        menu = TeamIndexMenu(view)
        self.assertEqual(
            ["join", "add_my_teams"],
            [link.name for link in menu.iterlinks() if link.enabled],
        )

    def test_TeamIndexMenu_owner(self):
        login_person(self.team.teamowner)
        view = create_view(self.team, "+index")
        menu = TeamIndexMenu(view)
        self.assertEqual(
            ["edit", "delete", "add_my_teams"],
            [link.name for link in menu.iterlinks() if link.enabled],
        )

    def test_TeamIndexMenu_admin(self):
        login_celebrity("admin")
        view = create_view(self.team, "+index")
        menu = TeamIndexMenu(view)
        self.assertEqual(
            ["edit", "administer", "delete", "join", "add_my_teams"],
            [link.name for link in menu.iterlinks() if link.enabled],
        )

    def test_TeamIndexMenu_registry_experts(self):
        login_celebrity("registry_experts")
        view = create_view(self.team, "+index")
        menu = TeamIndexMenu(view)
        self.assertEqual(
            ["administer", "delete", "join", "add_my_teams"],
            [link.name for link in menu.iterlinks() if link.enabled],
        )

    def test_TeamOverviewMenu_check_menu_links_without_mailing(self):
        menu = TeamOverviewMenu(self.team)
        # Remove moderate_mailing_list because it asserts that there is
        # a mailing list.
        no_mailinst_list_links = [
            link for link in menu.links if link != "moderate_mailing_list"
        ]
        menu.links = no_mailinst_list_links
        self.assertIs(True, check_menu_links(menu))
        link = menu.configure_mailing_list()
        self.assertEqual("Read more about it here.", link.text)

    def test_TeamOverviewMenu_new_user_without_add_poll(self):
        # A brand new user does not see the add_poll link.
        self.pushConfig(
            "launchpad", min_legitimate_karma=5, min_legitimate_account_age=5
        )
        login_person(self.team.teamowner)
        menu = TeamOverviewMenu(self.team)
        self.assertNotIn(
            "add_poll",
            [link.name for link in menu.iterlinks() if link.enabled],
        )

    def test_TeamOverviewMenu_legitimate_user_with_add_poll(self):
        # A user with some kind of track record sees the add_poll link.
        self.pushConfig(
            "launchpad", min_legitimate_karma=5, min_legitimate_account_age=5
        )
        team = self.factory.makeTeam(owner=self.factory.makePerson(karma=10))
        login_person(team.teamowner)
        menu = TeamOverviewMenu(team)
        self.assertIn(
            "add_poll",
            [link.name for link in menu.iterlinks() if link.enabled],
        )


class TestTeamMemberAddView(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.team = self.factory.makeTeam(name="test-team")
        login_person(self.team.teamowner)

    def getForm(self, new_member):
        return {
            "field.newmember": new_member.name,
            "field.actions.add": "Add Member",
        }

    def test_add_member_success(self):
        member = self.factory.makePerson(name="a-member")
        form = self.getForm(member)
        view = create_initialized_view(self.team, "+addmember", form=form)
        self.assertEqual([], view.errors)
        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        self.assertEqual(
            "A-member (a-member) has been added as a member of this team.",
            notifications[0].message,
        )
        self.assertTrue(member.inTeam(self.team))
        self.assertEqual(None, view.widgets["newmember"]._getCurrentValue())

    def test_add_private_team_member_success(self):
        member = self.factory.makeTeam(
            name="a-member",
            owner=self.team.teamowner,
            visibility=PersonVisibility.PRIVATE,
        )
        form = self.getForm(member)
        view = create_initialized_view(self.team, "+addmember", form=form)
        self.assertEqual([], view.errors)
        self.assertTrue(member.inTeam(self.team))

    def test_add_former_member_success(self):
        member = self.factory.makePerson(name="a-member")
        self.team.addMember(member, self.team.teamowner)
        with person_logged_in(member):
            member.leave(self.team)
        form = self.getForm(member)
        view = create_initialized_view(self.team, "+addmember", form=form)
        self.assertEqual([], view.errors)
        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        self.assertEqual(
            "A-member (a-member) has been added as a member of this team.",
            notifications[0].message,
        )
        self.assertTrue(member.inTeam(self.team))

    def test_add_existing_member_fail(self):
        member = self.factory.makePerson(name="a-member")
        self.team.addMember(member, self.team.teamowner)
        form = self.getForm(member)
        view = create_initialized_view(self.team, "+addmember", form=form)
        self.assertEqual(1, len(view.errors))
        self.assertEqual(
            "A-member (a-member) is already a member of Test Team.",
            view.errors[0],
        )

    def test_add_empty_team_fail(self):
        empty_team = self.factory.makeTeam(owner=self.team.teamowner)
        self.team.teamowner.leave(empty_team)
        form = self.getForm(empty_team)
        view = create_initialized_view(self.team, "+addmember", form=form)
        self.assertEqual(1, len(view.errors))
        self.assertEqual(
            html_escape(
                "You can't add a team that doesn't have any active members."
            ),
            view.errors[0],
        )

    def test_no_TeamMembershipTransitionError(self):
        # Attempting to add a team never triggers a
        # TeamMembershipTransitionError
        member_team = self.factory.makeTeam()
        self.team.addMember(member_team, self.team.teamowner)
        tm = getUtility(ITeamMembershipSet).getByPersonAndTeam(
            member_team, self.team
        )
        for status in TeamMembershipStatus.items:
            removeSecurityProxy(tm).status = status
            view = create_initialized_view(self.team, "+addmember")
            view.add_action.success(data={"newmember": member_team})


class TeamMembershipViewTestCase(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_init(self):
        team = self.factory.makeTeam(name="pting")
        view = create_initialized_view(team, name="+members")
        self.assertEqual("Members", view.page_title)
        self.assertEqual("Members of \u201cPting\u201d", view.label)


class TestTeamIndexView(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.team = self.factory.makeTeam(name="test-team")
        login_person(self.team.teamowner)

    def test_add_member_step_title(self):
        view = create_initialized_view(self.team, "+index")
        self.assertEqual("Search", view.add_member_step_title)

    def test_isMergePending(self):
        target_team = self.factory.makeTeam()
        job_source = getUtility(IPersonMergeJobSource)
        job_source.create(
            from_person=self.team,
            to_person=target_team,
            reviewer=target_team.teamowner,
            requester=target_team.teamowner,
        )
        view = create_initialized_view(self.team, name="+index")
        notifications = view.request.response.notifications
        message = (
            "Test Team is queued to be merged or deleted " "in a few minutes."
        )
        self.assertEqual(1, len(notifications))
        self.assertEqual(message, notifications[0].message)

    def test_user_without_launchpad_view(self):
        # When the user does not have launchpad.View on the context,
        user = self.factory.makePerson()
        owner = self.factory.makePerson()
        with person_logged_in(owner):
            team = self.factory.makeTeam(
                displayname="Waffles",
                owner=owner,
                visibility=PersonVisibility.PRIVATE,
            )
            archive = self.factory.makeArchive(private=True, owner=team)
            archive.newSubscription(user, registrant=owner)
        with person_logged_in(user):
            for rootsite, view_name in [
                (None, "+index"),
                ("code", "+branches"),
                ("bugs", "+bugs"),
                ("blueprints", "+specs"),
                ("answers", "+questions"),
                ("translations", "+translations"),
            ]:
                view = create_initialized_view(
                    team,
                    name=view_name,
                    path_info="",
                    principal=user,
                    server_url=canonical_url(team, rootsite=rootsite),
                    rootsite=rootsite,
                )
                document = find_tag_by_id(view(), "document")
                self.assertIsNone(document.find(True, id="side-portlets"))
                self.assertIsNone(document.find(True, id="registration"))
                self.assertEndsWith(
                    extract_text(document.find(True, id="maincontent")),
                    "The information in this page is not shared with you.",
                )

    @staticmethod
    def get_markup(view, team):
        def fake_method():
            return canonical_url(team)

        with monkey_patch(view, _getURL=fake_method):
            markup = view.render()
        return markup

    def test_show_oci_registry_credentials_link(self):
        member = self.factory.makePerson()
        team = self.factory.makeTeam(members=[member])
        view = create_initialized_view(team, "+index", principal=member)
        with person_logged_in(member):
            markup = self.get_markup(view, team)
        link_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "OCIRegistryCredentials link",
                "a",
                attrs={
                    "href": (
                        "http://launchpad.test/~%s/+oci-registry-credentials"
                        % team.name
                    )
                },
                text="OCI registry credentials",
            )
        )
        self.assertThat(markup, link_match)

        login_person(self.factory.makePerson())
        markup = self.get_markup(view, team)
        self.assertNotEqual("", markup)
        self.assertThat(markup, Not(link_match))

        login(ANONYMOUS)
        markup = self.get_markup(view, team)
        self.assertNotEqual("", markup)
        self.assertThat(markup, Not(link_match))

    def test_show_oci_recipes_link(self):
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        member = self.factory.makePerson()
        team = self.factory.makeTeam(owner=member, members=[member])

        # Creates a recipe, so the link appears.
        self.factory.makeOCIRecipe(owner=team, registrant=member)
        with person_logged_in(member):
            expected_url = "http://launchpad.test/~%s/+oci-recipes" % team.name
            team_url = canonical_url(team)
        browser = self.getUserBrowser(team_url, user=member)

        link_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "OCI recipes link",
                "a",
                attrs={"href": expected_url},
                text="View OCI recipes",
            )
        )
        self.assertThat(browser.contents, link_match)

        browser = self.getUserBrowser(team_url, user=None)
        self.assertThat(browser.contents, link_match)

    def test_shows_oci_recipes_link_if_user_doesnt_have_oci_recipes(self):
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        member = self.factory.makePerson()
        team = self.factory.makeTeam(owner=member, members=[member])

        # Creates a recipe from another user, just to make sure it will not
        # interfere.
        self.factory.makeOCIRecipe()

        with person_logged_in(member):
            expected_url = "http://launchpad.test/~%s/+oci-recipes" % team.name
            team_url = canonical_url(team)
        browser = self.getUserBrowser(team_url, user=member)

        link_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "OCI recipes link",
                "a",
                attrs={"href": expected_url},
                text="View OCI recipes",
            )
        )
        self.assertThat(browser.contents, link_match)

        browser = self.getUserBrowser(team_url, user=None)
        self.assertThat(browser.contents, link_match)


class TestPersonIndexVisibilityView(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def createTeams(self):
        team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.MODERATED
        )
        private = self.factory.makeTeam(
            visibility=PersonVisibility.PRIVATE,
            name="private-team",
            members=[team],
        )
        with person_logged_in(team.teamowner):
            team.acceptInvitationToBeMemberOf(private, "")
        return team

    def test_private_superteams_anonymous(self):
        # If the viewer is anonymous, the portlet is not shown.
        team = self.createTeams()
        self.factory.makePerson()
        view = create_initialized_view(
            team, "+index", server_url=canonical_url(team), path_info=""
        )
        html = view()
        superteams = find_tag_by_id(html, "subteam-of")
        self.assertIs(None, superteams)
        self.assertEqual([], view.super_teams)

    def test_private_superteams_hidden(self):
        # If the viewer has no permission to see any superteams, the portlet
        # is not shown.
        team = self.createTeams()
        viewer = self.factory.makePerson()
        with person_logged_in(viewer):
            view = create_initialized_view(
                team,
                "+index",
                server_url=canonical_url(team),
                path_info="",
                principal=viewer,
            )
            html = view()
            self.assertEqual([], view.super_teams)
            superteams = find_tag_by_id(html, "subteam-of")
        self.assertIs(None, superteams)

    def test_private_superteams_shown(self):
        # When the viewer has permission, the portlet is shown.
        team = self.createTeams()
        with person_logged_in(team.teamowner):
            view = create_initialized_view(
                team,
                "+index",
                server_url=canonical_url(team),
                path_info="",
                principal=team.teamowner,
            )
            html = view()
            self.assertEqual(view.super_teams, list(team.super_teams))
            superteams = find_tag_by_id(html, "subteam-of")
        self.assertFalse("&lt;hidden&gt;" in superteams)
        self.assertThat(
            superteams,
            soupmatchers.Tag(
                "private team link",
                "a",
                attrs={
                    "href": "/~private-team",
                    "class": "sprite team private",
                },
                text="Private Team",
            ),
        )


class TestTeamContactAddressView(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_team_change_contact_address_to_existing_address(self):
        # Test that the error message about changing email to an existing
        # one doesn't break when there are non-ASCII characters in the message.
        someone_email = "someone@canonical.com"
        someone = self.factory.makePerson(
            displayname="Unicode Person \xc9", email=someone_email
        )
        someone_url = canonical_url(someone)
        team = self.factory.makeTeam(email="team@canonical.com")
        with admin_logged_in():
            form = {
                "field.contact_method": "EXTERNAL_ADDRESS",
                "field.contact_address": "someone@canonical.com",
                "field.actions.change": "Change",
            }
            view = create_initialized_view(team, "+contactaddress", form=form)
            expected_msg = (
                "%s is already registered in Launchpad and is associated "
                'with <a href="%s">Unicode Person \xc9</a>.'
            )
            expected_msg %= (someone_email, someone_url)
            self.assertEqual([expected_msg], view.errors)


class TestTeamLeaveView(TestCaseWithFactory):
    """Tests for TeamLeaveView.

    Regression tests for bug #1793647: leaving a team must require a
    fresh (recently re-authenticated) login, just like other
    security-sensitive account actions such as +editsshkeys,
    +editpgpkeys and +editemails. A team can be used to scope a
    security-sensitive feature flag (for example, to enrol a cohort of
    users in mandatory two-factor authentication), so a member must not
    be able to opt themselves out of such a requirement using a session
    that was authenticated a long time ago.
    """

    layer = DatabaseFunctionalLayer

    def _loginWithStaleSession(self, user, request):
        """Log in on `request`'s session as though it happened long ago.

        `isFreshLogin` treats a login older than 120 seconds as stale,
        which is exactly the state of an existing browser session that
        was authenticated well before the current request.
        """
        with person_logged_in(user):
            OpenIDCallbackView(user, request).login(
                user, datetime.utcnow() - timedelta(seconds=180)
            )

    def _makeTeamAndMember(self):
        team = self.factory.makeTeam(name="two-factor-testers")
        member = self.factory.makePerson()
        with person_logged_in(team.teamowner):
            team.addMember(member, reviewer=team.teamowner)
        transaction.commit()
        return team, member

    def test_leave_requires_fresh_login(self):
        # A member with a stale (non-fresh) login is redirected to
        # reauthenticate, and does not leave the team, just as a stale
        # login blocks +editsshkeys (see
        # test_editsshkeys_requires_fresh_login below).
        team, member = self._makeTeamAndMember()

        request = LaunchpadTestRequest(
            method="POST", form={"field.actions.leave": "Leave the Team"}
        )
        self._loginWithStaleSession(member, request)
        self.assertFalse(isFreshLogin(request))

        with person_logged_in(member):
            create_initialized_view(
                team, "+leave", request=request, principal=member
            )

        self.assertEqual(302, request.response.getStatus())
        self.assertIn(
            "+login?reauth=1", request.response.getHeader("Location")
        )
        self.assertTrue(member.inTeam(team))

    def test_leave_succeeds_with_fresh_login(self):
        # A member with a fresh login can still leave the team normally.
        team, member = self._makeTeamAndMember()

        request = LaunchpadTestRequest(
            method="POST", form={"field.actions.leave": "Leave the Team"}
        )
        with person_logged_in(member):
            OpenIDCallbackView(member, request).login(member)
        self.assertTrue(isFreshLogin(request))

        with person_logged_in(member):
            view = create_initialized_view(
                team, "+leave", request=request, principal=member
            )

        self.assertEqual([], view.errors)
        self.assertFalse(member.inTeam(team))

    def test_editsshkeys_requires_fresh_login(self):
        # Contrast with test_leave_requires_fresh_login: an equally
        # stale login is redirected to reauthenticate before
        # +editsshkeys does anything, because that view calls
        # require_fresh_login.
        user = self.factory.makePerson()
        request = LaunchpadTestRequest(method="GET")
        self._loginWithStaleSession(user, request)
        self.assertFalse(isFreshLogin(request))

        with person_logged_in(user):
            create_initialized_view(
                user, "+editsshkeys", request=request, principal=user
            )

        self.assertEqual(302, request.response.getStatus())
        self.assertIn(
            "+login?reauth=1", request.response.getHeader("Location")
        )
