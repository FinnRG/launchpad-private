# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for merge proposal validation runs."""

__all__ = []

import transaction
from fixtures import MockPatchObject
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.buildmaster.enums import BuildStatus
from lp.code.enums import RevisionStatusResult
from lp.code.interfaces.revisionstatus import IRevisionStatusReportSet
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.database.interfaces import IStore
from lp.services.webapp.publisher import canonical_url
from lp.soyuz.enums import PackageUploadStatus
from lp.soyuz.interfaces.binarypackagebuild import IBinaryPackageBuildSet
from lp.soyuz.interfaces.sourcepackagegitbuild import (
    ISourcePackageGitBuildSource,
)
from lp.soyuz.model.mergeproposalvalidation import MergeProposalValidation
from lp.soyuz.model.queue import PackageUpload
from lp.soyuz.model.sourcepackagegitbuild import SourcePackageGitBuild
from lp.soyuz.tests.test_sourcepackagegitbuild import (
    SourcePackageGitBuildTestMixin,
)
from lp.testing import TestCaseWithFactory, person_logged_in
from lp.testing.dbuser import dbuser
from lp.testing.layers import DatabaseFunctionalLayer


class TestMergeProposalValidation(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.proposal = self.factory.makeBranchMergeProposalForGit()
        self.archive = self.factory.makeArchive(owner=self.proposal.registrant)

    def test_persistence(self):
        parameters = {"autopkgtest-all-proposed": "false"}
        run = MergeProposalValidation(
            self.proposal,
            self.proposal.source_git_commit_sha1,
            self.archive,
            parameters,
        )
        store = IStore(MergeProposalValidation)
        store.add(run)
        store.flush()
        store.autoreload(run)
        self.assertEqual(self.proposal, run.branch_merge_proposal)
        self.assertEqual(self.proposal.source_git_commit_sha1, run.commit_sha1)
        self.assertEqual(self.archive, run.archive)
        self.assertEqual(self.proposal.registrant, run.requester)
        self.assertEqual(parameters, run.parameters)
        self.assertEqual(RevisionStatusResult.RUNNING, run.status)
        self.assertIsNotNone(run.date_created)
        self.assertIsNone(run.date_finished)

    def test_preserves_previous_commit(self):
        store = IStore(MergeProposalValidation)
        for commit_sha1 in ("a" * 40, "b" * 40):
            store.add(
                MergeProposalValidation(
                    self.proposal, commit_sha1, self.archive, {}
                )
            )
        runs = store.find(
            MergeProposalValidation, branch_merge_proposal=self.proposal
        ).order_by(MergeProposalValidation.id)
        self.assertEqual(
            ["a" * 40, "b" * 40], [run.commit_sha1 for run in runs]
        )
        self.assertEqual([self.archive] * 2, [run.archive for run in runs])


class ValidationReportTestMixin:
    def makeValidation(self, build):
        [source_ref] = self.factory.makeGitRefs(
            repository=build.git_repository, sha1=build.commit_sha1
        )
        proposal = self.factory.makeBranchMergeProposalForGit(
            source_ref=source_ref
        )
        run = MergeProposalValidation(
            proposal, build.commit_sha1, build.archive, {}
        )
        IStore(run).add(run)
        IStore(run).flush()
        report = getUtility(IRevisionStatusReportSet).new(
            creator=build.requester,
            title="source-build",
            git_repository=build.git_repository,
            commit_sha1=build.commit_sha1,
            group_name="package-validation",
            source_package_git_build=build,
            result=RevisionStatusResult.WAITING,
            properties={"validation_id": run.id},
        )
        MergeProposalValidation.updateSourceBuildReport(build)
        return run, report


class TestValidationBuildReports(
    ValidationReportTestMixin,
    SourcePackageGitBuildTestMixin,
    TestCaseWithFactory,
):
    layer = DatabaseFunctionalLayer

    def test_source_build_transitions(self):
        build = self.makeBuild()
        run, report = self.makeValidation(build)
        self.assertEqual(RevisionStatusResult.WAITING, report.result)
        self.assertEqual(canonical_url(build), report.url)
        self.assertIsNone(report.date_finished)
        for status, result in (
            (BuildStatus.BUILDING, RevisionStatusResult.RUNNING),
            (BuildStatus.GATHERING, RevisionStatusResult.RUNNING),
            (BuildStatus.UPLOADING, RevisionStatusResult.RUNNING),
            (BuildStatus.FULLYBUILT, RevisionStatusResult.RUNNING),
            (BuildStatus.FAILEDTOUPLOAD, RevisionStatusResult.FAILED),
            (BuildStatus.NEEDSBUILD, RevisionStatusResult.WAITING),
            (BuildStatus.FAILEDTOBUILD, RevisionStatusResult.FAILED),
            (BuildStatus.MANUALDEPWAIT, RevisionStatusResult.WAITING),
            (BuildStatus.CHROOTWAIT, RevisionStatusResult.WAITING),
            (BuildStatus.CANCELLING, RevisionStatusResult.RUNNING),
            (BuildStatus.CANCELLED, RevisionStatusResult.CANCELLED),
            (BuildStatus.SUPERSEDED, RevisionStatusResult.SKIPPED),
        ):
            build.updateStatus(status, force_invalid_transition=True)
            self.assertEqual(result, report.result)
            self.assertEqual(status.title, report.result_summary)
            self.assertEqual(build.date_started, report.date_started)
            if result in (
                RevisionStatusResult.WAITING,
                RevisionStatusResult.RUNNING,
            ):
                self.assertIsNone(report.date_finished)
            else:
                self.assertIsNotNone(report.date_finished)

    def test_source_with_foreign_run_is_not_updated(self):
        build = self.makeBuild()
        run, report = self.makeValidation(build)
        run.archive = self.factory.makeArchive()
        build.updateStatus(BuildStatus.BUILDING)
        self.assertEqual(RevisionStatusResult.WAITING, report.result)

    def test_source_report_preserves_upload_acceptance(self):
        build = self.makeBuild()
        run, report = self.makeValidation(build)
        release = self.makeSourceRelease(build)
        upload = self.makeSourceUpload(build, release)
        build.updateStatus(BuildStatus.FULLYBUILT)
        for change_status in (
            None,
            upload.setUnapproved,
            upload.setRejected,
        ):
            if change_status is not None:
                change_status()
            MergeProposalValidation.updateSourceBuildReport(build)
            self.assertEqual(RevisionStatusResult.RUNNING, report.result)
            self.assertIsNone(report.date_finished)
        upload.setAccepted()
        self.assertEqual(RevisionStatusResult.SUCCEEDED, report.result)
        finished = report.date_finished
        self.assertIsNotNone(finished)
        for change_status in (None, upload.setDone):
            if change_status is not None:
                change_status()
            MergeProposalValidation.updateSourceBuildReport(build)
            self.assertEqual(RevisionStatusResult.SUCCEEDED, report.result)
            self.assertEqual(finished, report.date_finished)
        with person_logged_in(build.requester):
            report.update(result=RevisionStatusResult.WAITING)
        MergeProposalValidation.updateSourceBuildReport(build)
        self.assertEqual(RevisionStatusResult.SUCCEEDED, report.result)

    def test_source_report_ignores_foreign_uploads(self):
        build = self.makeBuild()
        run, report = self.makeValidation(build)
        release = self.makeSourceRelease(build)
        build.updateStatus(BuildStatus.FULLYBUILT)
        for overrides in (
            {
                "archive": self.factory.makeArchive(
                    distribution=build.distribution
                )
            },
            {
                "distroseries": self.factory.makeDistroSeries(
                    distribution=build.distribution
                )
            },
            {"pocket": PackagePublishingPocket.PROPOSED},
            {
                "source": self.factory.makeSourcePackageRelease(
                    archive=build.archive,
                    distroseries=build.distroseries,
                )
            },
        ):
            fields = dict(overrides)
            source = fields.pop("source", release)
            self.makeSourceUpload(
                build, source, status=PackageUploadStatus.DONE, **fields
            )
            MergeProposalValidation.updateSourceBuildReport(build)
            self.assertEqual(RevisionStatusResult.RUNNING, report.result)
            self.assertIsNone(report.date_finished)

    def test_source_build_retry_resets_existing_report(self):
        build = self.makeBuild()
        run, report = self.makeValidation(build)
        build.updateStatus(BuildStatus.BUILDING)
        build.updateStatus(BuildStatus.FAILEDTOBUILD)
        self.assertIsNotNone(report.date_started)
        self.assertIsNotNone(report.date_finished)
        self.useFixture(
            MockPatchObject(SourcePackageGitBuild, "can_be_retried", True)
        )
        with person_logged_in(build.archive.owner):
            build.retry()
        self.assertEqual(BuildStatus.NEEDSBUILD, build.status)
        self.assertIsNotNone(build.buildqueue_record)
        self.assertEqual(RevisionStatusResult.WAITING, report.result)
        self.assertIsNone(report.date_started)
        self.assertIsNone(report.date_finished)
        self.assertEqual(
            [report],
            list(getUtility(IRevisionStatusReportSet).findByValidation(run)),
        )

    def makeSourceRelease(self, source):
        release = self.factory.makeSourcePackageRelease(
            distroseries=source.distroseries, archive=source.archive
        )
        removeSecurityProxy(release).source_package_git_build = source
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=source.distroseries,
            archive=source.archive,
            sourcepackagerelease=release,
            pocket=source.pocket,
        )
        return release

    def makeBinaryBuild(self, source):
        return self.factory.makeBinaryPackageBuild(
            source_package_release=self.makeSourceRelease(source),
            distroarchseries=source.distroseries.nominatedarchindep,
            archive=source.archive,
        )

    def makeSourceUpload(self, build, release, **overrides):
        fields = dict(
            archive=build.archive,
            distroseries=build.distroseries,
            pocket=build.pocket,
        )
        fields.update(overrides)
        upload = PackageUpload(**fields)
        IStore(upload).add(upload)
        upload.addSource(release)
        return upload

    def test_binary_build_transitions_and_retry(self):
        source = self.makeBuild()
        run, source_report = self.makeValidation(source)
        build = self.makeBinaryBuild(source)
        reports = getUtility(IRevisionStatusReportSet)
        [report] = [
            report
            for report in reports.findByValidation(run)
            if report.title == "binary-build/i386"
        ]
        self.assertEqual(RevisionStatusResult.WAITING, report.result)
        self.assertEqual(canonical_url(build), report.url)
        self.assertEqual(build.distro_arch_series, report.distro_arch_series)
        self.assertEqual(source, report.source_package_git_build)
        self.assertEqual(
            {"validation_id": run.id, "binary_build_id": build.id},
            report.properties,
        )
        for status, result in (
            (BuildStatus.BUILDING, RevisionStatusResult.RUNNING),
            (BuildStatus.GATHERING, RevisionStatusResult.RUNNING),
            (BuildStatus.UPLOADING, RevisionStatusResult.RUNNING),
            (BuildStatus.FULLYBUILT, RevisionStatusResult.SUCCEEDED),
            (BuildStatus.FAILEDTOBUILD, RevisionStatusResult.FAILED),
            (BuildStatus.FAILEDTOUPLOAD, RevisionStatusResult.FAILED),
            (BuildStatus.MANUALDEPWAIT, RevisionStatusResult.WAITING),
            (BuildStatus.CHROOTWAIT, RevisionStatusResult.WAITING),
            (BuildStatus.CANCELLING, RevisionStatusResult.RUNNING),
            (BuildStatus.CANCELLED, RevisionStatusResult.CANCELLED),
            (BuildStatus.SUPERSEDED, RevisionStatusResult.SKIPPED),
        ):
            build.updateStatus(status, force_invalid_transition=True)
            self.assertEqual(result, report.result)
            self.assertEqual(build.date_started, report.date_started)
        with person_logged_in(source.archive.owner):
            build.retry()
        self.assertEqual(RevisionStatusResult.WAITING, report.result)
        self.assertIsNone(report.date_started)
        self.assertIsNone(report.date_finished)
        self.assertEqual(2, len(list(reports.findByValidation(run))))

    def test_copied_binary_builds_are_not_reported(self):
        # Copying a validated source creates builds as users that cannot
        # write validation reports; such builds must not touch them.
        source = self.makeBuild()
        run, source_report = self.makeValidation(source)
        release = self.makeSourceRelease(source)
        distro_arch_series = source.distroseries.nominatedarchindep
        targets = [
            (
                "copy_packages",
                self.factory.makeArchive(distribution=source.distribution),
            ),
            ("initializedistroseries", source.distribution.main_archive),
        ]
        transaction.commit()
        for user, archive in targets:
            with dbuser(user):
                getUtility(IBinaryPackageBuildSet).new(
                    release,
                    archive,
                    distro_arch_series,
                    PackagePublishingPocket.RELEASE,
                )
        self.assertEqual(
            [source_report],
            list(getUtility(IRevisionStatusReportSet).findByValidation(run)),
        )

    def test_binary_build_without_validation_is_not_reported(self):
        source = self.makeBuild()
        build = self.makeBinaryBuild(source)
        build.updateStatus(BuildStatus.BUILDING)
        self.assertEqual(
            [],
            list(
                getUtility(IRevisionStatusReportSet).findByCommit(
                    source.git_repository, source.commit_sha1
                )
            ),
        )

    def test_buildd_manager_updates_reports(self):
        source = self.makeBuild()
        run, source_report = self.makeValidation(source)
        build = self.makeBinaryBuild(source)
        self.makeSourceUpload(
            source,
            build.source_package_release,
            status=PackageUploadStatus.DONE,
        )
        source_id, build_id = source.id, build.id
        transaction.commit()
        with dbuser("buildd_manager"):
            source = getUtility(ISourcePackageGitBuildSource).getByID(
                source_id
            )
            build = getUtility(IBinaryPackageBuildSet).getByID(build_id)
            source.updateStatus(BuildStatus.BUILDING)
            build.updateStatus(BuildStatus.BUILDING)
            IStore(source).flush()
            self.assertEqual(
                [RevisionStatusResult.RUNNING] * 2,
                [
                    report.result
                    for report in getUtility(
                        IRevisionStatusReportSet
                    ).findByValidation(run)
                ],
            )
            source.updateStatus(BuildStatus.FULLYBUILT)
            IStore(source).flush()
            self.assertEqual(
                RevisionStatusResult.SUCCEEDED, source_report.result
            )
