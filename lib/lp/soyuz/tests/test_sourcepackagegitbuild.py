# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for source package Git builds."""

from zope.component import getUtility

from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.services.privacy.interfaces import IObjectPrivacy
from lp.soyuz.interfaces.sourcepackagegitbuild import (
    ISourcePackageGitBuild,
    ISourcePackageGitBuildSource,
)
from lp.testing import TestCaseWithFactory, verifyObject
from lp.testing.layers import ZopelessDatabaseLayer


class SourcePackageGitBuildTestMixin:
    def makeBuild(self):
        requester = self.factory.makePerson(
            email="requester@example.com",
            displayname="Example Requester",
        )
        distroseries = self.factory.makeDistroSeries(owner=requester)
        processor = getUtility(IProcessorSet).getByName("386")
        distroseries.nominatedarchindep = distroseries.newArch(
            "i386", processor, True, requester
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
