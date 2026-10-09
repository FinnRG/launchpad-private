# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for requesting source package Git builds."""

from fixtures import MockPatchObject
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.code.errors import (
    NoSuchGitReference,
    SourcePackageGitBuildRequestError,
)
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.series import SeriesStatus
from lp.services.webapp.interfaces import OAuthPermission
from lp.soyuz.enums import ArchivePurpose
from lp.soyuz.interfaces.archive import CannotUploadToPPA, InvalidPocketForPPA
from lp.testing import TestCaseWithFactory, api_url, person_logged_in
from lp.testing.layers import DatabaseFunctionalLayer, ZopelessDatabaseLayer
from lp.testing.pages import webservice_for_person


class TestBuildSourcePackageFromGitRef(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()
        requester = self.factory.makePerson()
        distroseries = self.factory.makeDistroSeries(owner=requester)
        distroseries.nominatedarchindep = distroseries.newArch(
            "i386",
            getUtility(IProcessorSet).getByName("386"),
            True,
            requester,
        )
        archive = self.factory.makeArchive(
            owner=requester, distribution=distroseries.distribution
        )
        [ref] = self.factory.makeGitRefs(
            owner=requester, paths=["refs/heads/packaging"]
        )
        self.requester = requester
        self.distroseries = distroseries
        self.archive = archive
        self.ref = ref

    def test_resolves_ref_and_queues_build(self):
        build = self.ref.repository.buildSourcePackageFromGitRef(
            "packaging", self.archive, self.requester, version_suffix="~ppa1"
        )

        self.assertEqual(self.ref.commit_sha1, build.commit_sha1)
        self.assertEqual(self.archive, build.archive)
        self.assertEqual(self.distroseries, build.distroseries)
        self.assertEqual("~ppa1", build.version_suffix)
        self.assertEqual(PackagePublishingPocket.RELEASE, build.pocket)
        self.assertIsNotNone(build.buildqueue_record)

    def test_version_suffix_defaults_to_empty(self):
        build = self.ref.repository.buildSourcePackageFromGitRef(
            "packaging", self.archive, user=self.requester
        )

        self.assertEqual("", build.version_suffix)
        self.assertIsNotNone(build.buildqueue_record)

    def test_accepts_commit_without_stored_ref(self):
        commit_sha1 = "a" * 40
        build = self.ref.repository.buildSourcePackageFromGitRef(
            commit_sha1, self.archive, self.requester
        )

        self.assertEqual(commit_sha1, build.commit_sha1)
        self.assertIsNotNone(build.buildqueue_record)

    def test_unknown_ref_rejected(self):
        self.assertRaises(
            NoSuchGitReference,
            self.ref.repository.buildSourcePackageFromGitRef,
            "missing",
            self.archive,
            self.requester,
        )

    def test_series_from_other_distribution_rejected(self):
        other_series = self.factory.makeDistroSeries()
        self.assertRaises(
            SourcePackageGitBuildRequestError,
            self.ref.repository.buildSourcePackageFromGitRef,
            "packaging",
            self.archive,
            self.requester,
            version_suffix="~ppa1",
            distroseries=other_series,
        )

    def test_private_repository_rejected(self):
        [ref] = self.factory.makeGitRefs(
            owner=self.requester,
            information_type=InformationType.USERDATA,
            paths=["refs/heads/packaging"],
        )
        self.assertRaises(
            SourcePackageGitBuildRequestError,
            ref.repository.buildSourcePackageFromGitRef,
            "packaging",
            self.archive,
            self.requester,
            version_suffix="~ppa1",
        )

    def test_non_ppa_archive_rejected(self):
        archive = self.factory.makeArchive(
            owner=self.requester,
            distribution=self.distroseries.distribution,
            purpose=ArchivePurpose.COPY,
        )
        self.assertRaises(
            SourcePackageGitBuildRequestError,
            self.ref.repository.buildSourcePackageFromGitRef,
            "packaging",
            archive,
            self.requester,
            version_suffix="~ppa1",
        )

    def test_requester_without_upload_permission_rejected(self):
        self.assertRaises(
            CannotUploadToPPA,
            self.ref.repository.buildSourcePackageFromGitRef,
            "packaging",
            self.archive,
            self.factory.makePerson(),
            version_suffix="~ppa1",
        )

    def test_pocket_passed_to_permission_check_and_build(self):
        archive = removeSecurityProxy(self.archive)
        check_upload = self.useFixture(
            MockPatchObject(
                archive,
                "checkUpload",
                return_value=None,
            )
        ).mock
        build = self.ref.repository.buildSourcePackageFromGitRef(
            "packaging",
            archive,
            self.requester,
            version_suffix="~ppa1",
            distroseries=self.distroseries,
            pocket=PackagePublishingPocket.PROPOSED,
        )
        check_upload.assert_called_once_with(
            self.requester,
            self.distroseries,
            None,
            self.archive.default_component,
            PackagePublishingPocket.PROPOSED,
        )
        self.assertEqual(PackagePublishingPocket.PROPOSED, build.pocket)

    def test_non_release_pocket_rejected_for_ppa(self):
        self.assertRaises(
            InvalidPocketForPPA,
            self.ref.repository.buildSourcePackageFromGitRef,
            "packaging",
            self.archive,
            self.requester,
            version_suffix="~ppa1",
            pocket=PackagePublishingPocket.PROPOSED,
        )


class TestBuildSourcePackageFromGitRefWebservice(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.requester = self.factory.makePerson()
        self.distroseries = self.factory.makeDistroSeries(owner=self.requester)
        removeSecurityProxy(self.distroseries).nominatedarchindep = (
            self.distroseries.newArch(
                "i386",
                getUtility(IProcessorSet).getByName("386"),
                True,
                self.requester,
            )
        )
        self.archive = self.factory.makeArchive(
            owner=self.requester,
            distribution=self.distroseries.distribution,
        )
        [self.ref] = self.factory.makeGitRefs(
            owner=self.requester, paths=["refs/heads/packaging"]
        )
        self.repository_url = api_url(self.ref.repository)
        self.archive_url = api_url(self.archive)
        self.commit_sha1 = self.ref.commit_sha1
        self.other_repository_url = api_url(self.factory.makeGitRepository())
        self.distroseries_url = api_url(self.distroseries)
        removeSecurityProxy(self.distroseries).status = SeriesStatus.SUPPORTED
        current_series = self.factory.makeDistroSeries(
            distribution=self.distroseries.distribution,
            status=SeriesStatus.DEVELOPMENT,
        )
        removeSecurityProxy(current_series).nominatedarchindep = (
            current_series.newArch(
                "i386",
                getUtility(IProcessorSet).getByName("386"),
                True,
                self.requester,
            )
        )
        self.currentseries_url = api_url(current_series)
        self.webservice = webservice_for_person(
            self.requester,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )

    def test_request_and_get_build(self):
        response = self.webservice.named_post(
            self.repository_url,
            "buildSourcePackageFromGitRef",
            git_ref="packaging",
            target_ppa=self.archive_url,
            version_suffix="~ppa1",
        )
        self.assertEqual(200, response.status, response.body)
        build = response.jsonBody()
        self.assertEqual(self.commit_sha1, build["commit_sha1"])
        self.assertEqual("~ppa1", build["version_suffix"])
        self.assertEndsWith(
            build["distro_series_link"], self.currentseries_url
        )
        self.assertEqual(200, self.webservice.get(build["self_link"]).status)

    def test_version_suffix_defaults_to_empty(self):
        response = self.webservice.named_post(
            self.repository_url,
            "buildSourcePackageFromGitRef",
            git_ref="packaging",
            target_ppa=self.archive_url,
        )

        self.assertEqual(200, response.status, response.body)
        self.assertEqual("", response.jsonBody()["version_suffix"])

    def test_request_explicit_older_series(self):
        response = self.webservice.named_post(
            self.repository_url,
            "buildSourcePackageFromGitRef",
            git_ref="packaging",
            target_ppa=self.archive_url,
            version_suffix="~ppa1",
            distroseries=self.distroseries_url,
            pocket=PackagePublishingPocket.RELEASE.title,
        )
        self.assertEqual(200, response.status, response.body)
        self.assertEndsWith(
            response.jsonBody()["distro_series_link"], self.distroseries_url
        )

    def test_non_ppa_archive_rejected_bad_request(self):
        with person_logged_in(self.requester):
            archive = self.factory.makeArchive(
                owner=self.requester,
                distribution=self.distroseries.distribution,
                purpose=ArchivePurpose.COPY,
            )
            archive_url = api_url(archive)

        response = self.webservice.named_post(
            self.repository_url,
            "buildSourcePackageFromGitRef",
            git_ref="packaging",
            target_ppa=archive_url,
            version_suffix="~ppa1",
        )

        self.assertEqual(400, response.status, response.body)

    def test_private_repository_rejected_bad_request(self):
        with person_logged_in(self.requester):
            [ref] = self.factory.makeGitRefs(
                owner=self.requester,
                information_type=InformationType.USERDATA,
                paths=["refs/heads/packaging"],
            )
            repository_url = api_url(ref.repository)
        webservice = webservice_for_person(
            self.requester,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )

        response = webservice.named_post(
            repository_url,
            "buildSourcePackageFromGitRef",
            git_ref="packaging",
            target_ppa=self.archive_url,
            version_suffix="~ppa1",
        )

        self.assertEqual(400, response.status, response.body)

    def test_series_from_other_distribution_rejected_bad_request(self):
        with person_logged_in(self.requester):
            other_series_url = api_url(self.factory.makeDistroSeries())

        response = self.webservice.named_post(
            self.repository_url,
            "buildSourcePackageFromGitRef",
            git_ref="packaging",
            target_ppa=self.archive_url,
            version_suffix="~ppa1",
            distroseries=other_series_url,
        )

        self.assertEqual(400, response.status, response.body)

    def test_repository_edit_permission_required(self):
        response = self.webservice.named_post(
            self.other_repository_url,
            "buildSourcePackageFromGitRef",
            git_ref="a" * 40,
            target_ppa=self.archive_url,
            version_suffix="~ppa1",
        )
        self.assertEqual(401, response.status, response.body)
