# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interfaces for native package validation jobs."""

__all__ = [
    "ICreateValidationArchiveJob",
    "ICreateValidationArchiveJobSource",
    "IMergeProposalValidation",
]

from zope.interface import Attribute, Interface

from lp.code.interfaces.branchmergeproposal import (
    IBranchMergeProposalJobSource,
)
from lp.services.job.interfaces.job import IRunnableJob


class IMergeProposalValidation(Interface):
    """A recorded validation run, visible with its MP and archive."""

    id = Attribute("Run ID.")
    branch_merge_proposal = Attribute("The owning merge proposal.")
    branch_merge_proposal_id = Attribute("The owning merge proposal ID.")
    commit_sha1 = Attribute("The recorded source commit.")
    archive = Attribute("The validation PPA.")
    status = Attribute("The stored run lifecycle status.")
    date_created = Attribute("When the run was created.")
    date_finished = Attribute("When the run finished, if known.")


class ICreateValidationArchiveJob(IRunnableJob):
    """Create a contributor-owned archive and request a source Git build."""


class ICreateValidationArchiveJobSource(IBranchMergeProposalJobSource):
    """Source of jobs preparing merge proposal validation archives."""

    def create(merge_proposal, needs_review=False):
        """Queue validation if the proposal and registrant are eligible."""
