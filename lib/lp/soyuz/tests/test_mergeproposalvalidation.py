# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for merge proposal validation runs."""

__all__ = []

from lp.code.enums import RevisionStatusResult
from lp.services.database.interfaces import IStore
from lp.soyuz.model.mergeproposalvalidation import MergeProposalValidation
from lp.testing import TestCaseWithFactory
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
