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
            # Deactivate pre-existing sample data builders so only our
            # test builders (which default to active=True) are rendered.
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
        short_text = "(for {})".format(
            DurationFormatterAPI(short_duration).approximateduration()
        )
        long_text = "(for {})".format(
            DurationFormatterAPI(long_duration).approximateduration()
        )
        # Both builds show their duration.
        self.assertIn(short_text, content)
        self.assertIn(long_text, content)
        # The long build gets the warning style.
        self.assertIn(
            'style="color: darkorange; font-weight: bold">{}'.format(
                long_text
            ),
            content,
        )
        # The short build renders without any style attribute.
        self.assertIn(
            f"<span>{short_text}</span>",
            content,
        )

    def test_build_status_duration_slow_processor_not_highlighted(self):
        # Builds on known slow/emulated processors (e.g. riscv64) are not
        # highlighted until they exceed the higher threshold (7 days).
        now = get_transaction_timestamp(IStore(Builder))
        riscv64 = self.factory.makeProcessor(
            name="riscv64", title="RISC-V 64bit", description="RISC-V 64bit"
        )
        under_threshold = timedelta(days=3)
        over_threshold = timedelta(days=8)
        with admin_logged_in():
            # Deactivate pre-existing sample data builders so only our
            # test builders (which default to active=True) are rendered.
            for builder in getUtility(IBuilderSet):
                builder.active = False
            builders = [
                self.factory.makeBuilder(processors=[riscv64]),
                self.factory.makeBuilder(processors=[riscv64]),
            ]
            for builder, duration in zip(
                builders, [under_threshold, over_threshold]
            ):
                build = self.factory.makeBinaryPackageBuild()
                build.updateStatus(
                    BuildStatus.NEEDSBUILD, force_invalid_transition=True
                )
                queue = build.queueBuild()
                queue.markAsBuilding(builder)
                removeSecurityProxy(queue).date_started = now - duration
        content = builders_homepage_render()
        under_text = "(for {})".format(
            DurationFormatterAPI(under_threshold).approximateduration()
        )
        over_text = "(for {})".format(
            DurationFormatterAPI(over_threshold).approximateduration()
        )
        # Both builds show their duration.
        self.assertIn(under_text, content)
        self.assertIn(over_text, content)
        # Only the build exceeding the riscv64 threshold is highlighted.
        self.assertIn(
            'style="color: darkorange; font-weight: bold">{}'.format(
                over_text
            ),
            content,
        )
        # The under-threshold build renders without any style attribute.
        self.assertIn(
            f"<span>{under_text}</span>",
            content,
        )
