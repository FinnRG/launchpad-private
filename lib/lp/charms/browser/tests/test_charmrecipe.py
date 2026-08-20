# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test charm recipe views."""

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import responses
import soupmatchers
import transaction
from fixtures import FakeLogger
from nacl.public import PrivateKey
from pymacaroons import Macaroon
from pymacaroons.serializers import JsonSerializer
from testtools.matchers import (
    AfterPreprocessing,
    ContainsDict,
    Equals,
    Is,
    MatchesDict,
    MatchesListwise,
    MatchesStructure,
)
from zope.component import getUtility
from zope.publisher.interfaces import NotFound
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy
from zope.testbrowser.browser import LinkNotFoundError

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.buildmaster.builderproxy import FetchServicePolicy
from lp.buildmaster.enums import BuildStatus
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.charms.browser.charmrecipe import (
    CharmRecipeAdminView,
    CharmRecipeEditView,
    CharmRecipeView,
)
from lp.charms.interfaces.charmrecipe import (
    CHARM_RECIPE_ALLOW_CREATE,
    CHARM_RECIPE_USSO_FEATURE_FLAG,
    CharmRecipeBuildRequestStatus,
    ICharmRecipeSet,
)
from lp.registry.enums import PersonVisibility
from lp.services.config import config
from lp.services.crypto.interfaces import IEncryptedContainer
from lp.services.database.constants import UTC_NOW
from lp.services.features.testing import FeatureFixture
from lp.services.job.interfaces.job import JobStatus
from lp.services.propertycache import get_property_cache
from lp.services.webapp import canonical_url
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.testing import (
    BrowserTestCase,
    TestCaseWithFactory,
    login,
    login_admin,
    login_person,
    person_logged_in,
    time_counter,
)
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.matchers import MatchesPickerText, MatchesTagText
from lp.testing.pages import (
    extract_text,
    find_main_content,
    find_tag_by_id,
    find_tags_by_class,
    get_feedback_messages,
)
from lp.testing.publication import test_traverse
from lp.testing.views import create_initialized_view, create_view


class TestCharmRecipeNavigation(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))

    def test_canonical_url(self):
        owner = self.factory.makePerson(name="person")
        project = self.factory.makeProduct(name="project")
        recipe = self.factory.makeCharmRecipe(
            registrant=owner, owner=owner, project=project, name="charm"
        )
        self.assertEqual(
            "http://launchpad.test/~person/project/+charm/charm",
            canonical_url(recipe),
        )

    def test_charm_recipe(self):
        recipe = self.factory.makeCharmRecipe()
        obj, _, _ = test_traverse(
            "http://launchpad.test/~%s/%s/+charm/%s"
            % (recipe.owner.name, recipe.project.name, recipe.name)
        )
        self.assertEqual(recipe, obj)


class BaseTestCharmRecipeView(BrowserTestCase):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))
        self.useFixture(FakeLogger())
        self.person = self.factory.makePerson(
            name="test-person", displayname="Test Person"
        )


class TestCharmRecipeAddView(BaseTestCharmRecipeView):
    def test_create_new_recipe_not_logged_in(self):
        [git_ref] = self.factory.makeGitRefs()
        self.assertRaises(
            Unauthorized,
            self.getViewBrowser,
            git_ref,
            view_name="+new-charm-recipe",
            no_login=True,
        )

    def test_create_new_recipe_git(self):
        project = self.factory.makeProduct(
            name="test-project", displayname="Test Project"
        )
        [git_ref] = self.factory.makeGitRefs(
            owner=self.person, target=self.person
        )
        source_display = git_ref.display_name
        browser = self.getViewBrowser(
            git_ref, view_name="+new-charm-recipe", user=self.person
        )
        browser.getControl(name="field.name").value = "charm-name"
        self.assertEqual("", browser.getControl(name="field.project").value)
        browser.getControl(name="field.project").value = "test-project"
        browser.getControl("Create charm recipe").click()

        content = find_main_content(browser.contents)
        self.assertEqual("charm-name", extract_text(content.h1))
        self.assertThat(
            "Test Person", MatchesPickerText(content, "edit-owner")
        )
        self.assertThat(
            "Project:\nTest Project\nEdit charm recipe",
            MatchesTagText(content, "project"),
        )
        self.assertThat(
            "Source:\n%s\nEdit charm recipe" % source_display,
            MatchesTagText(content, "source"),
        )
        self.assertIsNone(find_tag_by_id(content, "build_path"))
        self.assertThat(
            "Build schedule:\n(?)\nBuilt on request\nEdit charm recipe\n",
            MatchesTagText(content, "auto_build"),
        )
        self.assertIsNone(find_tag_by_id(content, "auto_build_channels"))
        self.assertThat(
            "Builds of this charm recipe are not automatically uploaded to "
            "the store.\nEdit charm recipe",
            MatchesTagText(content, "store_upload"),
        )

        login_person(self.person)
        recipe = getUtility(ICharmRecipeSet).getByName(
            self.person, project, "charm-name"
        )
        self.assertThat(
            recipe,
            MatchesStructure(
                owner=Equals(self.person),
                project=Equals(project),
                name=Equals("charm-name"),
                source=Equals(git_ref),
                build_path=Is(None),
                auto_build=Is(False),
                auto_build_channels=Equals({}),
                store_upload=Is(False),
            ),
        )

    def test_create_new_recipe_git_project_namespace(self):
        # If the Git repository is already in a project namespace, then that
        # project is the default for the new recipe.
        project = self.factory.makeProduct(
            name="test-project", displayname="Test Project"
        )
        [git_ref] = self.factory.makeGitRefs(target=project)
        source_display = git_ref.display_name
        browser = self.getViewBrowser(
            git_ref, view_name="+new-charm-recipe", user=self.person
        )
        browser.getControl(name="field.name").value = "charm-name"
        self.assertEqual(
            "test-project", browser.getControl(name="field.project").value
        )
        browser.getControl("Create charm recipe").click()

        content = find_main_content(browser.contents)
        self.assertEqual("charm-name", extract_text(content.h1))
        self.assertThat(
            "Test Person", MatchesPickerText(content, "edit-owner")
        )
        self.assertThat(
            "Project:\nTest Project\nEdit charm recipe",
            MatchesTagText(content, "project"),
        )
        self.assertThat(
            "Source:\n%s\nEdit charm recipe" % source_display,
            MatchesTagText(content, "source"),
        )
        self.assertThat(
            "Build schedule:\n(?)\nBuilt on request\nEdit charm recipe\n",
            MatchesTagText(content, "auto_build"),
        )
        self.assertIsNone(find_tag_by_id(content, "auto_build_channels"))
        self.assertThat(
            "Builds of this charm recipe are not automatically uploaded to "
            "the store.\nEdit charm recipe",
            MatchesTagText(content, "store_upload"),
        )

    def test_create_new_recipe_project(self):
        project = self.factory.makeProduct(displayname="Test Project")
        [git_ref] = self.factory.makeGitRefs()
        git_ref_shortened_path = git_ref.repository.shortened_path
        git_ref_path = git_ref.path
        source_display = git_ref.display_name
        browser = self.getViewBrowser(
            project, view_name="+new-charm-recipe", user=self.person
        )
        browser.getControl(name="field.name").value = "charm-name"
        browser.getControl(name="field.git_ref.repository").value = (
            git_ref_shortened_path
        )
        browser.getControl(name="field.git_ref.path").value = git_ref_path
        browser.getControl("Create charm recipe").click()

        content = find_main_content(browser.contents)
        self.assertEqual("charm-name", extract_text(content.h1))
        self.assertThat(
            "Test Person", MatchesPickerText(content, "edit-owner")
        )
        self.assertThat(
            "Project:\nTest Project\nEdit charm recipe",
            MatchesTagText(content, "project"),
        )
        self.assertThat(
            "Source:\n%s\nEdit charm recipe" % source_display,
            MatchesTagText(content, "source"),
        )
        self.assertThat(
            "Build schedule:\n(?)\nBuilt on request\nEdit charm recipe\n",
            MatchesTagText(content, "auto_build"),
        )
        self.assertIsNone(find_tag_by_id(content, "auto_build_channels"))
        self.assertThat(
            "Builds of this charm recipe are not automatically uploaded to "
            "the store.\nEdit charm recipe",
            MatchesTagText(content, "store_upload"),
        )

    def test_create_new_recipe_users_teams_as_owner_options(self):
        # Teams that the user is in are options for the charm recipe owner.
        self.factory.makeTeam(
            name="test-team", displayname="Test Team", members=[self.person]
        )
        [git_ref] = self.factory.makeGitRefs()
        browser = self.getViewBrowser(
            git_ref, view_name="+new-charm-recipe", user=self.person
        )
        options = browser.getControl("Owner").displayOptions
        self.assertEqual(
            ["Test Person (test-person)", "Test Team (test-team)"],
            sorted(str(option) for option in options),
        )

    def test_create_new_recipe_build_path(self):
        project = self.factory.makeProduct(name="test-project")
        [git_ref] = self.factory.makeGitRefs()
        browser = self.getViewBrowser(
            git_ref, view_name="+new-charm-recipe", user=self.person
        )
        browser.getControl(name="field.name").value = "charm-name"
        browser.getControl(name="field.project").value = "test-project"
        browser.getControl("Build path").value = "charm/foo"
        browser.getControl("Create charm recipe").click()

        content = find_main_content(browser.contents)
        self.assertThat(
            "Build path:\ncharm/foo\nEdit charm recipe\n",
            MatchesTagText(content, "build-path"),
        )

        login_person(self.person)
        recipe = getUtility(ICharmRecipeSet).getByName(
            self.person, project, "charm-name"
        )
        self.assertEqual("charm/foo", recipe.build_path)

    def test_create_new_recipe_auto_build(self):
        # Creating a new recipe and asking for it to be automatically built
        # sets all the appropriate fields.
        self.factory.makeProduct(
            name="test-project", displayname="Test Project"
        )
        [git_ref] = self.factory.makeGitRefs()
        browser = self.getViewBrowser(
            git_ref, view_name="+new-charm-recipe", user=self.person
        )
        browser.getControl(name="field.name").value = "charm-name"
        browser.getControl(name="field.project").value = "test-project"
        browser.getControl(
            "Automatically build when branch changes"
        ).selected = True
        browser.getControl(
            name="field.auto_build_channels.charmcraft"
        ).value = "edge"
        browser.getControl(name="field.auto_build_channels.core").value = (
            "stable"
        )
        browser.getControl(name="field.auto_build_channels.core18").value = (
            "beta"
        )
        browser.getControl(name="field.auto_build_channels.core20").value = (
            "edge/feature"
        )
        browser.getControl("Create charm recipe").click()

        content = find_main_content(browser.contents)
        self.assertThat(
            "Build schedule:\n(?)\nBuilt automatically\nEdit charm recipe\n",
            MatchesTagText(content, "auto_build"),
        )
        self.assertThat(
            "Source snap channels for automatic builds:\nEdit charm recipe\n"
            "charmcraft\nedge\ncore\nstable\ncore18\nbeta\n"
            "core20\nedge/feature\n",
            MatchesTagText(content, "auto_build_channels"),
        )

    @responses.activate
    def test_create_new_recipe_store_upload(self):
        # Creating a new recipe and asking for it to be automatically
        # uploaded to Charmhub sets all the appropriate fields and redirects
        # to Candid for authorization.
        self.pushConfig("charms", charmhub_url="http://charmhub.example/")
        self.pushConfig(
            "launchpad",
            candid_service_root="https://candid.test/",
            csrf_secret="test secret",
        )
        project = self.factory.makeProduct(
            name="test-project", displayname="Test Project"
        )
        [git_ref] = self.factory.makeGitRefs()
        view_url = canonical_url(git_ref, view_name="+new-charm-recipe")
        browser = self.getNonRedirectingBrowser(url=view_url, user=self.person)
        browser.getControl(name="field.name").value = "charm-name"
        browser.getControl(name="field.project").value = "test-project"
        browser.getControl("Automatically upload to store").selected = True
        browser.getControl("Registered store name").value = "charmhub-name"
        self.assertFalse(browser.getControl("Stable").selected)
        browser.getControl(name="field.store_channels.add_track").value = (
            "track"
        )
        browser.getControl(name="field.store_channels.add_risk").value = [
            "edge"
        ]
        root_macaroon = Macaroon(version=2)
        root_macaroon.add_third_party_caveat(
            "https://candid.test/", "", "identity"
        )
        caveat = root_macaroon.caveats[0]
        root_macaroon_raw = root_macaroon.serialize(JsonSerializer())
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens",
            json={"macaroon": root_macaroon_raw},
        )
        responses.add(
            "POST",
            "https://candid.test/discharge",
            status=401,
            json={
                "Code": "interaction required",
                "Message": (
                    "macaroon discharge required: authentication required"
                ),
                "Info": {
                    "InteractionMethods": {
                        "browser-redirect": {
                            "LoginURL": "https://candid.test/login-redirect",
                        },
                    },
                },
            },
        )
        browser.getControl("Create charm recipe").click()
        login_person(self.person)
        recipe = getUtility(ICharmRecipeSet).getByName(
            self.person, project, "charm-name"
        )
        self.assertThat(
            recipe,
            MatchesStructure.byEquality(
                owner=self.person,
                project=project,
                name="charm-name",
                source=git_ref,
                store_upload=True,
                store_name="charmhub-name",
                store_secrets={
                    "root": root_macaroon_raw,
                    "auth_method": "candid",
                },
                store_channels=["track/edge"],
            ),
        )
        tokens_matcher = MatchesStructure(
            url=Equals("http://charmhub.example/v1/tokens"),
            method=Equals("POST"),
            body=AfterPreprocessing(
                json.loads,
                Equals(
                    {
                        "description": ("charmhub-name for launchpad.test"),
                        "packages": [
                            {
                                "type": "charm",
                                "name": "charmhub-name",
                            },
                        ],
                        "permissions": [
                            "package-manage-releases",
                            "package-manage-revisions",
                            "package-view-revisions",
                        ],
                    }
                ),
            ),
        )
        discharge_matcher = MatchesStructure(
            url=Equals("https://candid.test/discharge"),
            headers=ContainsDict(
                {
                    "Content-Type": Equals(
                        "application/x-www-form-urlencoded"
                    ),
                }
            ),
            body=AfterPreprocessing(
                parse_qs,
                MatchesDict(
                    {
                        "id64": Equals(
                            [base64.b64encode(caveat.caveat_id_bytes).decode()]
                        ),
                    }
                ),
            ),
        )
        self.assertThat(
            responses.calls,
            MatchesListwise(
                [
                    MatchesStructure(request=tokens_matcher),
                    MatchesStructure(request=discharge_matcher),
                ]
            ),
        )
        self.assertEqual(303, browser.responseStatusCode)
        return_to_matcher = AfterPreprocessing(
            urlsplit,
            MatchesStructure(
                scheme=Equals("http"),
                netloc=Equals("launchpad.test"),
                path=Equals("/+candid-callback"),
                query=AfterPreprocessing(
                    parse_qs,
                    MatchesDict(
                        {
                            "starting_url": Equals(
                                [canonical_url(recipe) + "/+authorize"]
                            ),
                            "discharge_macaroon_action": Equals(
                                ["field.actions.complete"]
                            ),
                            "discharge_macaroon_field": Equals(
                                ["field.discharge_macaroon"]
                            ),
                        }
                    ),
                ),
                fragment=Equals(""),
            ),
        )
        self.assertThat(
            urlsplit(browser.headers["Location"]),
            MatchesStructure(
                scheme=Equals("https"),
                netloc=Equals("candid.test"),
                path=Equals("/login-redirect"),
                query=AfterPreprocessing(
                    parse_qs,
                    ContainsDict(
                        {"return_to": MatchesListwise([return_to_matcher])}
                    ),
                ),
                fragment=Equals(""),
            ),
        )

    def test_create_new_recipe_multiple_tracks_missing_recipe_name(self):
        # Missing charm recipe will result in error in browser
        self.factory.makeProduct(
            name="test-project", displayname="Test Project"
        )
        [git_ref] = self.factory.makeGitRefs()
        view_url = canonical_url(git_ref, view_name="+new-charm-recipe")
        browser = self.getNonRedirectingBrowser(url=view_url, user=self.person)
        browser.getControl(name="field.project").value = "test-project"
        browser.getControl("Automatically upload to store").selected = True
        browser.getControl("Registered store name").value = "charmhub-name"
        self.assertFalse(browser.getControl("Stable").selected)
        browser.getControl(name="field.store_channels.add_track").value = (
            "track"
        )
        browser.getControl("Edge").selected = True
        browser.getControl(name="field.store_channels.add_branch").value = (
            "branch"
        )

        browser.getControl("Create charm recipe").click()

        self.assertIn("Required input is missing.", browser.contents)

    def test_create_new_recipe_missing_channel_risk(self):
        # No track or branch selected
        self.factory.makeProduct(
            name="test-project", displayname="Test Project"
        )
        [git_ref] = self.factory.makeGitRefs()
        view_url = canonical_url(git_ref, view_name="+new-charm-recipe")
        browser = self.getNonRedirectingBrowser(url=view_url, user=self.person)
        browser.getControl(name="field.project").value = "test-project"
        browser.getControl("Automatically upload to store").selected = True
        browser.getControl(name="field.name").value = "test-recipe-name"
        browser.getControl("Registered store name").value = "charmhub-name"

        browser.getControl("Create charm recipe").click()

        self.assertIn("You must select a risk.", browser.contents)

        # Entering only the track is not enough
        browser = self.getNonRedirectingBrowser(url=view_url, user=self.person)
        browser.getControl(name="field.project").value = "test-project"
        browser.getControl("Automatically upload to store").selected = True
        browser.getControl(name="field.name").value = "test-recipe-name"
        browser.getControl("Registered store name").value = "charmhub-name"
        browser.getControl(name="field.store_channels.add_track").value = (
            "new-track"
        )

        browser.getControl("Create charm recipe").click()

        self.assertIn("You must select a risk.", browser.contents)

        # Entering only the track and branch will error
        browser = self.getNonRedirectingBrowser(url=view_url, user=self.person)
        browser.getControl(name="field.project").value = "test-project"
        browser.getControl("Automatically upload to store").selected = True
        browser.getControl(name="field.name").value = "test-recipe-name"
        browser.getControl("Registered store name").value = "charmhub-name"
        browser.getControl(name="field.store_channels.add_track").value = (
            "new-track"
        )
        browser.getControl(name="field.store_channels.add_branch").value = (
            "new-branch"
        )

        browser.getControl("Create charm recipe").click()

        self.assertIn("You must select a risk.", browser.contents)


class TestCharmRecipeAdminView(BaseTestCharmRecipeView):
    def test_unauthorized(self):
        # A non-admin user cannot administer a charm recipe.
        login_person(self.person)
        recipe = self.factory.makeCharmRecipe(registrant=self.person)
        recipe_url = canonical_url(recipe)
        browser = self.getViewBrowser(recipe, user=self.person)
        self.assertRaises(
            LinkNotFoundError, browser.getLink, "Administer charm recipe"
        )
        self.assertRaises(
            Unauthorized,
            self.getUserBrowser,
            recipe_url + "/+admin",
            user=self.person,
        )

    def test_admin_recipe(self):
        # Admins can change require_virtualized.
        login("admin@canonical.com")
        admin = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).admin]
        )
        login_person(self.person)
        recipe = self.factory.makeCharmRecipe(registrant=self.person)
        self.assertTrue(recipe.require_virtualized)

        browser = self.getViewBrowser(recipe, user=admin)
        browser.getLink("Administer charm recipe").click()
        browser.getControl("Require virtualized builders").selected = False
        browser.getControl("Update charm recipe").click()

        login_admin()
        self.assertFalse(recipe.require_virtualized)

    def test_admin_recipe_sets_date_last_modified(self):
        # Administering a charm recipe sets the date_last_modified property.
        login("admin@canonical.com")
        ppa_admin = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).ppa_admin]
        )
        login_person(self.person)
        date_created = datetime(2000, 1, 1, tzinfo=timezone.utc)
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person, date_created=date_created
        )
        login_person(ppa_admin)
        view = CharmRecipeAdminView(recipe, LaunchpadTestRequest())
        view.initialize()
        view.request_action.success({"require_virtualized": False})
        self.assertSqlAttributeEqualsDate(
            recipe, "date_last_modified", UTC_NOW
        )


class TestCharmRecipeEditView(BaseTestCharmRecipeView):
    def test_edit_recipe(self):
        [old_git_ref] = self.factory.makeGitRefs()
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            owner=self.person,
            git_ref=old_git_ref,
            store_channels=["track1/stable/branch1", "track2/edge/branch1"],
        )
        self.factory.makeTeam(
            name="new-team", displayname="New Team", members=[self.person]
        )
        [new_git_ref] = self.factory.makeGitRefs()
        new_git_ref_display_name = new_git_ref.display_name
        new_git_ref_identity = new_git_ref.repository.identity
        new_git_ref_path = new_git_ref.path

        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit charm recipe").click()
        browser.getControl("Owner").value = ["new-team"]
        browser.getControl(name="field.name").value = "new-name"
        browser.getControl(name="field.git_ref.repository").value = (
            new_git_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = new_git_ref_path
        browser.getControl("Build path").value = "some-path"
        browser.getControl(
            "Automatically build when branch changes"
        ).selected = True
        browser.getControl(
            name="field.auto_build_channels.charmcraft"
        ).value = "edge"
        browser.getControl("Update charm recipe").click()

        content = find_main_content(browser.contents)
        self.assertEqual("new-name", extract_text(content.h1))
        self.assertThat("New Team", MatchesPickerText(content, "edit-owner"))
        self.assertThat(
            "Source:\n%s\nEdit charm recipe" % new_git_ref_display_name,
            MatchesTagText(content, "source"),
        )
        self.assertThat(
            "Build path:\nsome-path\nEdit charm recipe\n",
            MatchesTagText(content, "build-path"),
        )
        self.assertThat(
            "Build schedule:\n(?)\nBuilt automatically\nEdit charm recipe\n",
            MatchesTagText(content, "auto_build"),
        )
        self.assertThat(
            "Source snap channels for automatic builds:\nEdit charm recipe\n"
            "charmcraft\nedge",
            MatchesTagText(content, "auto_build_channels"),
        )
        self.assertThat(
            "Builds of this charm recipe are not automatically uploaded to "
            "the store.\nEdit charm recipe",
            MatchesTagText(content, "store_upload"),
        )

    @responses.activate
    def test_edit_recipe_add_store_channel(self):
        self.pushConfig("charms", charmhub_url="http://charmhub.example/")
        self.pushConfig(
            "launchpad",
            candid_service_root="https://candid.test/",
            csrf_secret="test secret",
        )
        [git_ref] = self.factory.makeGitRefs()
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            owner=self.person,
            git_ref=git_ref,
            store_channels=["track1/stable/branch1", "track2/edge/branch1"],
            store_name="Store name",
            store_upload=True,
        )
        self.factory.makeTeam(
            name="new-team", displayname="New Team", members=[self.person]
        )
        view_url = canonical_url(recipe, view_name="+edit")
        browser = self.getNonRedirectingBrowser(url=view_url, user=self.person)
        browser.getControl(name="field.store_channels.add_track").value = (
            "new-track"
        )
        browser.getControl(name="field.store_channels.add_branch").value = (
            "new-branch"
        )
        browser.getControl(name="field.store_channels.add_risk").value = [
            "edge"
        ]
        root_macaroon = Macaroon(version=2)
        root_macaroon.add_third_party_caveat(
            "https://candid.test/", "", "identity"
        )
        root_macaroon_raw = root_macaroon.serialize(JsonSerializer())
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens",
            json={"macaroon": root_macaroon_raw},
        )
        responses.add(
            "POST",
            "https://candid.test/discharge",
            status=401,
            json={
                "Code": "interaction required",
                "Message": (
                    "macaroon discharge required: authentication required"
                ),
                "Info": {
                    "InteractionMethods": {
                        "browser-redirect": {
                            "LoginURL": "https://candid.test/login-redirect",
                        },
                    },
                },
            },
        )

        browser.getControl("Update charm recipe").click()

        self.assertEqual(303, browser.responseStatusCode)

        browser = self.getViewBrowser(recipe, user=self.person)
        content = find_main_content(browser.contents)
        self.assertThat(
            "Store channels:\n"
            "track1/stable/branch1, "
            "track2/edge/branch1, new-track/edge/new-branch"
            "\nEdit charm recipe",
            MatchesTagText(content, "store_channels"),
        )

    @responses.activate
    def test_edit_recipe_edit_store_channel_list(self):
        # Verify we can edit the first store channel defined for this recipe
        # from "track1/stable/branch1" to "new-track/candidate/new-branch"
        self.pushConfig("charms", charmhub_url="http://charmhub.example/")
        self.pushConfig(
            "launchpad",
            candid_service_root="https://candid.test/",
            csrf_secret="test secret",
        )
        [git_ref] = self.factory.makeGitRefs()
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            owner=self.person,
            git_ref=git_ref,
            store_channels=["track1/stable/branch1", "track2/edge/branch1"],
            store_name="Store name",
            store_upload=True,
        )
        self.factory.makeTeam(
            name="new-team", displayname="New Team", members=[self.person]
        )
        view_url = canonical_url(recipe, view_name="+edit")
        browser = self.getNonRedirectingBrowser(url=view_url, user=self.person)
        browser.getControl(name="field.store_channels.track_0").value = (
            "new-track"
        )
        browser.getControl(name="field.store_channels.branch_0").value = (
            "new-branch"
        )
        browser.getControl(name="field.store_channels.risk_0").value = [
            "candidate"
        ]
        root_macaroon = Macaroon(version=2)
        root_macaroon.add_third_party_caveat(
            "https://candid.test/", "", "identity"
        )
        root_macaroon_raw = root_macaroon.serialize(JsonSerializer())
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens",
            json={"macaroon": root_macaroon_raw},
        )
        responses.add(
            "POST",
            "https://candid.test/discharge",
            status=401,
            json={
                "Code": "interaction required",
                "Message": (
                    "macaroon discharge required: authentication required"
                ),
                "Info": {
                    "InteractionMethods": {
                        "browser-redirect": {
                            "LoginURL": "https://candid.test/login-redirect",
                        },
                    },
                },
            },
        )

        browser.getControl("Update charm recipe").click()

        self.assertEqual(303, browser.responseStatusCode)

        browser = self.getViewBrowser(recipe, user=self.person)
        content = find_main_content(browser.contents)
        self.assertThat(
            "Store channels:\n"
            "new-track/candidate/new-branch, track2/edge/branch1"
            "\nEdit charm recipe",
            MatchesTagText(content, "store_channels"),
        )

    def test_edit_recipe_delete_store_channel_list(self):
        # Verify we can edit the first store channel defined for this recipe
        # from "track1/stable/branch1" to "new-track/candidate/new-branch"
        [git_ref] = self.factory.makeGitRefs()
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            owner=self.person,
            git_ref=git_ref,
            store_channels=["track1/stable/branch1", "track2/edge/branch1"],
            store_name="Store name",
            store_upload=True,
        )
        self.factory.makeTeam(
            name="new-team", displayname="New Team", members=[self.person]
        )
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit charm recipe").click()
        browser.getControl(name="field.store_channels.delete_0").value = 1

        browser.getControl("Update charm recipe").click()

        content = find_main_content(browser.contents)
        self.assertThat(
            "Store channels:\n" "track2/edge/branch1" "\nEdit charm recipe",
            MatchesTagText(content, "store_channels"),
        )

    def test_edit_recipe_missing_channel_risk(self):
        # On the edit form we can also add a new channel.
        # The only valid combination for the
        # new channel entry on this form are:
        # 1: nothing selected - allows the user to edit existent channels,
        # without adding a new empty channel row when pressing Update Recipe.
        # 2: Risk selected and any combination of track / branch entered
        [git_ref] = self.factory.makeGitRefs()
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            owner=self.person,
            git_ref=git_ref,
            store_channels=["track1/stable/branch1", "track2/edge/branch1"],
            store_name="Store name",
            store_upload=True,
        )
        self.factory.makeTeam(
            name="new-team", displayname="New Team", members=[self.person]
        )
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit charm recipe").click()

        # If the user entered only Track for the new channel entry,
        # ensure we error with missing Risk message.
        browser.getControl(name="field.store_channels.add_track").value = (
            "new-track"
        )

        browser.getControl("Update charm recipe").click()

        self.assertIn("You must select a risk.", browser.contents)

        # If the user entered only Branch for the new channel entry,
        # ensure we error with missing Risk message.
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit charm recipe").click()
        browser.getControl(name="field.store_channels.add_branch").value = (
            "new-branch"
        )

        browser.getControl("Update charm recipe").click()

        self.assertIn("You must select a risk.", browser.contents)

        # For Track and Branch entered but no Risk selected,
        # ensure we error with missing Risk message.
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit charm recipe").click()
        browser.getControl(name="field.store_channels.add_track").value = (
            "new-track"
        )
        browser.getControl(name="field.store_channels.add_branch").value = (
            "new-branch"
        )

        browser.getControl("Update charm recipe").click()

        self.assertIn("You must select a risk.", browser.contents)

    def test_edit_recipe_sets_date_last_modified(self):
        # Editing a charm recipe sets the date_last_modified property.
        date_created = datetime(2000, 1, 1, tzinfo=timezone.utc)
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person, date_created=date_created
        )
        with person_logged_in(self.person):
            view = CharmRecipeEditView(recipe, LaunchpadTestRequest())
            view.initialize()
            view.request_action.success(
                {
                    "owner": recipe.owner,
                    "name": "changed",
                }
            )
        self.assertSqlAttributeEqualsDate(
            recipe, "date_last_modified", UTC_NOW
        )

    def test_edit_recipe_already_exists(self):
        project = self.factory.makeProduct(displayname="Test Project")
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            project=project,
            owner=self.person,
            name="one",
        )
        self.factory.makeCharmRecipe(
            registrant=self.person,
            project=project,
            owner=self.person,
            name="two",
        )
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit charm recipe").click()
        browser.getControl(name="field.name").value = "two"
        browser.getControl("Update charm recipe").click()
        self.assertEqual(
            "There is already a charm recipe owned by Test Person in "
            "Test Project with this name.",
            extract_text(find_tags_by_class(browser.contents, "message")[1]),
        )

    def test_edit_public_recipe_private_owner(self):
        login_person(self.person)
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person, owner=self.person
        )
        private_team = self.factory.makeTeam(
            owner=self.person, visibility=PersonVisibility.PRIVATE
        )
        private_team_name = private_team.name
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit charm recipe").click()
        browser.getControl("Owner").value = [private_team_name]
        browser.getControl("Update charm recipe").click()
        self.assertEqual(
            "A public charm recipe cannot have a private owner.",
            extract_text(find_tags_by_class(browser.contents, "message")[1]),
        )

    def test_edit_public_recipe_private_git_ref(self):
        login_person(self.person)
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            owner=self.person,
            git_ref=self.factory.makeGitRefs()[0],
        )
        login_person(self.person)
        [private_ref] = self.factory.makeGitRefs(
            owner=self.person, information_type=InformationType.PRIVATESECURITY
        )
        private_ref_identity = private_ref.repository.identity
        private_ref_path = private_ref.path
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit charm recipe").click()
        browser.getControl(name="field.git_ref.repository").value = (
            private_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = private_ref_path
        browser.getControl("Update charm recipe").click()
        self.assertEqual(
            "A public charm recipe cannot have a private repository.",
            extract_text(find_tags_by_class(browser.contents, "message")[1]),
        )

    def test_edit_fetch_service_policy(self):
        login_person(self.person)
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            owner=self.person,
            git_ref=self.factory.makeGitRefs()[0],
        )
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit charm recipe").click()
        with person_logged_in(self.person):
            self.assertFalse(recipe.use_fetch_service)
            self.assertEqual(
                recipe.fetch_service_policy, FetchServicePolicy.STRICT
            )
        browser.getControl("Use fetch service").selected = True
        with person_logged_in(self.person):
            self.assertEqual(
                recipe.fetch_service_policy, FetchServicePolicy.STRICT
            )
        browser.getControl("Fetch service policy").value = ["PERMISSIVE"]
        browser.getControl("Update charm recipe").click()

        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit charm recipe").click()
        with person_logged_in(self.person):
            self.assertTrue(recipe.use_fetch_service)
            self.assertEqual(
                recipe.fetch_service_policy, FetchServicePolicy.PERMISSIVE
            )

    def test_fetch_service_policy_when_opted_in(self):
        # If the use_fetch_service parameter is set to 'True', the
        # fetch_service_policy defaults to 'strict' mode unless specified
        login_person(self.person)
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            owner=self.person,
            git_ref=self.factory.makeGitRefs()[0],
            use_fetch_service=True,
        )
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit charm recipe").click()
        with person_logged_in(self.person):
            self.assertTrue(recipe.use_fetch_service)
            self.assertEqual(
                recipe.fetch_service_policy, FetchServicePolicy.STRICT
            )


class TestCharmRecipeAuthorizeView(BaseTestCharmRecipeView):
    def setUp(self):
        super().setUp()
        self.pushConfig("charms", charmhub_url="http://charmhub.example/")
        self.pushConfig(
            "launchpad",
            candid_service_root="https://candid.test/",
            csrf_secret="test secret",
        )
        self.recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            owner=self.person,
            store_upload=True,
            store_name=self.factory.getUniqueUnicode(),
        )

    def test_unauthorized(self):
        # A user without edit access cannot authorize charm recipe uploads.
        other_person = self.factory.makePerson()
        self.assertRaises(
            Unauthorized,
            self.getUserBrowser,
            canonical_url(self.recipe) + "/+authorize",
            user=other_person,
        )

    @responses.activate
    def test_begin_authorization(self):
        # With no special form actions, we return a form inviting the user
        # to begin authorization.  This allows (re-)authorizing uploads of
        # an existing charm recipe without having to edit it.
        recipe_url = canonical_url(self.recipe)
        owner = self.recipe.owner
        store_name = self.recipe.store_name
        browser = self.getNonRedirectingBrowser(
            url=recipe_url + "/+authorize", user=self.recipe.owner
        )
        root_macaroon = Macaroon(version=2)
        root_macaroon.add_third_party_caveat(
            "https://candid.test/", "", "identity"
        )
        caveat = root_macaroon.caveats[0]
        root_macaroon_raw = root_macaroon.serialize(JsonSerializer())
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens",
            json={"macaroon": root_macaroon_raw},
        )
        responses.add(
            "POST",
            "https://candid.test/discharge",
            status=401,
            json={
                "Code": "interaction required",
                "Message": (
                    "macaroon discharge required: authentication required"
                ),
                "Info": {
                    "InteractionMethods": {
                        "browser-redirect": {
                            "LoginURL": "https://candid.test/login-redirect",
                        },
                    },
                },
            },
        )
        browser.getControl("Begin authorization").click()
        tokens_matcher = MatchesStructure(
            url=Equals("http://charmhub.example/v1/tokens"),
            method=Equals("POST"),
            body=AfterPreprocessing(
                json.loads,
                Equals(
                    {
                        "description": (f"{store_name} for launchpad.test"),
                        "packages": [
                            {
                                "type": "charm",
                                "name": store_name,
                            },
                        ],
                        "permissions": [
                            "package-manage-releases",
                            "package-manage-revisions",
                            "package-view-revisions",
                        ],
                    }
                ),
            ),
        )
        discharge_matcher = MatchesStructure(
            url=Equals("https://candid.test/discharge"),
            headers=ContainsDict(
                {
                    "Content-Type": Equals(
                        "application/x-www-form-urlencoded"
                    ),
                }
            ),
            body=AfterPreprocessing(
                parse_qs,
                MatchesDict(
                    {
                        "id64": Equals(
                            [base64.b64encode(caveat.caveat_id_bytes).decode()]
                        ),
                    }
                ),
            ),
        )
        self.assertThat(
            responses.calls,
            MatchesListwise(
                [
                    MatchesStructure(request=tokens_matcher),
                    MatchesStructure(request=discharge_matcher),
                ]
            ),
        )
        with person_logged_in(owner):
            self.assertEqual(
                {"root": root_macaroon_raw, "auth_method": "candid"},
                self.recipe.store_secrets,
            )
        self.assertEqual(303, browser.responseStatusCode)
        return_to_matcher = AfterPreprocessing(
            urlsplit,
            MatchesStructure(
                scheme=Equals("http"),
                netloc=Equals("launchpad.test"),
                path=Equals("/+candid-callback"),
                query=AfterPreprocessing(
                    parse_qs,
                    MatchesDict(
                        {
                            "starting_url": Equals(
                                [recipe_url + "/+authorize"]
                            ),
                            "discharge_macaroon_action": Equals(
                                ["field.actions.complete"]
                            ),
                            "discharge_macaroon_field": Equals(
                                ["field.discharge_macaroon"]
                            ),
                        }
                    ),
                ),
                fragment=Equals(""),
            ),
        )
        self.assertThat(
            urlsplit(browser.headers["Location"]),
            MatchesStructure(
                scheme=Equals("https"),
                netloc=Equals("candid.test"),
                path=Equals("/login-redirect"),
                query=AfterPreprocessing(
                    parse_qs,
                    ContainsDict(
                        {"return_to": MatchesListwise([return_to_matcher])}
                    ),
                ),
                fragment=Equals(""),
            ),
        )

    @responses.activate
    def test_begin_authorization_usso(self):
        # With the Ubuntu SSO feature flag on, beginning authorization
        # redirects to Launchpad's own login flow to discharge the caveat.
        self.useFixture(
            FeatureFixture(
                {
                    CHARM_RECIPE_ALLOW_CREATE: "on",
                    CHARM_RECIPE_USSO_FEATURE_FLAG: "on",
                }
            )
        )
        recipe_url = canonical_url(self.recipe)
        owner = self.recipe.owner
        store_name = self.recipe.store_name
        browser = self.getNonRedirectingBrowser(
            url=recipe_url + "/+authorize", user=self.recipe.owner
        )
        root_macaroon = Macaroon(version=2)
        root_macaroon.add_third_party_caveat(
            urlsplit(config.launchpad.openid_provider_root).netloc,
            "",
            "dummy",
        )
        root_macaroon_raw = root_macaroon.serialize()
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/usso",
            json={"macaroon": root_macaroon_raw},
        )
        browser.getControl("Begin authorization").click()
        tokens_matcher = MatchesStructure(
            url=Equals("http://charmhub.example/v1/tokens/usso"),
            method=Equals("POST"),
            body=AfterPreprocessing(
                json.loads,
                Equals(
                    {
                        "ttl": 31536000,
                        "packages": [
                            {"type": "charm", "name": store_name},
                        ],
                        "permissions": [
                            "package-manage-releases",
                            "package-manage-revisions",
                            "package-view-revisions",
                        ],
                    }
                ),
            ),
        )
        self.assertThat(
            responses.calls,
            MatchesListwise([MatchesStructure(request=tokens_matcher)]),
        )
        with person_logged_in(owner):
            self.assertEqual(
                {"root": root_macaroon_raw, "auth_method": "usso"},
                self.recipe.store_secrets,
            )
        self.assertEqual(303, browser.responseStatusCode)
        self.assertEqual(
            recipe_url + "/+authorize/+login?macaroon_caveat_id=dummy&"
            "discharge_macaroon_action=field.actions.complete&"
            "discharge_macaroon_field=field.discharge_macaroon",
            browser.headers["Location"],
        )

    def test_complete_authorization_missing_discharge_macaroon(self):
        # If the form does not include a discharge macaroon, the "complete"
        # action fails.
        with person_logged_in(self.recipe.owner):
            self.recipe.store_secrets = {
                "root": Macaroon(version=2).serialize(JsonSerializer()),
            }
            transaction.commit()
            form = {"field.actions.complete": "1"}
            view = create_initialized_view(
                self.recipe,
                "+authorize",
                form=form,
                method="POST",
                principal=self.recipe.owner,
            )
            html = view()
            self.assertEqual(
                "Uploads of %s to Charmhub were not authorized."
                % self.recipe.name,
                get_feedback_messages(html)[1],
            )
            self.assertNotIn("exchanged_encrypted", self.recipe.store_secrets)

    @responses.activate
    def test_complete_authorization(self):
        # If the form includes a discharge macaroon, the "complete" action
        # exchanges the root and discharge pair with Charmhub for a single
        # macaroon, then succeeds and records the new secrets.
        private_key = PrivateKey.generate()
        self.pushConfig(
            "charms",
            charmhub_secrets_public_key=base64.b64encode(
                bytes(private_key.public_key)
            ).decode(),
        )
        root_macaroon = Macaroon(version=2)
        root_macaroon_raw = root_macaroon.serialize(JsonSerializer())
        unbound_discharge_macaroon = Macaroon(version=2)
        unbound_discharge_macaroon_raw = unbound_discharge_macaroon.serialize(
            JsonSerializer()
        )
        discharge_macaroon_raw = root_macaroon.prepare_for_request(
            unbound_discharge_macaroon
        ).serialize(JsonSerializer())
        exchanged_macaroon = Macaroon(version=2)
        exchanged_macaroon_raw = exchanged_macaroon.serialize(JsonSerializer())
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/exchange",
            json={"macaroon": exchanged_macaroon_raw},
        )
        with person_logged_in(self.recipe.owner):
            self.recipe.store_secrets = {"root": root_macaroon_raw}
            transaction.commit()
            form = {
                "field.actions.complete": "1",
                "field.discharge_macaroon": unbound_discharge_macaroon_raw,
            }
            view = create_initialized_view(
                self.recipe,
                "+authorize",
                form=form,
                method="POST",
                principal=self.recipe.owner,
            )
            self.assertEqual(302, view.request.response.getStatus())
            self.assertEqual(
                canonical_url(self.recipe),
                view.request.response.getHeader("Location"),
            )
            self.assertEqual(
                "Uploads of %s to Charmhub are now authorized."
                % self.recipe.name,
                view.request.response.notifications[0].message,
            )
            self.pushConfig(
                "charms",
                charmhub_secrets_private_key=base64.b64encode(
                    bytes(private_key)
                ).decode(),
            )
            container = getUtility(IEncryptedContainer, "charmhub-secrets")
            self.assertThat(
                self.recipe.store_secrets,
                MatchesDict(
                    {
                        "exchanged_encrypted": AfterPreprocessing(
                            lambda data: container.decrypt(data).decode(),
                            Equals(exchanged_macaroon_raw),
                        ),
                    }
                ),
            )
        exchange_matcher = MatchesStructure(
            url=Equals("http://charmhub.example/v1/tokens/exchange"),
            method=Equals("POST"),
            headers=ContainsDict(
                {
                    "Macaroons": AfterPreprocessing(
                        lambda v: json.loads(
                            base64.b64decode(v.encode()).decode()
                        ),
                        Equals(
                            [
                                json.loads(m)
                                for m in (
                                    root_macaroon_raw,
                                    discharge_macaroon_raw,
                                )
                            ]
                        ),
                    ),
                }
            ),
            body=AfterPreprocessing(json.loads, Equals({})),
        )
        self.assertThat(
            responses.calls,
            MatchesListwise([MatchesStructure(request=exchange_matcher)]),
        )


class TestCharmRecipeDeleteView(BaseTestCharmRecipeView):
    def test_unauthorized(self):
        # A user without edit access cannot delete a charm recipe.
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person, owner=self.person
        )
        recipe_url = canonical_url(recipe)
        other_person = self.factory.makePerson()
        browser = self.getViewBrowser(recipe, user=other_person)
        self.assertRaises(
            LinkNotFoundError, browser.getLink, "Delete charm recipe"
        )
        self.assertRaises(
            Unauthorized,
            self.getUserBrowser,
            recipe_url + "/+delete",
            user=other_person,
        )

    def test_delete_recipe_without_builds(self):
        # A charm recipe without builds can be deleted.
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person, owner=self.person
        )
        recipe_url = canonical_url(recipe)
        owner_url = canonical_url(self.person)
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Delete charm recipe").click()
        browser.getControl("Delete charm recipe").click()
        self.assertEqual(owner_url + "/+charm-recipes", browser.url)
        self.assertRaises(NotFound, browser.open, recipe_url)

    def test_delete_recipe_with_builds(self):
        # A charm recipe with builds can be deleted.
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person, owner=self.person
        )
        build = self.factory.makeCharmRecipeBuild(recipe=recipe)
        self.factory.makeCharmFile(build=build)
        recipe_url = canonical_url(recipe)
        owner_url = canonical_url(self.person)
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Delete charm recipe").click()
        browser.getControl("Delete charm recipe").click()
        self.assertEqual(owner_url + "/+charm-recipes", browser.url)
        self.assertRaises(NotFound, browser.open, recipe_url)


class TestCharmRecipeView(BaseTestCharmRecipeView):
    def setUp(self):
        super().setUp()
        self.project = self.factory.makeProduct(
            name="test-project", displayname="Test Project"
        )
        self.ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.distroseries = self.factory.makeDistroSeries(
            distribution=self.ubuntu
        )
        processor = getUtility(IProcessorSet).getByName("386")
        self.distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries,
            architecturetag="i386",
            processor=processor,
        )
        self.factory.makeBuilder(virtualized=True)

    def makeCharmRecipe(self, **kwargs):
        if "project" not in kwargs:
            kwargs["project"] = self.project
        if "git_ref" not in kwargs:
            kwargs["git_ref"] = self.factory.makeGitRefs()[0]
        return self.factory.makeCharmRecipe(
            registrant=self.person,
            owner=self.person,
            name="charm-name",
            **kwargs,
        )

    def makeBuild(self, recipe=None, date_created=None, **kwargs):
        if recipe is None:
            recipe = self.makeCharmRecipe()
        if date_created is None:
            date_created = datetime.now(timezone.utc) - timedelta(hours=1)
        build = self.factory.makeCharmRecipeBuild(
            requester=self.person,
            recipe=recipe,
            distro_arch_series=self.distroarchseries,
            date_created=date_created,
            **kwargs,
        )
        job = removeSecurityProxy(
            removeSecurityProxy(build.build_request)._job
        )
        job.job._status = JobStatus.COMPLETED
        return build

    def test_breadcrumb(self):
        recipe = self.makeCharmRecipe()
        view = create_view(recipe, "+index")
        # To test the breadcrumbs we need a correct traversal stack.
        view.request.traversed_objects = [
            recipe.owner,
            recipe.project,
            recipe,
            view,
        ]
        view.initialize()
        breadcrumbs_tag = soupmatchers.Tag(
            "breadcrumbs", "ol", attrs={"class": "breadcrumbs"}
        )
        self.assertThat(
            view(),
            soupmatchers.HTMLContains(
                soupmatchers.Within(
                    breadcrumbs_tag,
                    soupmatchers.Tag(
                        "project breadcrumb",
                        "a",
                        text="Test Project",
                        attrs={"href": re.compile(r"/test-project$")},
                    ),
                ),
                soupmatchers.Within(
                    breadcrumbs_tag,
                    soupmatchers.Tag(
                        "charm breadcrumb",
                        "li",
                        text=re.compile(r"\scharm-name\s"),
                    ),
                ),
            ),
        )

    def test_index_git(self):
        [ref] = self.factory.makeGitRefs(
            owner=self.person,
            target=self.project,
            name="charm-repository",
            paths=["refs/heads/master"],
        )
        recipe = self.makeCharmRecipe(git_ref=ref)
        build = self.makeBuild(
            recipe=recipe,
            status=BuildStatus.FULLYBUILT,
            duration=timedelta(minutes=30),
        )
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""\
            Test Project
            charm-name
            .*
            Charm recipe information
            Owner: Test Person
            Project: Test Project
            Source: ~test-person/test-project/\+git/charm-repository:master
            Build schedule: \(\?\)
            Built on request
            Builds of this charm recipe are not automatically uploaded to
            the store.
            Latest builds
            Status When complete Architecture
            Successfully built 30 minutes ago i386
            """,
            self.getMainText(build.recipe),
        )

    def test_index_success_with_buildlog(self):
        # The build log is shown if it is there.
        build = self.makeBuild(
            status=BuildStatus.FULLYBUILT, duration=timedelta(minutes=30)
        )
        build.setLog(self.factory.makeLibraryFileAlias())
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""\
            Latest builds
            Status When complete Architecture
            Successfully built 30 minutes ago buildlog \(.*\) i386
            """,
            self.getMainText(build.recipe),
        )

    def test_index_no_builds(self):
        # A message is shown when there are no builds.
        recipe = self.makeCharmRecipe()
        self.assertIn(
            "This charm recipe has not been built yet.",
            self.getMainText(recipe),
        )

    def test_index_pending_build(self):
        # A pending build is listed as such.
        build = self.makeBuild()
        build.queueBuild()
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""\
            Latest builds
            Status When complete Architecture
            Needs building in .* \(estimated\) i386
            """,
            self.getMainText(build.recipe),
        )

    def test_index_pending_build_request(self):
        # A pending build request is listed as such.
        recipe = self.makeCharmRecipe()
        with person_logged_in(recipe.owner):
            recipe.requestBuilds(recipe.owner)
        self.assertTextMatchesExpressionIgnoreWhitespace(
            """\
            Latest builds
            Status When complete Architecture
            Pending build request
            """,
            self.getMainText(recipe),
        )

    def test_index_failed_build_request(self):
        # A failed build request is listed as such, with its error message.
        recipe = self.makeCharmRecipe()
        with person_logged_in(recipe.owner):
            request = recipe.requestBuilds(recipe.owner)
        job = removeSecurityProxy(removeSecurityProxy(request)._job)
        job.job._status = JobStatus.FAILED
        job.job.date_finished = datetime.now(timezone.utc) - timedelta(hours=1)
        job.error_message = "Boom"
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""\
            Latest builds
            Status When complete Architecture
            Failed build request 1 hour ago \(Boom\)
            """,
            self.getMainText(recipe),
        )

    def test_index_build_path(self):
        recipe = self.makeCharmRecipe(build_path="charm/foo")
        self.assertTextMatchesExpressionIgnoreWhitespace(
            "Build path: charm/foo", self.getMainText(recipe)
        )

    def setStatus(self, build, status):
        build.updateStatus(
            BuildStatus.BUILDING, date_started=build.date_created
        )
        build.updateStatus(
            status, date_finished=build.date_started + timedelta(minutes=30)
        )

    def test_builds_and_requests(self):
        # CharmRecipeView.builds_and_requests produces reasonable results,
        # interleaving build requests with builds.
        recipe = self.makeCharmRecipe()
        # Create oldest builds first so that they sort properly by id.
        date_gen = time_counter(
            datetime(2000, 1, 1, tzinfo=timezone.utc), timedelta(days=1)
        )
        builds = [
            self.makeBuild(recipe=recipe, date_created=next(date_gen))
            for i in range(3)
        ]
        self.setStatus(builds[2], BuildStatus.FULLYBUILT)
        with person_logged_in(recipe.owner):
            request = recipe.requestBuilds(recipe.owner)
        job = removeSecurityProxy(removeSecurityProxy(request)._job)
        job.job.date_created = next(date_gen)
        view = CharmRecipeView(recipe, None)
        # The pending build request is interleaved in date order with
        # pending builds, and these are followed by completed builds.
        self.assertThat(
            view.builds_and_requests,
            MatchesListwise(
                [
                    MatchesStructure.byEquality(id=request.id),
                    Equals(builds[1]),
                    Equals(builds[0]),
                    Equals(builds[2]),
                ]
            ),
        )
        transaction.commit()
        builds.append(self.makeBuild(recipe=recipe))
        del get_property_cache(view).builds_and_requests
        self.assertThat(
            view.builds_and_requests,
            MatchesListwise(
                [
                    Equals(builds[3]),
                    MatchesStructure.byEquality(id=request.id),
                    Equals(builds[1]),
                    Equals(builds[0]),
                    Equals(builds[2]),
                ]
            ),
        )
        # If we pretend that the job failed, it is still listed, but after
        # any pending builds.
        job.job._status = JobStatus.FAILED
        job.job.date_finished = job.date_created + timedelta(minutes=30)
        del get_property_cache(view).builds_and_requests
        self.assertThat(
            view.builds_and_requests,
            MatchesListwise(
                [
                    Equals(builds[3]),
                    Equals(builds[1]),
                    Equals(builds[0]),
                    MatchesStructure.byEquality(id=request.id),
                    Equals(builds[2]),
                ]
            ),
        )

    def test_store_channels_empty(self):
        recipe = self.factory.makeCharmRecipe()
        view = create_initialized_view(recipe, "+index")
        self.assertEqual("", view.store_channels)

    def test_store_channels_display(self):
        recipe = self.factory.makeCharmRecipe(
            store_channels=["track/stable/fix-123", "track/edge/fix-123"]
        )
        view = create_initialized_view(recipe, "+index")
        self.assertEqual(
            "track/stable/fix-123, track/edge/fix-123", view.store_channels
        )


class TestCharmRecipeRequestBuildsView(BaseTestCharmRecipeView):
    def setUp(self):
        super().setUp()
        self.project = self.factory.makeProduct(
            name="test-project", displayname="Test Project"
        )
        self.ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.distroseries = self.factory.makeDistroSeries(
            distribution=self.ubuntu
        )
        self.architectures = []
        for processor, architecture in ("386", "i386"), ("amd64", "amd64"):
            das = self.factory.makeDistroArchSeries(
                distroseries=self.distroseries,
                architecturetag=architecture,
                processor=getUtility(IProcessorSet).getByName(processor),
            )
            das.addOrUpdateChroot(self.factory.makeLibraryFileAlias())
            self.architectures.append(das)
        self.recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            owner=self.person,
            project=self.project,
            name="charm-name",
        )

    def test_request_builds_page(self):
        # The +request-builds page is sensible.
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""
            Request builds for charm-name
            Test Project
            charm-name
            Request builds
            Source snap channels:
            charmcraft
            core
            core18
            core20
            core22
            core24
            core26
            The channels to use for build tools when building the charm
            recipe.
            or
            Cancel
            """,
            self.getMainText(self.recipe, "+request-builds", user=self.person),
        )

    def test_request_builds_not_owner(self):
        # A user without launchpad.Edit cannot request builds.
        self.assertRaises(
            Unauthorized, self.getViewBrowser, self.recipe, "+request-builds"
        )

    def test_request_builds_action(self):
        # Requesting a build creates a pending build request.
        browser = self.getViewBrowser(
            self.recipe, "+request-builds", user=self.person
        )
        browser.getControl("Request builds").click()

        login_person(self.person)
        [request] = self.recipe.pending_build_requests
        self.assertThat(
            removeSecurityProxy(request),
            MatchesStructure(
                recipe=Equals(self.recipe),
                status=Equals(CharmRecipeBuildRequestStatus.PENDING),
                error_message=Is(None),
                builds=AfterPreprocessing(list, Equals([])),
                _job=MatchesStructure(
                    requester=Equals(self.person),
                    channels=Equals({}),
                    architectures=Is(None),
                ),
            ),
        )

    def test_request_builds_channels(self):
        # Selecting different channels creates a build request using those
        # channels.
        browser = self.getViewBrowser(
            self.recipe, "+request-builds", user=self.person
        )
        browser.getControl(name="field.channels.core").value = "edge"
        browser.getControl("Request builds").click()

        login_person(self.person)
        [request] = self.recipe.pending_build_requests
        self.assertEqual({"core": "edge"}, request.channels)
