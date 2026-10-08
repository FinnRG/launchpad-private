# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Prepare contributor-owned archives for package merge proposals."""

__all__ = ["CreateValidationArchiveJob", "queue_validation"]

from datetime import datetime, timezone
from email.parser import HeaderParser

from storm.expr import Desc
from zope.component import getUtility
from zope.interface import implementer, provider
from zope.security.proxy import removeSecurityProxy

from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.code.enums import BranchMergeProposalStatus, RevisionStatusResult
from lp.code.interfaces.event import IBranchMergeProposalNeedsReviewEvent
from lp.code.interfaces.revisionstatus import IRevisionStatusReportSet
from lp.code.model.branchmergeproposaljob import (
    BranchMergeProposalJob,
    BranchMergeProposalJobDerived,
    BranchMergeProposalJobSource,
    BranchMergeProposalJobType,
)
from lp.registry.errors import NoSuchDistroSeries
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.series import SeriesStatus
from lp.services.config import config
from lp.services.database.interfaces import IPrimaryStore
from lp.services.features.flags import FeatureController
from lp.services.features.scopes import (
    DefaultScope,
    FixedScope,
    MultiScopeHandler,
    TeamScope,
)
from lp.services.job.model.job import Job
from lp.services.webapp import canonical_url
from lp.soyuz.enums import ArchivePurpose
from lp.soyuz.interfaces.archive import IArchiveSet
from lp.soyuz.interfaces.mergeproposalvalidation import (
    ICreateValidationArchiveJob,
    ICreateValidationArchiveJobSource,
)
from lp.soyuz.interfaces.sourcepackagegitbuild import (
    ISourcePackageGitBuildSource,
)
from lp.soyuz.model.mergeproposalvalidation import MergeProposalValidation


def _parameters(description):
    lines = []
    keys = {
        "activate-workflow",
        "autopkgtest-trigger",
        "autopkgtest-all-proposed",
    }
    for line in reversed((description or "").rstrip().splitlines()):
        if line.partition(":")[0].lower() not in keys:
            break
        lines.append(line)
    headers = HeaderParser().parsestr("\n".join(reversed(lines)))
    parameters = {key.lower(): value.strip() for key, value in headers.items()}
    for key in ("activate-workflow", "autopkgtest-all-proposed"):
        if key in parameters:
            parameters[key] = parameters[key].lower()
    parameters.setdefault("activate-workflow", "true")
    parameters.setdefault("autopkgtest-all-proposed", "false")
    return parameters


def _validation_series(merge_proposal, needs_review=False):
    if not needs_review and (
        merge_proposal.queue_status != BranchMergeProposalStatus.NEEDS_REVIEW
    ):
        return None
    repository = merge_proposal.target_git_repository
    source = merge_proposal.source_git_repository
    registrant = merge_proposal.registrant
    if (
        repository is None
        or source is None
        or not repository.target_default
        or not IDistributionSourcePackage.providedBy(repository.target)
        or merge_proposal.private
        or source.private
        or repository.private
        or not registrant.is_valid_person
        or registrant.is_team
        or merge_proposal.source_git_commit_sha1 is None
        or _parameters(merge_proposal.description)["activate-workflow"].lower()
        != "true"
    ):
        return None
    distribution = repository.target.distribution
    if not distribution.supports_ppas:
        return None
    scopes = MultiScopeHandler(
        [
            DefaultScope(),
            TeamScope(lambda: registrant),
            FixedScope("distribution:" + distribution.name),
        ]
    )
    if not FeatureController(scopes.lookup).getFlag(
        "soyuz.mp_validation.enabled"
    ):
        return None
    prefix = "refs/heads/%s/" % distribution.name
    if not merge_proposal.target_git_path.startswith(prefix):
        return None
    series_name = merge_proposal.target_git_path[len(prefix) :]
    for suffix in ("-devel", "-proposed", "-updates", "-security"):
        if series_name.endswith(suffix):
            series_name = series_name[: -len(suffix)]
            break
    if series_name == "devel":
        series = distribution.currentseries
    else:
        try:
            series = distribution.getSeries(series_name)
        except NoSuchDistroSeries:
            return None
    if (
        series is None
        or series.status == SeriesStatus.OBSOLETE
        or series.nominatedarchindep is None
    ):
        return None
    return series


def queue_validation(merge_proposal, event):
    """Start validation on creation or a request for review."""
    getUtility(ICreateValidationArchiveJobSource).create(
        merge_proposal,
        needs_review=IBranchMergeProposalNeedsReviewEvent.providedBy(event),
    )


@implementer(ICreateValidationArchiveJob)
@provider(ICreateValidationArchiveJobSource)
class CreateValidationArchiveJob(BranchMergeProposalJobDerived):
    """Create a validation PPA and queue a source package Git build."""

    class_job_type = BranchMergeProposalJobType.CREATE_VALIDATION_ARCHIVE
    config = config.ICreateValidationArchiveJobSource
    task_queue = "launchpad_job"

    @classmethod
    def iterReady(cls):
        return BranchMergeProposalJobSource.iterReady(cls.class_job_type)

    @classmethod
    def create(cls, merge_proposal, needs_review=False):
        merge_proposal = removeSecurityProxy(merge_proposal)
        series = _validation_series(merge_proposal, needs_review=needs_review)
        if series is None:
            return None
        store = IPrimaryStore(BranchMergeProposalJob)
        store.execute(
            "SELECT id FROM BranchMergeProposal WHERE id = ? FOR UPDATE",
            (merge_proposal.id,),
        )
        commit = merge_proposal.source_git_commit_sha1
        for job in store.find(
            BranchMergeProposalJob,
            branch_merge_proposal=merge_proposal,
            job_type=cls.class_job_type,
        ):
            if job.metadata["commit_sha1"] != commit:
                continue
            if (
                job.job.status in Job.PENDING_STATUSES
                or not store.find(
                    MergeProposalValidation,
                    branch_merge_proposal=merge_proposal,
                    commit_sha1=commit,
                ).is_empty()
            ):
                return cls(job)
        return cls._create(
            merge_proposal,
            {
                "commit_sha1": commit,
                "distroseries": series.id,
                "parameters": _parameters(merge_proposal.description),
                "version_suffix": "~ppa%s+git%s"
                % (
                    datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
                    commit[:7],
                ),
            },
        )

    def run(self):
        merge_proposal = self.branch_merge_proposal
        store = IPrimaryStore(MergeProposalValidation)
        store.execute(
            "SELECT id FROM BranchMergeProposal WHERE id = ? FOR UPDATE",
            (merge_proposal.id,),
        )
        store.autoreload(merge_proposal)
        series = _validation_series(merge_proposal)
        if (
            series is None
            or series.id != self.metadata["distroseries"]
            or merge_proposal.source_git_commit_sha1
            != self.metadata["commit_sha1"]
        ):
            return
        if not store.find(
            MergeProposalValidation,
            branch_merge_proposal=merge_proposal,
            commit_sha1=self.metadata["commit_sha1"],
        ).is_empty():
            return
        previous = (
            store.find(
                MergeProposalValidation, branch_merge_proposal=merge_proposal
            )
            .order_by(Desc(MergeProposalValidation.id))
            .first()
        )
        if previous is not None and (
            previous.archive.is_active
            and previous.archive.enabled
            and not previous.archive.private
        ):
            archive = previous.archive
        else:
            archives = getUtility(IArchiveSet)
            name = "mp-%s" % merge_proposal.id
            attempt = 1
            while (
                archives.getPPAOwnedByPerson(
                    merge_proposal.registrant,
                    distribution=series.distribution,
                    name=name,
                )
                is not None
            ):
                attempt += 1
                name = "mp-%s-%s" % (merge_proposal.id, attempt)
            archive = removeSecurityProxy(
                archives.new(
                    owner=merge_proposal.registrant,
                    purpose=ArchivePurpose.PPA,
                    distribution=series.distribution,
                    name=name,
                    description="Created for merge proposal %s"
                    % canonical_url(merge_proposal),
                    processors=[
                        processor
                        for processor in getUtility(IProcessorSet).getAll()
                        if processor.supports_virtualized
                    ],
                )
            )
            archive.addArchiveDependency(
                series.distribution.main_archive,
                PackagePublishingPocket.PROPOSED,
            )
        rejection = archive.checkUpload(
            merge_proposal.registrant,
            series,
            None,
            archive.default_component,
            PackagePublishingPocket.RELEASE,
        )
        if rejection is not None:
            raise rejection
        run = MergeProposalValidation(
            merge_proposal,
            self.metadata["commit_sha1"],
            archive,
            self.metadata["parameters"],
        )
        store.add(run)
        build = removeSecurityProxy(
            getUtility(ISourcePackageGitBuildSource).new(
                merge_proposal.source_git_repository,
                run.commit_sha1,
                archive,
                series,
                run.requester,
                self.metadata["version_suffix"],
            )
        )
        build.queueBuild()
        reports = getUtility(IRevisionStatusReportSet)
        common = {
            "creator": run.requester,
            "git_repository": merge_proposal.source_git_repository,
            "commit_sha1": run.commit_sha1,
            "group_name": "package-validation",
        }
        reports.new(
            title="archive",
            url=canonical_url(archive),
            result=RevisionStatusResult.SUCCEEDED,
            **common,
        )
        reports.new(
            title="source-build",
            source_package_git_build=build,
            result=RevisionStatusResult.WAITING,
            **common,
        )
