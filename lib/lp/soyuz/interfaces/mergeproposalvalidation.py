# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interfaces for native package validation jobs."""

__all__ = ["ICreateValidationArchiveJob", "ICreateValidationArchiveJobSource"]

from lp.code.interfaces.branchmergeproposal import (
    IBranchMergeProposalJobSource,
)
from lp.services.job.interfaces.job import IRunnableJob


class ICreateValidationArchiveJob(IRunnableJob):
    """Create a contributor-owned archive and request a source Git build."""


class ICreateValidationArchiveJobSource(IBranchMergeProposalJobSource):
    """Source of jobs preparing merge proposal validation archives."""

    def create(merge_proposal, needs_review=False):
        """Queue validation if the proposal and registrant are eligible."""
