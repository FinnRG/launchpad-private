# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for current-check selection."""

__all__ = []

from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.code.enums import RevisionStatusResult
from lp.code.interfaces.revisionstatus import IRevisionStatusReportSet
from lp.code.model.revisionstatussummary import (
    CheckSelection,
    selectCommitChecks,
    selectValidationChecks,
)
from lp.services.database.interfaces import IStore
from lp.soyuz.model.mergeproposalvalidation import MergeProposalValidation
from lp.soyuz.tests.test_mergeproposalvalidation import (
    ValidationReportTestMixin,
)
from lp.soyuz.tests.test_sourcepackagegitbuild import (
    SourcePackageGitBuildTestMixin,
)
from lp.testing import TestCaseWithFactory, person_logged_in
from lp.testing.layers import DatabaseFunctionalLayer


class TestCurrentChecks(
    ValidationReportTestMixin,
    SourcePackageGitBuildTestMixin,
    TestCaseWithFactory,
):
    layer = DatabaseFunctionalLayer

    def test_latest_run_uses_recorded_commit_and_includes_terminal_runs(self):
        build = self.makeBuild()
        run, report = self.makeValidation(build)
        proposal = run.branch_merge_proposal
        proposal.source_git_commit_sha1 = "b" * 40
        self.assertEqual(run, MergeProposalValidation.getLatest(proposal))
        other = MergeProposalValidation(proposal, "c" * 40, run.archive, {})
        IStore(other).add(other)
        other.status = RevisionStatusResult.CANCELLED
        self.assertEqual(other, MergeProposalValidation.getLatest(proposal))

    def test_no_run(self):
        proposal = self.factory.makeBranchMergeProposalForGit()
        self.assertIsNone(MergeProposalValidation.getLatest(proposal))

    def test_successful_source_without_binary_reports_is_incomplete(self):
        run, report = self.makeValidation(self.makeBuild())
        removeSecurityProxy(report).result = RevisionStatusResult.SUCCEEDED
        self.factory.makeRevisionStatusReport(
            git_repository=report.git_repository,
            commit_sha1=report.commit_sha1,
            title="archive",
            group_name="package-validation",
            properties={"validation_id": run.id},
            result=RevisionStatusResult.SUCCEEDED,
        )
        selection = selectValidationChecks(run)
        self.assertEqual([report], selection.current)
        self.assertEqual([], selection.history)
        self.assertTrue(selection.issues)
        self.assertEqual(RevisionStatusResult.WAITING, selection.result)

    def test_ambiguous_source_reports_do_not_choose_a_winner(self):
        run, report = self.makeValidation(self.makeBuild())
        self.factory.makeRevisionStatusReport(
            git_repository=report.git_repository,
            commit_sha1=report.commit_sha1,
            title="source-build",
            group_name="package-validation",
            source_package_git_build=report.source_package_git_build,
            properties={"validation_id": run.id},
            result=RevisionStatusResult.SUCCEEDED,
        )
        selection = selectValidationChecks(run)
        self.assertEqual([], selection.current)
        self.assertTrue(selection.issues)
        self.assertEqual(2, len(selection.history))

    def test_same_commit_proposals_stay_separate(self):
        run, report = self.makeValidation(self.makeBuild())
        other = MergeProposalValidation(
            self.factory.makeBranchMergeProposalForGit(
                source_ref=run.branch_merge_proposal.source_git_ref
            ),
            run.commit_sha1,
            run.archive,
            {},
        )
        IStore(other).add(other)
        selections = selectCommitChecks(
            report.git_repository, report.commit_sha1
        )
        self.assertEqual([run, other], [s.validation for s in selections])
        self.assertEqual([report], selections[0].current)
        self.assertEqual([], selections[1].current)
        self.assertEqual(
            run, MergeProposalValidation.getLatest(run.branch_merge_proposal)
        )

    def test_empty_is_not_success(self):
        self.assertIsNone(CheckSelection().result)

    def test_all_result_states(self):
        report = removeSecurityProxy(self.factory.makeRevisionStatusReport())
        selection = CheckSelection(current=[report])
        for result in RevisionStatusResult.items:
            report.result = result
            self.assertEqual(result, selection.result)
        report.result = None
        self.assertEqual(RevisionStatusResult.WAITING, selection.result)

    def test_mixed_results_and_merge_gate_agree(self):
        proposal = self.factory.makeBranchMergeProposalForGit()
        for result in (
            RevisionStatusResult.FAILED,
            RevisionStatusResult.SUCCEEDED,
        ):
            self.factory.makeRevisionStatusReport(
                git_repository=proposal.source_git_repository,
                commit_sha1=proposal.source_git_commit_sha1,
                result=result,
            )
        [selection] = selectCommitChecks(
            proposal.source_git_repository, proposal.source_git_commit_sha1
        )
        self.assertEqual(RevisionStatusResult.FAILED, selection.result)
        self.assertFalse(proposal.CIChecksPassed())

    def test_lp_ci_new_build_replaces_only_its_own_architecture(self):
        repository = self.factory.makeGitRepository()
        sha1 = "a" * 40
        first = self.factory.makeCIBuild(
            git_repository=repository, commit_sha1=sha1
        )
        old = self.factory.makeRevisionStatusReport(
            ci_build=first, result=RevisionStatusResult.FAILED
        )
        second = self.factory.makeCIBuild(
            git_repository=repository,
            commit_sha1=sha1,
            distro_arch_series=first.distro_arch_series,
        )
        current = self.factory.makeRevisionStatusReport(
            ci_build=second, result=RevisionStatusResult.SUCCEEDED
        )
        other = self.factory.makeRevisionStatusReport(
            ci_build=self.factory.makeCIBuild(
                git_repository=repository,
                commit_sha1=sha1,
                distro_arch_series=self.factory.makeDistroArchSeries(
                    distroseries=first.distro_arch_series.distroseries
                ),
            ),
            result=RevisionStatusResult.FAILED,
        )
        [selection] = selectCommitChecks(repository, sha1)
        self.assertIn(current, selection.current)
        self.assertIn(other, selection.current)
        self.assertNotIn(old, selection.current)
        self.assertIn(old, selection.history)
        self.assertEqual(RevisionStatusResult.FAILED, selection.result)

    def test_history_only_group_does_not_block_merge(self):
        proposal = self.factory.makeBranchMergeProposalForGit()
        first = self.factory.makeCIBuild(
            git_repository=proposal.source_git_repository,
            commit_sha1=proposal.source_git_commit_sha1,
        )
        old = self.factory.makeRevisionStatusReport(
            ci_build=first,
            group_name="old-group",
            result=RevisionStatusResult.FAILED,
        )
        second = self.factory.makeCIBuild(
            git_repository=first.git_repository,
            commit_sha1=first.commit_sha1,
            distro_arch_series=first.distro_arch_series,
        )
        current = self.factory.makeRevisionStatusReport(
            ci_build=second,
            group_name="new-group",
            result=RevisionStatusResult.SUCCEEDED,
        )
        historical, active = getUtility(
            IRevisionStatusReportSet
        ).getCommitChecks(first.git_repository, first.commit_sha1)
        self.assertEqual([], historical.current)
        self.assertEqual([old], historical.history)
        self.assertEqual([], historical.issues)
        self.assertIsNone(historical.result)
        self.assertEqual([current], active.current)
        self.assertTrue(proposal.CIChecksPassed())

    def test_orphaned_run_report_cannot_make_merge_pass(self):
        proposal = self.factory.makeBranchMergeProposalForGit()
        self.factory.makeRevisionStatusReport(
            git_repository=proposal.source_git_repository,
            commit_sha1=proposal.source_git_commit_sha1,
            result=RevisionStatusResult.SUCCEEDED,
        )
        self.factory.makeRevisionStatusReport(
            git_repository=proposal.source_git_repository,
            commit_sha1=proposal.source_git_commit_sha1,
            properties={"validation_id": 999999},
            result=RevisionStatusResult.SUCCEEDED,
        )
        selections = selectCommitChecks(
            proposal.source_git_repository, proposal.source_git_commit_sha1
        )
        [incomplete] = [s for s in selections if s.issues]
        self.assertEqual([], incomplete.current)
        self.assertTrue(incomplete.history)
        self.assertFalse(proposal.CIChecksPassed())

    def test_latest_report_for_each_title_is_current(self):
        proposal = self.factory.makeBranchMergeProposalForGit()
        old, new = (
            self.factory.makeRevisionStatusReport(
                title="external-ci",
                git_repository=proposal.source_git_repository,
                commit_sha1=proposal.source_git_commit_sha1,
                result=result,
            )
            for result in (
                RevisionStatusResult.FAILED,
                RevisionStatusResult.SUCCEEDED,
            )
        )
        [selection] = selectCommitChecks(
            proposal.source_git_repository, proposal.source_git_commit_sha1
        )
        self.assertEqual([new], selection.current)
        self.assertEqual([old], selection.history)
        self.assertTrue(proposal.CIChecksPassed())

    def test_same_title_in_other_group_is_a_separate_check(self):
        proposal = self.factory.makeBranchMergeProposalForGit()
        for group_name, result in (
            ("first", RevisionStatusResult.FAILED),
            ("second", RevisionStatusResult.SUCCEEDED),
        ):
            self.factory.makeRevisionStatusReport(
                title="test",
                group_name=group_name,
                git_repository=proposal.source_git_repository,
                commit_sha1=proposal.source_git_commit_sha1,
                result=result,
            )
        selections = selectCommitChecks(
            proposal.source_git_repository, proposal.source_git_commit_sha1
        )
        self.assertEqual(
            [RevisionStatusResult.FAILED, RevisionStatusResult.SUCCEEDED],
            [selection.result for selection in selections],
        )
        self.assertFalse(proposal.CIChecksPassed())

    def test_merge_gate_does_not_depend_on_run_visibility(self):
        run, report = self.makeValidation(self.makeBuild())
        removeSecurityProxy(report).result = RevisionStatusResult.FAILED
        proposal = removeSecurityProxy(run.branch_merge_proposal)
        proposal.source_git_commit_sha1 = run.commit_sha1
        removeSecurityProxy(run.archive).disable()
        reports = getUtility(IRevisionStatusReportSet)

        def visible_runs():
            return [
                selection.validation.id
                for selection in reports.getCommitChecks(
                    report.git_repository, report.commit_sha1
                )
                if selection.validation is not None
            ]

        with person_logged_in(self.factory.makePerson()):
            self.assertFalse(proposal.CIChecksPassed())
            self.assertEqual([], visible_runs())
        with person_logged_in(run.archive.owner):
            self.assertFalse(proposal.CIChecksPassed())
            self.assertEqual([run.id], visible_runs())
