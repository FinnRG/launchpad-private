# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Validation runs for distribution source package merge proposals."""

__all__ = ["MergeProposalValidation"]

from datetime import timezone

from storm.databases.postgres import JSON
from storm.locals import DateTime, Int, Reference, Unicode

from lp.code.enums import RevisionStatusResult
from lp.services.database.constants import UTC_NOW
from lp.services.database.enumcol import DBEnum
from lp.services.database.stormbase import StormBase


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
