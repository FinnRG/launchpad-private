# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for source package Git builds."""

import time
from urllib.parse import urlsplit

from fixtures import MockPatch
from testtools.matchers import StartsWith
from testtools.twistedsupport import (
    AsynchronousDeferredRunTestForBrokenTwisted,
)
from twisted.internet import defer
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.buildmaster.interactor import shut_down_default_process_pool
from lp.buildmaster.interfaces.buildfarmjobbehaviour import (
    IBuildFarmJobBehaviour,
)
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.buildmaster.tests.builderproxy import (
    InProcessProxyAuthAPIFixture,
    ProxyURLMatcher,
    RevocationEndpointMatcher,
)
from lp.buildmaster.tests.mock_workers import MockBuilder, WorkerTestHelpers
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.config import config
from lp.services.features.testing import FeatureFixture
from lp.services.log.logger import BufferLogger
from lp.services.privacy.interfaces import IObjectPrivacy
from lp.services.propertycache import get_property_cache
from lp.services.webapp.authorization import check_permission
from lp.soyuz.interfaces.sourcepackagegitbuild import (
    ISourcePackageGitBuild,
    ISourcePackageGitBuildSource,
)
from lp.testing import TestCaseWithFactory, person_logged_in, verifyObject
from lp.testing.layers import LaunchpadFunctionalLayer, ZopelessDatabaseLayer


class SourcePackageGitBuildTestMixin:
    def makeBuild(self):
        requester = self.factory.makePerson(
            email="requester@example.com",
            displayname="Example Requester",
        )
        distroseries = self.factory.makeDistroSeries(owner=requester)
        processor = getUtility(IProcessorSet).getByName("386")
        removeSecurityProxy(distroseries).nominatedarchindep = (
            distroseries.newArch("i386", processor, True, requester)
        )
        archive = self.factory.makeArchive(
            owner=requester, distribution=distroseries.distribution
        )
        repository = self.factory.makeGitRepository(owner=requester)
        build = getUtility(ISourcePackageGitBuildSource).new(
            repository,
            "a" * 40,
            archive,
            distroseries,
            requester,
            "~ppa1",
        )
        return build


class TestSourcePackageGitBuild(
    SourcePackageGitBuildTestMixin, TestCaseWithFactory
):
    layer = ZopelessDatabaseLayer

    def test_implements_interface(self):
        build = self.makeBuild()
        self.assertProvides(build, ISourcePackageGitBuild)
        verifyObject(ISourcePackageGitBuild, build)

    def test_object_privacy(self):
        build = self.makeBuild()
        self.assertFalse(IObjectPrivacy(build).is_private)
        build.archive.private = True
        self.assertTrue(IObjectPrivacy(build).is_private)

    def test_build_source_utility_is_public(self):
        source = getUtility(ISourcePackageGitBuildSource)
        self.assertFalse(IObjectPrivacy(source).is_private)

    def test_queueBuild(self):
        build = self.makeBuild()
        self.assertEqual(
            build,
            getUtility(ISourcePackageGitBuildSource).getByBuildFarmJob(
                build.build_farm_job
            ),
        )
        queue_record = build.queueBuild()
        self.assertEqual(build, queue_record.specific_build)
        self.assertEqual(build.processor, queue_record.processor)

    def test_getByBuildFarmJobs(self):
        builds = [self.makeBuild()]
        self.assertContentEqual(
            builds,
            getUtility(ISourcePackageGitBuildSource).getByBuildFarmJobs(
                [build.build_farm_job for build in builds]
            ),
        )

    def test_getByBuildFarmJobs_empty(self):
        self.assertContentEqual(
            [],
            getUtility(ISourcePackageGitBuildSource).getByBuildFarmJobs([]),
        )


class TestSourcePackageGitBuildSecurity(
    SourcePackageGitBuildTestMixin, TestCaseWithFactory
):
    layer = LaunchpadFunctionalLayer

    def test_edit_permission(self):
        build = self.makeBuild()
        buildd_admin = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).buildd_admin]
        )
        unrelated = self.factory.makePerson()

        with person_logged_in(build.git_repository.owner):
            self.assertTrue(check_permission("launchpad.Edit", build))
        with person_logged_in(buildd_admin):
            self.assertTrue(check_permission("launchpad.Edit", build))
        with person_logged_in(unrelated):
            self.assertFalse(check_permission("launchpad.Edit", build))

    def test_view_private_archive(self):
        build = self.makeBuild()
        removeSecurityProxy(build.archive).private = True
        unrelated = self.factory.makePerson()

        with person_logged_in(build.archive.owner):
            self.assertTrue(check_permission("launchpad.View", build))
        with person_logged_in(unrelated):
            self.assertFalse(check_permission("launchpad.View", build))

    def test_view_private_repository(self):
        build = self.makeBuild()
        removeSecurityProxy(build.git_repository).information_type = (
            InformationType.USERDATA
        )
        unrelated = self.factory.makePerson()

        with person_logged_in(build.git_repository.owner):
            self.assertTrue(check_permission("launchpad.View", build))
        with person_logged_in(unrelated):
            self.assertFalse(check_permission("launchpad.View", build))


class TestSourcePackageGitBuildBehaviour(
    SourcePackageGitBuildTestMixin, TestCaseWithFactory
):
    layer = ZopelessDatabaseLayer
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )

    @defer.inlineCallbacks
    def makeProxyBehaviour(self):
        self.useFixture(
            FeatureFixture({"soyuz.sourcepackagegitbuild.orig_proxy": "on"})
        )
        self.pushConfig(
            "builddmaster",
            bzr_builder_sources_list="deb http://foo %(series)s main",
        )
        proxy_api = self.useFixture(InProcessProxyAuthAPIFixture())
        yield proxy_api.start()
        self.addCleanup(shut_down_default_process_pool)
        behaviour = IBuildFarmJobBehaviour(self.makeBuild())
        builder = MockBuilder()
        builder.processor = behaviour.build.processor
        worker = self.useFixture(WorkerTestHelpers()).getClientWorker()
        behaviour.setBuilder(builder, worker)
        self.addCleanup(worker.pool.closeCachedConnections)
        return behaviour, proxy_api

    @defer.inlineCallbacks
    def test_extraBuildArgs_proxy(self):
        behaviour, proxy_api = yield self.makeProxyBehaviour()
        now = time.time()
        self.useFixture(MockPatch("time.time", return_value=now))
        args = yield behaviour.extraBuildArgs()
        self.assertThat(args["proxy_url"], ProxyURLMatcher(behaviour, now))
        self.assertThat(
            args["revocation_endpoint"],
            RevocationEndpointMatcher(behaviour, now),
        )
        self.assertFalse(args["use_fetch_service"])
        (request,) = proxy_api.tokens.requests
        self.assertEqual(b"POST", request["method"])
        self.assertEqual(
            urlsplit(
                config.builddmaster.builder_proxy_auth_api_endpoint
            ).path.encode("UTF-8"),
            request["uri"],
        )
        self.assertThat(
            request["json"]["username"],
            StartsWith(behaviour.build.build_cookie + "-"),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_proxy_unconfigured(self):
        behaviour, proxy_api = yield self.makeProxyBehaviour()
        self.pushConfig("builddmaster", builder_proxy_host=None)
        self.pushConfig("snappy", builder_proxy_host=None)
        args = yield behaviour.extraBuildArgs()
        self.assertEqual([], proxy_api.tokens.requests)
        self.assertNotIn("proxy_url", args)
        self.assertNotIn("revocation_endpoint", args)

    @defer.inlineCallbacks
    def test_extraBuildArgs_proxy_disabled(self):
        behaviour, proxy_api = yield self.makeProxyBehaviour()
        self.useFixture(FeatureFixture({}))
        args = yield behaviour.extraBuildArgs()
        self.assertEqual([], proxy_api.tokens.requests)
        self.assertNotIn("proxy_url", args)

    @defer.inlineCallbacks
    def test_extraBuildArgs(self):
        self.pushConfig(
            "builddmaster",
            bzr_builder_sources_list="deb http://foo %(series)s main",
        )
        build = self.makeBuild()
        behaviour = IBuildFarmJobBehaviour(build)
        builder = MockBuilder()
        builder.processor = build.processor
        behaviour.setBuilder(builder, None)

        args = yield behaviour.extraBuildArgs()

        self.assertEqual("sourcepackagegit", behaviour.builder_type)
        self.assertEqual(
            build.git_repository.git_https_url, args["git_repository"]
        )
        self.assertEqual(build.commit_sha1, args["git_ref"])
        self.assertEqual("~ppa1", args["version_suffix"])
        self.assertEqual("requester@example.com", args["author_email"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_no_preferred_email(self):
        self.pushConfig(
            "builddmaster",
            bzr_builder_sources_list="deb http://foo %(series)s main",
        )
        build = self.makeBuild()
        get_property_cache(build.requester).preferredemail = None
        behaviour = IBuildFarmJobBehaviour(build)
        builder = MockBuilder()
        builder.processor = build.processor
        behaviour.setBuilder(builder, None)

        args = yield behaviour.extraBuildArgs()

        self.assertEqual("Launchpad Package Builder", args["author_name"])
        self.assertEqual(
            config.canonical.noreply_from_address, args["author_email"]
        )

    def test_verifyBuildRequest(self):
        build = self.makeBuild()
        behaviour = IBuildFarmJobBehaviour(build)
        builder = MockBuilder()
        builder.processor = build.processor
        behaviour.setBuilder(builder, None)

        behaviour.verifyBuildRequest(BufferLogger())

    def test_verifyBuildRequest_non_virtual_builder(self):
        build = self.makeBuild()
        behaviour = IBuildFarmJobBehaviour(build)
        builder = MockBuilder(virtualized=False)
        builder.processor = build.processor
        behaviour.setBuilder(builder, None)

        error = self.assertRaises(
            AssertionError, behaviour.verifyBuildRequest, BufferLogger()
        )

        self.assertEqual(
            "Attempt to build virtual item on a non-virtual builder.",
            str(error),
        )

    def test_verifyBuildRequest_invalid_pocket(self):
        build = self.makeBuild()
        removeSecurityProxy(build).pocket = PackagePublishingPocket.PROPOSED
        behaviour = IBuildFarmJobBehaviour(build)
        builder = MockBuilder()
        builder.processor = build.processor
        behaviour.setBuilder(builder, None)

        self.assertRaises(
            AssertionError, behaviour.verifyBuildRequest, BufferLogger()
        )
