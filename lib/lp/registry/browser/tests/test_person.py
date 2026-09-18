# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import doctest
import email
import re
from datetime import datetime, timedelta, timezone
from operator import attrgetter
from textwrap import dedent
from urllib.parse import urljoin

import soupmatchers
import transaction
from fixtures import FakeLogger
from storm.store import Store
from testscenarios import WithScenarios, load_tests_apply_scenarios
from testtools.matchers import (
    DocTestMatches,
    Equals,
    LessThan,
    MatchesDict,
    MatchesSetwise,
    MatchesStructure,
    Not,
)
from testtools.testcase import ExpectedException
from zope.component import getUtility
from zope.publisher.interfaces import NotFound
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy
from zope.testbrowser.browser import LinkNotFoundError

from lp.app.browser.lazrjs import TextAreaEditorWidget
from lp.app.browser.tales import DateTimeFormatterAPI
from lp.app.enums import InformationType
from lp.app.errors import NotFoundError
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.blueprints.enums import SpecificationImplementationStatus
from lp.buildmaster.enums import BuildStatus
from lp.oci.interfaces.ocipushrule import IOCIPushRuleSet
from lp.oci.interfaces.ocirecipe import OCI_RECIPE_ALLOW_CREATE
from lp.oci.interfaces.ociregistrycredentials import IOCIRegistryCredentialsSet
from lp.oci.tests.helpers import (
    MatchesOCIRegistryCredentials,
    OCIConfigHelperMixin,
)
from lp.registry.browser.person import PersonView
from lp.registry.browser.team import TeamInvitationView
from lp.registry.enums import PersonVisibility
from lp.registry.interfaces.karma import IKarmaCacheManager
from lp.registry.interfaces.persontransferjob import (
    IPersonCloseAccountJobSource,
    IPersonMergeJobSource,
)
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.teammembership import (
    ITeamMembershipSet,
    TeamMembershipStatus,
)
from lp.registry.model.karma import KarmaCategory
from lp.registry.model.milestone import milestone_sort_key
from lp.scripts.garbo import PopulateLatestPersonSourcePackageReleaseCache
from lp.services.config import config
from lp.services.database.interfaces import IStore
from lp.services.features.testing import FeatureFixture
from lp.services.identity.interfaces.account import AccountStatus
from lp.services.identity.interfaces.emailaddress import IEmailAddressSet
from lp.services.job.interfaces.job import JobStatus
from lp.services.log.logger import DevNullLogger
from lp.services.mail import stub
from lp.services.propertycache import clear_property_cache
from lp.services.verification.interfaces.authtoken import LoginTokenType
from lp.services.verification.interfaces.logintoken import ILoginTokenSet
from lp.services.verification.tests.logintoken import get_token_url_from_email
from lp.services.webapp import canonical_url
from lp.services.webapp.escaping import html_escape
from lp.services.webapp.interfaces import ILaunchBag
from lp.services.webapp.publisher import RedirectionView
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.services.webapp.vhosts import allvhosts
from lp.soyuz.enums import (
    ArchivePurpose,
    ArchiveStatus,
    PackagePublishingStatus,
)
from lp.soyuz.interfaces.livefs import LIVEFS_FEATURE_FLAG
from lp.soyuz.tests.test_publishing import SoyuzTestPublisher
from lp.testing import (
    ANONYMOUS,
    BrowserTestCase,
    StormStatementRecorder,
    TestCaseWithFactory,
    login,
    login_person,
    monkey_patch,
    person_logged_in,
    record_two_runs,
)
from lp.testing.dbuser import dbuser, switch_dbuser
from lp.testing.layers import (
    DatabaseFunctionalLayer,
    LaunchpadFunctionalLayer,
    LaunchpadZopelessLayer,
)
from lp.testing.matchers import HasQueryCount
from lp.testing.pages import extract_text, find_tag_by_id, setupBrowserForUser
from lp.testing.publication import test_traverse
from lp.testing.views import create_initialized_view, create_view


class TestPersonNavigation(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def assertRedirect(self, path, redirect, status_code=303):
        view = test_traverse(path)[1]
        self.assertIsInstance(view, RedirectionView)
        self.assertEqual(
            urljoin(allvhosts.configs["mainsite"].rooturl, redirect),
            removeSecurityProxy(view).target,
        )
        self.assertEqual(status_code, removeSecurityProxy(view).status)

    def test_traverse_archive_distroful(self):
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        in_suf = "/~%s/+archive/%s/%s" % (
            archive.owner.name,
            archive.distribution.name,
            archive.name,
        )
        self.assertEqual(archive, test_traverse(in_suf)[0])
        self.assertEqual(archive, test_traverse("/api/devel" + in_suf)[0])
        self.assertEqual(archive, test_traverse("/api/1.0" + in_suf)[0])

    def test_traverse_archive_distroless(self):
        # Pre-mid-2014 distroless PPA URLs redirect to the new ones.
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        in_suf = "/~%s/+archive/%s" % (archive.owner.name, archive.name)
        out_suf = "/~%s/+archive/%s/%s" % (
            archive.owner.name,
            archive.distribution.name,
            archive.name,
        )
        self.assertRedirect(in_suf, out_suf)
        self.assertRedirect("/api/devel" + in_suf, "/api/devel" + out_suf)
        # 1.0 API requests don't redirect, since some manually construct
        # URLs and don't cope with redirects (most notably the Python 2
        # implementation of apt-add-repository).
        self.assertEqual(archive, test_traverse("/api/1.0" + out_suf)[0])

    def test_traverse_archive_distroless_implies_ubuntu(self):
        # The distroless PPA redirect only finds Ubuntu PPAs, since
        # distroful URLs were implemented as a requirement for
        # non-Ubuntu PPAs.
        other_archive = self.factory.makeArchive(
            purpose=ArchivePurpose.PPA,
            distribution=self.factory.makeDistribution(),
        )
        with ExpectedException(NotFound):
            test_traverse(
                "/~%s/+archive/%s"
                % (other_archive.owner.name, other_archive.name)
            )

    def test_traverse_archive_redirects_nameless(self):
        # Pre-2009 nameless PPA URLs redirect to the new ones.
        archive = self.factory.makeArchive(
            purpose=ArchivePurpose.PPA, name="ppa"
        )
        in_suf = "/~%s/+archive" % archive.owner.name
        out_suf = "/~%s/+archive/%s/%s" % (
            archive.owner.name,
            archive.distribution.name,
            archive.name,
        )
        self.assertRedirect(in_suf, out_suf)
        self.assertRedirect("/api/devel" + in_suf, "/api/devel" + out_suf)
        self.assertRedirect("/api/1.0" + in_suf, "/api/1.0" + out_suf)

    def test_traverse_git_repository_project(self):
        project = self.factory.makeProduct()
        repository = self.factory.makeGitRepository(target=project)
        url = "/~%s/%s/+git/%s" % (
            repository.owner.name,
            project.name,
            repository.name,
        )
        self.assertEqual(repository, test_traverse(url)[0])

    def test_traverse_git_repository_project_alias(self):
        project = self.factory.makeProduct()
        alias = project.name + "-2"
        removeSecurityProxy(project).setAliases([alias])
        repository = self.factory.makeGitRepository(target=project)
        url = "/~%s/%s/+git/%s" % (
            repository.owner.name,
            alias,
            repository.name,
        )
        expected_url = "http://code.launchpad.test/~%s/%s/+git/%s" % (
            repository.owner.name,
            project.name,
            repository.name,
        )
        _, view, _ = test_traverse(url)
        self.assertIsInstance(view, RedirectionView)
        self.assertEqual(expected_url, removeSecurityProxy(view).target)

    def test_traverse_git_repository_project_alias_api(self):
        project = self.factory.makeProduct()
        alias = project.name + "-2"
        removeSecurityProxy(project).setAliases([alias])
        repository = self.factory.makeGitRepository(target=project)
        url = "http://api.launchpad.test/devel/~%s/%s/+git/%s" % (
            repository.owner.name,
            alias,
            repository.name,
        )
        expected_url = "http://api.launchpad.test/devel/~%s/%s/+git/%s" % (
            repository.owner.name,
            project.name,
            repository.name,
        )
        _, view, _ = test_traverse(url)
        self.assertIsInstance(view, RedirectionView)
        self.assertEqual(expected_url, removeSecurityProxy(view).target)

    def test_traverse_git_repository_package(self):
        dsp = self.factory.makeDistributionSourcePackage()
        repository = self.factory.makeGitRepository(target=dsp)
        url = "/~%s/%s/+source/%s/+git/%s" % (
            repository.owner.name,
            dsp.distribution.name,
            dsp.sourcepackagename.name,
            repository.name,
        )
        self.assertEqual(repository, test_traverse(url)[0])

    def test_traverse_git_repository_package_alias(self):
        dsp = self.factory.makeDistributionSourcePackage()
        alias = dsp.distribution.name + "-2"
        removeSecurityProxy(dsp.distribution).setAliases([alias])
        repository = self.factory.makeGitRepository(target=dsp)
        url = "/~%s/%s/+source/%s/+git/%s" % (
            repository.owner.name,
            alias,
            dsp.sourcepackagename.name,
            repository.name,
        )
        expected_url = (
            "http://code.launchpad.test/~%s/%s/+source/%s/+git/%s"
            % (
                repository.owner.name,
                dsp.distribution.name,
                dsp.sourcepackagename.name,
                repository.name,
            )
        )
        _, view, _ = test_traverse(url)
        self.assertIsInstance(view, RedirectionView)
        self.assertEqual(expected_url, removeSecurityProxy(view).target)

    def test_traverse_git_repository_package_alias_api(self):
        dsp = self.factory.makeDistributionSourcePackage()
        alias = dsp.distribution.name + "-2"
        removeSecurityProxy(dsp.distribution).setAliases([alias])
        repository = self.factory.makeGitRepository(target=dsp)
        url = "http://api.launchpad.test/devel/~%s/%s/+source/%s/+git/%s" % (
            repository.owner.name,
            alias,
            dsp.sourcepackagename.name,
            repository.name,
        )
        expected_url = (
            "http://api.launchpad.test/devel/~%s/%s/+source/%s/+git/%s"
            % (
                repository.owner.name,
                dsp.distribution.name,
                dsp.sourcepackagename.name,
                repository.name,
            )
        )
        _, view, _ = test_traverse(url)
        self.assertIsInstance(view, RedirectionView)
        self.assertEqual(expected_url, removeSecurityProxy(view).target)

    def test_traverse_git_repository_oci_project(self):
        oci_project = self.factory.makeOCIProject()
        repository = self.factory.makeGitRepository(target=oci_project)
        url = "/~%s/%s/+oci/%s/+git/%s" % (
            repository.owner.name,
            oci_project.pillar.name,
            oci_project.name,
            repository.name,
        )
        self.assertEqual(repository, test_traverse(url)[0])

    def test_traverse_git_repository_personal(self):
        person = self.factory.makePerson()
        repository = self.factory.makeGitRepository(
            owner=person, target=person
        )
        url = "/~%s/+git/%s" % (person.name, repository.name)
        self.assertEqual(repository, test_traverse(url)[0])


class PersonViewOpenidIdentityUrlTestCase(TestCaseWithFactory):
    """Tests for the public OpenID identifier shown on the profile page."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        TestCaseWithFactory.setUp(self)
        self.user = self.factory.makePerson(name="eris")
        self.request = LaunchpadTestRequest(
            SERVER_URL="http://launchpad.test/"
        )
        login_person(self.user, self.request)
        self.view = PersonView(self.user, self.request)
        # Marker allowing us to reset the config.
        config.push(self.id(), "")
        self.addCleanup(config.pop, self.id())

    def test_should_be_profile_page_when_delegating(self):
        """The profile page is the OpenID identifier in normal situation."""
        self.assertEqual(
            "http://launchpad.test/~eris", self.view.openid_identity_url
        )

    def test_should_be_production_profile_page_when_not_delegating(self):
        """When the profile page is not delegated, the OpenID identity URL
        should be the one on the main production site."""
        config.push(
            "non-delegating",
            dedent(
                """
            [vhost.mainsite]
            openid_delegate_profile: False

            [launchpad]
            non_restricted_hostname: prod.launchpad.test
            """
            ),
        )
        self.assertEqual(
            "http://prod.launchpad.test/~eris", self.view.openid_identity_url
        )


class TestPersonIndexView(BrowserTestCase):
    layer = DatabaseFunctionalLayer

    def test_isMergePending(self):
        dupe_person = self.factory.makePerson(name="finch")
        target_person = self.factory.makePerson()
        requester = self.factory.makePerson()
        job_source = getUtility(IPersonMergeJobSource)
        job_source.create(
            from_person=dupe_person,
            to_person=target_person,
            requester=requester,
        )
        view = create_initialized_view(dupe_person, name="+index")
        notifications = view.request.response.notifications
        message = "Finch is queued to be merged in a few minutes."
        self.assertEqual(1, len(notifications))
        self.assertEqual(message, notifications[0].message)

    def test_closeAccount_admin(self):
        person = self.factory.makePerson()
        admin = getUtility(ILaunchpadCelebrities).admin.teamowner
        browser = self.getViewBrowser(
            person, view_name="+close-account", user=admin
        )
        browser.getControl("Close").click()
        self.assertIn(
            "This account will now be permanently closed.", browser.contents
        )

        # the close account job is created with Waiting status
        job_source = getUtility(IPersonCloseAccountJobSource)
        with person_logged_in(admin):
            job = removeSecurityProxy(job_source.find(person).one())
            self.assertEqual(JobStatus.WAITING, job.status)

    def test_closeAccount_registry_expert(self):
        person = self.factory.makePerson()
        registry_expert = self.factory.makeRegistryExpert()
        admin = getUtility(ILaunchpadCelebrities).admin.teamowner
        with person_logged_in(registry_expert):
            browser = self.getViewBrowser(
                person, view_name="+close-account", user=registry_expert
            )
            browser.getControl("Close").click()
            self.assertIn(
                "This account will now be permanently closed.",
                browser.contents,
            )
        # the close account job is created with Waiting status
        job_source = getUtility(IPersonCloseAccountJobSource)
        with person_logged_in(admin):
            job = removeSecurityProxy(job_source.find(person).one())
            self.assertEqual(JobStatus.WAITING, job.status)

    def test_closeAccount_user_themselves(self):
        # The user themselves cannot close their own account
        person = self.factory.makePerson()

        # The user themselves will not see the Administer Account
        # option in the context menu so they won't be able to navigate
        # to the Close Account screen
        browser = self.getViewBrowser(person, view_name="+index", user=person)
        self.assertRaises(
            LinkNotFoundError, browser.getLink, "Administer Account"
        )
        # if the user goes to the URL directly they will get
        # an Unauthorized
        self.assertRaises(
            Unauthorized,
            self.getViewBrowser,
            person,
            view_name="+close-account",
            user=person,
        )

    def test_closeAccount_other_user(self):
        # Another user cannot close the account for a regular permissions user
        person = self.factory.makePerson()
        other_user = self.factory.makePerson()

        browser = self.getViewBrowser(
            person, view_name="+index", user=other_user
        )
        self.assertRaises(
            LinkNotFoundError, browser.getLink, "Administer Account"
        )
        # if the other user goes to the URL directly they will get
        # an Unauthorized
        self.assertRaises(
            Unauthorized,
            self.getViewBrowser,
            person,
            view_name="+close-account",
            user=other_user,
        )

    def test_display_utcoffset(self):
        person = self.factory.makePerson(time_zone="Asia/Kolkata")
        html = create_initialized_view(person, "+portlet-contact-details")()
        self.assertThat(
            extract_text(html),
            DocTestMatches(
                extract_text("... Asia/Kolkata (UTC+0530) ..."),
                doctest.ELLIPSIS
                | doctest.NORMALIZE_WHITESPACE
                | doctest.REPORT_NDIFF,
            ),
        )

    def test_description_widget(self):
        # The view provides a widget to render and edit the person description.
        person = self.factory.makePerson()
        view = create_initialized_view(person, "+index")
        self.assertIsInstance(view.description_widget, TextAreaEditorWidget)
        self.assertEqual(
            "description", view.description_widget.exported_field.__name__
        )

    def test_description_widget_is_probationary(self):
        # Description text is not linkified when the user is probationary.
        person = self.factory.makePerson()
        view = create_initialized_view(person, "+index")
        self.assertIs(True, person.is_probationary)
        self.assertIs(False, view.description_widget.linkify_text)

    def test_description_widget_non_probationary(self):
        # Description text is linkified when the user is non-probationary.
        person = self.factory.makeTeam()
        view = create_initialized_view(person, "+index")
        self.assertIs(False, person.is_probationary)
        self.assertIs(True, view.description_widget.linkify_text)

    @staticmethod
    def get_markup(view, person):
        def fake_method():
            return canonical_url(person)

        with monkey_patch(view, _getURL=fake_method):
            markup = view.render()
        return markup

    def test_is_probationary_or_invalid_user_with_non_probationary(self):
        team = self.factory.makeTeam()
        view = create_initialized_view(
            team, "+index", principal=team.teamowner
        )
        self.assertIs(False, view.is_probationary_or_invalid_user)
        markup = view.render()
        self.assertFalse('name="robots" content="noindex,nofollow"' in markup)

    def test_is_probationary_or_invalid_user_with_probationary(self):
        person = self.factory.makePerson()
        view = create_initialized_view(person, "+index", principal=person)
        self.assertIs(True, view.is_probationary_or_invalid_user)
        markup = self.get_markup(view, person)
        self.assertTrue('name="robots" content="noindex,nofollow"' in markup)

    def test_is_probationary_or_invalid_user_with_invalid(self):
        person = self.factory.makePerson(
            account_status=AccountStatus.NOACCOUNT
        )
        observer = self.factory.makePerson()
        view = create_initialized_view(person, "+index", principal=observer)
        self.assertIs(True, view.is_probationary_or_invalid_user)
        markup = self.get_markup(view, person)
        self.assertTrue('name="robots" content="noindex,nofollow"' in markup)

    def test_person_view_page_description(self):
        person_description = self.factory.getUniqueString()
        person = self.factory.makePerson(description=person_description)
        view = create_initialized_view(person, "+index")
        self.assertThat(view.page_description, Equals(person_description))

    def test_person_view_change_password(self):
        person = self.factory.makePerson()
        view = create_initialized_view(person, "+index", principal=person)
        with person_logged_in(person):
            markup = self.get_markup(view, person)
        password_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Change password",
                "a",
                attrs={"href": "http://testopenid.test/"},
                text="Change password",
            )
        )
        self.assertThat(markup, password_match)

    def test_assigned_blueprints(self):
        person = self.factory.makePerson()
        public_spec = self.factory.makeSpecification(
            assignee=person,
            implementation_status=SpecificationImplementationStatus.STARTED,
            information_type=InformationType.PUBLIC,
        )
        private_name = "super-private"
        self.factory.makeSpecification(
            name=private_name,
            assignee=person,
            implementation_status=SpecificationImplementationStatus.STARTED,
            information_type=InformationType.PROPRIETARY,
        )
        with person_logged_in(None):
            browser = self.getViewBrowser(person)
        self.assertIn(public_spec.name, browser.contents)
        self.assertNotIn(private_name, browser.contents)

    def test_only_assigned_blueprints(self):
        # Only assigned blueprints are listed, not arbitrary related
        # blueprints
        person = self.factory.makePerson()
        spec = self.factory.makeSpecification(
            implementation_status=SpecificationImplementationStatus.STARTED,
            owner=person,
            drafter=person,
            approver=person,
        )
        spec.subscribe(person)
        with person_logged_in(None):
            browser = self.getViewBrowser(person)
        self.assertNotIn(spec.name, browser.contents)

    def test_show_gpg_keys_for_view_owner(self):
        person = self.factory.makePerson()
        with person_logged_in(person):
            view = create_initialized_view(person, "+index")
            self.assertTrue(view.should_show_gpgkeys_section)

    def test_gpg_keys_not_shown_for_user_with_no_gpg_keys(self):
        person = self.factory.makePerson()
        view = create_initialized_view(person, "+index")
        self.assertFalse(view.should_show_gpgkeys_section)

    def test_gpg_keys_shown_for_user_with_gpg_keys(self):
        person = self.factory.makePerson()
        self.factory.makeGPGKey(person)
        view = create_initialized_view(person, "+index")
        self.assertTrue(view.should_show_gpgkeys_section)

    def test_show_oci_registry_credentials_link(self):
        person = self.factory.makePerson()
        view = create_initialized_view(person, "+index", principal=person)
        with person_logged_in(person):
            markup = self.get_markup(view, person)
        link_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "OCIRegistryCredentials link",
                "a",
                attrs={
                    "href": (
                        "http://launchpad.test/~%s/+oci-registry-credentials"
                        % person.name
                    )
                },
                text="OCI registry credentials",
            )
        )
        self.assertThat(markup, link_match)

        login(ANONYMOUS)
        markup = self.get_markup(view, person)
        self.assertNotEqual("", markup)
        self.assertThat(markup, Not(link_match))

    def test_show_oci_recipes_link(self):
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        person = self.factory.makePerson()
        # Creates a recipe, so the link appears.
        self.factory.makeOCIRecipe(owner=person, registrant=person)
        view = create_initialized_view(person, "+index", principal=person)
        with person_logged_in(person):
            markup = self.get_markup(view, person)
        expected_url = "http://launchpad.test/~%s/+oci-recipes" % person.name
        link_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "OCI recipes link",
                "a",
                attrs={"href": expected_url},
                text="View OCI recipes",
            )
        )
        self.assertThat(markup, link_match)

        login(ANONYMOUS)
        markup = self.get_markup(view, person)
        self.assertThat(markup, link_match)

    def test_shows_oci_recipes_link_if_user_doesnt_have_oci_recipes(self):
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        person = self.factory.makePerson()
        # Creates a recipe from another user, just to make sure it will not
        # interfere.
        self.factory.makeOCIRecipe()
        view = create_initialized_view(person, "+index", principal=person)
        with person_logged_in(person):
            markup = self.get_markup(view, person)
        expected_url = "http://launchpad.test/~%s/+oci-recipes" % person.name
        link_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "OCI recipes link",
                "a",
                attrs={"href": expected_url},
                text="View OCI recipes",
            )
        )
        self.assertThat(markup, link_match)

        login(ANONYMOUS)
        markup = self.get_markup(view, person)
        self.assertThat(markup, link_match)

    def test_ppas_query_count(self):
        owner = self.factory.makePerson()

        def create_ppa_and_permission():
            ppa = self.factory.makeArchive(
                owner=owner, purpose=ArchivePurpose.PPA, private=True
            )
            ppa.newComponentUploader(self.user, "main")

        recorder1, recorder2 = record_two_runs(
            lambda: self.getMainText(owner, "+index"),
            create_ppa_and_permission,
            5,
            login_method=lambda: login_person(owner),
            record_request=True,
        )
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))


class TestPersonViewKarma(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        person = self.factory.makePerson()
        product = self.factory.makeProduct()
        transaction.commit()
        self.view = PersonView(person, LaunchpadTestRequest())
        self._makeKarmaCache(
            person,
            product,
            IStore(KarmaCategory).find(KarmaCategory, name="bugs").one(),
        )
        self._makeKarmaCache(
            person,
            product,
            IStore(KarmaCategory).find(KarmaCategory, name="answers").one(),
        )
        self._makeKarmaCache(
            person,
            product,
            IStore(KarmaCategory).find(KarmaCategory, name="code").one(),
        )

    def test_karma_category_sort(self):
        categories = self.view.contributed_categories
        category_names = []
        for category in categories:
            category_names.append(category.name)

        self.assertEqual(
            category_names,
            ["code", "bugs", "answers"],
            "Categories are not sorted correctly",
        )

    def _makeKarmaCache(self, person, product, category, value=10):
        """Create and return a KarmaCache entry with the given arguments.

        A commit is implicitly triggered because the 'karma' dbuser is used.
        """
        with dbuser("karma"):
            cache_manager = getUtility(IKarmaCacheManager)
            karmacache = cache_manager.new(
                value, person.id, category.id, product_id=product.id
            )

            try:
                cache_manager.updateKarmaValue(
                    value, person.id, category_id=None, product_id=product.id
                )
            except NotFoundError:
                cache_manager.new(
                    value, person.id, category_id=None, product_id=product.id
                )

        return karmacache


class TestShouldShowPpaSection(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        TestCaseWithFactory.setUp(self)
        self.owner = self.factory.makePerson(name="mowgli")
        self.person_ppa = self.factory.makeArchive(owner=self.owner)
        self.team = self.factory.makeTeam(name="jbook", owner=self.owner)

        # The team is the owner of the PPA.
        self.team_ppa = self.factory.makeArchive(owner=self.team)
        self.team_view = PersonView(self.team, LaunchpadTestRequest())

    def make_ppa_private(self, ppa):
        """Helper method to privatise a ppa."""
        login("foo.bar@canonical.com")
        ppa.private = True
        login(ANONYMOUS)

    def test_viewing_person_with_public_ppa(self):
        # Show PPA section only if context has at least one PPA the user is
        # authorised to view the PPA.
        login(ANONYMOUS)
        person_view = PersonView(self.owner, LaunchpadTestRequest())
        self.assertTrue(person_view.should_show_ppa_section)

    def test_viewing_person_without_ppa(self):
        # If the context person does not have a ppa then the section
        # should not display.
        login(ANONYMOUS)
        person_without_ppa = self.factory.makePerson()
        person_view = PersonView(person_without_ppa, LaunchpadTestRequest())
        self.assertFalse(person_view.should_show_ppa_section)

    def test_viewing_self(self):
        # If the current user has edit access to the context person then
        # the section should always display.
        login_person(self.owner)
        person_view = PersonView(self.owner, LaunchpadTestRequest())
        self.assertTrue(person_view.should_show_ppa_section)

        # If the ppa is private, the section is still displayed to
        # a user with edit access to the person.
        self.make_ppa_private(self.person_ppa)
        login_person(self.owner)
        person_view = PersonView(self.owner, LaunchpadTestRequest())
        self.assertTrue(person_view.should_show_ppa_section)

        # Even a person without a PPA will see the section when viewing
        # themselves.
        person_without_ppa = self.factory.makePerson()
        login_person(person_without_ppa)
        person_view = PersonView(person_without_ppa, LaunchpadTestRequest())
        self.assertTrue(person_view.should_show_ppa_section)

    def test_anon_viewing_person_with_private_ppa(self):
        # If the ppa is private, the ppa section will not be displayed
        # to users without view access to the ppa.
        self.make_ppa_private(self.person_ppa)
        login(ANONYMOUS)
        person_view = PersonView(self.owner, LaunchpadTestRequest())
        self.assertFalse(person_view.should_show_ppa_section)

        # But if the context person has a second ppa that is public,
        # then anon users will see the section.
        self.factory.makeArchive(owner=self.owner)
        person_view = PersonView(self.owner, LaunchpadTestRequest())
        self.assertTrue(person_view.should_show_ppa_section)

    def test_viewing_team_with_private_ppa(self):
        # If a team PPA is private, the ppa section will be displayed
        # to team members.
        self.make_ppa_private(self.team_ppa)
        member = self.factory.makePerson()
        login_person(self.owner)
        self.team.addMember(member, self.owner)
        login_person(member)

        # So the member will see the section.
        person_view = PersonView(self.team, LaunchpadTestRequest())
        self.assertTrue(person_view.should_show_ppa_section)

        # But other users who are not members will not.
        non_member = self.factory.makePerson()
        login_person(non_member)
        person_view = PersonView(self.team, LaunchpadTestRequest())
        self.assertFalse(person_view.should_show_ppa_section)

        # Unless the team also has another ppa which is public.
        self.factory.makeArchive(owner=self.team)
        person_view = PersonView(self.team, LaunchpadTestRequest())
        self.assertTrue(person_view.should_show_ppa_section)


class TestPersonRenameFormMixin:
    def test_can_rename_with_empty_PPA(self):
        # If a PPA exists but has no packages, we can rename the
        # person.
        self.view.initialize()
        self.assertFalse(self.view.form_fields["name"].for_display)

    def _publishPPAPackage(self):
        stp = SoyuzTestPublisher()
        stp.setUpDefaultDistroSeries()
        stp.getPubSource(archive=self.ppa)

    def test_cannot_rename_with_non_empty_PPA(self):
        # Publish some packages in the PPA and test that we can't rename
        # the person.
        self._publishPPAPackage()
        self.view.initialize()
        self.assertTrue(self.view.form_fields["name"].for_display)
        self.assertEqual(
            self.view.widgets["name"].hint,
            "This person has an active PPA with packages published and "
            "may not be renamed.",
        )

    def test_cannot_rename_with_deleting_PPA(self):
        # When a PPA is in the DELETING state we should not allow
        # renaming just yet.
        self._publishPPAPackage()
        self.view.initialize()
        self.ppa.delete(self.person)
        self.assertEqual(self.ppa.status, ArchiveStatus.DELETING)
        self.assertTrue(self.view.form_fields["name"].for_display)

    def test_can_rename_with_deleted_PPA(self):
        # Delete a PPA and test that the person can be renamed.
        self._publishPPAPackage()
        # Deleting the PPA will remove the publications, which is
        # necessary for the renaming check.
        self.ppa.delete(self.person)
        # Simulate the external script running and finalising the
        # DELETED status.
        self.ppa.status = ArchiveStatus.DELETED
        self.view.initialize()
        self.assertFalse(self.view.form_fields["name"].for_display)


class TestPersonEditView(TestPersonRenameFormMixin, TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.valid_email_address = self.factory.getUniqueEmailAddress()
        self.person = self.factory.makePerson(email=self.valid_email_address)
        login_person(self.person)
        self.ppa = self.factory.makeArchive(owner=self.person)
        self.view = create_initialized_view(self.person, "+edit")

    def test_unclean_usernames_cannot_be_set(self):
        # Users cannot set unclean usernames
        form = {
            "field.name": "unclean.name",
            "field.actions.save": "Save Changes",
        }
        view = create_initialized_view(self.person, "+edit", form=form)

        expected_msg = html_escape(
            dedent(
                """
            Invalid username 'unclean.name'. Usernames must be at least three
            and no longer than 32 characters long. They must contain at least
            one letter, start and end with a letter or number. All letters
            must be lower-case and non-consecutive hyphens are allowed."""
            )
        )
        self.assertEqual(expected_msg, view.errors[0])

    def test_unclean_usernames_do_not_block_edit(self):
        # Users with unclean usernames (less restrictive) are not forced
        # to update it along with other details of their account.
        dirty_person = self.factory.makePerson(name="unclean.name")
        login_person(dirty_person)

        form = {
            "field.display_name": "Nice Displayname",
            "field.name": dirty_person.name,
            "field.actions.save": "Save Changes",
        }
        view = create_initialized_view(dirty_person, "+edit", form=form)

        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        self.assertEqual(
            "The changes to your personal details have been saved.",
            notifications[0].message,
        )
        self.assertEqual("Nice Displayname", dirty_person.displayname)
        self.assertEqual("unclean.name", dirty_person.name)

    def createAddEmailView(self, email_address):
        """Test helper to create +editemails view."""
        form = {
            "field.VALIDATED_SELECTED": self.valid_email_address,
            "field.VALIDATED_SELECTED-empty-marker": 1,
            "field.actions.add_email": "Add",
            "field.newemail": email_address,
        }
        return create_initialized_view(self.person, "+editemails", form=form)

    def createSetContactViaAddEmailView(self, email_address):
        """Test helper to use +editemails view to set preferred address."""
        form = {
            "field.VALIDATED_SELECTED": email_address,
            "field.actions.set_preferred": "Set as Contact Address",
        }
        return create_initialized_view(self.person, "+editemails", form=form)

    def _assertEmailAndError(self, email_str, expected_msg):
        """Special assert function for dealing with email-related errors."""
        view = self.createAddEmailView(email_str)
        error_msg = view.errors[0]
        if not isinstance(error_msg, str):
            error_msg = error_msg.doc()
        self.assertEqual(expected_msg, error_msg)

    def test_add_email(self):
        """Adding email should add a login token, notification, and email."""
        stub.test_emails = []
        email_address = self.factory.getUniqueEmailAddress()
        view = self.createAddEmailView(email_address)
        # If everything worked, there should now be a login token to validate
        # this email address for this user.
        token = getUtility(ILoginTokenSet).searchByEmailRequesterAndType(
            email_address, self.person, LoginTokenType.VALIDATEEMAIL
        )
        self.assertIsNotNone(token)
        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        expected_msg = html_escape(
            "A confirmation message has been sent to '%s'."
            " Follow the instructions in that message to confirm"
            " that the address is yours. (If the message doesn't arrive in a"
            " few minutes, your mail provider might use 'greylisting', which"
            " could delay the message for up to an hour or two.)"
            % email_address
        )
        self.assertEqual(expected_msg, notifications[0].message)
        transaction.commit()
        self.assertEqual(2, len(stub.test_emails))
        to_addrs = [to_addr for from_addr, to_addr, msg in stub.test_emails]
        # Both the new and old addr should be sent email.
        self.assertIn([self.valid_email_address], to_addrs)
        self.assertIn([email_address], to_addrs)

    def test_add_email_address_taken(self):
        """Adding an already existing email should give error notice."""
        email_address = self.factory.getUniqueEmailAddress()
        self.factory.makePerson(
            name="deadaccount",
            displayname="deadaccount",
            email=email_address,
            account_status=AccountStatus.NOACCOUNT,
        )
        view = self.createAddEmailView(email_address)
        error_msg = view.errors[0]
        expected_msg = (
            "The email address '%s' is already registered to "
            '<a href="http://launchpad.test/~deadaccount">deadaccount</a>. '
            "If you think that is a duplicated account, you can "
            '<a href="http://launchpad.test/people/+requestmerge?'
            'field.dupe_person=deadaccount">merge it</a> into your account.'
            % email_address
        )
        self.assertEqual(expected_msg, error_msg)

    def test_validate_email(self):
        """Validating an email should send a notice email to the user."""
        stub.test_emails = []
        added_email = self.factory.getUniqueEmailAddress()
        view = self.createAddEmailView(added_email)
        form = {
            "field.UNVALIDATED_SELECTED": added_email,
            "field.actions.validate": "Confirm",
        }
        view = create_initialized_view(self.person, "+editemails", form=form)
        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        expected_msg = html_escape(
            "An email message was sent to '%s' "
            "with instructions on how to confirm that it belongs to you."
            % added_email
        )
        self.assertEqual(expected_msg, notifications[0].message)
        # Ensure we sent mail to the right address.
        transaction.commit()
        to_addrs = [to_addr for from_addr, to_addr, msg in stub.test_emails]
        self.assertIn([added_email], to_addrs)

    def test_validate_token(self):
        """Hitting +validateemail should actually validate the email."""
        stub.test_emails = []
        added_email = self.factory.getUniqueEmailAddress()
        self.createAddEmailView(added_email)
        form = {
            "field.UNVALIDATED_SELECTED": added_email,
            "field.actions.validate": "Confirm",
        }
        create_initialized_view(self.person, "+editemails", form=form)
        # Get the token from the email msg.
        transaction.commit()
        messages = [msg for from_addr, to_addr, msg in stub.test_emails]
        raw_msg = None
        for orig_msg in messages:
            msg = email.message_from_bytes(orig_msg)
            if msg.get("to") == added_email:
                raw_msg = orig_msg
        token_url = get_token_url_from_email(raw_msg)
        browser = setupBrowserForUser(user=self.person)
        browser.open(token_url)
        expected_msg = "Confirm email address <code>%s</code>" % added_email
        self.assertIn(expected_msg, browser.contents)
        browser.getControl("Continue").click()
        # Login again to access displayname, since browser logged us out.
        login_person(self.person)
        expected_title = "%s in Launchpad" % self.person.displayname
        self.assertEqual(expected_title, browser.title)

    def test_remove_unvalidated_email_address(self):
        """A user should be able to remove and unvalidated email."""
        added_email = self.factory.getUniqueEmailAddress()
        view = self.createAddEmailView(added_email)
        form = {
            "field.UNVALIDATED_SELECTED": added_email,
            "field.actions.remove_unvalidated": "Remove",
        }
        view = create_initialized_view(self.person, "+editemails", form=form)
        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        expected_msg = html_escape(
            "The email address '%s' has been removed." % added_email
        )
        self.assertEqual(expected_msg, notifications[0].message)

    def test_cannot_remove_contact_address(self):
        """A user should not be able to remove their own contact email."""
        form = {
            "field.VALIDATED_SELECTED": self.valid_email_address,
            "field.actions.remove_validated": "Remove",
        }
        view = create_initialized_view(self.person, "+editemails", form=form)
        error_msg = view.errors[0]
        expected_msg = html_escape(
            "You can't remove %s because it's your contact email address."
            % self.valid_email_address
        )
        self.assertEqual(expected_msg, error_msg)

    def test_set_contact_address(self):
        """A user should be able to change to a new contact email."""
        added_email = self.factory.getUniqueEmailAddress()
        view = self.createAddEmailView(added_email)
        # We need a commit to make sure person and other data are in DB.
        transaction.commit()
        validated_email = getUtility(IEmailAddressSet).new(
            added_email, self.person
        )
        self.person.validateAndEnsurePreferredEmail(validated_email)
        view = self.createSetContactViaAddEmailView(added_email)
        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        expected_msg = (
            "Your contact address has been changed to: %s" % added_email
        )
        self.assertEqual(expected_msg, notifications[0].message)

    def test_set_contact_address_already_set(self):
        """Users should be warned when setting the same contact email."""
        view = self.createSetContactViaAddEmailView(self.valid_email_address)
        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        expected_msg = (
            "%s is already set as your contact address."
            % self.valid_email_address
        )
        self.assertEqual(expected_msg, notifications[0].message)

    def test_team_editemails_not_found(self):
        """Teams should not have a +editemails page."""
        self.useFixture(FakeLogger())
        team = self.factory.makeTeam(owner=self.person, members=[self.person])
        url = "%s/+editemails" % canonical_url(team)
        browser = setupBrowserForUser(user=self.person)
        self.assertRaises(NotFound, browser.open, url)

    def test_team_editmailinglists_not_found(self):
        """Teams should not have a +editmailinglists page."""
        self.useFixture(FakeLogger())
        team = self.factory.makeTeam(owner=self.person, members=[self.person])
        url = "%s/+editmailinglists" % canonical_url(team)
        browser = setupBrowserForUser(user=self.person)
        self.assertRaises(NotFound, browser.open, url)

    def test_email_string_validation_no_email_prodvided(self):
        """+editemails should warn if no email is provided."""
        no_email = ""
        expected_msg = "Required input is missing."
        self._assertEmailAndError(no_email, expected_msg)

    def test_email_string_validation_invalid_email(self):
        """+editemails should warn when provided data is not an email."""
        not_an_email = "foo"
        expected_msg = html_escape(
            "'foo' doesn't seem to be a valid email address."
        )
        self._assertEmailAndError(not_an_email, expected_msg)

    def test_email_string_validation_is_escaped(self):
        """+editemails should escape output to prevent XSS."""
        xss_email = "foo@example.com<script>window.alert('XSS')</script>"
        expected_msg = (
            "&#x27;foo@example.com&lt;script&gt;"
            "window.alert(&#x27;XSS&#x27;)&lt;/script&gt;&#x27;"
            " doesn&#x27;t seem to be a valid email address."
        )
        self._assertEmailAndError(xss_email, expected_msg)

    def test_edit_email_login_redirect(self):
        """+editemails should redirect to force you to re-authenticate."""
        view = create_initialized_view(self.person, "+editemails")
        response = view.request.response
        self.assertEqual(302, response.getStatus())
        expected_url = "%s/+editemails/+login?reauth=1" % canonical_url(
            self.person
        )
        self.assertEqual(expected_url, response.getHeader("location"))

    def test_description_hint_depends_on_probationary_status(self):
        """
        The hint message for the 'Description' field should change
        depending on the `probationary` status of the person. We don't
        linkify the URLs in the description for people without karma, this
        should be reflected in the hint text.
        """
        person_with_karma = self.factory.makePerson(karma=10)
        view = create_initialized_view(person_with_karma, "+edit")
        self.assertIn("URLs are linked", view.widgets["description"].hint)

        person_without_karma = self.factory.makePerson(karma=0)
        view = create_initialized_view(person_without_karma, "+edit")
        self.assertNotIn("URLs are linked", view.widgets["description"].hint)


class TestPersonParticipationView(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.user = self.factory.makePerson()
        self.view = create_view(self.user, name="+participation")

    def test__asParticipation_owner(self):
        # Team owners have the role of 'Owner'.
        self.factory.makeTeam(owner=self.user)
        [participation] = self.view.active_participations
        self.assertEqual("Owner", participation["role"])

    def test__asParticipation_admin(self):
        # Team admins have the role of 'Admin'.
        team = self.factory.makeTeam()
        login_person(team.teamowner)
        team.addMember(self.user, team.teamowner)
        for membership in self.user.team_memberships:
            membership.setStatus(TeamMembershipStatus.ADMIN, team.teamowner)
        [participation] = self.view.active_participations
        self.assertEqual("Admin", participation["role"])

    def test__asParticipation_member(self):
        # The default team role is 'Member'.
        team = self.factory.makeTeam()
        login_person(team.teamowner)
        team.addMember(self.user, team.teamowner)
        [participation] = self.view.active_participations
        self.assertEqual("Member", participation["role"])

    def test__asParticipation_without_mailing_list(self):
        # The default team role is 'Member'.
        team = self.factory.makeTeam()
        login_person(team.teamowner)
        team.addMember(self.user, team.teamowner)
        [participation] = self.view.active_participations
        self.assertEqual("&mdash;", participation["subscribed"])

    def test__asParticipation_unsubscribed_to_mailing_list(self):
        # The default team role is 'Member'.
        team = self.factory.makeTeam()
        self.factory.makeMailingList(team, team.teamowner)
        login_person(team.teamowner)
        team.addMember(self.user, team.teamowner)
        [participation] = self.view.active_participations
        self.assertEqual("Not subscribed", participation["subscribed"])

    def test__asParticipation_subscribed_to_mailing_list(self):
        # The default team role is 'Member'.
        team = self.factory.makeTeam()
        mailing_list = self.factory.makeMailingList(team, team.teamowner)
        mailing_list.subscribe(self.user)
        login_person(team.teamowner)
        team.addMember(self.user, team.teamowner)
        [participation] = self.view.active_participations
        self.assertEqual("Subscribed", participation["subscribed"])

    def test__asParticipation_dateexpires(self):
        team = self.factory.makeTeam(owner=self.user)
        [participation] = self.view.active_participations

        self.assertIsNone(participation["dateexpires"])

        membership_set = getUtility(ITeamMembershipSet)
        membership = membership_set.getByPersonAndTeam(self.user, team)
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        with person_logged_in(self.user):
            membership.setExpirationDate(tomorrow, self.user)
        view = create_view(self.user, name="+participation")
        [participation] = view.active_participations

        self.assertEqual(tomorrow, participation["dateexpires"])

    def test_active_participations_with_direct_private_team(self):
        # Users cannot see private teams that they are not members of.
        owner = self.factory.makePerson()
        team = self.factory.makeTeam(
            owner=owner, visibility=PersonVisibility.PRIVATE
        )
        login_person(owner)
        team.addMember(self.user, owner)
        # The team is included in active_participations.
        login_person(self.user)
        view = create_view(
            self.user, name="+participation", principal=self.user
        )
        self.assertEqual(1, len(view.active_participations))
        # The team is not included in active_participations.
        observer = self.factory.makePerson()
        login_person(observer)
        view = create_view(
            self.user, name="+participation", principal=observer
        )
        self.assertEqual(0, len(view.active_participations))

    def test_active_participations_with_indirect_private_team(self):
        # Users cannot see private teams that they are not members of.
        owner = self.factory.makePerson()
        team = self.factory.makeTeam(
            owner=owner, visibility=PersonVisibility.PRIVATE
        )
        direct_team = self.factory.makeTeam(owner=owner)
        login_person(owner)
        direct_team.addMember(self.user, owner)
        team.addMember(direct_team, owner)
        # The team is included in active_participations.
        login_person(self.user)
        view = create_view(
            self.user, name="+participation", principal=self.user
        )
        self.assertEqual(2, len(view.active_participations))
        # The team is not included in active_participations.
        observer = self.factory.makePerson()
        login_person(observer)
        view = create_view(
            self.user, name="+participation", principal=observer
        )
        self.assertEqual(1, len(view.active_participations))

    def test_active_participations_indirect_membership(self):
        # Verify the path of indirect membership.
        a_team = self.factory.makeTeam(name="a")
        b_team = self.factory.makeTeam(name="b", owner=a_team)
        self.factory.makeTeam(name="c", owner=b_team)
        login_person(a_team.teamowner)
        a_team.addMember(self.user, a_team.teamowner)
        transaction.commit()
        participations = self.view.active_participations
        self.assertEqual(3, len(participations))
        display_names = [
            participation["displayname"] for participation in participations
        ]
        self.assertEqual(["A", "B", "C"], display_names)
        self.assertEqual(None, participations[0]["via"])
        self.assertEqual("A", participations[1]["via"])
        self.assertEqual("B, A", participations[2]["via"])

    def test_active_participations_public_via_private_team(self):
        # Private teams that grant a user access to public teams are listed,
        # but redacted if the requesting user does not have access to them.
        owner = self.factory.makePerson()
        direct_team = self.factory.makeTeam(
            owner=owner, name="a", visibility=PersonVisibility.PRIVATE
        )
        indirect_team = self.factory.makeTeam(owner=owner, name="b")
        login_person(owner)
        direct_team.addMember(self.user, owner)
        indirect_team.addMember(direct_team, owner)
        # The private team is included in active_participations and via.
        login_person(self.user)
        view = create_view(
            self.user, name="+participation", principal=self.user
        )
        participations = view.active_participations
        self.assertEqual(2, len(participations))
        self.assertIsNone(participations[0]["via"])
        self.assertEqual("A", participations[1]["via"])
        # The private team is not included in active_participations and via.
        observer = self.factory.makePerson()
        login_person(observer)
        view = create_view(
            self.user, name="+participation", principal=observer
        )
        participations = view.active_participations
        self.assertEqual(1, len(participations))
        self.assertEqual("[private team]", participations[0]["via"])

    def test_has_participations_false(self):
        participations = self.view.active_participations
        self.assertEqual(0, len(participations))
        self.assertEqual(False, self.view.has_participations)

    def test_has_participations_true(self):
        self.factory.makeTeam(owner=self.user)
        participations = self.view.active_participations
        self.assertEqual(1, len(participations))
        self.assertEqual(True, self.view.has_participations)

    def test_mailing_list_subscriptions_query_count(self):
        # Additional mailing list subscriptions do not add additional queries.
        def create_subscriptions():
            direct_team = self.factory.makeTeam(members=[self.user])
            direct_list = self.factory.makeMailingList(
                direct_team, direct_team.teamowner
            )
            direct_list.subscribe(self.user)
            indirect_team = self.factory.makeTeam(members=[direct_team])
            indirect_list = self.factory.makeMailingList(
                indirect_team, indirect_team.teamowner
            )
            indirect_list.subscribe(self.user)

        def get_participations():
            clear_property_cache(self.view)
            return list(self.view.active_participations)

        recorder1, recorder2 = record_two_runs(
            get_participations, create_subscriptions, 5
        )
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))


class TestPersonRelatedPackagesView(TestCaseWithFactory):
    """Test the related software view."""

    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.user = self.factory.makePerson()
        self.factory.makeGPGKey(self.user)
        self.ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.warty = self.ubuntu.getSeries("warty")
        self.view = create_initialized_view(self.user, "+related-packages")

    def publishSources(self, archive, maintainer):
        publisher = SoyuzTestPublisher()
        publisher.person = self.user
        login("foo.bar@canonical.com")
        spphs = []
        for count in range(0, self.view.max_results_to_display + 3):
            source_name = "foo" + str(count)
            spph = publisher.getPubSource(
                sourcename=source_name,
                status=PackagePublishingStatus.PUBLISHED,
                archive=archive,
                maintainer=maintainer,
                creator=self.user,
                distroseries=self.warty,
            )
            spphs.append(spph)
        # Update the releases cache table.
        switch_dbuser("garbo_frequently")
        job = PopulateLatestPersonSourcePackageReleaseCache(DevNullLogger())
        while not job.isDone():
            job(chunk_size=100)
        switch_dbuser("launchpad")
        login(ANONYMOUS)
        return spphs

    def copySources(self, spphs, copier, dest_distroseries):
        self.copier = self.factory.makePerson()
        for spph in spphs:
            spph.copyTo(
                dest_distroseries,
                creator=copier,
                pocket=PackagePublishingPocket.UPDATES,
                archive=dest_distroseries.main_archive,
            )

    def test_view_helper_attributes(self):
        # Verify view helper attributes.
        self.assertEqual("Related packages", self.view.page_title)
        self.assertEqual("summary_list_size", self.view._max_results_key)
        self.assertEqual(
            config.launchpad.summary_list_size,
            self.view.max_results_to_display,
        )

    def test_tableHeaderMessage(self):
        limit = self.view.max_results_to_display
        expected = "Displaying first %s packages out of 100 total" % limit
        self.assertEqual(expected, self.view._tableHeaderMessage(100))
        expected = "%s packages" % limit
        self.assertEqual(expected, self.view._tableHeaderMessage(limit))
        expected = "1 package"
        self.assertEqual(expected, self.view._tableHeaderMessage(1))

    def test_latest_uploaded_ppa_packages_with_stats(self):
        # Verify number of PPA packages to display.
        ppa = self.factory.makeArchive(owner=self.user)
        self.publishSources(ppa, self.user)
        count = len(self.view.latest_uploaded_ppa_packages_with_stats)
        self.assertEqual(self.view.max_results_to_display, count)

    def test_latest_maintained_packages_with_stats(self):
        # Verify number of maintained packages to display.
        self.publishSources(self.warty.main_archive, self.user)
        count = len(self.view.latest_maintained_packages_with_stats)
        self.assertEqual(self.view.max_results_to_display, count)

    def test_latest_uploaded_nonmaintained_packages_with_stats(self):
        # Verify number of non maintained packages to display.
        maintainer = self.factory.makePerson()
        self.publishSources(self.warty.main_archive, maintainer)
        count = len(
            self.view.latest_uploaded_but_not_maintained_packages_with_stats
        )
        self.assertEqual(self.view.max_results_to_display, count)

    def test_latest_synchronised_publishings_with_stats(self):
        # Verify number of non synchronised publishings to display.
        creator = self.factory.makePerson()
        spphs = self.publishSources(self.warty.main_archive, creator)
        dest_distroseries = self.factory.makeDistroSeries()
        self.copySources(spphs, self.user, dest_distroseries)
        count = len(self.view.latest_synchronised_publishings_with_stats)
        self.assertEqual(self.view.max_results_to_display, count)


class TestPersonMaintainedPackagesView(TestCaseWithFactory):
    """Test the maintained packages view."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.user = self.factory.makePerson()
        self.view = create_initialized_view(self.user, "+maintained-packages")

    def test_view_helper_attributes(self):
        # Verify view helper attributes.
        self.assertEqual("Maintained Packages", self.view.page_title)
        self.assertEqual("default_batch_size", self.view._max_results_key)
        self.assertEqual(
            config.launchpad.default_batch_size,
            self.view.max_results_to_display,
        )


class TestPersonUploadedPackagesView(TestCaseWithFactory):
    """Test the maintained packages view."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.user = self.factory.makePerson()
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PRIMARY)
        spr = self.factory.makeSourcePackageRelease(
            creator=self.user, archive=archive
        )
        self.spph = self.factory.makeSourcePackagePublishingHistory(
            sourcepackagerelease=spr, archive=archive
        )
        self.view = create_initialized_view(self.user, "+uploaded-packages")

    def test_view_helper_attributes(self):
        # Verify view helper attributes.
        self.assertEqual("Uploaded packages", self.view.page_title)
        self.assertEqual("default_batch_size", self.view._max_results_key)
        self.assertEqual(
            config.launchpad.default_batch_size,
            self.view.max_results_to_display,
        )


class TestPersonPPAPackagesView(TestCaseWithFactory):
    """Test the maintained packages view."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.user = self.factory.makePerson()
        self.view = create_initialized_view(self.user, "+ppa-packages")

    def test_view_helper_attributes(self):
        # Verify view helper attributes.
        self.assertEqual("PPA packages", self.view.page_title)
        self.assertEqual("default_batch_size", self.view._max_results_key)
        self.assertEqual(
            config.launchpad.default_batch_size,
            self.view.max_results_to_display,
        )


class PersonOwnedTeamsViewTestCase(TestCaseWithFactory):
    """Test +owned-teams view."""

    layer = DatabaseFunctionalLayer

    def test_properties(self):
        # The batch is created when the view is initialized.
        owner = self.factory.makePerson()
        team = self.factory.makeTeam(owner=owner)
        view = create_initialized_view(owner, "+owned-teams")
        self.assertEqual("Owned teams", view.page_title)
        self.assertEqual("team", view.batchnav._singular_heading)
        self.assertEqual([team], view.batch)

    def test_page_text_with_teams(self):
        # When the person owns teams, the page shows a a listing
        # table. There is always a link to the team participation page.
        owner = self.factory.makePerson(name="snarf")
        self.factory.makeTeam(owner=owner, name="pting")
        with person_logged_in(owner):
            view = create_initialized_view(
                owner, "+owned-teams", principal=owner
            )
            markup = view()
        soup = find_tag_by_id(markup, "maincontent")
        participation_link = "http://launchpad.test/~snarf/+participation"
        self.assertIsNotNone(soup.find("a", {"href": participation_link}))
        self.assertIsNotNone(soup.find("table", {"id": "owned-teams"}))
        self.assertIsNotNone(soup.find("a", {"href": "/~pting"}))
        self.assertIsNotNone(soup.find("table", {"class": "upper-batch-nav"}))
        self.assertIsNotNone(soup.find("table", {"class": "lower-batch-nav"}))

    def test_page_text_without_teams(self):
        # When the person does not own teams, the page states the case.
        owner = self.factory.makePerson(name="pting")
        with person_logged_in(owner):
            view = create_initialized_view(
                owner, "+owned-teams", principal=owner
            )
            markup = view()
        soup = find_tag_by_id(markup, "maincontent")
        self.assertIsNotNone(soup.find("p", {"id": "no-teams"}))


class TestPersonSynchronisedPackagesView(TestCaseWithFactory):
    """Test the synchronised packages view."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        user = self.factory.makePerson()
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PRIMARY)
        spr = self.factory.makeSourcePackageRelease(
            creator=user, archive=archive
        )
        spph = self.factory.makeSourcePackagePublishingHistory(
            sourcepackagerelease=spr, archive=archive
        )
        self.copier = self.factory.makePerson()
        dest_distroseries = self.factory.makeDistroSeries()
        self.copied_spph = spph.copyTo(
            dest_distroseries,
            creator=self.copier,
            pocket=PackagePublishingPocket.UPDATES,
            archive=dest_distroseries.main_archive,
        )
        self.view = create_initialized_view(
            self.copier, "+synchronised-packages"
        )

    def test_view_helper_attributes(self):
        # Verify view helper attributes.
        self.assertEqual("Synchronised packages", self.view.page_title)
        self.assertEqual("default_batch_size", self.view._max_results_key)
        self.assertEqual(
            config.launchpad.default_batch_size,
            self.view.max_results_to_display,
        )


class TestPersonRelatedProjectsView(TestCaseWithFactory):
    """Test the maintained packages view."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.user = self.factory.makePerson()

    def test_view_helper_attributes(self):
        # Verify view helper attributes.
        view = create_initialized_view(self.user, "+related-projects")
        self.assertEqual("Related projects", view.page_title)
        self.assertEqual("default_batch_size", view._max_results_key)
        self.assertEqual(
            config.launchpad.default_batch_size, view.max_results_to_display
        )

    def test_batching(self):
        for _ in range(10):
            self.factory.makeProduct(owner=self.user)
        view = create_initialized_view(self.user, "+related-projects")
        next_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Next link",
                "a",
                attrs={
                    "href": re.compile(re.escape("?batch=5&memo=5&start=5"))
                },
                text="Next",
            )
        )
        self.assertThat(view(), next_match)


class TestPersonOCIRegistryCredentialsView(
    WithScenarios, BrowserTestCase, OCIConfigHelperMixin
):
    layer = DatabaseFunctionalLayer

    scenarios = [
        ("person", {"use_team": False}),
        ("team", {"use_team": True}),
    ]

    def setUp(self):
        super().setUp()
        self.setConfig()
        if self.use_team:
            self.owner = self.factory.makeTeam(members=[self.user])
        else:
            self.owner = self.user
        self.ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.distroseries = self.factory.makeDistroSeries(
            distribution=self.ubuntu, name="shiny", displayname="Shiny"
        )
        self.useFixture(
            FeatureFixture(
                {
                    OCI_RECIPE_ALLOW_CREATE: "on",
                    "oci.build_series.%s"
                    % self.ubuntu.name: self.distroseries.name,
                }
            )
        )
        oci_project = self.factory.makeOCIProject(pillar=self.ubuntu)
        self.recipe = self.factory.makeOCIRecipe(
            registrant=self.owner, owner=self.owner, oci_project=oci_project
        )

    def test_view_oci_registry_credentials(self):
        # Verify view helper attributes.
        url = self.factory.getUniqueURL()
        credentials = {"username": "foo", "password": "bar"}
        getUtility(IOCIRegistryCredentialsSet).new(
            registrant=self.user,
            owner=self.owner,
            url=url,
            credentials=credentials,
        )
        login_person(self.user)
        view = create_initialized_view(
            self.owner, "+oci-registry-credentials", principal=self.user
        )
        self.assertEqual("OCI registry credentials", view.page_title)
        self.assertThat(
            view.oci_registry_credentials,
            MatchesSetwise(
                MatchesOCIRegistryCredentials(
                    MatchesStructure.byEquality(owner=self.owner, url=url),
                    Equals(credentials),
                )
            ),
        )

    def test_edit_oci_registry_credentials(self):
        url = self.factory.getUniqueURL()
        newurl = self.factory.getUniqueURL()
        third_url = self.factory.getUniqueURL()
        credentials = {"username": "foo", "password": "bar"}
        registry_credentials = getUtility(IOCIRegistryCredentialsSet).new(
            registrant=self.user,
            owner=self.owner,
            url=url,
            credentials=credentials,
        )

        browser = self.getViewBrowser(
            self.owner, view_name="+oci-registry-credentials", user=self.user
        )
        browser.getLink("Edit OCI registry credentials").click()

        # Change only the username
        registry_credentials_id = removeSecurityProxy(registry_credentials).id
        username_control = browser.getControl(
            name="field.username.%d" % registry_credentials_id
        )
        username_control.value = "different_username"
        browser.getControl("Save").click()
        with person_logged_in(self.user):
            self.assertThat(
                registry_credentials,
                MatchesOCIRegistryCredentials(
                    MatchesStructure.byEquality(owner=self.owner, url=url),
                    MatchesDict(
                        {
                            "username": Equals("different_username"),
                            "password": Equals("bar"),
                        }
                    ),
                ),
            )

        # change only the registry url and region
        browser = self.getViewBrowser(
            self.owner, view_name="+oci-registry-credentials", user=self.user
        )
        browser.getLink("Edit OCI registry credentials").click()
        url_control = browser.getControl(
            name="field.url.%d" % registry_credentials_id
        )
        url_control.value = newurl
        url_control = browser.getControl(
            name="field.region.%d" % registry_credentials_id
        )
        url_control.value = "us-west-2"
        browser.getControl("Save").click()
        with person_logged_in(self.user):
            self.assertThat(
                registry_credentials,
                MatchesOCIRegistryCredentials(
                    MatchesStructure.byEquality(owner=self.owner, url=newurl),
                    MatchesDict(
                        {
                            "username": Equals("different_username"),
                            "password": Equals("bar"),
                            "region": Equals("us-west-2"),
                        }
                    ),
                ),
            )

        # change only the password
        browser = self.getViewBrowser(
            self.owner, view_name="+oci-registry-credentials", user=self.user
        )
        browser.getLink("Edit OCI registry credentials").click()
        password_control = browser.getControl(
            name="field.password.%d" % registry_credentials_id
        )
        password_control.value = "newpassword"

        browser.getControl("Save").click()
        self.assertIn("Passwords do not match.", browser.contents)

        # change all fields (except region) with one edit action
        username_control = browser.getControl(
            name="field.username.%d" % registry_credentials_id
        )
        username_control.value = "third_different_username"
        url_control = browser.getControl(
            name="field.url.%d" % registry_credentials_id
        )
        url_control.value = third_url
        password_control = browser.getControl(
            name="field.password.%d" % registry_credentials_id
        )
        password_control.value = "third_newpassword"
        confirm_password_control = browser.getControl(
            name="field.confirm_password.%d" % registry_credentials_id
        )
        confirm_password_control.value = "third_newpassword"
        browser.getControl("Save").click()
        with person_logged_in(self.user):
            self.assertThat(
                registry_credentials,
                MatchesOCIRegistryCredentials(
                    MatchesStructure.byEquality(
                        owner=self.owner, url=third_url
                    ),
                    MatchesDict(
                        {
                            "username": Equals("third_different_username"),
                            "password": Equals("third_newpassword"),
                            "region": Equals("us-west-2"),
                        }
                    ),
                ),
            )

    def test_add_oci_registry_credentials(self):
        url = self.factory.getUniqueURL()
        credentials = {"username": "foo", "password": "bar"}
        image_name = self.factory.getUniqueUnicode()
        registry_credentials = getUtility(IOCIRegistryCredentialsSet).new(
            registrant=self.user,
            owner=self.owner,
            url=url,
            credentials=credentials,
        )
        getUtility(IOCIPushRuleSet).new(
            recipe=self.recipe,
            registry_credentials=registry_credentials,
            image_name=image_name,
        )
        owner_name = self.owner.name
        new_owner = self.factory.makeTeam(members=[self.user])
        new_owner_name = new_owner.name

        browser = self.getViewBrowser(
            self.owner, view_name="+oci-registry-credentials", user=self.user
        )
        browser.getLink("Edit OCI registry credentials").click()
        self.assertEqual(
            [owner_name], browser.getControl(name="field.add_owner").value
        )

        browser.getControl(name="field.add_url").value = url
        browser.getControl(name="field.add_region").value = "sa-east-1"
        browser.getControl(name="field.add_owner").value = [new_owner_name]
        browser.getControl(name="field.add_username").value = "new_username"
        browser.getControl(name="field.add_password").value = "password"
        browser.getControl(name="field.add_confirm_password").value = (
            "password"
        )
        browser.getControl("Save").click()

        with person_logged_in(self.user):
            self.assertThat(
                getUtility(IOCIRegistryCredentialsSet).findByOwner(self.owner),
                MatchesSetwise(
                    MatchesOCIRegistryCredentials(
                        MatchesStructure.byEquality(owner=self.owner, url=url),
                        MatchesDict(
                            {
                                "username": Equals("foo"),
                                "password": Equals("bar"),
                            }
                        ),
                    )
                ),
            )
            self.assertThat(
                getUtility(IOCIRegistryCredentialsSet).findByOwner(new_owner),
                MatchesSetwise(
                    MatchesOCIRegistryCredentials(
                        MatchesStructure.byEquality(owner=new_owner, url=url),
                        MatchesDict(
                            {
                                "username": Equals("new_username"),
                                "password": Equals("password"),
                                "region": Equals("sa-east-1"),
                            }
                        ),
                    )
                ),
            )

    def test_delete_oci_registry_credentials(self):
        # Test that we do not delete credentials when there are
        # push rules defined to use them
        url = self.factory.getUniqueURL()
        credentials = {"username": "foo", "password": "bar"}
        registry_credentials = getUtility(IOCIRegistryCredentialsSet).new(
            registrant=self.user,
            owner=self.owner,
            url=url,
            credentials=credentials,
        )
        IStore(registry_credentials).flush()
        registry_credentials_id = removeSecurityProxy(registry_credentials).id
        image_name = self.factory.getUniqueUnicode()
        push_rule = getUtility(IOCIPushRuleSet).new(
            recipe=self.recipe,
            registry_credentials=registry_credentials,
            image_name=image_name,
        )

        browser = self.getViewBrowser(
            self.owner, view_name="+oci-registry-credentials", user=self.user
        )
        browser.getLink("Edit OCI registry credentials").click()
        # assert full rule is displayed
        self.assertEqual(
            url,
            browser.getControl(
                name="field.url.%d" % registry_credentials_id
            ).value,
        )
        self.assertEqual(
            credentials.get("username"),
            browser.getControl(
                name="field.username.%d" % registry_credentials_id
            ).value,
        )

        # mark one line of credentials for delete
        browser.getControl(
            name="field.delete.%d" % registry_credentials_id
        ).value = True
        browser.getControl("Save").click()
        self.assertIn(
            "These credentials cannot be deleted as there are "
            "push rules defined that still use them.",
            browser.contents,
        )

        # make sure we don't have any push rules defined to use
        # the credentials we want to remove
        with person_logged_in(self.user):
            removeSecurityProxy(push_rule).destroySelf()

        browser.getControl(
            name="field.delete.%d" % registry_credentials_id
        ).value = True
        browser.getControl("Save").click()
        credentials_set = getUtility(IOCIRegistryCredentialsSet)
        with person_logged_in(self.user):
            self.assertEqual(
                0, credentials_set.findByOwner(self.owner).count()
            )


class TestPersonLiveFSView(BrowserTestCase):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({LIVEFS_FEATURE_FLAG: "on"}))
        self.person = self.factory.makePerson(
            name="test-person", displayname="Test Person"
        )

    def makeLiveFS(self, count=1):
        with person_logged_in(self.person):
            return [
                self.factory.makeLiveFS(
                    registrant=self.person, owner=self.person
                )
                for _ in range(count)
            ]

    def test_displays_livefs(self):
        livefs = self.factory.makeLiveFS(
            registrant=self.person, owner=self.person
        )
        view = create_initialized_view(
            self.person, "+livefs", principal=self.person
        )

        expected_url = "/~%s/+livefs/%s/%s/%s" % (
            livefs.owner.name,
            livefs.distro_series.distribution.name,
            livefs.distro_series.name,
            livefs.name,
        )
        link_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Livefs name link",
                "a",
                attrs={"href": expected_url},
                text=livefs.name,
            )
        )
        date_formatter = DateTimeFormatterAPI(livefs.date_created)
        date_created_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Livefs date created",
                "td",
                text="%s" % date_formatter.displaydate(),
            )
        )
        series_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Livefs series",
                "td",
                text="%s" % livefs.distro_series.display_name,
            )
        )
        with person_logged_in(self.person):
            self.assertThat(view.render(), link_match)
            self.assertThat(view.render(), date_created_match)
            self.assertThat(view.render(), series_match)

    def test_displays_no_livefs(self):
        view = create_initialized_view(
            self.person, "+livefs", principal=self.person
        )
        no_livefs_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "No livefs",
                "p",
                text="There are no live filesystems for %s"
                % self.person.display_name,
            )
        )
        with person_logged_in(self.person):
            self.assertThat(view.render(), no_livefs_match)

    def test_paginates_livefs(self):
        batch_size = 5
        self.pushConfig("launchpad", default_batch_size=batch_size)
        livefs = self.makeLiveFS(10)
        view = create_initialized_view(
            self.person, "+livefs", principal=self.person
        )
        no_livefs_match = soupmatchers.HTMLContains(
            soupmatchers.Tag("Top livefs paragraph", "strong", text="10")
        )
        first_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Navigation first",
                "span",
                attrs={"class": "first inactive"},
                text="First",
            )
        )
        previous_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Navigation previous",
                "span",
                attrs={"class": "previous inactive"},
                text="Previous",
            )
        )
        with person_logged_in(self.person):
            self.assertThat(view.render(), no_livefs_match)
            self.assertThat(view.render(), first_match)
            self.assertThat(view.render(), previous_match)
            self.assertThat(
                view.render(),
                soupmatchers.HTMLContains(
                    soupmatchers.Within(
                        soupmatchers.Tag(
                            "next element",
                            "a",
                            attrs={"id": "lower-batch-nav-batchnav-next"},
                        ),
                        soupmatchers.Tag("next link", "strong", text="Next"),
                    )
                ),
            )
            self.assertThat(
                view.render(),
                soupmatchers.HTMLContains(
                    soupmatchers.Tag(
                        "last element",
                        "a",
                        attrs={"id": "lower-batch-nav-batchnav-last"},
                        text="Last",
                    )
                ),
            )

            # Assert we're listing the first set of live filesystems
            items = sorted(livefs, key=attrgetter("name"))
            for lfs in items[:batch_size]:
                expected_url = "/~%s/+livefs/%s/%s/%s" % (
                    lfs.owner.name,
                    lfs.distro_series.distribution.name,
                    lfs.distro_series.name,
                    lfs.name,
                )
                link_match = soupmatchers.HTMLContains(
                    soupmatchers.Tag(
                        "Livefs name link",
                        "a",
                        attrs={"href": expected_url},
                        text=lfs.name,
                    )
                )
                self.assertThat(view.render(), link_match)

            view = create_initialized_view(
                self.person,
                "+livefs",
                principal=self.person,
                query_string="batch=5&memo=5&start=5",
            )
            for lfs in items[batch_size:]:
                self.assertIn(lfs.name, view.render())
            self.assertNotIn(items[0].name, view.render())

    def test_displays_livefs_only_for_owner(self):
        livefs = self.factory.makeLiveFS(
            registrant=self.person, owner=self.person
        )
        different_owner = self.factory.makePerson(
            name="different-person", displayname="Different Person"
        )
        livefs_different_owner = self.factory.makeLiveFS(
            registrant=different_owner, owner=different_owner
        )
        view = create_initialized_view(
            self.person, "+livefs", principal=self.person
        )
        expected_url = "/~%s/+livefs/%s/%s/%s" % (
            livefs.owner.name,
            livefs.distro_series.distribution.name,
            livefs.distro_series.name,
            livefs.name,
        )
        link_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Livefs name link",
                "a",
                attrs={"href": expected_url},
                text=livefs.name,
            )
        )
        date_formatter = DateTimeFormatterAPI(livefs.date_created)
        date_created_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Livefs date created",
                "td",
                text="%s" % date_formatter.displaydate(),
            )
        )

        different_owner_url = "/~%s/+livefs/%s/%s/%s" % (
            livefs_different_owner.owner.name,
            livefs_different_owner.distro_series.distribution.name,
            livefs_different_owner.distro_series.name,
            livefs_different_owner.name,
        )
        different_owner_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Livefs name link",
                "a",
                attrs={"href": different_owner_url},
                text=livefs_different_owner.name,
            )
        )

        with person_logged_in(self.person):
            self.assertThat(view.render(), link_match)
            self.assertThat(view.render(), date_created_match)
            self.assertNotIn(different_owner_match, view.render())


class TestPersonRelatedPackagesFailedBuild(TestCaseWithFactory):
    """The related packages views display links to failed builds."""

    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.user = self.factory.makePerson()

        # First we need to publish some PPA packages with failed builds
        # for this person.
        # XXX michaeln 2010-06-10 bug=592050.
        # Strangely, the builds need to be built in the context of a
        # main archive to reproduce bug 591010 for which this test was
        # written to demonstrate.
        login("foo.bar@canonical.com")
        publisher = SoyuzTestPublisher()
        publisher.prepareBreezyAutotest()
        ppa = self.factory.makeArchive(owner=self.user)
        spph = self.factory.makeSourcePackagePublishingHistory(
            sourcepackagename="foo",
            version="666",
            spr_creator=self.user,
            maintainer=self.user,
            archive=ppa,
        )
        das = self.factory.makeDistroArchSeries(
            distroseries=spph.distroseries, architecturetag="cyr128"
        )
        self.build = self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            archive=spph.distroseries.distribution.main_archive,
            distroarchseries=das,
        )
        self.build.updateStatus(BuildStatus.FAILEDTOBUILD)
        # Update the releases cache table.
        switch_dbuser("garbo_frequently")
        job = PopulateLatestPersonSourcePackageReleaseCache(DevNullLogger())
        while not job.isDone():
            job(chunk_size=100)
        switch_dbuser("launchpad")
        login(ANONYMOUS)

    def test_related_software_with_failed_build(self):
        # The link to the failed build is displayed.
        self.view = create_view(self.user, name="+related-packages")
        html = self.view()
        self.assertIn(
            '<a href="/ubuntu/+source/foo/666/+build/%d">cyr128</a>'
            % (self.build.id),
            html,
        )

    def test_related_ppa_packages_with_failed_build(self):
        # The link to the failed build is displayed.
        self.view = create_view(self.user, name="+ppa-packages")
        html = self.view()
        self.assertIn(
            '<a href="/ubuntu/+source/foo/666/+build/%d">cyr128</a>'
            % (self.build.id),
            html,
        )


class TestPersonRelatedPackagesSynchronisedPackages(TestCaseWithFactory):
    """The related packages views display links to synchronised packages."""

    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.user = self.factory.makePerson()
        self.spph = self.factory.makeSourcePackagePublishingHistory()

    def createCopiedSource(self, copier, spph):
        self.copier = self.factory.makePerson()
        dest_distroseries = self.factory.makeDistroSeries()
        return spph.copyTo(
            dest_distroseries,
            creator=copier,
            pocket=PackagePublishingPocket.UPDATES,
            archive=dest_distroseries.main_archive,
        )

    def getLinkToSynchronisedMatcher(self):
        person_url = canonical_url(self.user)
        return soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Synchronised packages link",
                "a",
                attrs={"href": person_url + "/+synchronised-packages"},
                text="Synchronised packages",
            )
        )

    def test_related_software_no_link_synchronised_packages(self):
        # No link to the synchronised packages page if no synchronised
        # packages.
        view = create_view(self.user, name="+related-packages")
        synced_package_link_matcher = self.getLinkToSynchronisedMatcher()
        self.assertThat(view(), Not(synced_package_link_matcher))

    def test_related_software_link_synchronised_packages(self):
        # If this person has synced packages, the link to the synchronised
        # packages page is present.
        self.createCopiedSource(self.user, self.spph)
        view = create_view(self.user, name="+related-packages")
        synced_package_link_matcher = self.getLinkToSynchronisedMatcher()
        self.assertThat(view(), synced_package_link_matcher)

    def test_related_software_displays_synchronised_packages(self):
        copied_spph = self.createCopiedSource(self.user, self.spph)
        view = create_view(self.user, name="+related-packages")
        synced_packages_title = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Synchronised packages title",
                "h2",
                text="Synchronised packages",
            )
        )
        expected_base = "/%s/+source/%s" % (
            copied_spph.distroseries.distribution.name,
            copied_spph.source_package_name,
        )
        source_link = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Source package link",
                "a",
                text=copied_spph.sourcepackagerelease.name,
                attrs={"href": expected_base},
            )
        )
        version_url = (
            expected_base + "/%s" % copied_spph.sourcepackagerelease.version
        )
        version_link = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Source package version link",
                "a",
                text=copied_spph.sourcepackagerelease.version,
                attrs={"href": version_url},
            )
        )

        self.assertThat(view(), synced_packages_title)
        self.assertThat(view(), source_link)
        self.assertThat(view(), version_link)


class TestPersonDeactivateAccountView(TestCaseWithFactory):
    """Tests for the PersonDeactivateAccountView."""

    layer = DatabaseFunctionalLayer
    form = {
        "field.comment": "Gotta go.",
        "field.actions.deactivate": "Deactivate My Account",
    }

    def test_deactivate_user_active(self):
        user = self.factory.makePerson()
        login_person(user)
        view = create_initialized_view(
            user, "+deactivate-account", form=self.form
        )
        self.assertEqual([], view.errors)
        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        self.assertEqual(
            "Your account has been deactivated.", notifications[0].message
        )
        self.assertEqual(AccountStatus.DEACTIVATED, user.account_status)

    def test_deactivate_user_already_deactivated(self):
        deactivated_user = self.factory.makePerson()
        login_person(deactivated_user)
        deactivated_user.deactivate(comment="going.")
        view = create_initialized_view(
            deactivated_user, "+deactivate-account", form=self.form
        )
        self.assertEqual(1, len(view.errors))
        self.assertEqual(
            "This account is already deactivated.", view.errors[0]
        )


class TestTeamInvitationView(TestCaseWithFactory):
    """Tests for TeamInvitationView."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.a_team = self.factory.makeTeam(
            name="team-a", displayname="A-Team"
        )
        self.b_team = self.factory.makeTeam(
            name="team-b", displayname="B-Team"
        )
        transaction.commit()

    def test_circular_invite(self):
        """Two teams can invite each other without horrifying results."""

        # Make the criss-cross invitations.
        # A invites B.
        login_person(self.a_team.teamowner)
        form = {
            "field.newmember": "team-b",
            "field.actions.add": "Add Member",
        }
        view = create_initialized_view(self.a_team, "+addmember", form=form)
        self.assertEqual([], view.errors)
        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        self.assertEqual(
            "B-Team (team-b) has been invited to join this team.",
            notifications[0].message,
        )

        # B invites A.
        login_person(self.b_team.teamowner)
        form["field.newmember"] = "team-a"
        view = create_initialized_view(self.b_team, "+addmember", form=form)
        self.assertEqual([], view.errors)
        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        self.assertEqual(
            "A-Team (team-a) has been invited to join this team.",
            notifications[0].message,
        )

        # Team A accepts the invitation.
        login_person(self.a_team.teamowner)
        form = {
            "field.actions.accept": "Accept",
            "field.acknowledger_comment": "Thanks for inviting us.",
        }
        request = LaunchpadTestRequest(form=form, method="POST")
        request.setPrincipal(self.a_team.teamowner)
        membership_set = getUtility(ITeamMembershipSet)
        membership = membership_set.getByPersonAndTeam(
            self.a_team, self.b_team
        )
        view = TeamInvitationView(membership, request)
        view.initialize()
        self.assertEqual([], view.errors)
        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        self.assertEqual(
            "This team is now a member of B-Team.", notifications[0].message
        )

        # Team B attempts to accept the invitation.
        login_person(self.b_team.teamowner)
        request = LaunchpadTestRequest(form=form, method="POST")
        request.setPrincipal(self.b_team.teamowner)
        membership = membership_set.getByPersonAndTeam(
            self.b_team, self.a_team
        )
        view = TeamInvitationView(membership, request)
        view.initialize()
        self.assertEqual([], view.errors)
        notifications = view.request.response.notifications
        self.assertEqual(1, len(notifications))
        expected = (
            "This team may not be added to A-Team because it is a member "
            "of B-Team."
        )
        self.assertEqual(expected, notifications[0].message)


class TestSubscriptionsView(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp(user="test@canonical.com")
        self.user = getUtility(ILaunchBag).user
        self.person = self.factory.makePerson()
        self.other_person = self.factory.makePerson()
        self.team = self.factory.makeTeam(owner=self.user)
        self.team.addMember(self.person, self.user)

    def test_unsubscribe_link_appears_for_user(self):
        login_person(self.person)
        view = create_view(self.person, "+subscriptions")
        self.assertTrue(view.canUnsubscribeFromBugTasks())

    def test_unsubscribe_link_does_not_appear_for_not_user(self):
        login_person(self.other_person)
        view = create_view(self.person, "+subscriptions")
        self.assertFalse(view.canUnsubscribeFromBugTasks())

    def test_unsubscribe_link_appears_for_team_member(self):
        login_person(self.person)
        view = create_initialized_view(self.team, "+subscriptions")
        self.assertTrue(view.canUnsubscribeFromBugTasks())


class BugTaskViewsTestBase:
    """A base class for bugtask search related tests."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()
        with person_logged_in(self.person):
            self.subscribed_bug = self.factory.makeBug()
            self.subscribed_bug.subscribe(
                self.person, subscribed_by=self.person
            )
            self.assigned_bug = self.factory.makeBug()
            self.assigned_bug.default_bugtask.transitionToAssignee(self.person)
            self.owned_bug = self.factory.makeBug(owner=self.person)
            self.commented_bug = self.factory.makeBug()
            self.commented_bug.newMessage(owner=self.person)
            self.affecting_bug = self.factory.makeBug()
            self.affecting_bug.markUserAffected(self.person)

        for bug in (
            self.subscribed_bug,
            self.assigned_bug,
            self.owned_bug,
            self.commented_bug,
            self.affecting_bug,
        ):
            with person_logged_in(bug.default_bugtask.product.owner):
                milestone = self.factory.makeMilestone(
                    product=bug.default_bugtask.product
                )
                bug.default_bugtask.transitionToMilestone(
                    milestone, bug.default_bugtask.product.owner
                )

    def test_searchUnbatched(self):
        view = create_initialized_view(self.person, self.view_name)
        self.assertEqual(
            self.expected_for_search_unbatched, list(view.searchUnbatched())
        )

    def test_getMilestoneWidgetValues(self):
        view = create_initialized_view(self.person, self.view_name)
        milestones = [
            bugtask.milestone for bugtask in self.expected_for_search_unbatched
        ]
        milestones = sorted(milestones, key=milestone_sort_key, reverse=True)
        expected = [
            {
                "title": milestone.title,
                "value": milestone.id,
                "checked": False,
            }
            for milestone in milestones
        ]
        Store.of(milestones[0]).invalidate()
        with StormStatementRecorder() as recorder:
            self.assertEqual(expected, view.getMilestoneWidgetValues())
        self.assertThat(recorder, HasQueryCount(LessThan(6)))


class TestPersonRelatedBugTaskSearchListingView(
    BugTaskViewsTestBase, TestCaseWithFactory
):
    """Tests for PersonRelatedBugTaskSearchListingView."""

    view_name = "+bugs"

    def setUp(self):
        super().setUp()
        self.expected_for_search_unbatched = [
            self.subscribed_bug.default_bugtask,
            self.assigned_bug.default_bugtask,
            self.owned_bug.default_bugtask,
            self.commented_bug.default_bugtask,
        ]


class TestPersonAssignedBugTaskSearchListingView(
    BugTaskViewsTestBase, TestCaseWithFactory
):
    """Tests for PersonAssignedBugTaskSearchListingView."""

    view_name = "+assignedbugs"

    def setUp(self):
        super().setUp()
        self.expected_for_search_unbatched = [
            self.assigned_bug.default_bugtask,
        ]


class TestPersonCommentedBugTaskSearchListingView(
    BugTaskViewsTestBase, TestCaseWithFactory
):
    """Tests for PersonAssignedBugTaskSearchListingView."""

    view_name = "+commentedbugs"

    def setUp(self):
        super().setUp()
        self.expected_for_search_unbatched = [
            self.commented_bug.default_bugtask,
        ]


class TestPersonReportedBugTaskSearchListingView(
    BugTaskViewsTestBase, TestCaseWithFactory
):
    """Tests for PersonAssignedBugTaskSearchListingView."""

    view_name = "+reportedbugs"

    def setUp(self):
        super().setUp()
        self.expected_for_search_unbatched = [
            self.owned_bug.default_bugtask,
        ]


class TestPersonSubscribedBugTaskSearchListingView(
    BugTaskViewsTestBase, TestCaseWithFactory
):
    """Tests for PersonAssignedBugTaskSearchListingView."""

    view_name = "+subscribedbugs"

    def setUp(self):
        super().setUp()
        self.expected_for_search_unbatched = [
            self.subscribed_bug.default_bugtask,
            self.owned_bug.default_bugtask,
        ]


class TestPersonAffectingBugTaskSearchListingView(
    BugTaskViewsTestBase, TestCaseWithFactory
):
    """Tests for PersonAffectingBugTaskSearchListingView."""

    view_name = "+affectingbugs"

    def setUp(self):
        super().setUp()
        # Bugs filed by this user are marked as affecting them by default, so
        # the bug we filed is returned.
        self.expected_for_search_unbatched = [
            self.owned_bug.default_bugtask,
            self.affecting_bug.default_bugtask,
        ]


class TestPersonRdfView(BrowserTestCase):
    """Test the RDF view."""

    layer = DatabaseFunctionalLayer

    def test_headers(self):
        """The headers for the RDF view of a person should be as expected."""
        person = self.factory.makePerson()
        content_disposition = 'attachment; filename="%s.rdf"' % person.name
        browser = self.getViewBrowser(person, view_name="+rdf")
        self.assertEqual(
            content_disposition, browser.headers["Content-disposition"]
        )
        self.assertEqual(
            'application/rdf+xml;charset="utf-8"',
            browser.headers["Content-type"],
        )


class TestPersonViewSSHKeys(BrowserTestCase):
    """Tests for Person:+sshkeys."""

    layer = DatabaseFunctionalLayer

    def test_no_keys(self):
        person = self.factory.makePerson()
        browser = self.getViewBrowser(person, view_name="+sshkeys")
        self.assertEqual(
            "text/plain;charset=utf-8", browser.headers["Content-Type"]
        )
        self.assertEqual("", browser.contents)

    def test_keys(self):
        person = self.factory.makePerson()
        with person_logged_in(person):
            keys = [self.factory.makeSSHKey(person) for _ in range(2)]
        browser = self.getViewBrowser(person, view_name="+sshkeys")
        self.assertEqual(
            "text/plain;charset=utf-8", browser.headers["Content-Type"]
        )
        self.assertContentEqual(
            [key.getFullKeyText() + "\n" for key in keys],
            re.findall(r".*\n", browser.contents),
        )


load_tests = load_tests_apply_scenarios
