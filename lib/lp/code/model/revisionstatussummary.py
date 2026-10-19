# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Current revision checks and their available history."""

__all__ = ["CheckSelection", "selectCommitChecks", "selectValidationChecks"]

import logging

from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.code.enums import RevisionStatusResult
from lp.code.interfaces.revisionstatus import IRevisionStatusReportSet
from lp.code.revisionstatus import CheckSelection
from lp.services.database.bulk import load_related
from lp.services.database.interfaces import IStore
from lp.services.webapp.authorization import check_permission

log = logging.getLogger(__name__)


def _getTestArchitectures(validation, source, builds, attempts):
    """Return the architectures that should have an autopkgtest.

    Architecture-independent binaries are published to every enabled
    architecture, and each publication requests its own test, so builds
    alone do not determine the expected tests.
    """
    from lp.soyuz.interfaces.publishing import active_publishing_status
    from lp.soyuz.model.binarypackagerelease import BinaryPackageRelease
    from lp.soyuz.model.distroarchseries import DistroArchSeries
    from lp.soyuz.model.publishing import BinaryPackagePublishingHistory

    arch_ids = {build.distro_arch_series_id for build in builds}
    arch_ids.update(metadata["distro_arch_series_id"] for metadata in attempts)
    if builds:
        arch_ids.update(
            IStore(BinaryPackagePublishingHistory)
            .find(
                BinaryPackagePublishingHistory.distroarchseries_id,
                BinaryPackagePublishingHistory.archive == validation.archive,
                BinaryPackagePublishingHistory.pocket == source.pocket,
                BinaryPackagePublishingHistory.status.is_in(
                    active_publishing_status
                ),
                BinaryPackagePublishingHistory.binarypackagerelease_id
                == BinaryPackageRelease.id,
                BinaryPackageRelease.build_id.is_in(
                    [build.id for build in builds]
                ),
            )
            .config(distinct=True)
        )
    if not arch_ids:
        return []
    return sorted(
        IStore(DistroArchSeries).find(
            DistroArchSeries, DistroArchSeries.id.is_in(arch_ids)
        ),
        key=lambda arch: arch.architecturetag,
    )


def selectValidationChecks(validation, include_history=True):
    """Select by build ownership and explicit request attempt, not dates.

    The current checks do not depend on the user.  History is only for
    display, so earlier runs that the user cannot see are left out of it.
    """
    from lp.code.model.branchmergeproposaljob import (
        BranchMergeProposalJob,
        BranchMergeProposalJobType,
    )
    from lp.code.model.revisionstatus import RevisionStatusReport
    from lp.soyuz.model.binarypackagebuild import BinaryPackageBuild
    from lp.soyuz.model.mergeproposalvalidation import MergeProposalValidation

    selection = CheckSelection(validation=validation)
    previous = []
    if include_history:
        previous = [
            report
            for run in IStore(MergeProposalValidation)
            .find(
                MergeProposalValidation,
                MergeProposalValidation.branch_merge_proposal_id
                == validation.branch_merge_proposal_id,
                MergeProposalValidation.id < validation.id,
            )
            .order_by(MergeProposalValidation.id)
            if check_permission("launchpad.View", run)
            for report in getUtility(
                IRevisionStatusReportSet
            ).findByValidation(run)
            if report.title != "archive"
        ]
    reports = [
        report
        for report in getUtility(IRevisionStatusReportSet).findByValidation(
            validation
        )
        if isinstance(removeSecurityProxy(report.properties), dict)
    ]
    sources = [
        report
        for report in reports
        if report.group_name == "package-validation"
        and report.title == "source-build"
        and report.source_package_git_build is not None
    ]
    if len(sources) != 1:
        selection.issues.append(
            "The source build report is missing or ambiguous."
        )
        selection.history = previous + reports
        log.warning(
            "Validation %s has %s source reports", validation.id, len(sources)
        )
        return selection
    source_report = sources[0]
    # The build's permission also depends on its archive, and the selection
    # must not depend on whether the user can see a disabled archive.
    source = removeSecurityProxy(source_report.source_package_git_build)
    source_reports = IStore(RevisionStatusReport).find(
        RevisionStatusReport,
        source_package_git_build=source,
        group_name="package-validation",
        title="source-build",
    )
    if source_reports.count() != 1:
        selection.issues.append("The source build belongs to several reports.")
        selection.history = previous + reports
        log.warning(
            "Validation %s has ambiguous source ownership", validation.id
        )
        return selection
    selection.current.append(source_report)
    consumed = {source_report.id}
    release = source.source_package_release
    builds = (
        list(
            IStore(BinaryPackageBuild).find(
                BinaryPackageBuild,
                BinaryPackageBuild.archive == validation.archive,
                BinaryPackageBuild.source_package_release == release,
                BinaryPackageBuild.distro_series == source.distroseries,
                BinaryPackageBuild.pocket == source.pocket,
            )
        )
        if release is not None
        else []
    )
    if source_report.result == RevisionStatusResult.SUCCEEDED and not builds:
        selection.issues.append("Binary build reports are not available yet.")
    attempts = []
    for job in IStore(BranchMergeProposalJob).find(
        BranchMergeProposalJob,
        branch_merge_proposal=validation.branch_merge_proposal,
        job_type=BranchMergeProposalJobType.REQUEST_AUTOPKGTEST,
    ):
        metadata = job.metadata
        if (
            metadata.get("validation_id") == validation.id
            and metadata.get("source_package_git_build_id") == source.id
            and release is not None
            and metadata.get("source_package_release_id") == release.id
        ):
            attempts.append(dict(metadata, job_id=job.id))
    for build in sorted(
        builds, key=lambda build: build.distro_arch_series.architecturetag
    ):
        candidates = [
            report
            for report in reports
            if report.group_name == "package-validation"
            and report.source_package_git_build == source
            and report.distro_arch_series == build.distro_arch_series
            and report.properties.get("binary_build_id") == build.id
        ]
        if len(candidates) == 1:
            selection.current.extend(candidates)
            consumed.add(candidates[0].id)
        else:
            selection.issues.append(
                "Binary build report for %s is missing or ambiguous."
                % build.distro_arch_series.architecturetag
            )
    for arch in _getTestArchitectures(validation, source, builds, attempts):
        arch_attempts = [
            metadata
            for metadata in attempts
            if metadata.get("distro_arch_series_id") == arch.id
        ]
        if not arch_attempts:
            selection.issues.append(
                "Autopkgtest report for %s is not available yet."
                % arch.architecturetag
            )
            continue
        latest_attempt = max(
            metadata.get("attempt", 0) for metadata in arch_attempts
        )
        latest = [
            metadata
            for metadata in arch_attempts
            if metadata.get("attempt", 0) == latest_attempt
        ]
        candidates = []
        if len(latest) == 1:
            metadata = latest[0]
            state = metadata.get("autopkgtest") or {}
            report_id = state.get("report_id", metadata.get("report_id"))
            request_key = state.get("request", {}).get(
                "key", metadata.get("request_key")
            )
            candidates = [
                report
                for report in reports
                if report.id == report_id
                and report.source_package_git_build == source
                and report.distro_arch_series == arch
                and report.group_name == "package-validation"
                and report.properties.get("request_job_id")
                == metadata["job_id"]
                and report.properties.get("attempt") == latest_attempt
                and report.properties.get("request_key") == request_key
            ]
        if len(candidates) == 1:
            selection.current.extend(candidates)
            consumed.add(candidates[0].id)
        else:
            selection.issues.append(
                "The current autopkgtest attempt for %s is missing or "
                "ambiguous." % arch.architecturetag
            )
    selection.history = previous + [
        report
        for report in reports
        if report.id not in consumed and report.title != "archive"
    ]
    if selection.issues:
        log.info("Validation %s: %s", validation.id, selection.issues)
    return selection


def selectCommitChecks(
    repository, commit_sha1, proposal=None, include_history=True
):
    """Keep each MP's latest run separate, retaining earlier run history.

    The selection does not depend on the user, so it can be used for merge
    criteria.  Callers that display it must filter what the user can see.
    """
    from lp.code.model.branchmergeproposal import BranchMergeProposal
    from lp.code.model.cibuild import CIBuild
    from lp.soyuz.model.mergeproposalvalidation import MergeProposalValidation

    reports = list(repository.getStatusReports(commit_sha1))
    runs = list(
        IStore(MergeProposalValidation)
        .find(
            MergeProposalValidation,
            MergeProposalValidation.commit_sha1 == commit_sha1,
            MergeProposalValidation.branch_merge_proposal_id
            == BranchMergeProposal.id,
            BranchMergeProposal.source_git_repository == repository,
        )
        .order_by(MergeProposalValidation.id)
    )
    latest = {}
    for run in runs:
        if proposal is None or run.branch_merge_proposal_id == proposal.id:
            latest[run.branch_merge_proposal_id] = run
    selections = [
        selectValidationChecks(run, include_history=include_history)
        for run in latest.values()
    ]
    owned = {
        report.id
        for run in runs
        for report in getUtility(IRevisionStatusReportSet).findByValidation(
            run
        )
    }
    load_related(
        CIBuild,
        [removeSecurityProxy(report) for report in reports],
        ["ci_build_id"],
    )

    def is_commit_ci_report(report):
        build = removeSecurityProxy(report).ci_build
        return (
            build is not None
            and build.git_repository == repository
            and build.commit_sha1 == commit_sha1
        )

    # LPCI replaces all of an architecture's checks with each new build.
    # Other producers have no attempt identity, so the latest report for
    # each group and title is current.
    newest_ci_builds = {}
    newest_reports = {}
    generic_reports = []
    orphaned = []
    for report in reports:
        properties = removeSecurityProxy(report.properties)
        if report.id in owned:
            continue
        if isinstance(properties, dict) and "validation_id" in properties:
            orphaned.append(report)
            continue
        generic_reports.append(report)
        if is_commit_ci_report(report):
            build = removeSecurityProxy(report).ci_build
            newest_ci_builds[build.distro_arch_series_id] = max(
                newest_ci_builds.get(build.distro_arch_series_id, 0),
                build.id,
            )
        else:
            newest_reports[(report.group_name, report.title)] = report.id
    generic = {}
    for report in generic_reports:
        group = generic.setdefault(report.group_name, CheckSelection())
        if is_commit_ci_report(report):
            build = removeSecurityProxy(report).ci_build
            is_current = (
                build.id == newest_ci_builds[build.distro_arch_series_id]
            )
        else:
            is_current = (
                report.id == newest_reports[(report.group_name, report.title)]
            )
        if is_current:
            group.current.append(report)
        else:
            group.history.append(report)
    if orphaned:
        selections.append(
            CheckSelection(
                history=orphaned,
                issues=[
                    "Some validation reports have no verified run "
                    "association."
                ],
            )
        )
    return list(generic.values()) + selections
