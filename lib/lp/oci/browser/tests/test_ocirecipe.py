# Copyright 2020-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test OCI recipe views."""

from datetime import datetime, timedelta, timezone
from operator import attrgetter
from urllib.parse import quote

import soupmatchers
from fixtures import FakeLogger
from storm.locals import Store
from testtools.matchers import (
    Equals,
    Is,
    MatchesDict,
    MatchesSetwise,
    MatchesStructure,
    Not,
)
from zope.component import getUtility
from zope.publisher.interfaces import NotFound
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy
from zope.testbrowser.browser import LinkNotFoundError

from lp.app.browser.tales import GitRepositoryFormatterAPI
from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.buildmaster.enums import BuildStatus
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.code.interfaces.gitrepository import IGitRepositorySet
from lp.oci.browser.ocirecipe import (
    OCIRecipeAdminView,
    OCIRecipeEditView,
    OCIRecipeView,
)
from lp.oci.interfaces.ocipushrule import (
    IOCIPushRuleSet,
    OCIPushRuleAlreadyExists,
)
from lp.oci.interfaces.ocirecipe import (
    OCI_RECIPE_ALLOW_CREATE,
    CannotModifyOCIRecipeProcessor,
    IOCIRecipeSet,
    IOCIRecipeView,
)
from lp.oci.interfaces.ocirecipejob import IOCIRecipeRequestBuildsJobSource
from lp.oci.interfaces.ociregistrycredentials import IOCIRegistryCredentialsSet
from lp.oci.tests.helpers import OCIConfigHelperMixin
from lp.registry.enums import BranchSharingPolicy
from lp.registry.interfaces.person import IPersonSet
from lp.services.config import config
from lp.services.database.constants import UTC_NOW
from lp.services.database.interfaces import IStore
from lp.services.features.testing import FeatureFixture
from lp.services.job.runner import JobRunner
from lp.services.propertycache import get_property_cache
from lp.services.webapp import canonical_url
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.testing import (
    BrowserTestCase,
    TestCaseWithFactory,
    admin_logged_in,
    anonymous_logged_in,
    login,
    login_person,
    person_logged_in,
    record_two_runs,
    time_counter,
)
from lp.testing.dbuser import dbuser
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.matchers import (
    DoesNotSnapshot,
    MatchesPickerText,
    MatchesTagText,
)
from lp.testing.pages import (
    extract_text,
    find_main_content,
    find_tag_by_id,
    find_tags_by_class,
)
from lp.testing.publication import test_traverse
from lp.testing.views import create_initialized_view, create_view


class TestOCIRecipeNavigation(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))

    def test_canonical_url(self):
        owner = self.factory.makePerson(name="person")
        distribution = self.factory.makeDistribution(name="distro")
        oci_project = self.factory.makeOCIProject(
            pillar=distribution, ociprojectname="oci-project"
        )
        recipe = self.factory.makeOCIRecipe(
            name="recipe",
            registrant=owner,
            owner=owner,
            oci_project=oci_project,
        )
        self.assertEqual(
            "http://launchpad.test/~person/distro/+oci/oci-project/"
            "+recipe/recipe",
            canonical_url(recipe),
        )

    def test_recipe_traverse_distribution(self):
        # Make sure we can reach recipe of distro-based OCI projects.
        distro = self.factory.makeDistribution()
        oci_project = self.factory.makeOCIProject(pillar=distro)
        recipe = self.factory.makeOCIRecipe(oci_project=oci_project)
        obj, _, _ = test_traverse(
            "http://launchpad.test/~%s/%s/+oci/%s/+recipe/%s"
            % (
                recipe.owner.name,
                recipe.oci_project.pillar.name,
                recipe.oci_project.name,
                recipe.name,
            )
        )
        self.assertEqual(recipe, obj)

    def test_recipe_traverse_project(self):
        # Make sure we can reach recipe of project-based OCI projects.
        project = self.factory.makeProduct()
        oci_project = self.factory.makeOCIProject(pillar=project)
        recipe = self.factory.makeOCIRecipe(oci_project=oci_project)
        obj, _, _ = test_traverse(
            "http://launchpad.test/~%s/%s/+oci/%s/+recipe/%s"
            % (
                recipe.owner.name,
                recipe.oci_project.pillar.name,
                recipe.oci_project.name,
                recipe.name,
            )
        )
        self.assertEqual(recipe, obj)


class BaseTestOCIRecipeView(BrowserTestCase):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FakeLogger())
        self.person = self.factory.makePerson(
            name="test-person", displayname="Test Person"
        )


class TestOCIRecipeAddView(OCIConfigHelperMixin, BaseTestOCIRecipeView):
    def setUp(self):
        super().setUp()
        self.distroseries = self.factory.makeDistroSeries()
        self.distribution = self.distroseries.distribution
        self.useFixture(
            FeatureFixture(
                {
                    OCI_RECIPE_ALLOW_CREATE: "on",
                    "oci.build_series.%s"
                    % self.distribution.name: self.distroseries.name,
                }
            )
        )
        self.setConfig()

    def setUpDistroSeries(self):
        """Set up self.distroseries with some available processors."""
        processor_names = ["386", "amd64", "hppa"]
        for name in processor_names:
            processor = getUtility(IProcessorSet).getByName(name)
            self.factory.makeDistroArchSeries(
                distroseries=self.distroseries,
                architecturetag=name,
                processor=processor,
            )

    def assertProcessorControls(self, processors_control, enabled, disabled):
        matchers = [
            MatchesStructure.byEquality(optionValue=name, disabled=False)
            for name in enabled
        ]
        matchers.extend(
            [
                MatchesStructure.byEquality(optionValue=name, disabled=True)
                for name in disabled
            ]
        )
        self.assertThat(processors_control.controls, MatchesSetwise(*matchers))

    def test_create_new_recipe_not_logged_in(self):
        oci_project = self.factory.makeOCIProject()
        self.assertRaises(
            Unauthorized,
            self.getViewBrowser,
            oci_project,
            view_name="+new-recipe",
            no_login=True,
        )

    def test_create_new_recipe(self):
        oci_project = self.factory.makeOCIProject()
        oci_project_display = oci_project.display_name
        [git_ref] = self.factory.makeGitRefs(paths=["refs/heads/v2.0-20.04"])
        git_ref_identity = git_ref.repository.identity
        git_ref_path = git_ref.path
        source_display = git_ref.display_name
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        browser.getControl(name="field.name").value = "recipe-name"
        browser.getControl("Description").value = "Recipe description"
        browser.getControl(name="field.git_ref.repository").value = (
            git_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = git_ref_path
        browser.getControl("Create OCI recipe").click()

        content = find_main_content(browser.contents)
        self.assertEqual("recipe-name", extract_text(content.h1))
        self.assertThat(
            "Recipe description", MatchesTagText(content, "recipe-description")
        )
        self.assertThat(
            "Test Person", MatchesPickerText(content, "edit-owner")
        )
        self.assertThat(
            "OCI project:\n%s" % oci_project_display,
            MatchesTagText(content, "oci-project"),
        )
        self.assertThat(
            "Source:\n%s\nEdit OCI recipe" % source_display,
            MatchesTagText(content, "source"),
        )
        self.assertThat(
            "Build file path:\nDockerfile\n"
            "Edit OCI recipe\n"
            "Build context directory:\n.\n"
            "Edit OCI recipe",
            MatchesTagText(content, "build-file"),
        )
        self.assertThat(
            "Build schedule:\nBuilt on request\nEdit OCI recipe\n",
            MatchesTagText(content, "build-schedule"),
        )
        self.assertThat(
            "Official recipe:\nNo", MatchesTagText(content, "official-recipe")
        )

    def test_create_new_available_information_types(self):
        public_pillar = self.factory.makeProduct(owner=self.person)
        private_pillar = self.factory.makeProduct(
            owner=self.person,
            information_type=InformationType.PROPRIETARY,
            branch_sharing_policy=BranchSharingPolicy.PROPRIETARY,
        )
        public_oci_project = self.factory.makeOCIProject(
            registrant=self.person, pillar=public_pillar
        )
        private_oci_project = self.factory.makeOCIProject(
            registrant=self.person, pillar=private_pillar
        )

        # Public pillar.
        browser = self.getViewBrowser(
            public_oci_project, view_name="+new-recipe", user=self.person
        )
        self.assertContentEqual(
            ["PUBLIC", "PUBLICSECURITY", "PRIVATESECURITY", "USERDATA"],
            browser.getControl(name="field.information_type").options,
        )

        # Proprietary pillar.
        browser = self.getViewBrowser(
            private_oci_project, view_name="+new-recipe", user=self.person
        )
        self.assertContentEqual(
            ["PROPRIETARY"],
            browser.getControl(name="field.information_type").options,
        )

    def test_create_new_recipe_invalid_format(self):
        oci_project = self.factory.makeOCIProject()
        [git_ref] = self.factory.makeGitRefs(paths=["refs/heads/invalid"])
        git_ref_identity = git_ref.repository.identity
        git_ref_path = git_ref.path
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        browser.getControl(name="field.name").value = "recipe-name"
        browser.getControl("Description").value = "Recipe description"
        browser.getControl(name="field.git_ref.repository").value = (
            git_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = git_ref_path
        browser.getControl("Create OCI recipe").click()
        self.assertIn("Branch does not match format", browser.contents)

    def test_create_new_recipe_with_build_args(self):
        oci_project = self.factory.makeOCIProject()
        [git_ref] = self.factory.makeGitRefs(paths=["refs/heads/v2.0-20.04"])
        git_ref_identity = git_ref.repository.identity
        git_ref_path = git_ref.path
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        browser.getControl(name="field.name").value = "recipe-name"
        browser.getControl("Description").value = "Recipe description"
        browser.getControl(name="field.git_ref.repository").value = (
            git_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = git_ref_path
        browser.getControl("Build-time ARG variables").value = (
            "VAR1=10\nVAR2=20"
        )
        browser.getControl("Create OCI recipe").click()

        content = find_main_content(browser.contents)
        self.assertEqual("recipe-name", extract_text(content.h1))
        self.assertThat(
            "Build-time\nARG variables:\nVAR1=10\nVAR2=20",
            MatchesTagText(content, "build-args"),
        )

    def test_create_new_recipe_with_image_name(self):
        oci_project = self.factory.makeOCIProject()
        credentials = self.factory.makeOCIRegistryCredentials()
        with person_logged_in(oci_project.distribution.owner):
            oci_project.distribution.oci_registry_credentials = credentials
        [git_ref] = self.factory.makeGitRefs(paths=["refs/heads/v2.0-20.04"])
        git_ref_identity = git_ref.repository.identity
        git_ref_path = git_ref.path
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        browser.getControl(name="field.name").value = "recipe-name"
        browser.getControl("Description").value = "Recipe description"
        browser.getControl(name="field.git_ref.repository").value = (
            git_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = git_ref_path

        image_name = self.factory.getUniqueUnicode()
        browser.getControl(name="field.image_name").value = image_name
        browser.getControl("Create OCI recipe").click()
        content = find_main_content(browser.contents)
        self.assertThat(
            f"Registry image name:\n{image_name}",
            MatchesTagText(content, "image-name"),
        )

    def test_create_new_recipe_users_teams_as_owner_options(self):
        # Teams that the user is in are options for the OCI recipe owner.
        self.factory.makeTeam(
            name="test-team", displayname="Test Team", members=[self.person]
        )
        oci_project = self.factory.makeOCIProject()
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        options = browser.getControl("Owner").displayOptions
        self.assertEqual(
            ["Test Person (test-person)", "Test Team (test-team)"],
            sorted(str(option) for option in options),
        )

    def test_create_new_recipe_display_processors(self):
        self.setUpDistroSeries()
        oci_project = self.factory.makeOCIProject(pillar=self.distribution)
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(
            ["Intel 386 (386)", "AMD 64bit (amd64)", "HPPA Processor (hppa)"],
            [extract_text(option) for option in processors.displayOptions],
        )
        self.assertContentEqual(["386", "amd64", "hppa"], processors.options)
        self.assertContentEqual(["386", "amd64", "hppa"], processors.value)

    def test_create_new_recipe_display_restricted_processors(self):
        # A restricted processor is shown with a disabled (greyed out)
        # checkbox in the UI.
        self.setUpDistroSeries()
        oci_project = self.factory.makeOCIProject(pillar=self.distribution)
        proc_armhf = self.factory.makeProcessor(
            name="armhf", restricted=True, build_by_default=False
        )
        self.factory.makeDistroArchSeries(
            distroseries=self.distroseries,
            architecturetag="armhf",
            processor=proc_armhf,
        )
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        processors = browser.getControl(name="field.processors")
        self.assertProcessorControls(
            processors, ["386", "amd64", "hppa"], ["armhf"]
        )

    def test_create_new_recipe_processors(self):
        self.setUpDistroSeries()
        oci_project = self.factory.makeOCIProject(pillar=self.distribution)
        [git_ref] = self.factory.makeGitRefs(paths=["refs/heads/v2.0-20.04"])
        git_ref_identity = git_ref.repository.identity
        git_ref_path = git_ref.path
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        processors = browser.getControl(name="field.processors")
        processors.value = ["386", "amd64"]
        browser.getControl(name="field.name").value = "recipe-name"
        browser.getControl(name="field.git_ref.repository").value = (
            git_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = git_ref_path
        browser.getControl("Create OCI recipe").click()
        login_person(self.person)
        recipe = getUtility(IOCIRecipeSet).getByName(
            self.person, oci_project, "recipe-name"
        )
        self.assertContentEqual(
            ["386", "amd64"], [proc.name for proc in recipe.processors]
        )

    def test_create_new_recipe_no_default_repo_warning(self):
        self.setUpDistroSeries()
        oci_project = self.factory.makeOCIProject(pillar=self.distribution)
        with admin_logged_in():
            oci_project_url = canonical_url(oci_project)
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        error_message = (
            "The default git repository for this OCI project was not created "
            "yet.<br/>"
            'Check the <a href="{url}">OCI project page</a> for instructions '
            "on how to create one."
        ).format(url=oci_project_url)
        self.assertIn(error_message, browser.contents)

    def test_create_new_recipe_with_default_repo_already_created(self):
        self.setUpDistroSeries()
        oci_project = self.factory.makeOCIProject(pillar=self.distribution)
        repository = self.factory.makeGitRepository(
            name=oci_project.name,
            target=oci_project,
            owner=self.person,
            registrant=self.person,
        )
        with person_logged_in(self.distribution.owner):
            getUtility(IGitRepositorySet).setDefaultRepository(
                oci_project, repository
            )
            default_repo_path = "%s/+oci/%s" % (
                self.distribution.name,
                oci_project.name,
            )
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        error_message = (
            "The default git repository for this OCI project was not created "
            "yet."
        )
        self.assertNotIn(error_message, browser.contents)
        self.assertThat(
            browser.contents,
            soupmatchers.HTMLContains(
                soupmatchers.Tag(
                    "Repository pre-filled",
                    "input",
                    attrs={
                        "id": "field.git_ref.repository",
                        "value": default_repo_path,
                    },
                )
            ),
        )

    def test_official_is_disabled(self):
        oci_project = self.factory.makeOCIProject()
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        official_control = browser.getControl("Official recipe")
        self.assertTrue(official_control.disabled)

    def test_official_is_enabled(self):
        distribution = self.factory.makeDistribution(
            oci_project_admin=self.person
        )
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        official_control = browser.getControl("Official recipe")
        self.assertFalse(official_control.disabled)

    def test_set_official(self):
        distribution = self.factory.makeDistribution(
            oci_project_admin=self.person
        )
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        [git_ref] = self.factory.makeGitRefs(paths=["refs/heads/v2.0-20.04"])
        git_ref_identity = git_ref.repository.identity
        git_ref_path = git_ref.path
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        browser.getControl(name="field.name").value = "recipe-name"
        browser.getControl("Description").value = "Recipe description"
        browser.getControl(name="field.git_ref.repository").value = (
            git_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = git_ref_path
        official_control = browser.getControl("Official recipe")
        official_control.selected = True
        browser.getControl("Create OCI recipe").click()

        content = find_main_content(browser.contents)
        self.assertThat(
            "Official recipe:\nYes", MatchesTagText(content, "official-recipe")
        )

    def test_set_official_multiple(self):
        distribution = self.factory.makeDistribution(
            oci_project_admin=self.person
        )

        # do it once
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        [git_ref] = self.factory.makeGitRefs(paths=["refs/heads/v2.0-20.04"])
        git_ref_identity = git_ref.repository.identity
        git_ref_path = git_ref.path

        # and then do it again
        oci_project2 = self.factory.makeOCIProject(pillar=distribution)
        [git_ref2] = self.factory.makeGitRefs(paths=["refs/heads/v3.0-20.04"])
        git_ref2_identity = git_ref2.repository.identity
        git_ref2_path = git_ref2.path
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        browser.getControl(name="field.name").value = "recipe-name"
        browser.getControl("Description").value = "Recipe description"
        browser.getControl(name="field.git_ref.repository").value = (
            git_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = git_ref_path
        official_control = browser.getControl("Official recipe")
        official_control.selected = True
        browser.getControl("Create OCI recipe").click()

        content = find_main_content(browser.contents)
        self.assertThat(
            "Official recipe:\nYes", MatchesTagText(content, "official-recipe")
        )

        browser2 = self.getViewBrowser(
            oci_project2, view_name="+new-recipe", user=self.person
        )
        browser2.getControl(name="field.name").value = "recipe-name"
        browser2.getControl("Description").value = "Recipe description"
        browser2.getControl(name="field.git_ref.repository").value = (
            git_ref2_identity
        )
        browser2.getControl(name="field.git_ref.path").value = git_ref2_path
        official_control = browser2.getControl("Official recipe")
        official_control.selected = True
        browser2.getControl("Create OCI recipe").click()

        content = find_main_content(browser2.contents)
        self.assertThat(
            "Official recipe:\nYes", MatchesTagText(content, "official-recipe")
        )

        browser.reload()
        content = find_main_content(browser.contents)
        self.assertThat(
            "Official recipe:\nYes", MatchesTagText(content, "official-recipe")
        )

    def test_set_official_no_permissions(self):
        distro_owner = self.factory.makePerson()
        distribution = self.factory.makeDistribution(
            oci_project_admin=distro_owner
        )
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        [git_ref] = self.factory.makeGitRefs(paths=["refs/heads/v2.0-20.04"])
        git_ref_identity = git_ref.repository.identity
        git_ref_path = git_ref.path
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        browser.getControl(name="field.name").value = "recipe-name"
        browser.getControl("Description").value = "Recipe description"
        browser.getControl(name="field.git_ref.repository").value = (
            git_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = git_ref_path
        official_control = browser.getControl("Official recipe")
        official_control.selected = True
        browser.getControl("Create OCI recipe").click()

        error_message = (
            "You do not have permission to set the official status "
            "of this recipe."
        )
        self.assertIn(error_message, browser.contents)

    def test_create_recipe_doesnt_override_gitref_errors(self):
        oci_project = self.factory.makeOCIProject()
        [git_ref] = self.factory.makeGitRefs(paths=["refs/heads/v2.0-20.04"])
        git_ref_identity = git_ref.repository.identity
        browser = self.getViewBrowser(
            oci_project, view_name="+new-recipe", user=self.person
        )
        browser.getControl(name="field.name").value = "recipe-name"
        browser.getControl("Description").value = "Recipe description"
        browser.getControl(name="field.git_ref.repository").value = (
            git_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = "non-exist"
        browser.getControl("Create OCI recipe").click()

        error_message = "does not contain a branch named"
        self.assertIn(error_message, browser.contents)


class TestOCIRecipeAdminView(BaseTestOCIRecipeView):
    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))

    def test_unauthorized(self):
        # A non-admin user cannot administer an OCI recipe.
        login_person(self.person)
        recipe = self.factory.makeOCIRecipe(registrant=self.person)
        recipe_url = canonical_url(recipe)
        browser = self.getViewBrowser(recipe, user=self.person)
        self.assertRaises(
            LinkNotFoundError, browser.getLink, "Administer OCI recipe"
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
        commercial_admin = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).commercial_admin]
        )
        login_person(self.person)
        recipe = self.factory.makeOCIRecipe(registrant=self.person)
        self.assertTrue(recipe.require_virtualized)
        self.assertTrue(recipe.allow_internet)

        browser = self.getViewBrowser(recipe, user=commercial_admin)
        browser.getLink("Administer OCI recipe").click()
        browser.getControl("Require virtualized builders").selected = False
        browser.getControl("Allow external network access").selected = False
        browser.getControl("Update OCI recipe").click()

        login_person(self.person)
        self.assertFalse(recipe.require_virtualized)
        self.assertFalse(recipe.allow_internet)

    def test_admin_recipe_sets_date_last_modified(self):
        # Administering an OCI recipe sets the date_last_modified property.
        login("admin@canonical.com")
        ppa_admin = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).ppa_admin]
        )
        login_person(self.person)
        date_created = datetime(2000, 1, 1, tzinfo=timezone.utc)
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, date_created=date_created
        )
        login_person(ppa_admin)
        view = OCIRecipeAdminView(recipe, LaunchpadTestRequest())
        view.initialize()
        view.request_action.success({"require_virtualized": False})
        self.assertSqlAttributeEqualsDate(
            recipe, "date_last_modified", UTC_NOW
        )


class TestOCIRecipeEditView(OCIConfigHelperMixin, BaseTestOCIRecipeView):
    def setUp(self):
        super().setUp()
        self.distroseries = self.factory.makeDistroSeries()
        self.distribution = self.distroseries.distribution
        self.useFixture(
            FeatureFixture(
                {
                    OCI_RECIPE_ALLOW_CREATE: "on",
                    "oci.build_series.%s"
                    % self.distribution.name: self.distroseries.name,
                }
            )
        )
        self.setConfig()

    def setUpDistroSeries(self):
        """Set up self.distroseries with some available processors."""
        processor_names = ["386", "amd64", "hppa"]
        for name in processor_names:
            processor = getUtility(IProcessorSet).getByName(name)
            self.factory.makeDistroArchSeries(
                distroseries=self.distroseries,
                architecturetag=name,
                processor=processor,
            )

    def assertRecipeProcessors(self, recipe, names):
        self.assertContentEqual(
            names, [processor.name for processor in recipe.processors]
        )

    def assertProcessorControls(self, processors_control, enabled, disabled):
        matchers = [
            MatchesStructure.byEquality(optionValue=name, disabled=False)
            for name in enabled
        ]
        matchers.extend(
            [
                MatchesStructure.byEquality(optionValue=name, disabled=True)
                for name in disabled
            ]
        )
        self.assertThat(processors_control.controls, MatchesSetwise(*matchers))

    def assertShowsPrivateBanner(self, browser):
        banners = find_tags_by_class(
            browser.contents, "private_banner_container"
        )
        self.assertEqual(1, len(banners))
        self.assertEqual(
            "The information on this page is private.",
            extract_text(banners[0]),
        )

    def test_edit_private_recipe_shows_banner(self):
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person,
            owner=self.person,
            information_type=InformationType.USERDATA,
        )
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit OCI recipe").click()
        self.assertShowsPrivateBanner(browser)

    def test_edit_recipe(self):
        oci_project = self.factory.makeOCIProject()
        oci_project_display = oci_project.display_name
        [old_git_ref] = self.factory.makeGitRefs(
            paths=["refs/heads/v1.0-20.04"]
        )
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person,
            owner=self.person,
            oci_project=oci_project,
            git_ref=old_git_ref,
        )
        self.factory.makeTeam(
            name="new-team", displayname="New Team", members=[self.person]
        )
        [new_git_ref] = self.factory.makeGitRefs(
            paths=["refs/heads/v2.0-20.04"]
        )
        new_git_ref_display_name = new_git_ref.display_name
        new_git_ref_identity = new_git_ref.repository.identity
        new_git_ref_path = new_git_ref.path
        self.factory.makeOCIPushRule(recipe=recipe)

        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit OCI recipe").click()
        browser.getControl("Owner").value = ["new-team"]
        browser.getControl(name="field.name").value = "new-name"
        browser.getControl("Description").value = "New description"
        browser.getControl(name="field.git_ref.repository").value = (
            new_git_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = new_git_ref_path
        browser.getControl("Build file path").value = "Dockerfile-2"
        browser.getControl("Build directory context").value = "apath"
        browser.getControl("Build daily").selected = True
        browser.getControl("Update OCI recipe").click()

        content = find_main_content(browser.contents)
        self.assertEqual("new-name", extract_text(content.h1))
        self.assertThat("New Team", MatchesPickerText(content, "edit-owner"))
        self.assertThat(
            "OCI project:\n%s" % oci_project_display,
            MatchesTagText(content, "oci-project"),
        )
        self.assertThat(
            "Source:\n%s\nEdit OCI recipe" % new_git_ref_display_name,
            MatchesTagText(content, "source"),
        )
        self.assertThat(
            "Build file path:\nDockerfile-2\n"
            "Edit OCI recipe\n"
            "Build context directory:\napath\n"
            "Edit OCI recipe",
            MatchesTagText(content, "build-file"),
        )
        self.assertThat(
            "Build schedule:\nBuilt daily\nEdit OCI recipe\n",
            MatchesTagText(content, "build-schedule"),
        )

    def test_edit_recipe_invalid_branch(self):
        oci_project = self.factory.makeOCIProject()
        repository = self.factory.makeGitRepository()
        [old_git_ref] = self.factory.makeGitRefs(
            paths=["refs/heads/v1.0-20.04"], repository=repository
        )
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person,
            owner=self.person,
            oci_project=oci_project,
            git_ref=old_git_ref,
        )
        self.factory.makeTeam(
            name="new-team", displayname="New Team", members=[self.person]
        )
        [new_git_ref] = self.factory.makeGitRefs(
            repository=repository, paths=["refs/heads/invalid"]
        )
        self.factory.makeOCIPushRule(recipe=recipe)

        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit OCI recipe").click()
        browser.getControl(name="field.git_ref.path").value = (
            "refs/heads/invalid"
        )
        browser.getControl("Update OCI recipe").click()
        self.assertIn("Branch does not match format", browser.contents)

    def test_edit_can_make_recipe_private(self):
        pillar = self.factory.makeProduct(
            owner=self.person,
            information_type=InformationType.PUBLIC,
            branch_sharing_policy=BranchSharingPolicy.PUBLIC_OR_PROPRIETARY,
        )
        oci_project = self.factory.makeOCIProject(
            registrant=self.person, pillar=pillar
        )
        [git_ref] = self.factory.makeGitRefs(
            owner=self.person, paths=["refs/heads/v2.0-20.04"]
        )
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person,
            owner=self.person,
            oci_project=oci_project,
            git_ref=git_ref,
            information_type=InformationType.PUBLIC,
        )

        browser = self.getViewBrowser(recipe, "+edit", user=self.person)

        # Make sure we are showing all available information types:
        info_type_field = browser.getControl(name="field.information_type")
        self.assertContentEqual(
            [
                "PUBLIC",
                "PUBLICSECURITY",
                "PRIVATESECURITY",
                "USERDATA",
                "PROPRIETARY",
            ],
            info_type_field.options,
        )

        info_type_field.value = InformationType.PROPRIETARY.name
        browser.getControl("Update OCI recipe").click()
        self.assertShowsPrivateBanner(browser)

    def test_edit_recipe_on_public_pillar_information_types(self):
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, owner=self.person
        )
        browser = self.getViewBrowser(recipe, "+edit", user=self.person)

        info_type_field = browser.getControl(name="field.information_type")
        self.assertContentEqual(
            ["PUBLIC", "PUBLICSECURITY", "PRIVATESECURITY", "USERDATA"],
            info_type_field.options,
        )

    def test_edit_recipe_sets_date_last_modified(self):
        # Editing an OCI recipe sets the date_last_modified property.
        date_created = datetime(2000, 1, 1, tzinfo=timezone.utc)
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, date_created=date_created
        )
        with person_logged_in(self.person):
            view = OCIRecipeEditView(recipe, LaunchpadTestRequest())
            view.initialize()
            view.request_action.success(
                {
                    "owner": recipe.owner,
                    "name": "changed",
                    "description": "changed",
                }
            )
        self.assertSqlAttributeEqualsDate(
            recipe, "date_last_modified", UTC_NOW
        )

    def test_edit_recipe_already_exists(self):
        oci_project = self.factory.makeOCIProject()
        oci_project_display = oci_project.display_name
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person,
            owner=self.person,
            oci_project=oci_project,
            name="one",
        )
        self.factory.makeOCIRecipe(
            registrant=self.person,
            owner=self.person,
            oci_project=oci_project,
            name="two",
        )
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit OCI recipe").click()
        browser.getControl(name="field.name").value = "two"
        browser.getControl("Update OCI recipe").click()
        self.assertEqual(
            "There is already an OCI recipe owned by Test Person in %s with "
            "this name." % oci_project_display,
            extract_text(find_tags_by_class(browser.contents, "message")[1]),
        )

    def test_display_processors(self):
        self.setUpDistroSeries()
        oci_project = self.factory.makeOCIProject(pillar=self.distribution)
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, owner=self.person, oci_project=oci_project
        )
        browser = self.getViewBrowser(
            recipe, view_name="+edit", user=recipe.owner
        )
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(
            ["Intel 386 (386)", "AMD 64bit (amd64)", "HPPA Processor (hppa)"],
            [extract_text(option) for option in processors.displayOptions],
        )
        self.assertContentEqual(["386", "amd64", "hppa"], processors.options)

    def test_edit_processors(self):
        self.setUpDistroSeries()
        oci_project = self.factory.makeOCIProject(pillar=self.distribution)
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, owner=self.person, oci_project=oci_project
        )
        self.assertRecipeProcessors(recipe, ["386", "amd64", "hppa"])
        browser = self.getViewBrowser(
            recipe, view_name="+edit", user=recipe.owner
        )
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(["386", "amd64", "hppa"], processors.value)
        processors.value = ["386", "amd64"]
        browser.getControl("Update OCI recipe").click()
        login_person(self.person)
        self.assertRecipeProcessors(recipe, ["386", "amd64"])

    def test_edit_build_args(self):
        self.setUpDistroSeries()
        oci_project = self.factory.makeOCIProject(pillar=self.distribution)
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person,
            owner=self.person,
            oci_project=oci_project,
            build_args={"VAR1": "xxx", "VAR2": "uu"},
        )
        browser = self.getViewBrowser(
            recipe, view_name="+edit", user=recipe.owner
        )
        args = browser.getControl(name="field.build_args")
        self.assertContentEqual("VAR1=xxx\r\nVAR2=uu", args.value)
        args.value = "VAR=aa\nANOTHER_VAR=bbb"
        browser.getControl("Update OCI recipe").click()
        login_person(self.person)
        IStore(recipe).reload(recipe)
        self.assertEqual(
            {"VAR": "aa", "ANOTHER_VAR": "bbb"}, recipe.build_args
        )

    def test_edit_build_args_invalid_content(self):
        self.setUpDistroSeries()
        oci_project = self.factory.makeOCIProject(pillar=self.distribution)
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person,
            owner=self.person,
            oci_project=oci_project,
            build_args={"VAR1": "xxx", "VAR2": "uu"},
        )
        browser = self.getViewBrowser(
            recipe, view_name="+edit", user=recipe.owner
        )
        args = browser.getControl(name="field.build_args")
        self.assertContentEqual("VAR1=xxx\r\nVAR2=uu", args.value)
        args.value = "VAR=aa\nmessed up text"
        browser.getControl("Update OCI recipe").click()

        # Error message should be shown.
        content = find_main_content(browser.contents)
        self.assertIn(
            "'messed up text' at line 2 is not a valid KEY=value pair.",
            extract_text(content),
        )

        # Assert that recipe still have the original build_args.
        login_person(self.person)
        IStore(recipe).reload(recipe)
        self.assertEqual({"VAR1": "xxx", "VAR2": "uu"}, recipe.build_args)

    def test_edit_image_name(self):
        self.setUpDistroSeries()
        credentials = self.factory.makeOCIRegistryCredentials()
        original_name = self.factory.getUniqueUnicode()
        with person_logged_in(self.distribution.owner):
            self.distribution.oci_registry_credentials = credentials
            oci_project = self.factory.makeOCIProject(pillar=self.distribution)
            recipe = self.factory.makeOCIRecipe(
                name=original_name,
                registrant=self.person,
                owner=self.person,
                oci_project=oci_project,
            )
            oci_project.setOfficialRecipeStatus(recipe, True)
        browser = self.getViewBrowser(
            recipe, view_name="+edit", user=recipe.owner
        )
        image_name = self.factory.getUniqueUnicode()
        field = browser.getControl(name="field.image_name")
        # Default is the recipe name
        self.assertEqual(field.value, original_name)
        field.value = image_name
        browser.getControl("Update OCI recipe").click()
        content = find_main_content(browser.contents)
        self.assertThat(
            f"Registry image name:\n{image_name}",
            MatchesTagText(content, "image-name"),
        )

    def test_edit_with_invisible_processor(self):
        # It's possible for existing recipes to have an enabled processor
        # that's no longer usable with the current distroseries, which will
        # mean it's hidden from the UI, but the non-admin
        # OCIRecipe.setProcessors isn't allowed to disable it.  Editing the
        # processor list of such a recipe leaves the invisible processor
        # intact.
        proc_386 = getUtility(IProcessorSet).getByName("386")
        proc_amd64 = getUtility(IProcessorSet).getByName("amd64")
        proc_armel = self.factory.makeProcessor(
            name="armel", restricted=True, build_by_default=False
        )
        self.setUpDistroSeries()
        oci_project = self.factory.makeOCIProject(pillar=self.distribution)
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, owner=self.person, oci_project=oci_project
        )
        recipe.setProcessors([proc_386, proc_amd64, proc_armel])
        browser = self.getViewBrowser(
            recipe, view_name="+edit", user=recipe.owner
        )
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(["386", "amd64"], processors.value)
        processors.value = ["amd64"]
        browser.getControl("Update OCI recipe").click()
        login_person(self.person)
        self.assertRecipeProcessors(recipe, ["amd64", "armel"])

    def test_edit_processors_restricted(self):
        # A restricted processor is shown with a disabled (greyed out)
        # checkbox in the UI, and the processor cannot be enabled.
        self.setUpDistroSeries()
        proc_armhf = self.factory.makeProcessor(
            name="armhf", restricted=True, build_by_default=False
        )
        self.factory.makeDistroArchSeries(
            distroseries=self.distroseries,
            architecturetag="armhf",
            processor=proc_armhf,
        )
        oci_project = self.factory.makeOCIProject(pillar=self.distribution)
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, owner=self.person, oci_project=oci_project
        )
        self.assertRecipeProcessors(recipe, ["386", "amd64", "hppa"])
        browser = self.getViewBrowser(
            recipe, view_name="+edit", user=recipe.owner
        )
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(["386", "amd64", "hppa"], processors.value)
        self.assertProcessorControls(
            processors, ["386", "amd64", "hppa"], ["armhf"]
        )
        # Even if the user works around the disabled checkbox and forcibly
        # enables it, they can't enable the restricted processor.
        for control in processors.controls:
            if control.optionValue == "armhf":
                del control._control.attrs["disabled"]
        processors.value = ["386", "amd64", "armhf"]
        self.assertRaises(
            CannotModifyOCIRecipeProcessor,
            browser.getControl("Update OCI recipe").click,
        )

    def test_edit_processors_restricted_already_enabled(self):
        # A restricted processor that is already enabled is shown with a
        # disabled (greyed out) checkbox in the UI.  This causes form
        # submission to omit it, but the validation code fixes that up
        # behind the scenes so that we don't get
        # CannotModifyOCIRecipeProcessor.
        proc_386 = getUtility(IProcessorSet).getByName("386")
        proc_amd64 = getUtility(IProcessorSet).getByName("amd64")
        proc_armhf = self.factory.makeProcessor(
            name="armhf", restricted=True, build_by_default=False
        )
        self.setUpDistroSeries()
        self.factory.makeDistroArchSeries(
            distroseries=self.distroseries,
            architecturetag="armhf",
            processor=proc_armhf,
        )
        oci_project = self.factory.makeOCIProject(pillar=self.distribution)
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, owner=self.person, oci_project=oci_project
        )
        recipe.setProcessors([proc_386, proc_amd64, proc_armhf])
        self.assertRecipeProcessors(recipe, ["386", "amd64", "armhf"])
        browser = self.getUserBrowser(
            canonical_url(recipe) + "/+edit", user=recipe.owner
        )
        processors = browser.getControl(name="field.processors")
        # armhf is checked but disabled.
        self.assertContentEqual(["386", "amd64", "armhf"], processors.value)
        self.assertProcessorControls(
            processors, ["386", "amd64", "hppa"], ["armhf"]
        )
        processors.value = ["386"]
        browser.getControl("Update OCI recipe").click()
        login_person(self.person)
        self.assertRecipeProcessors(recipe, ["386", "armhf"])

    def test_edit_without_default_repo_for_ociproject(self):
        self.setUpDistroSeries()
        repo = self.factory.makeGitRepository(
            owner=self.person, registrant=self.person
        )
        [git_ref] = self.factory.makeGitRefs(
            repository=repo, paths=["refs/heads/v1.0-20.04"]
        )
        oci_project = self.factory.makeOCIProject(
            registrant=self.person, pillar=self.distribution
        )
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, oci_project=oci_project, git_ref=git_ref
        )
        with person_logged_in(self.person):
            oci_project_url = canonical_url(oci_project)
            browser = self.getViewBrowser(
                recipe, view_name="+edit", user=self.person
            )
        error_message = (
            "This recipe's git repository is not in the correct "
            'namespace.<br/>Check the <a href="{url}">OCI project page</a> '
            "for instructions on how to create it correctly."
        )
        self.assertIn(
            error_message.format(url=oci_project_url), browser.contents
        )

    def test_edit_repository_is_not_default_for_ociproject(self):
        self.setUpDistroSeries()
        oci_project = self.factory.makeOCIProject(
            registrant=self.person, pillar=self.distribution
        )
        [random_git_ref] = self.factory.makeGitRefs(
            paths=["refs/heads/v1.0-20.04"]
        )
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person,
            owner=self.person,
            oci_project=oci_project,
            git_ref=random_git_ref,
        )

        # Make the default git repository that should have been used by the
        # recipe.
        default_repo = self.factory.makeGitRepository(
            name=oci_project.name,
            target=oci_project,
            owner=self.person,
            registrant=self.person,
        )
        with person_logged_in(self.distribution.owner):
            getUtility(IGitRepositorySet).setDefaultRepository(
                oci_project, default_repo
            )

        with person_logged_in(self.person):
            repo_link = GitRepositoryFormatterAPI(default_repo).link("")
            browser = self.getViewBrowser(
                recipe, view_name="+edit", user=self.person
            )
        error_message = (
            "This recipe's git repository is not in the correct "
            "namespace.<br/>Consider using {repo} instead."
        )
        self.assertIn(error_message.format(repo=repo_link), browser.contents)

    def test_edit_repository_in_the_correct_namespace(self):
        self.setUpDistroSeries()
        oci_project = self.factory.makeOCIProject(
            registrant=self.person, pillar=self.distribution
        )
        default_repo = self.factory.makeGitRepository(
            name=oci_project.name,
            target=oci_project,
            owner=self.person,
            registrant=self.person,
        )

        [git_ref] = self.factory.makeGitRefs(
            repository=default_repo, paths=["refs/heads/v1.0-20.04"]
        )
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person,
            owner=self.person,
            oci_project=oci_project,
            git_ref=git_ref,
        )

        with person_logged_in(self.person):
            browser = self.getViewBrowser(
                recipe, view_name="+edit", user=self.person
            )
        self.assertNotIn(
            "This recipe's git repository is not in the correct namespace",
            browser.contents,
        )

    def test_edit_repository_dont_override_important_msgs(self):
        self.setUpDistroSeries()
        oci_project = self.factory.makeOCIProject(
            registrant=self.person, pillar=self.distribution
        )

        [git_ref] = self.factory.makeGitRefs(paths=["refs/heads/v1.0-20.04"])
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person,
            owner=self.person,
            oci_project=oci_project,
            git_ref=git_ref,
        )

        wrong_namespace_msg = (
            "This recipe's git repository is not in the correct namespace"
        )
        wrong_ref_path_msg = (
            "The repository at %s does not contain a branch named "
            "&#x27;non-existing git-ref&#x27;."
        ) % git_ref.repository.display_name
        with person_logged_in(self.person):
            browser = self.getViewBrowser(
                recipe, view_name="+edit", user=self.person
            )
            self.assertIn(wrong_namespace_msg, browser.contents)
            args = browser.getControl(name="field.git_ref.path")
            args.value = "non-existing git-ref"
            browser.getControl("Update OCI recipe").click()

            # The error message should have changed.
            self.assertNotIn(wrong_namespace_msg, browser.contents)
            self.assertIn(wrong_ref_path_msg, browser.contents)

    def test_official_is_disabled(self):
        oci_project = self.factory.makeOCIProject()
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, owner=self.person, oci_project=oci_project
        )

        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit OCI recipe").click()
        official_control = browser.getControl("Official recipe")
        self.assertTrue(official_control.disabled)

    def test_official_is_set_while_disabled(self):
        distribution = self.factory.makeDistribution(
            oci_project_admin=self.person
        )
        non_admin = self.factory.makePerson()
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        recipe = self.factory.makeOCIRecipe(
            registrant=non_admin, owner=non_admin, oci_project=oci_project
        )
        with person_logged_in(self.person):
            oci_project.setOfficialRecipeStatus(recipe, True)
        browser = self.getViewBrowser(recipe, user=non_admin)
        browser.getLink("Edit OCI recipe").click()
        official_control = browser.getControl("Official recipe")
        self.assertTrue(official_control.disabled)
        self.assertTrue(official_control.selected)

    def test_official_is_enabled(self):
        distribution = self.factory.makeDistribution(
            oci_project_admin=self.person
        )
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, owner=self.person, oci_project=oci_project
        )

        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit OCI recipe").click()
        official_control = browser.getControl("Official recipe")
        self.assertFalse(official_control.disabled)

    def test_set_official(self):
        distribution = self.factory.makeDistribution(
            oci_project_admin=self.person
        )
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, owner=self.person, oci_project=oci_project
        )

        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit OCI recipe").click()
        official_control = browser.getControl("Official recipe")
        official_control.selected = True
        browser.getControl("Update OCI recipe").click()

        content = find_main_content(browser.contents)
        self.assertThat(
            "Official recipe:\nYes", MatchesTagText(content, "official-recipe")
        )

    def test_set_official_no_permissions(self):
        distro_owner = self.factory.makePerson()
        distribution = self.factory.makeDistribution(
            oci_project_admin=distro_owner
        )
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, owner=self.person, oci_project=oci_project
        )

        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Edit OCI recipe").click()
        official_control = browser.getControl("Official recipe")
        official_control.selected = True
        browser.getControl("Update OCI recipe").click()

        error_message = (
            "You do not have permission to change the official status "
            "of this recipe."
        )
        self.assertIn(error_message, browser.contents)


class TestOCIRecipeDeleteView(BaseTestOCIRecipeView):
    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))

    def test_unauthorized(self):
        # A user without edit access cannot delete an OCI recipe.
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, owner=self.person
        )
        recipe_url = canonical_url(recipe)
        other_person = self.factory.makePerson()
        browser = self.getViewBrowser(recipe, user=other_person)
        self.assertRaises(
            LinkNotFoundError, browser.getLink, "Delete OCI recipe"
        )
        self.assertRaises(
            Unauthorized,
            self.getUserBrowser,
            recipe_url + "/+delete",
            user=other_person,
        )

    def test_delete_recipe_without_builds(self):
        # An OCI recipe without builds can be deleted.
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, owner=self.person
        )
        recipe_url = canonical_url(recipe)
        oci_project_url = canonical_url(recipe.oci_project)
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Delete OCI recipe").click()
        browser.getControl("Delete OCI recipe").click()
        self.assertEqual(oci_project_url, browser.url)
        self.assertRaises(NotFound, browser.open, recipe_url)

    def test_delete_recipe_with_builds(self):
        # An OCI recipe with builds can be deleted.
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person, owner=self.person
        )
        ocibuild = self.factory.makeOCIRecipeBuild(recipe=recipe)
        job = self.factory.makeOCIRecipeBuildJob(build=ocibuild)
        ocifile = self.factory.makeOCIFile(build=ocibuild)

        unrelated_build = self.factory.makeOCIRecipeBuild()
        unrelated_job = self.factory.makeOCIRecipeBuildJob()
        unrelated_file = self.factory.makeOCIFile(build=unrelated_build)

        recipe_url = canonical_url(recipe)
        oci_project_url = canonical_url(recipe.oci_project)
        browser = self.getViewBrowser(recipe, user=self.person)
        browser.getLink("Delete OCI recipe").click()
        browser.getControl("Delete OCI recipe").click()
        self.assertEqual(oci_project_url, browser.url)
        self.assertRaises(NotFound, browser.open, recipe_url)

        # Checks that only the related artifacts were deleted too.
        def obj_exists(obj, search_key="id"):
            obj = removeSecurityProxy(obj)
            store = IStore(obj)
            cls = obj.__class__
            cls_attribute = getattr(cls, search_key)
            identifier = getattr(obj, search_key)
            return not store.find(cls, cls_attribute == identifier).is_empty()

        self.assertFalse(obj_exists(ocibuild))
        self.assertFalse(obj_exists(ocifile))
        self.assertFalse(obj_exists(job, "job_id"))

        self.assertTrue(obj_exists(unrelated_build))
        self.assertTrue(obj_exists(unrelated_file))
        self.assertTrue(obj_exists(unrelated_job, "job_id"))


class TestOCIRecipeView(BaseTestOCIRecipeView):
    def setUp(self):
        super().setUp()
        self.distroseries = self.factory.makeDistroSeries()
        processor = getUtility(IProcessorSet).getByName("386")
        self.distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries,
            architecturetag="i386",
            processor=processor,
        )
        self.factory.makeBuilder(virtualized=True)
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))

    def makeOCIRecipe(self, oci_project=None, **kwargs):
        if oci_project is None:
            oci_project = self.factory.makeOCIProject(
                pillar=self.distroseries.distribution
            )
        return self.factory.makeOCIRecipe(
            registrant=self.person,
            owner=self.person,
            name="recipe-name",
            oci_project=oci_project,
            **kwargs,
        )

    def test_avoids_problematic_snapshots(self):
        """Do not snapshot fields with too many items."""
        self.assertThat(
            self.factory.makeOCIRecipe(),
            DoesNotSnapshot(
                [
                    "builds",
                    "completed_builds",
                    "completed_builds_without_build_request",
                    "pending_builds",
                ],
                IOCIRecipeView,
            ),
        )

    def makeBuild(self, recipe=None, date_created=None, **kwargs):
        if recipe is None:
            recipe = self.makeOCIRecipe()
        if date_created is None:
            date_created = datetime.now(timezone.utc) - timedelta(hours=1)
        return self.factory.makeOCIRecipeBuild(
            requester=self.person,
            recipe=recipe,
            distro_arch_series=self.distroarchseries,
            date_created=date_created,
            **kwargs,
        )

    def test_breadcrumb_and_top_header(self):
        oci_project = self.factory.makeOCIProject(
            pillar=self.distroseries.distribution
        )
        oci_project_name = oci_project.name
        oci_project_url = canonical_url(oci_project)
        pillar_name = oci_project.pillar.name
        pillar_url = canonical_url(oci_project.pillar)
        recipe = self.makeOCIRecipe(oci_project=oci_project)
        view = create_view(recipe, "+index")
        # To test the breadcrumbs we need a correct traversal stack.
        view.request.traversed_objects = [self.person, recipe, view]
        view.initialize()
        content = view()
        breadcrumbs = soupmatchers.Tag(
            "breadcrumbs", "ol", attrs={"class": "breadcrumbs"}
        )

        # Should not have a breadcrumbs (OCI project link should be at the
        # top of the page, close to project/distribution name).
        self.assertThat(content, Not(soupmatchers.HTMLContains(breadcrumbs)))

        # OCI project should appear at the top header, right after pillar link.
        header = soupmatchers.Tag(
            "subtitle", "h2", attrs={"id": "watermark-heading"}
        )
        self.assertThat(
            content,
            soupmatchers.HTMLContains(
                soupmatchers.Within(
                    header,
                    soupmatchers.Tag(
                        "pillar link",
                        "a",
                        text=pillar_name.title(),
                        attrs={"href": pillar_url},
                    ),
                )
            ),
        )
        self.assertThat(
            content,
            soupmatchers.HTMLContains(
                soupmatchers.Within(
                    header,
                    soupmatchers.Tag(
                        "OCI project link",
                        "a",
                        text="%s OCI project" % oci_project_name,
                        attrs={"href": oci_project_url},
                    ),
                )
            ),
        )

    def makeRecipe(self, processor_names, **kwargs):
        recipe = self.factory.makeOCIRecipe(**kwargs)
        processors_list = []
        distroseries = self.factory.makeDistroSeries(
            distribution=recipe.oci_project.distribution
        )
        for proc_name in processor_names:
            proc = getUtility(IProcessorSet).getByName(proc_name)
            distro = self.factory.makeDistroArchSeries(
                distroseries=distroseries,
                architecturetag=proc_name,
                processor=proc,
            )
            distro.addOrUpdateChroot(self.factory.makeLibraryFileAlias())
            processors_list.append(proc)
        recipe.setProcessors(processors_list)
        return recipe

    def test_index(self):
        oci_project = self.factory.makeOCIProject(
            pillar=self.distroseries.distribution
        )
        oci_project_display = oci_project.display_name
        [ref] = self.factory.makeGitRefs(
            owner=self.person,
            target=self.person,
            name="recipe-repository",
            paths=["refs/heads/v1.0-20.04"],
        )
        recipe = self.makeRecipe(
            processor_names=["amd64", "386"],
            build_file="Dockerfile",
            git_ref=ref,
            oci_project=oci_project,
            registrant=self.person,
            owner=self.person,
        )
        build_request = recipe.requestBuilds(self.person)
        builds = recipe.requestBuildsFromJob(self.person, build_request)
        job = removeSecurityProxy(build_request).job
        removeSecurityProxy(job).builds = builds

        for build in builds:
            removeSecurityProxy(build).updateStatus(
                BuildStatus.BUILDING,
                builder=None,
                date_started=build.date_created,
            )
            removeSecurityProxy(build).updateStatus(
                BuildStatus.FULLYBUILT,
                builder=None,
                date_finished=build.date_started + timedelta(minutes=30),
            )

        # We also need to account for builds that don't have a build_request
        build = self.makeBuild(
            recipe=recipe,
            status=BuildStatus.FULLYBUILT,
            duration=timedelta(minutes=30),
        )

        browser = self.getViewBrowser(build_request.recipe)
        login_person(self.person)
        self.assertTextMatchesExpressionIgnoreWhitespace(
            """\
            .*
            OCI recipe information
            Owner: Test Person
            OCI project: %s
            Source: ~test-person/\\+git/recipe-repository:v1.0-20.04
            Build file path: Dockerfile
            Build context directory: %s
            Build schedule: Built on request
            Official recipe:
            No
            Latest builds
            Build status
            Upload status
            When requested
            When complete
            All builds were built successfully.
            No registry upload requested.
            a moment ago
            in 29 minutes
            amd64
            Successfully built
            386
            Successfully built
            amd64
            in 29 minutes
            386
            in 29 minutes
            All builds were built successfully.
            No registry upload requested.
            1 hour ago
            30 minutes ago
            386
            Successfully built
            386
            30 minutes ago
            Recipe push rules
            This OCI recipe has no push rules defined yet.
            """
            % (oci_project_display, recipe.build_path),
            extract_text(find_main_content(browser.contents)),
        )

        # Check portlet on side menu.
        privacy_tag = find_tag_by_id(browser.contents, "privacy")
        self.assertTextMatchesExpressionIgnoreWhitespace(
            "This OCI recipe contains Public information",
            extract_text(privacy_tag),
        )

    def test_index_cancelled_build(self):
        oci_project = self.factory.makeOCIProject(
            pillar=self.distroseries.distribution
        )
        oci_project_display = oci_project.display_name
        [ref] = self.factory.makeGitRefs(
            owner=self.person,
            target=self.person,
            name="recipe-repository",
            paths=["refs/heads/v1.0-20.04"],
        )
        recipe = self.makeRecipe(
            processor_names=["amd64", "386"],
            build_file="Dockerfile",
            git_ref=ref,
            oci_project=oci_project,
            registrant=self.person,
            owner=self.person,
        )
        build_request = recipe.requestBuilds(self.person)
        builds = recipe.requestBuildsFromJob(self.person, build_request)
        job = removeSecurityProxy(build_request).job
        removeSecurityProxy(job).builds = builds

        for build in builds:
            removeSecurityProxy(build).updateStatus(
                BuildStatus.BUILDING,
                builder=None,
                date_started=build.date_created,
            )
            removeSecurityProxy(build).updateStatus(
                BuildStatus.CANCELLED,
                builder=None,
                date_finished=build.date_started + timedelta(minutes=30),
            )

        # We also need to account for builds that don't have a build_request
        build = self.makeBuild(
            recipe=recipe,
            status=BuildStatus.FULLYBUILT,
            duration=timedelta(minutes=30),
        )

        browser = self.getViewBrowser(build_request.recipe)
        login_person(self.person)
        self.assertTextMatchesExpressionIgnoreWhitespace(
            """\
            .*
            OCI recipe information
            Owner: Test Person
            OCI project: %s
            Source: ~test-person/\\+git/recipe-repository:v1.0-20.04
            Build file path: Dockerfile
            Build context directory: %s
            Build schedule: Built on request
            Official recipe:
            No
            Latest builds
            Build status
            Upload status
            When requested
            When complete
            There were build failures.
            No registry upload requested.
            a moment ago
            in 29 minutes
            amd64
            Cancelled build
            386
            Cancelled build
            amd64
            in 29 minutes
            386
            in 29 minutes
            All builds were built successfully.
            No registry upload requested.
            1 hour ago
            30 minutes ago
            386
            Successfully built
            386
            30 minutes ago
            Recipe push rules
            This OCI recipe has no push rules defined yet.
            """
            % (oci_project_display, recipe.build_path),
            extract_text(find_main_content(browser.contents)),
        )

        # Check portlet on side menu.
        privacy_tag = find_tag_by_id(browser.contents, "privacy")
        self.assertTextMatchesExpressionIgnoreWhitespace(
            "This OCI recipe contains Public information",
            extract_text(privacy_tag),
        )

    def test_index_cancelling_build(self):
        oci_project = self.factory.makeOCIProject(
            pillar=self.distroseries.distribution
        )
        [ref] = self.factory.makeGitRefs(
            owner=self.person,
            target=self.person,
            name="recipe-repository",
            paths=["refs/heads/v1.0-20.04"],
        )
        recipe = self.makeRecipe(
            processor_names=["amd64", "386"],
            build_file="Dockerfile",
            git_ref=ref,
            oci_project=oci_project,
            registrant=self.person,
            owner=self.person,
        )
        build_request = recipe.requestBuilds(self.person)
        builds = recipe.requestBuildsFromJob(self.person, build_request)
        job = removeSecurityProxy(build_request).job
        removeSecurityProxy(job).builds = builds

        for build in builds:
            removeSecurityProxy(build).updateStatus(
                BuildStatus.BUILDING,
                builder=None,
                date_started=build.date_created,
            )
            removeSecurityProxy(build).updateStatus(
                BuildStatus.CANCELLING,
                builder=None,
                date_finished=build.date_started + timedelta(minutes=30),
            )

        browser = self.getViewBrowser(build_request.recipe)
        login_person(self.person)
        self.assertTextMatchesExpressionIgnoreWhitespace(
            """\
            .*
            There were build failures.
            No registry upload requested.
            a moment ago
            in 30 minutes
            \\(estimated\\)
            amd64
            Cancelling build
            386
            Cancelling build
            amd64
            386
            in 30 minutes
            \\(estimated\\)
            .*
            """,
            extract_text(find_main_content(browser.contents)),
        )

        # Check portlet on side menu.
        privacy_tag = find_tag_by_id(browser.contents, "privacy")
        self.assertTextMatchesExpressionIgnoreWhitespace(
            "This OCI recipe contains Public information",
            extract_text(privacy_tag),
        )

    def test_index_for_private_recipe_shows_banner(self):
        recipe = self.factory.makeOCIRecipe(
            registrant=self.person,
            owner=self.person,
            information_type=InformationType.USERDATA,
        )
        browser = self.getViewBrowser(recipe, user=self.person)

        # Check top banner.
        banners = find_tags_by_class(
            browser.contents, "private_banner_container"
        )
        self.assertEqual(1, len(banners))
        self.assertTextMatchesExpressionIgnoreWhitespace(
            "The information on this page is private.",
            extract_text(banners[0]),
        )

        # Check portlet on side menu.
        privacy_tag = find_tag_by_id(browser.contents, "privacy")
        self.assertTextMatchesExpressionIgnoreWhitespace(
            "This OCI recipe contains Private information",
            extract_text(privacy_tag),
        )

    def test_index_with_build_args(self):
        oci_project = self.factory.makeOCIProject(
            pillar=self.distroseries.distribution
        )
        oci_project_display = oci_project.display_name
        [ref] = self.factory.makeGitRefs(
            owner=self.person,
            target=self.person,
            name="recipe-repository",
            paths=["refs/heads/v1.0-20.04"],
        )
        recipe = self.makeOCIRecipe(
            oci_project=oci_project,
            git_ref=ref,
            build_file="Dockerfile",
            build_args={"VAR1": "123", "VAR2": "XXX"},
        )
        build_path = recipe.build_path
        build = self.makeBuild(
            recipe=recipe,
            status=BuildStatus.FULLYBUILT,
            duration=timedelta(minutes=30),
        )
        self.assertTextMatchesExpressionIgnoreWhitespace(
            """\
            recipe-name
            .*
            OCI recipe information
            Owner: Test Person
            OCI project: %s
            Source: ~test-person/\\+git/recipe-repository:v1.0-20.04
            Build file path: Dockerfile
            Build context directory: %s
            Build schedule: Built on request
            Build-time\nARG variables: VAR1=123 VAR2=XXX
            Official recipe:
            No
            Latest builds
            Build status
            Upload status
            When requested
            When complete
            All builds were built successfully.
            No registry upload requested.
            1 hour ago
            30 minutes ago
            386
            Successfully built
            386
            30 minutes ago
            """
            % (oci_project_display, build_path),
            self.getMainText(build.recipe),
        )

    def test_index_for_subscriber_without_git_repo_access(self):
        oci_project = self.factory.makeOCIProject(
            pillar=self.distroseries.distribution
        )
        oci_project_display = oci_project.display_name
        [ref] = self.factory.makeGitRefs(
            owner=self.person,
            target=self.person,
            name="recipe-repository",
            paths=["refs/heads/v1.0-20.04"],
            information_type=InformationType.PRIVATESECURITY,
        )
        with person_logged_in(self.person):
            recipe = self.makeOCIRecipe(
                oci_project=oci_project,
                git_ref=ref,
                build_file="Dockerfile",
                information_type=InformationType.PRIVATESECURITY,
            )
        with admin_logged_in():
            build_path = recipe.build_path
            self.makeBuild(
                recipe=recipe,
                status=BuildStatus.FULLYBUILT,
                duration=timedelta(minutes=30),
            )

        # Subscribe a user.
        subscriber = self.factory.makePerson()
        with person_logged_in(self.person):
            recipe.subscribe(subscriber, self.person)

        with person_logged_in(subscriber):
            main_text = self.getMainText(recipe, user=subscriber)
        self.assertTextMatchesExpressionIgnoreWhitespace(
            """\
            recipe-name
            .*
            OCI recipe information
            Owner: Test Person
            OCI project: %s
            Source: &lt;redacted&gt;
            Build file path: Dockerfile
            Build context directory: %s
            Build schedule: Built on request
            Official recipe:
            No
            Latest builds
            Build status
            Upload status
            When requested
            When complete
            All builds were built successfully.
            No registry upload requested.
            1 hour ago
            30 minutes ago
            386
            Successfully built
            386
            30 minutes ago
            """
            % (oci_project_display, build_path),
            main_text,
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
            Build status
            Upload status
            When requested
            When complete
            All builds were built successfully.
            No registry upload requested.
            1 hour ago
            30 minutes ago
            386
            buildlog
            \(.*\)
            Successfully built
            386
            30 minutes ago
            """,
            self.getMainText(build.recipe),
        )

    def test_index_no_builds(self):
        # A message is shown when there are no builds.
        recipe = self.factory.makeOCIRecipe()
        self.assertIn(
            "This OCI recipe has not been built yet.", self.getMainText(recipe)
        )

    def test_index_pending_build(self):
        # A pending build is listed as such.
        oci_project = self.factory.makeOCIProject(
            pillar=self.distroseries.distribution
        )
        [ref] = self.factory.makeGitRefs(
            owner=self.person,
            target=self.person,
            name="recipe-repository",
            paths=["refs/heads/v1.0-20.04"],
        )
        recipe = self.makeRecipe(
            processor_names=["amd64", "386"],
            build_file="Dockerfile",
            git_ref=ref,
            oci_project=oci_project,
            registrant=self.person,
            owner=self.person,
        )
        build_request = recipe.requestBuilds(self.person)
        builds = recipe.requestBuildsFromJob(self.person, build_request)
        job = removeSecurityProxy(build_request).job
        removeSecurityProxy(job).builds = builds
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""\
            Latest builds
            Build status
            Upload status
            When requested
            When complete
            There are some builds waiting to be built.
            Waiting for builds to start.
            a moment ago
            in .* \(estimated\)
            """,
            self.getMainText(recipe),
        )

    def test_index_request_builds_link(self):
        # Recipe owners get a link to allow requesting builds.
        owner = self.factory.makePerson()
        distroseries = self.factory.makeDistroSeries()
        oci_project = self.factory.makeOCIProject(
            pillar=distroseries.distribution
        )
        recipe = self.factory.makeOCIRecipe(
            registrant=owner, owner=owner, oci_project=oci_project
        )
        recipe_name = recipe.name
        browser = self.getViewBrowser(recipe, user=owner)
        browser.getLink("Request builds").click()
        self.assertIn("Request builds for %s" % recipe_name, browser.contents)

    def test_index_request_builds_link_unauthorized(self):
        # People who cannot edit the recipe do not get a link to allow
        # requesting builds.
        distroseries = self.factory.makeDistroSeries()
        oci_project = self.factory.makeOCIProject(
            pillar=distroseries.distribution
        )
        recipe = self.factory.makeOCIRecipe(oci_project=oci_project)
        recipe_url = canonical_url(recipe)
        browser = self.getViewBrowser(recipe, user=self.person)
        self.assertRaises(LinkNotFoundError, browser.getLink, "Request builds")
        self.assertRaises(
            Unauthorized,
            self.getUserBrowser,
            recipe_url + "/+request-builds",
            user=self.person,
        )

    def setStatus(self, build, status):
        build.updateStatus(
            BuildStatus.BUILDING, date_started=build.date_created
        )
        build.updateStatus(
            status, date_finished=build.date_started + timedelta(minutes=30)
        )

    def test_builds(self):
        # OCIRecipeView.builds produces reasonable results.
        recipe = self.makeOCIRecipe()
        # Create oldest builds first so that they sort properly by id.
        date_gen = time_counter(
            datetime(2000, 1, 1, tzinfo=timezone.utc), timedelta(days=1)
        )
        builds = [
            self.makeBuild(recipe=recipe, date_created=next(date_gen))
            for i in range(11)
        ]
        view = OCIRecipeView(recipe, None)
        self.assertEqual(list(reversed(builds)), view.builds)
        self.setStatus(builds[10], BuildStatus.FULLYBUILT)
        self.setStatus(builds[9], BuildStatus.FAILEDTOBUILD)
        del get_property_cache(view).builds
        # When there are >= 9 pending builds, only the most recent of any
        # completed builds is returned.
        self.assertEqual(
            list(reversed(builds[:9])) + [builds[10]], view.builds
        )
        for build in builds[:9]:
            self.setStatus(build, BuildStatus.FULLYBUILT)
        del get_property_cache(view).builds
        self.assertEqual(list(reversed(builds[1:])), view.builds)


class TestOCIRecipeRequestBuildsView(BaseTestOCIRecipeView):
    def setUp(self):
        super().setUp()
        self.ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.distroseries = self.factory.makeDistroSeries(
            distribution=self.ubuntu, name="shiny", displayname="Shiny"
        )
        distribution = self.distroseries.distribution
        self.architectures = []
        for processor, architecture in ("386", "i386"), ("amd64", "amd64"):
            das = self.factory.makeDistroArchSeries(
                distroseries=self.distroseries,
                architecturetag=architecture,
                processor=getUtility(IProcessorSet).getByName(processor),
            )
            das.addOrUpdateChroot(self.factory.makeLibraryFileAlias())
            self.architectures.append(das)
        self.useFixture(
            FeatureFixture(
                {
                    OCI_RECIPE_ALLOW_CREATE: "on",
                    "oci.build_series.%s"
                    % distribution.name: self.distroseries.name,
                }
            )
        )
        oci_project = self.factory.makeOCIProject(
            pillar=distribution,
            ociprojectname="oci-project-name",
        )
        self.recipe = self.factory.makeOCIRecipe(
            name="recipe-name",
            registrant=self.person,
            owner=self.person,
            oci_project=oci_project,
        )

    def test_request_builds_page(self):
        # The +request-builds page is sane.
        self.assertTextMatchesExpressionIgnoreWhitespace(
            """
            Request builds for recipe-name
            recipe-name
            Request builds
            Architectures:
            amd64
            i386
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

    def runRequestBuildJobs(self):
        with admin_logged_in():
            jobs = getUtility(IOCIRecipeRequestBuildsJobSource).iterReady()
            with dbuser(config.IOCIRecipeRequestBuildsJobSource.dbuser):
                JobRunner(jobs).runAll()

    def test_request_builds_action(self):
        # Requesting a build creates pending builds.
        browser = self.getViewBrowser(
            self.recipe, "+request-builds", user=self.person
        )
        self.assertTrue(browser.getControl("amd64").selected)
        self.assertTrue(browser.getControl("i386").selected)
        browser.getControl("Request builds").click()

        self.runRequestBuildJobs()

        login_person(self.person)
        builds = self.recipe.pending_builds
        self.assertContentEqual(
            ["amd64", "i386"],
            [build.distro_arch_series.architecturetag for build in builds],
        )
        self.assertContentEqual(
            [4010], {build.buildqueue_record.lastscore for build in builds}
        )

    def test_request_builds_no_architectures(self):
        # Selecting no architectures causes a validation failure.
        browser = self.getViewBrowser(
            self.recipe, "+request-builds", user=self.person
        )
        browser.getControl("amd64").selected = False
        browser.getControl("i386").selected = False
        browser.getControl("Request builds").click()
        self.assertIn(
            "You need to select at least one architecture.",
            extract_text(find_main_content(browser.contents)),
        )


class TestOCIRecipeEditPushRulesView(
    OCIConfigHelperMixin, BaseTestOCIRecipeView
):
    def setUp(self):
        super().setUp()
        self.ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.distroseries = self.factory.makeDistroSeries(
            distribution=self.ubuntu, name="shiny", displayname="Shiny"
        )
        distribution = self.distroseries.distribution

        self.useFixture(
            FeatureFixture(
                {
                    OCI_RECIPE_ALLOW_CREATE: "on",
                    "oci.build_series.%s"
                    % distribution.name: self.distroseries.name,
                }
            )
        )
        self.oci_project = self.factory.makeOCIProject(
            pillar=distribution,
            ociprojectname="oci-project-name",
        )

        self.member = self.factory.makePerson()
        self.team = self.factory.makeTeam(members=[self.person, self.member])

        self.recipe = self.factory.makeOCIRecipe(
            name="recipe-name",
            registrant=self.person,
            owner=self.person,
            oci_project=self.oci_project,
        )

        self.team_owned_recipe = self.factory.makeOCIRecipe(
            name="recipe-name",
            registrant=self.person,
            owner=self.team,
            oci_project=self.oci_project,
        )

        self.setConfig()

    def test_view_oci_push_rules_owner(self):
        url = self.factory.getUniqueURL()
        credentials = {"username": "foo", "password": "bar"}
        registry_credentials = getUtility(IOCIRegistryCredentialsSet).new(
            registrant=self.person,
            owner=self.person,
            url=url,
            credentials=credentials,
        )
        image_name = self.factory.getUniqueUnicode()
        push_rule = getUtility(IOCIPushRuleSet).new(
            recipe=self.recipe,
            registry_credentials=registry_credentials,
            image_name=image_name,
        )
        view = create_initialized_view(
            self.recipe, "+index", principal=self.person
        )

        # Display the Registry URL and the Username
        # for the credentials owner
        with person_logged_in(self.person):
            rendered_view = view.render()
            row = soupmatchers.Tag(
                "push rule row", "tr", attrs={"id": "rule-%d" % push_rule.id}
            )
            self.assertThat(
                rendered_view,
                soupmatchers.HTMLContains(
                    soupmatchers.Within(
                        row,
                        soupmatchers.Tag(
                            "Registry URL", "td", text=registry_credentials.url
                        ),
                    ),
                    soupmatchers.Within(
                        row,
                        soupmatchers.Tag(
                            "Username",
                            "td",
                            text=registry_credentials.username,
                        ),
                    ),
                    soupmatchers.Within(
                        row,
                        soupmatchers.Tag("Image name", "td", text=image_name),
                    ),
                ),
            )

    def test_view_oci_push_rules_non_owner(self):
        url = self.factory.getUniqueURL()
        credentials = {"username": "foo", "password": "bar"}
        registry_credentials = getUtility(IOCIRegistryCredentialsSet).new(
            registrant=self.person,
            owner=self.person,
            url=url,
            credentials=credentials,
        )
        image_name = self.factory.getUniqueUnicode()
        push_rule = getUtility(IOCIPushRuleSet).new(
            recipe=self.recipe,
            registry_credentials=registry_credentials,
            image_name=image_name,
        )
        non_owner = self.factory.makePerson()
        admin = self.factory.makePerson(
            member_of=[getUtility(IPersonSet).getByName("admins")]
        )
        view = create_initialized_view(
            self.recipe, "+index", principal=non_owner
        )

        # Display only the image name for users
        # who are not the registry credentials owner
        with person_logged_in(non_owner):
            rendered_view = view.render()
            row = soupmatchers.Tag(
                "push rule row", "tr", attrs={"id": "rule-%d" % push_rule.id}
            )
            self.assertThat(
                rendered_view,
                soupmatchers.HTMLContains(
                    soupmatchers.Within(
                        row,
                        soupmatchers.Tag(
                            "Registry URL", "td", text=soupmatchers._not_passed
                        ),
                    ),
                    soupmatchers.Within(
                        row,
                        soupmatchers.Tag(
                            "Username", "td", text=soupmatchers._not_passed
                        ),
                    ),
                    soupmatchers.Within(
                        row,
                        soupmatchers.Tag("Image name", "td", text=image_name),
                    ),
                ),
            )

        # Anonymous users can't see registry credentials
        # even though they can see the push rule
        with anonymous_logged_in():
            rendered_view = view.render()
            row = soupmatchers.Tag(
                "push rule row", "tr", attrs={"id": "rule-%d" % push_rule.id}
            )
            self.assertThat(
                rendered_view,
                soupmatchers.HTMLContains(
                    soupmatchers.Within(
                        row,
                        soupmatchers.Tag(
                            "Registry URL", "td", text=soupmatchers._not_passed
                        ),
                    ),
                    soupmatchers.Within(
                        row,
                        soupmatchers.Tag(
                            "Region", "td", text=soupmatchers._not_passed
                        ),
                    ),
                    soupmatchers.Within(
                        row,
                        soupmatchers.Tag(
                            "Username", "td", text=soupmatchers._not_passed
                        ),
                    ),
                    soupmatchers.Within(
                        row,
                        soupmatchers.Tag("Image name", "td", text=image_name),
                    ),
                ),
            )

        # Although not the owner of the registry credentials
        # the admin user has launchpad.View permission on
        # OCI registry credentials and should be able to see
        # the registry URL and username of the owner.
        # see ViewOCIRegistryCredentials
        with person_logged_in(admin):
            rendered_view = view.render()
            row = soupmatchers.Tag(
                "push rule row", "tr", attrs={"id": "rule-%d" % push_rule.id}
            )
            self.assertThat(
                rendered_view,
                soupmatchers.HTMLContains(
                    soupmatchers.Within(
                        row,
                        soupmatchers.Tag(
                            "Registry URL", "td", text=registry_credentials.url
                        ),
                    ),
                    soupmatchers.Within(
                        row,
                        soupmatchers.Tag(
                            "Username",
                            "td",
                            text=registry_credentials.username,
                        ),
                    ),
                    soupmatchers.Within(
                        row,
                        soupmatchers.Tag("Image name", "td", text=image_name),
                    ),
                ),
            )

    def test_edit_oci_push_rules(self):
        url = self.factory.getUniqueURL()
        credentials = {"username": "foo", "password": "bar"}
        registry_credentials = getUtility(IOCIRegistryCredentialsSet).new(
            registrant=self.person,
            owner=self.person,
            url=url,
            credentials=credentials,
        )
        image_name = self.factory.getUniqueUnicode()
        push_rule = getUtility(IOCIPushRuleSet).new(
            recipe=self.recipe,
            registry_credentials=registry_credentials,
            image_name=image_name,
        )
        browser = self.getViewBrowser(self.recipe, user=self.person)
        browser.getLink("Edit push rules").click()
        # assert image name is displayed correctly
        with person_logged_in(self.person):
            self.assertEqual(
                image_name,
                browser.getControl(
                    name="field.image_name.%d" % push_rule.id
                ).value,
            )

        # assert image name is required
        with person_logged_in(self.person):
            browser.getControl(
                name="field.image_name.%d" % push_rule.id
            ).value = ""
        browser.getControl("Save").click()
        self.assertIn("Required input is missing", browser.contents)

        # set image name to valid string
        with person_logged_in(self.person):
            browser.getControl(
                name="field.image_name.%d" % push_rule.id
            ).value = "image1"
        browser.getControl("Save").click()
        # and assert model changed
        with person_logged_in(self.person):
            self.assertEqual(push_rule.image_name, "image1")
            # Create a second push rule and test we call setNewImageName only
            # in cases where image name is different than the one on the model
            # otherwise we get the exception on rows the user doesn't actually
            # edit as the image name stays the same as it already is on the
            # model and setNewImageName will obviously find that rule in the
            # db with the same details
            second_rule = getUtility(IOCIPushRuleSet).new(
                recipe=self.recipe,
                registry_credentials=registry_credentials,
                image_name="second image",
            )
        browser = self.getViewBrowser(self.recipe, user=self.person)
        browser.getLink("Edit push rules").click()
        with person_logged_in(self.person):
            browser.getControl(
                name="field.image_name.%d" % push_rule.id
            ).value = "image2"
        browser.getControl("Save").click()
        with person_logged_in(self.person):
            self.assertEqual(push_rule.image_name, "image2")

        # Attempt to set the same name on the second rule
        # will result in expected exception
        browser = self.getViewBrowser(self.recipe, user=self.person)
        browser.getLink("Edit push rules").click()
        with person_logged_in(self.person):
            browser.getControl(
                name="field.image_name.%d" % second_rule.id
            ).value = "image2"
        self.assertRaises(
            OCIPushRuleAlreadyExists, browser.getControl("Save").click
        )

    def test_edit_oci_push_rules_non_owner_of_credentials(self):
        url = self.factory.getUniqueURL()
        credentials = {"username": "foo", "password": "bar"}
        registry_credentials = getUtility(IOCIRegistryCredentialsSet).new(
            registrant=self.person,
            owner=self.person,
            url=url,
            credentials=credentials,
        )
        image_names = [self.factory.getUniqueUnicode() for _ in range(2)]
        push_rules = [
            getUtility(IOCIPushRuleSet).new(
                recipe=self.team_owned_recipe,
                registry_credentials=registry_credentials,
                image_name=image_name,
            )
            for image_name in image_names
        ]
        Store.of(push_rules[-1]).flush()
        push_rule_ids = [push_rule.id for push_rule in push_rules]
        browser = self.getViewBrowser(self.team_owned_recipe, user=self.member)
        browser.getLink("Edit push rules").click()
        row = soupmatchers.Tag(
            "push rule row", "tr", attrs={"class": "push-rule"}
        )
        self.assertThat(
            browser.contents,
            soupmatchers.HTMLContains(
                soupmatchers.Within(
                    row,
                    soupmatchers.Tag(
                        "username widget",
                        "span",
                        attrs={
                            "id": "field.username.%d" % push_rule_ids[0],
                            "class": "sprite private",
                        },
                    ),
                ),
                soupmatchers.Within(
                    row,
                    soupmatchers.Tag(
                        "url widget",
                        "span",
                        attrs={
                            "id": "field.url.%d" % push_rule_ids[0],
                            "class": "sprite private",
                        },
                    ),
                ),
            ),
        )
        browser.getControl(
            name="field.image_name.%d" % push_rule_ids[0]
        ).value = "image1"
        browser.getControl("Save").click()
        with person_logged_in(self.member):
            self.assertEqual("image1", push_rules[0].image_name)
            self.assertEqual(image_names[1], push_rules[1].image_name)

    def test_delete_oci_push_rules(self):
        url = self.factory.getUniqueURL()
        credentials = {"username": "foo", "password": "bar"}
        registry_credentials = getUtility(IOCIRegistryCredentialsSet).new(
            registrant=self.person,
            owner=self.person,
            url=url,
            credentials=credentials,
        )
        image_name = self.factory.getUniqueUnicode()
        push_rule = getUtility(IOCIPushRuleSet).new(
            recipe=self.recipe,
            registry_credentials=registry_credentials,
            image_name=image_name,
        )
        push_rule_id = push_rule.id
        browser = self.getViewBrowser(self.recipe, user=self.person)
        browser.getLink("Edit push rules").click()
        with person_logged_in(self.person):
            browser.getControl(name="field.delete.%d" % push_rule_id).value = (
                True
            )
        browser.getControl("Save").click()

        with person_logged_in(self.person):
            self.assertIsNone(
                getUtility(IOCIPushRuleSet).getByID(push_rule_id)
            )

    def test_add_oci_push_rules_validations(self):
        # Add new rule works when there are no rules in the DB.
        browser = self.getViewBrowser(self.recipe, user=self.person)
        browser.getLink("Edit push rules").click()

        # Save does not error if there are no changes on the form.
        browser.getControl("Save").click()
        self.assertIn("Saved push rules", browser.contents)

        # If only an image name is given but no registry URL, we fail with
        # "Registry URL must be set".
        browser.getLink("Edit push rules").click()
        browser.getControl(name="field.add_image_name").value = "imagename1"
        browser.getControl(name="field.add_credentials").value = "new"
        browser.getControl("Save").click()
        self.assertIn("Registry URL must be set", browser.contents)

        # No image name entered on the form.  We assume user is only editing
        # and we allow saving the form.
        browser.getControl(name="field.add_image_name").value = ""
        browser.getControl("Save").click()
        self.assertIn("Saved push rules", browser.contents)

    def test_add_oci_push_rules_new_empty_credentials(self):
        # Supplying an image name and registry URL creates a credentials
        # object without username or password, and a valid push rule based
        # on that credentials object.
        url = self.factory.getUniqueURL()
        browser = self.getViewBrowser(self.recipe, user=self.person)
        browser.getLink("Edit push rules").click()
        browser.getControl(name="field.add_credentials").value = "new"
        browser.getControl(name="field.add_image_name").value = "imagename1"
        browser.getControl(name="field.add_url").value = url
        browser.getControl("Save").click()
        with person_logged_in(self.person):
            rules = list(
                removeSecurityProxy(
                    getUtility(IOCIPushRuleSet).findByRecipe(self.recipe)
                )
            )
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertThat(
            rule,
            MatchesStructure(
                image_name=Equals("imagename1"),
                registry_url=Equals(url),
                registry_credentials=MatchesStructure(
                    url=Equals(url), username=Is(None)
                ),
            ),
        )

        with person_logged_in(self.person):
            self.assertEqual(
                {"password": None}, rule.registry_credentials.getCredentials()
            )

    def test_add_oci_push_rules_new_username_password(self):
        # Supplying an image name, registry URL, username, and password
        # creates a credentials object with the given username or password,
        # and a valid push rule based on that credentials object.
        url = self.factory.getUniqueURL()
        browser = self.getViewBrowser(self.recipe, user=self.person)
        browser.getLink("Edit push rules").click()
        browser.getControl(name="field.add_credentials").value = "new"
        browser.getControl(name="field.add_image_name").value = "imagename3"
        browser.getControl(name="field.add_url").value = url
        browser.getControl(name="field.add_region").value = "somewhere-02"
        browser.getControl(name="field.add_username").value = "username"
        browser.getControl(name="field.add_password").value = "password"
        browser.getControl(name="field.add_confirm_password").value = (
            "password"
        )
        browser.getControl("Save").click()
        with person_logged_in(self.person):
            rules = list(
                removeSecurityProxy(
                    getUtility(IOCIPushRuleSet).findByRecipe(self.recipe)
                )
            )
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertThat(
            rule,
            MatchesStructure(
                image_name=Equals("imagename3"),
                registry_url=Equals(url),
                registry_credentials=MatchesStructure.byEquality(
                    url=url, username="username"
                ),
            ),
        )
        with person_logged_in(self.person):
            self.assertEqual(
                {
                    "username": "username",
                    "password": "password",
                    "region": "somewhere-02",
                },
                rule.registry_credentials.getCredentials(),
            )

    def test_add_oci_push_rules_existing_credentials_duplicate(self):
        # Adding a new push rule using existing credentials fails if a rule
        # with the same image name already exists.
        existing_rule = self.factory.makeOCIPushRule(
            recipe=self.recipe,
            registry_credentials=self.factory.makeOCIRegistryCredentials(
                registrant=self.recipe.owner, owner=self.recipe.owner
            ),
        )
        existing_image_name = existing_rule.image_name
        existing_registry_url = existing_rule.registry_url
        existing_username = existing_rule.username
        browser = self.getViewBrowser(self.recipe, user=self.person)
        browser.getLink("Edit push rules").click()
        browser.getControl(name="field.add_credentials").value = "existing"
        browser.getControl(name="field.add_image_name").value = (
            existing_image_name
        )
        browser.getControl(name="field.existing_credentials").value = (
            "%s %s"
            % (
                quote(existing_registry_url),
                quote(existing_username),
            )
        )
        browser.getControl("Save").click()
        self.assertIn(
            "A push rule already exists with the same URL, "
            "image name, and credentials.",
            browser.contents,
        )

    def test_add_oci_push_rules_existing_credentials(self):
        # Previously added registry credentials can be chosen from the radio
        # widget when adding a new rule.
        # We correctly display the radio buttons widget when the
        # username is empty in registry credentials and
        # allow correctly adding new rule based on it
        existing_rule = self.factory.makeOCIPushRule(
            recipe=self.recipe,
            registry_credentials=self.factory.makeOCIRegistryCredentials(
                registrant=self.recipe.owner,
                owner=self.recipe.owner,
                credentials={},
            ),
        )
        existing_registry_url = existing_rule.registry_url
        browser = self.getViewBrowser(self.recipe, user=self.person)
        browser.getLink("Edit push rules").click()
        browser.getControl(name="field.add_credentials").value = "existing"
        browser.getControl(name="field.add_image_name").value = "imagename2"
        browser.getControl(name="field.existing_credentials").value = quote(
            existing_registry_url
        )
        browser.getControl("Save").click()
        with person_logged_in(self.person):
            rules = list(
                removeSecurityProxy(
                    getUtility(IOCIPushRuleSet).findByRecipe(self.recipe)
                )
            )
        self.assertEqual(len(rules), 2)
        rule = rules[1]
        self.assertThat(
            rule,
            MatchesStructure(
                image_name=Equals("imagename2"),
                registry_url=Equals(existing_registry_url),
                registry_credentials=MatchesStructure(
                    url=Equals(existing_registry_url), username=Is(None)
                ),
            ),
        )
        with person_logged_in(self.person):
            self.assertEqual({}, rule.registry_credentials.getCredentials())

    def test_add_oci_push_rules_team_owned(self):
        url = self.factory.getUniqueURL()
        browser = self.getViewBrowser(self.team_owned_recipe, user=self.member)
        browser.getLink("Edit push rules").click()
        browser.getControl(name="field.add_image_name").value = "imagename1"
        browser.getControl(name="field.add_url").value = url
        browser.getControl(name="field.add_credentials").value = "new"
        browser.getControl("Save").click()

        with person_logged_in(self.member):
            rules = list(
                removeSecurityProxy(
                    getUtility(IOCIPushRuleSet).findByRecipe(
                        self.team_owned_recipe
                    )
                )
            )
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertThat(
            rule,
            MatchesStructure(
                image_name=Equals("imagename1"),
                registry_url=Equals(url),
                registry_credentials=MatchesStructure(
                    url=Equals(url), username=Is(None)
                ),
            ),
        )

        with person_logged_in(self.member):
            self.assertThat(
                rule.registry_credentials.getCredentials(),
                MatchesDict({"password": Equals(None)}),
            )

    def test_edit_oci_push_rules_team_owned(self):
        url = self.factory.getUniqueURL()
        browser = self.getViewBrowser(self.team_owned_recipe, user=self.member)
        browser.getLink("Edit push rules").click()
        browser.getControl(name="field.add_image_name").value = "imagename1"
        browser.getControl(name="field.add_url").value = url
        browser.getControl(name="field.add_credentials").value = "new"
        browser.getControl("Save").click()

        # push rules created by another team member (self.member)
        # can be edited by self.person
        browser = self.getViewBrowser(self.team_owned_recipe, user=self.person)
        browser.getLink("Edit push rules").click()
        with person_logged_in(self.person):
            rules = list(
                removeSecurityProxy(
                    getUtility(IOCIPushRuleSet).findByRecipe(
                        self.team_owned_recipe
                    )
                )
            )
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertEqual(
            "imagename1",
            browser.getControl(name="field.image_name.%d" % rule.id).value,
        )

        # set image name to valid string
        with person_logged_in(self.person):
            browser.getControl(name="field.image_name.%d" % rule.id).value = (
                "image1"
            )
        browser.getControl("Save").click()

        # and assert model changed
        with person_logged_in(self.member):
            self.assertEqual(rule.image_name, "image1")

        # self.member will see the new image name
        browser = self.getViewBrowser(self.team_owned_recipe, user=self.member)
        browser.getLink("Edit push rules").click()
        with person_logged_in(self.member):
            self.assertEqual(
                "image1",
                browser.getControl(name="field.image_name.%d" % rule.id).value,
            )

    def test_edit_oci_registry_creds(self):
        url = self.factory.getUniqueURL()
        credentials = {"username": "foo", "password": "bar"}
        image_name = self.factory.getUniqueUnicode()
        registry_credentials = getUtility(IOCIRegistryCredentialsSet).new(
            registrant=self.person,
            owner=self.person,
            url=url,
            credentials=credentials,
        )
        getUtility(IOCIPushRuleSet).new(
            recipe=self.recipe,
            registry_credentials=registry_credentials,
            image_name=image_name,
        )
        browser = self.getViewBrowser(self.recipe, user=self.person)
        browser.getLink("Edit push rules").click()
        browser.getLink("Edit OCI registry credentials").click()

        browser.getControl(name="field.add_url").value = url
        browser.getControl(name="field.add_region").value = "new_region1"
        browser.getControl(name="field.add_username").value = "new_username"
        browser.getControl(name="field.add_password").value = "password"
        browser.getControl(name="field.add_confirm_password").value = (
            "password"
        )

        browser.getControl("Save").click()
        with person_logged_in(self.person):
            creds = list(
                getUtility(IOCIRegistryCredentialsSet).findByOwner(self.person)
            )

            self.assertEqual(url, creds[1].url)
            self.assertThat(
                (creds[1]).getCredentials(),
                MatchesDict(
                    {
                        "username": Equals("new_username"),
                        "password": Equals("password"),
                        "region": Equals("new_region1"),
                    }
                ),
            )


class TestOCIRecipeListingView(BaseTestOCIRecipeView):
    def setUp(self):
        super().setUp()
        self.ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.distroseries = self.factory.makeDistroSeries(
            distribution=self.ubuntu, name="shiny", displayname="Shiny"
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
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        self.oci_project = self.factory.makeOCIProject(
            pillar=self.distroseries.distribution,
            ociprojectname="oci-project-name",
        )

    def makeRecipes(self, count=1, **kwargs):
        with person_logged_in(self.person):
            owner = self.factory.makePerson()
            return [
                self.factory.makeOCIRecipe(
                    registrant=owner,
                    owner=owner,
                    oci_project=self.oci_project,
                    **kwargs,
                )
                for _ in range(count)
            ]

    def test_oci_recipe_list_for_person(self):
        owner = self.factory.makePerson(name="recipe-owner")
        for i in range(2):
            self.factory.makeOCIRecipe(
                name="my-oci-recipe-%s" % i, owner=owner, registrant=owner
            )

        # This recipe should not be present.
        someone_else = self.factory.makePerson()
        self.factory.makeOCIRecipe(owner=someone_else, registrant=someone_else)

        # self.person now visits ~owner/+oci-recipes page.
        browser = self.getViewBrowser(owner, "+oci-recipes", user=self.person)
        main_text = extract_text(find_main_content(browser.contents))
        self.assertTextMatchesExpressionIgnoreWhitespace(
            """
            OCI recipes for recipe-owner
            There are 2 recipes registered for recipe-owner.
            Name             Owner          Source   Build file   Date created
            my-oci-recipe-0  Recipe-owner   .*
            my-oci-recipe-1  Recipe-owner   .*
            1 .* 2 of 2 results
            First .* Previous .* Next .* Last
            """,
            main_text,
        )

    def test_shows_no_recipe(self):
        """Should shows correct message when there are no visible recipes."""
        # Create a private OCI recipe that should not be shown.
        owner = self.factory.makePerson()
        self.factory.makeOCIRecipe(
            owner=owner,
            registrant=owner,
            oci_project=self.oci_project,
            information_type=InformationType.PRIVATESECURITY,
        )
        browser = self.getViewBrowser(
            self.oci_project, "+recipes", user=self.person
        )
        main_text = extract_text(find_main_content(browser.contents))
        with person_logged_in(self.person):
            self.assertIn(
                "There are no recipes registered for %s"
                % self.oci_project.name,
                main_text,
            )

    def test_paginates_recipes(self):
        batch_size = 5
        self.pushConfig("launchpad", default_batch_size=batch_size)
        # We will create 1 private recipe with proper permission in the
        # list, and 9 others. This way, we should have 10 recipes in the list.
        [private_recipe] = self.makeRecipes(
            1, information_type=InformationType.PRIVATESECURITY
        )
        with admin_logged_in():
            private_recipe.subscribe(self.person, private_recipe.owner)
        recipes = self.makeRecipes(9)
        recipes.append(private_recipe)

        browser = self.getViewBrowser(
            self.oci_project, "+recipes", user=self.person
        )
        main_text = extract_text(find_main_content(browser.contents))
        no_wrap_main_text = main_text.replace("\n", " ")
        with person_logged_in(self.person):
            self.assertIn(
                "There are 10 recipes registered for %s"
                % self.oci_project.name,
                no_wrap_main_text,
            )
            self.assertIn("1 → 5 of 10 results", no_wrap_main_text)
            self.assertIn("First • Previous • Next • Last", no_wrap_main_text)

            # Make sure it's listing the first set of recipes
            items = sorted(recipes, key=attrgetter("name"))
            for recipe in items[:batch_size]:
                self.assertIn(recipe.name, main_text)

    def test_constant_query_count(self):
        batch_size = 3
        self.pushConfig("launchpad", default_batch_size=batch_size)

        def getView():
            view = self.getViewBrowser(
                self.oci_project, "+recipes", user=self.person
            )
            return view

        def do_login():
            self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
            login_person(self.person)

        recorder1, recorder2 = record_two_runs(
            getView, self.makeRecipes, 1, 15, login_method=do_login
        )

        # The first run (with no extra pages) makes BatchNavigator issue one
        # extra count(*) on OCIRecipe. Shouldn't be a big deal.
        self.assertEqual(recorder1.count, recorder2.count - 1)
