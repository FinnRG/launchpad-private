# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for source package Git build views."""

from zope.component import getUtility
from zope.publisher.interfaces import NotFound
from zope.security.proxy import removeSecurityProxy

from lp.services.webapp import canonical_url
from lp.soyuz.browser.sourcepackagegitbuild import SourcePackageGitBuildView
from lp.soyuz.interfaces.sourcepackagegitbuild import (
    ISourcePackageGitBuildSource,
)
from lp.testing import BrowserTestCase
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.publication import test_traverse


class TestSourcePackageGitBuildView(BrowserTestCase):
    layer = DatabaseFunctionalLayer

    def makeBuild(self):
        distroseries = self.factory.makeDistroSeries()
        removeSecurityProxy(distroseries).nominatedarchindep = (
            self.factory.makeDistroArchSeries(distroseries=distroseries)
        )
        archive = self.factory.makeArchive(
            distribution=distroseries.distribution
        )
        return getUtility(ISourcePackageGitBuildSource).new(
            self.factory.makeGitRepository(),
            "a" * 40,
            archive,
            distroseries,
            archive.owner,
            "~ppa1",
        )

    def test_canonical_url(self):
        build = self.makeBuild()
        self.assertEqual(
            "%s/+gitbuild/%d" % (canonical_url(build.archive), build.id),
            canonical_url(build),
        )

    def test_traverse(self):
        build = self.makeBuild()
        obj, view, _ = test_traverse(canonical_url(build))
        self.assertEqual(build, obj)
        self.assertIsInstance(view, SourcePackageGitBuildView)

    def test_traverse_from_other_archive(self):
        build = self.makeBuild()
        other_archive = self.factory.makeArchive(
            distribution=build.distribution
        )
        self.assertRaises(
            NotFound,
            test_traverse,
            "%s/+gitbuild/%d" % (canonical_url(other_archive), build.id),
        )

    def test_traverse_log_file(self):
        build = self.makeBuild()
        log = self.factory.makeLibraryFileAlias(
            filename="buildlog.txt.gz", db_only=True
        )
        removeSecurityProxy(build).setLog(log)
        _, view, _ = test_traverse(build.log_url)
        self.assertEqual(
            log.getURL(include_token=True), removeSecurityProxy(view).target
        )

    def test_index(self):
        build = self.makeBuild()
        expected = r"Build status\s+Needs building.*Commit:\s+%s" % (
            build.commit_sha1
        )
        self.assertTextMatchesExpressionIgnoreWhitespace(
            expected, self.getMainText(build)
        )
