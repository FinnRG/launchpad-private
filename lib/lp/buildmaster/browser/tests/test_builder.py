# Copyright 2010-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the lp.soyuz.browser.builder module."""

from datetime import timedelta

from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.browser.tales import DurationFormatterAPI
from lp.buildmaster.browser.tests.test_builder_views import BuildCreationMixin
from lp.buildmaster.enums import BuilderCleanStatus, BuildStatus
from lp.buildmaster.interfaces.builder import IBuilderSet
from lp.buildmaster.model.builder import Builder
from lp.oci.interfaces.ocirecipe import OCI_RECIPE_ALLOW_CREATE
from lp.services.database.interfaces import IStore
from lp.services.database.sqlbase import get_transaction_timestamp
from lp.services.features.testing import FeatureFixture
from lp.services.job.model.job import Job
from lp.services.webapp.publisher import canonical_url
from lp.soyuz.interfaces.livefs import LIVEFS_FEATURE_FLAG
from lp.testing import (
    TestCaseWithFactory,
    admin_logged_in,
    logout,
    record_two_runs,
)
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.matchers import HasQueryCount
from lp.testing.pages import extract_text, find_tags_by_class, setupBrowser
from lp.testing.views import create_initialized_view


def builders_homepage_render():
    builders = getUtility(IBuilderSet)
    return create_initialized_view(builders, "+index").render()


class TestBuilderSetNavigation(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_binary_package_build_api_redirects(self):
        build = self.factory.makeBinaryPackageBuild()
        url = "http://api.launchpad.test/devel/builders/+build/%s" % build.id
        expected_url = "http://api.launchpad.test/devel" + canonical_url(
            build, path_only_if_possible=True
        )
        logout()
        browser = setupBrowser()
        browser.open(url)
        self.assertEqual(expected_url, browser.url)

    def test_source_package_recipe_build_api_redirects(self):
        build = self.factory.makeSourcePackageRecipeBuild()
        url = (
            "http://api.launchpad.test/devel/builders/+recipebuild/%s"
            % build.id
        )
        expected_url = "http://api.launchpad.test/devel" + canonical_url(
            build, path_only_if_possible=True
        )
        logout()
        browser = setupBrowser()
        browser.open(url)
        self.assertEqual(expected_url, browser.url)

    def test_livefs_build_api_redirects(self):
        self.useFixture(FeatureFixture({LIVEFS_FEATURE_FLAG: "on"}))
        build = self.factory.makeLiveFSBuild()
        url = (
            "http://api.launchpad.test/devel/builders/+livefsbuild/%s"
            % build.id
        )
        expected_url = "http://api.launchpad.test/devel" + canonical_url(
            build, path_only_if_possible=True
        )
        logout()
        browser = setupBrowser()
        browser.open(url)
        self.assertEqual(expected_url, browser.url)

    def test_snap_build_api_redirects(self):
        build = self.factory.makeSnapBuild()
        url = (
            "http://api.launchpad.test/devel/builders/+snapbuild/%s" % build.id
        )
        expected_url = "http://api.launchpad.test/devel" + canonical_url(
            build, path_only_if_possible=True
        )
        logout()
        browser = setupBrowser()
        browser.open(url)
        self.assertEqual(expected_url, browser.url)

    def test_oci_recipe_build_api_redirects(self):
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        build = self.factory.makeOCIRecipeBuild()
        url = (
            "http://api.launchpad.test/devel/builders/+ocirecipebuild/%s"
            % build.id
        )
        expected_url = "http://api.launchpad.test/devel" + canonical_url(
            build, path_only_if_possible=True
        )
        logout()
        browser = setupBrowser()
        browser.open(url)
        self.assertEqual(expected_url, browser.url)

    def test_ci_build_api_redirects(self):
        build = self.factory.makeCIBuild()
        url = "http://api.launchpad.test/devel/builders/+cibuild/%s" % build.id
        expected_url = "http://api.launchpad.test/devel" + canonical_url(
            build, path_only_if_possible=True
        )
        logout()
        browser = setupBrowser()
        browser.open(url)
        self.assertEqual(expected_url, browser.url)


class TestBuildersHomepage(TestCaseWithFactory, BuildCreationMixin):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        # Create a non-buildfarm job to ensure that the BuildQueue and
        # Job IDs differ, detecting bug #919116.
        Job()
        # And create BuildFarmJobs of the various types to throw IDs off
        # even further, detecting more preloading issues.
        self.factory.makeBinaryPackageBuild().queueBuild()
        self.factory.makeSourcePackageRecipeBuild().queueBuild()
        self.factory.makeTranslationTemplatesBuild().queueBuild()

    def test_builders_binary_package_build_query_count(self):
        def create_build():
            build = self.createBinaryPackageBuild()
            build.updateStatus(
                BuildStatus.NEEDSBUILD, force_invalid_transition=True
            )
            queue = build.queueBuild()
            queue.markAsBuilding(build.builder)

        nb_objects = 2
        recorder1, recorder2 = record_two_runs(
            builders_homepage_render, create_build, nb_objects
        )
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    def test_builders_recipe_build_query_count(self):
        def create_build():
            build = self.createRecipeBuildWithBuilder()
            build.updateStatus(
                BuildStatus.NEEDSBUILD, force_invalid_transition=True
            )
            queue = build.queueBuild()
            queue.markAsBuilding(build.builder)

        nb_objects = 2
        recorder1, recorder2 = record_two_runs(
            builders_homepage_render, create_build, nb_objects
        )
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    def test_builders_translation_template_build_query_count(self):
        def create_build():
            queue = self.factory.makeTranslationTemplatesBuild().queueBuild()
            queue.markAsBuilding(self.factory.makeBuilder())

        nb_objects = 2
        recorder1, recorder2 = record_two_runs(
            builders_homepage_render, create_build, nb_objects
        )
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    def test_builders_variety_query_count(self):
        def create_builds():
            bqs = [
                self.factory.makeBinaryPackageBuild().queueBuild(),
                self.factory.makeSourcePackageRecipeBuild().queueBuild(),
                self.factory.makeTranslationTemplatesBuild().queueBuild(),
            ]
            for bq in bqs:
                bq.markAsBuilding(self.factory.makeBuilder())

        nb_objects = 2
        recorder1, recorder2 = record_two_runs(
            builders_homepage_render, create_builds, nb_objects
        )
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    def test_category_portlet_not_shown_if_empty(self):
        content = builders_homepage_render()
        self.assertIn("Virtual build status", content)
        self.assertIn("Non-virtual build status", content)

        with admin_logged_in():
            getUtility(IBuilderSet).getByName("frog").active = False
        content = builders_homepage_render()
        self.assertNotIn("Virtual build status", content)
        self.assertIn("Non-virtual build status", content)

        with admin_logged_in():
            getUtility(IBuilderSet).getByName("bob").active = False
            getUtility(IBuilderSet).getByName("frog").active = True
        content = builders_homepage_render()
        self.assertIn("Virtual build status", content)
        self.assertNotIn("Non-virtual build status", content)

        with admin_logged_in():
            getUtility(IBuilderSet).getByName("frog").active = False
        content = builders_homepage_render()
        self.assertNotIn("Virtual build status", content)
        self.assertNotIn("Non-virtual build status", content)

    def test_clean_status_duration(self):
        now = get_transaction_timestamp(IStore(Builder))
        durations = [
            timedelta(minutes=5),
            timedelta(minutes=11),
            timedelta(hours=1),
            timedelta(hours=2),
        ]
        with admin_logged_in():
            for builder in getUtility(IBuilderSet):
                builder.active = False
            builders = [
                self.factory.makeBuilder() for _ in range(len(durations))
            ]
            for builder, duration in zip(builders, durations):
                naked_builder = removeSecurityProxy(builder)
                naked_builder.clean_status = BuilderCleanStatus.CLEANING
                naked_builder.date_clean_status_changed = now - duration
        content = builders_homepage_render()
        # We don't show a duration for a builder that has only been cleaning
        # for a short time.
        expected_text = [f"{builders[0].name}\nCleaning"]
        # We show durations for builders that have been cleaning for more
        # than ten minutes.
        expected_text.extend(
            [
                "{}\nCleaning for {}".format(
                    builder.name,
                    DurationFormatterAPI(duration).approximateduration(),
                )
                for builder, duration in zip(builders[1:], durations[1:])
            ]
        )
        self.assertEqual(
            expected_text,
            [
                extract_text(row)
                for row in find_tags_by_class(content, "builder-row")
            ],
        )

    def test_build_status_duration(self):
        now = get_transaction_timestamp(IStore(Builder))
        short_duration = timedelta(hours=12)
        long_duration = timedelta(days=2)
        with admin_logged_in():
            for builder in getUtility(IBuilderSet):
                builder.active = False
            builders = [self.factory.makeBuilder(), self.factory.makeBuilder()]
            for builder, duration in zip(
                builders, [short_duration, long_duration]
            ):
                build = self.factory.makeBinaryPackageBuild()
                build.updateStatus(
                    BuildStatus.NEEDSBUILD, force_invalid_transition=True
                )
                queue = build.queueBuild()
                queue.markAsBuilding(builder)
                removeSecurityProxy(queue).date_started = now - duration
        content = builders_homepage_render()
        self.assertIn(
            "Building for {}".format(
                DurationFormatterAPI(long_duration).approximateduration()
            ),
            content,
        )
        self.assertNotIn(
            "Building for {}".format(
                DurationFormatterAPI(short_duration).approximateduration()
            ),
            content,
        )
