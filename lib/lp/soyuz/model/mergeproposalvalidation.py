# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Validation runs for distribution source package merge proposals."""

__all__ = ["MergeProposalValidation"]

import json
from datetime import timezone

from storm.databases.postgres import JSON
from storm.expr import Cast
from storm.locals import DateTime, Int, Reference, Unicode
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.buildmaster.enums import BuildStatus
from lp.code.enums import RevisionStatusResult
from lp.code.interfaces.revisionstatus import IRevisionStatusReportSet
from lp.code.model.revisionstatus import RevisionStatusReport
from lp.services.database.constants import UTC_NOW
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.database.stormexpr import JSONContains
from lp.services.webapp.publisher import canonical_url
from lp.soyuz.enums import PackageUploadStatus
from lp.soyuz.model.queue import PackageUpload, PackageUploadSource
from lp.soyuz.model.sourcepackagerelease import SourcePackageRelease

_BUILD_RESULTS = {
    BuildStatus.NEEDSBUILD: RevisionStatusResult.WAITING,
    BuildStatus.BUILDING: RevisionStatusResult.RUNNING,
    BuildStatus.GATHERING: RevisionStatusResult.RUNNING,
    BuildStatus.UPLOADING: RevisionStatusResult.RUNNING,
    BuildStatus.CANCELLING: RevisionStatusResult.RUNNING,
    BuildStatus.FULLYBUILT: RevisionStatusResult.SUCCEEDED,
    BuildStatus.FAILEDTOBUILD: RevisionStatusResult.FAILED,
    BuildStatus.FAILEDTOUPLOAD: RevisionStatusResult.FAILED,
    BuildStatus.MANUALDEPWAIT: RevisionStatusResult.WAITING,
    BuildStatus.CHROOTWAIT: RevisionStatusResult.WAITING,
    BuildStatus.SUPERSEDED: RevisionStatusResult.SKIPPED,
    BuildStatus.CANCELLED: RevisionStatusResult.CANCELLED,
}


class MergeProposalValidation(StormBase):
    """The durable link between a proposal, commit and validation archive."""

    __storm_table__ = "MergeProposalValidation"

    id = Int(primary=True)
    branch_merge_proposal_id = Int("branch_merge_proposal", allow_none=False)
    branch_merge_proposal = Reference(
        branch_merge_proposal_id, "BranchMergeProposal.id"
    )
    commit_sha1 = Unicode(allow_none=False)
    archive_id = Int("archive", allow_none=False)
    archive = Reference(archive_id, "Archive.id")
    requester_id = Int("requester", allow_none=False)
    requester = Reference(requester_id, "Person.id")
    parameters = JSON(allow_none=False)
    status = DBEnum(enum=RevisionStatusResult, allow_none=False)
    date_created = DateTime(tzinfo=timezone.utc, allow_none=False)
    date_finished = DateTime(tzinfo=timezone.utc)

    def __init__(self, merge_proposal, commit_sha1, archive, parameters):
        super().__init__()
        self.branch_merge_proposal = merge_proposal
        self.commit_sha1 = commit_sha1
        self.archive = archive
        self.requester = merge_proposal.registrant
        self.parameters = parameters
        self.status = RevisionStatusResult.RUNNING
        self.date_created = UTC_NOW

    @classmethod
    def _getSourceBuildReport(cls, build):
        reports = IStore(RevisionStatusReport).find(
            RevisionStatusReport,
            RevisionStatusReport.git_repository == build.git_repository,
            RevisionStatusReport.commit_sha1 == build.commit_sha1,
            RevisionStatusReport.source_package_git_build == build,
            RevisionStatusReport.title == "source-build",
            RevisionStatusReport.group_name == "package-validation",
        )
        for report in reports:
            if not isinstance(report.properties, dict):
                continue
            validation_id = report.properties.get("validation_id")
            if type(validation_id) is not int:
                continue
            validation = IStore(cls).get(cls, validation_id)
            if (
                validation is not None
                and validation.archive == build.archive
                and validation.commit_sha1 == build.commit_sha1
                and validation.branch_merge_proposal.source_git_repository
                == build.git_repository
            ):
                return validation, report
        return None, None

    @staticmethod
    def _updateBuildReport(report, build, result):
        finished = report.date_finished if report.result == result else None
        report.update(
            url=canonical_url(build),
            result_summary=build.status.title,
            result=result,
        )
        report.date_started = build.date_started
        if result in (
            RevisionStatusResult.WAITING,
            RevisionStatusResult.RUNNING,
        ):
            report.date_finished = None
        else:
            report.date_finished = finished or UTC_NOW

    @classmethod
    def updateSourceBuildReport(cls, build):
        """Update a source report from persisted build and upload status."""
        validation, report = cls._getSourceBuildReport(build)
        if report is None:
            return
        result = _BUILD_RESULTS[build.status]
        if build.status == BuildStatus.FULLYBUILT:
            uploads = IStore(PackageUpload).find(
                PackageUpload,
                PackageUploadSource.packageupload_id == PackageUpload.id,
                PackageUploadSource.sourcepackagerelease_id
                == SourcePackageRelease.id,
                SourcePackageRelease.source_package_git_build == build,
                PackageUpload.archive == build.archive,
                PackageUpload.distroseries == build.distroseries,
                PackageUpload.pocket == build.pocket,
                PackageUpload.status.is_in(
                    (PackageUploadStatus.ACCEPTED, PackageUploadStatus.DONE)
                ),
            )
            if uploads.is_empty():
                result = RevisionStatusResult.RUNNING
        cls._updateBuildReport(report, build, result)

    @classmethod
    def updateBinaryBuildReport(cls, build):
        """Create or update the report for a validation binary build."""
        if not build.archive.is_ppa:
            return
        source_build = build.source_package_release.source_package_git_build
        if source_build is None or (
            build.archive != source_build.archive
            or build.distro_series != source_build.distroseries
            or build.pocket != source_build.pocket
        ):
            return
        validation, _ = cls._getSourceBuildReport(source_build)
        if validation is None:
            return
        reports = removeSecurityProxy(getUtility(IRevisionStatusReportSet))
        title = "binary-build/%s" % build.distro_arch_series.architecturetag
        report = (
            reports.findByValidation(validation)
            .find(
                RevisionStatusReport.source_package_git_build == source_build,
                RevisionStatusReport.title == title,
                RevisionStatusReport.distro_arch_series
                == build.distro_arch_series,
                JSONContains(
                    RevisionStatusReport.properties,
                    Cast(json.dumps({"binary_build_id": build.id}), "jsonb"),
                ),
            )
            .one()
        )
        if report is None:
            report = reports.new(
                creator=validation.requester,
                title=title,
                git_repository=source_build.git_repository,
                commit_sha1=source_build.commit_sha1,
                group_name="package-validation",
                source_package_git_build=source_build,
                distro_arch_series=build.distro_arch_series,
                properties={
                    "validation_id": validation.id,
                    "binary_build_id": build.id,
                },
            )
        cls._updateBuildReport(report, build, _BUILD_RESULTS[build.status])
