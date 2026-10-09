# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for creation of merge proposal validation archives."""

__all__ = []

import transaction
from lazr.lifecycle.event import ObjectCreatedEvent
from zope.component import getUtility
from zope.event import notify
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.code.enums import BranchMergeProposalStatus, GitObjectType
from lp.code.interfaces.revisionstatus import IRevisionStatusReportSet
from lp.code.model.branchmergeproposaljob import (
    BranchMergeProposalJob,
    BranchMergeProposalJobSource,
)
from lp.code.tests.helpers import GitHostingFixture
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.database.interfaces import IStore
from lp.services.features.testing import FeatureFixture
from lp.services.identity.interfaces.account import AccountStatus
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.runner import JobRunner
from lp.services.job.tests import block_on_job
from lp.soyuz.enums import ArchivePurpose, ArchiveStatus
from lp.soyuz.model.mergeproposalvalidation import MergeProposalValidation
from lp.soyuz.model.mergeproposalvalidationjob import (
    CreateValidationArchiveJob,
)
from lp.testing import TestCaseWithFactory
from lp.testing.dbuser import dbuser
from lp.testing.layers import CeleryJobLayer, DatabaseFunctionalLayer


class ValidationArchiveJobTestMixin:
    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({"soyuz.mp_validation.enabled": "on"}))
        self.registrant = self.factory.makePerson()
        self.series = self.factory.makeDistroSeries(name="noble")
        distribution = self.series.distribution
        removeSecurityProxy(distribution).supports_ppas = True
        self.factory.makeArchive(
            distribution=distribution, purpose=ArchivePurpose.PRIMARY
        )
        processor = getUtility(IProcessorSet).getByName("386")
        removeSecurityProxy(self.series).nominatedarchindep = (
            self.series.newArch("i386", processor, True, self.registrant)
        )
        self.target = self.factory.makeDistributionSourcePackage(
            distribution=distribution
        )
        [self.target_ref] = self.factory.makeGitRefs(
            target=self.target,
            paths=["refs/heads/%s/noble-devel" % distribution.name],
        )
        removeSecurityProxy(self.target_ref.repository).target_default = True
        [self.source_ref] = self.factory.makeGitRefs(
            target=self.target, owner=self.registrant
        )

    def makeProposal(self, **kwargs):
        return removeSecurityProxy(
            self.factory.makeBranchMergeProposalForGit(
                source_ref=self.source_ref,
                target_ref=self.target_ref,
                registrant=self.registrant,
                set_state=BranchMergeProposalStatus.NEEDS_REVIEW,
                **kwargs,
            )
        )

    def getJobs(self, proposal):
        return list(
            IStore(BranchMergeProposalJob).find(
                BranchMergeProposalJob,
                branch_merge_proposal=proposal,
                job_type=CreateValidationArchiveJob.class_job_type,
            )
        )


class TestCreateValidationArchiveJob(
    ValidationArchiveJobTestMixin, TestCaseWithFactory
):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(GitHostingFixture())

    def updateRef(self, git_ref, commit_sha1):
        git_ref.repository.createOrUpdateRefs(
            {
                git_ref.path: {
                    "sha1": commit_sha1,
                    "type": GitObjectType.COMMIT,
                }
            }
        )

    def test_needs_review_queues_job(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        self.assertEqual(
            proposal.source_git_commit_sha1, job.metadata["commit_sha1"]
        )

    def test_all_proposed_defaults_to_false(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        self.assertEqual(
            "false", job.metadata["parameters"]["autopkgtest-all-proposed"]
        )

    def test_all_proposed_explicit_opt_in(self):
        proposal = self.makeProposal(
            description="A change.\n\nAutopkgtest-All-Proposed: True"
        )
        [job] = self.getJobs(proposal)
        self.assertEqual(
            "True", job.metadata["parameters"]["autopkgtest-all-proposed"]
        )

    def test_run_creates_archive_and_queues_source_build(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        CreateValidationArchiveJob(job).run()
        run = (
            IStore(MergeProposalValidation)
            .find(MergeProposalValidation, branch_merge_proposal=proposal)
            .one()
        )
        self.assertEqual(self.registrant, run.archive.owner)
        self.assertEqual("false", run.parameters["autopkgtest-all-proposed"])
        self.assertEqual("mp-%s" % proposal.id, run.archive.name)
        self.assertFalse(run.archive.private)
        self.assertEqual(8192, run.archive.authorized_size)
        [dependency] = list(run.archive.dependencies)
        self.assertEqual(PackagePublishingPocket.PROPOSED, dependency.pocket)
        reports = list(
            getUtility(IRevisionStatusReportSet).findByCommit(
                proposal.source_git_repository, proposal.source_git_commit_sha1
            )
        )
        self.assertEqual(
            ["package-validation"] * 2, [r.group_name for r in reports]
        )
        build = reports[1].source_package_git_build
        self.assertEqual(run.archive, build.archive)
        self.assertEqual(self.series, build.distroseries)
        self.assertEqual(proposal.source_git_commit_sha1, build.commit_sha1)
        self.assertIsNotNone(build.buildqueue_record)

    def test_disabled_does_not_queue(self):
        with FeatureFixture({}):
            self.assertEqual([], self.getJobs(self.makeProposal()))

    def test_opt_out_does_not_queue(self):
        self.assertEqual(
            [],
            self.getJobs(
                self.makeProposal(
                    description="A change.\n\nActivate-Workflow: False"
                )
            ),
        )

    def test_non_default_repository_does_not_queue(self):
        removeSecurityProxy(self.target_ref.repository).target_default = False
        self.assertEqual([], self.getJobs(self.makeProposal()))

    def test_repeated_events_and_runs_are_idempotent(self):
        proposal = self.makeProposal()
        job = CreateValidationArchiveJob.create(proposal)
        self.assertEqual(1, len(self.getJobs(proposal)))
        job.run()
        job.run()
        self.assertEqual(
            1,
            IStore(MergeProposalValidation)
            .find(MergeProposalValidation, branch_merge_proposal=proposal)
            .count(),
        )

    def test_review_again_after_job_completed_without_validation(self):
        proposal = self.makeProposal()
        commit_sha1 = proposal.source_git_commit_sha1
        [original] = self.getJobs(proposal)
        proposal.setAsWorkInProgress()
        JobRunner([CreateValidationArchiveJob(original)]).runAll()
        self.assertEqual(JobStatus.COMPLETED, original.job.status)
        self.assertTrue(
            IStore(MergeProposalValidation)
            .find(MergeProposalValidation, branch_merge_proposal=proposal)
            .is_empty()
        )
        proposal.requestReview()
        jobs = self.getJobs(proposal)
        self.assertEqual(2, len(jobs))
        [replacement] = [job for job in jobs if job.id != original.id]
        self.assertEqual(JobStatus.WAITING, replacement.job.status)
        self.assertEqual(commit_sha1, replacement.metadata["commit_sha1"])
        proposal.setAsWorkInProgress()
        proposal.requestReview()
        self.assertEqual(2, len(self.getJobs(proposal)))
        JobRunner([CreateValidationArchiveJob(replacement)]).runAll()
        self.assertEqual(JobStatus.COMPLETED, replacement.job.status)
        self.assertEqual(
            1,
            IStore(MergeProposalValidation)
            .find(
                MergeProposalValidation,
                branch_merge_proposal=proposal,
                commit_sha1=commit_sha1,
            )
            .count(),
        )

    def test_review_again_after_validation_does_not_queue(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        JobRunner([CreateValidationArchiveJob(job)]).runAll()
        self.assertEqual(JobStatus.COMPLETED, job.job.status)
        proposal.setAsWorkInProgress()
        proposal.requestReview()
        with dbuser("branchscanner"):
            self.updateRef(self.source_ref, proposal.source_git_commit_sha1)
        self.assertEqual([job], self.getJobs(proposal))
        self.assertEqual(
            1,
            IStore(MergeProposalValidation)
            .find(MergeProposalValidation, branch_merge_proposal=proposal)
            .count(),
        )

    def test_running_job_is_reused(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        job.job.start()
        self.assertEqual(
            job, CreateValidationArchiveJob.create(proposal).context
        )
        self.assertEqual([job], self.getJobs(proposal))

    def test_suspended_job_is_reused(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        job.job.suspend()
        self.assertEqual(
            job, CreateValidationArchiveJob.create(proposal).context
        )
        self.assertEqual([job], self.getJobs(proposal))
        self.assertEqual(JobStatus.SUSPENDED, job.job.status)

    def test_failed_job_without_validation_is_replaced(self):
        proposal = self.makeProposal()
        [original] = self.getJobs(proposal)
        original.job.start()
        original.job.fail()
        proposal.setAsWorkInProgress()
        proposal.requestReview()
        jobs = self.getJobs(proposal)
        self.assertEqual(2, len(jobs))
        [replacement] = [job for job in jobs if job.id != original.id]
        self.assertEqual(JobStatus.WAITING, replacement.job.status)
        self.assertEqual(
            original.metadata["commit_sha1"],
            replacement.metadata["commit_sha1"],
        )
        self.assertEqual(
            replacement, CreateValidationArchiveJob.create(proposal).context
        )

    def test_closed_proposal_does_not_build(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        proposal.queue_status = BranchMergeProposalStatus.MERGED
        CreateValidationArchiveJob(job).run()
        self.assertTrue(
            IStore(MergeProposalValidation)
            .find(MergeProposalValidation, branch_merge_proposal=proposal)
            .is_empty()
        )

    def test_job_sources_use_the_correct_role(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        self.assertEqual(
            [job],
            [
                ready.context
                for ready in CreateValidationArchiveJob.iterReady()
            ],
        )
        self.assertNotIn(
            job,
            [
                ready.context
                for ready in BranchMergeProposalJobSource.iterReady()
            ],
        )

    def test_suspended_registrant_does_not_queue(self):
        removeSecurityProxy(self.registrant).setAccountStatus(
            AccountStatus.SUSPENDED, self.registrant, "Suspended for testing"
        )
        self.assertEqual([], self.getJobs(self.makeProposal()))

    def test_disabled_after_queue_does_not_build(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        with FeatureFixture({}):
            CreateValidationArchiveJob(job).run()
        self.assertTrue(
            IStore(MergeProposalValidation)
            .find(MergeProposalValidation, branch_merge_proposal=proposal)
            .is_empty()
        )

    def test_new_commit_reuses_validation_archive(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        CreateValidationArchiveJob(job).run()
        first = (
            IStore(MergeProposalValidation)
            .find(MergeProposalValidation, branch_merge_proposal=proposal)
            .one()
        )
        commit_sha1 = "b" * 40
        self.updateRef(self.source_ref, commit_sha1)
        self.assertEqual(commit_sha1, proposal.source_git_commit_sha1)
        self.assertEqual(
            BranchMergeProposalStatus.NEEDS_REVIEW, proposal.queue_status
        )
        jobs = self.getJobs(proposal)
        self.assertContentEqual(
            [first.commit_sha1, commit_sha1],
            [job.metadata["commit_sha1"] for job in jobs],
        )
        [job] = [
            job for job in jobs if job.metadata["commit_sha1"] == commit_sha1
        ]
        CreateValidationArchiveJob(job).run()
        second = (
            IStore(MergeProposalValidation)
            .find(
                MergeProposalValidation,
                branch_merge_proposal=proposal,
                commit_sha1=commit_sha1,
            )
            .one()
        )
        self.assertEqual(first.archive, second.archive)
        reports = getUtility(IRevisionStatusReportSet).findByCommit(
            proposal.source_git_repository, commit_sha1
        )
        [build] = [
            report.source_package_git_build
            for report in reports
            if report.title == "source-build"
        ]
        self.assertEqual(commit_sha1, build.commit_sha1)
        self.assertEqual(first.archive, build.archive)
        self.assertIsNotNone(build.buildqueue_record)

    def test_source_update_queues_as_branchscanner(self):
        proposal = self.makeProposal()
        original_commit = proposal.source_git_commit_sha1
        with dbuser("branchscanner"):
            self.updateRef(self.source_ref, "b" * 40)
        self.assertContentEqual(
            [original_commit, "b" * 40],
            [job.metadata["commit_sha1"] for job in self.getJobs(proposal)],
        )

    def test_source_update_does_not_queue_for_wip(self):
        proposal = self.factory.makeBranchMergeProposalForGit(
            source_ref=self.source_ref,
            target_ref=self.target_ref,
            registrant=self.registrant,
        )
        self.updateRef(self.source_ref, "b" * 40)
        self.assertEqual("b" * 40, proposal.source_git_commit_sha1)
        self.assertEqual([], self.getJobs(proposal))

    def test_source_update_does_not_queue_when_disabled(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        with FeatureFixture({}):
            self.updateRef(self.source_ref, "b" * 40)
        self.assertEqual("b" * 40, proposal.source_git_commit_sha1)
        self.assertEqual([job], self.getJobs(proposal))

    def test_target_update_does_not_queue_validation(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        self.updateRef(self.target_ref, "b" * 40)
        self.assertEqual("b" * 40, proposal.target_git_commit_sha1)
        self.assertEqual([job], self.getJobs(proposal))

    def test_unrelated_source_ref_update_does_not_queue(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        [unrelated_ref] = self.factory.makeGitRefs(
            repository=self.source_ref.repository,
            paths=["refs/heads/unrelated"],
        )
        self.updateRef(unrelated_ref, "b" * 40)
        self.assertEqual(
            job.metadata["commit_sha1"], proposal.source_git_commit_sha1
        )
        self.assertEqual([job], self.getJobs(proposal))

    def test_deleted_archive_name_is_not_reused(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        CreateValidationArchiveJob(job).run()
        first = (
            IStore(MergeProposalValidation)
            .find(MergeProposalValidation, branch_merge_proposal=proposal)
            .one()
        )
        first.archive.status = ArchiveStatus.DELETED
        proposal.source_git_commit_sha1 = "b" * 40
        CreateValidationArchiveJob.create(proposal).run()
        second = (
            IStore(MergeProposalValidation)
            .find(
                MergeProposalValidation,
                branch_merge_proposal=proposal,
                commit_sha1="b" * 40,
            )
            .one()
        )
        self.assertEqual("mp-%s-2" % proposal.id, second.archive.name)

    def test_creation_event_queues_job(self):
        proposal = removeSecurityProxy(
            self.factory.makeBranchMergeProposalForGit(
                source_ref=self.source_ref,
                target_ref=self.target_ref,
                registrant=self.registrant,
            )
        )
        proposal.queue_status = BranchMergeProposalStatus.NEEDS_REVIEW
        notify(ObjectCreatedEvent(proposal, user=self.registrant))
        self.assertEqual(1, len(self.getJobs(proposal)))

    def test_private_source_does_not_queue(self):
        removeSecurityProxy(self.source_ref.repository).information_type = (
            InformationType.USERDATA
        )
        self.assertEqual([], self.getJobs(self.makeProposal()))

    def test_private_target_does_not_queue(self):
        removeSecurityProxy(self.target_ref.repository).information_type = (
            InformationType.USERDATA
        )
        self.assertEqual([], self.getJobs(self.makeProposal()))

    def test_wip_does_not_queue(self):
        proposal = self.factory.makeBranchMergeProposalForGit(
            source_ref=self.source_ref,
            target_ref=self.target_ref,
            registrant=self.registrant,
        )
        self.assertEqual([], self.getJobs(proposal))

    def test_unknown_series_does_not_queue(self):
        proposal = self.makeProposal()
        proposal.target_git_path = "refs/heads/%s/missing" % (
            self.series.distribution.name
        )
        self.assertIsNone(CreateValidationArchiveJob.create(proposal))

    def test_registrant_team_scope(self):
        team = self.factory.makeTeam()
        removeSecurityProxy(team).addMember(self.registrant, team.teamowner)
        with FeatureFixture(
            {},
            full_feature_rules=[
                {
                    "flag": "soyuz.mp_validation.enabled",
                    "scope": "team:" + team.name,
                    "priority": 100,
                    "value": "on",
                }
            ],
        ):
            self.assertEqual(1, len(self.getJobs(self.makeProposal())))

    def test_stale_commit_does_not_build(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        proposal.source_git_commit_sha1 = "a" * 40
        CreateValidationArchiveJob(job).run()
        self.assertTrue(
            IStore(MergeProposalValidation)
            .find(MergeProposalValidation, branch_merge_proposal=proposal)
            .is_empty()
        )

    def test_unrelated_archive_is_not_reused(self):
        proposal = self.makeProposal()
        existing = self.factory.makeArchive(
            owner=self.registrant,
            distribution=self.series.distribution,
            name="mp-%s" % proposal.id,
        )
        [job] = self.getJobs(proposal)
        CreateValidationArchiveJob(job).run()
        run = (
            IStore(MergeProposalValidation)
            .find(MergeProposalValidation, branch_merge_proposal=proposal)
            .one()
        )
        self.assertNotEqual(existing, run.archive)
        self.assertEqual("mp-%s-2" % proposal.id, run.archive.name)

    def test_runs_as_job_dbuser(self):
        proposal = self.makeProposal()
        [job] = self.getJobs(proposal)
        job_id = job.id
        with dbuser("mp-validation-job"):
            CreateValidationArchiveJob.get(job_id).run()
        self.assertEqual(
            1,
            IStore(MergeProposalValidation)
            .find(MergeProposalValidation, branch_merge_proposal=proposal)
            .count(),
        )


class TestCreateValidationArchiveJobViaCelery(
    ValidationArchiveJobTestMixin, TestCaseWithFactory
):
    layer = CeleryJobLayer

    def test_runs_via_celery(self):
        self.useFixture(
            FeatureFixture(
                {
                    "soyuz.mp_validation.enabled": "on",
                    "jobs.celery.enabled_classes": (
                        "CreateValidationArchiveJob"
                    ),
                }
            )
        )
        with block_on_job():
            proposal = self.makeProposal()
            transaction.commit()
        self.assertEqual(
            1,
            IStore(MergeProposalValidation)
            .find(MergeProposalValidation, branch_merge_proposal=proposal)
            .count(),
        )
