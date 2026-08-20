# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""CodeReviewVoteReference database class."""

__all__ = [
    "CodeReviewVoteReference",
]

from datetime import timezone

from storm.locals import DateTime, Int, Reference, Store, Unicode
from zope.interface import implementer

from lp.code.errors import (
    ClaimReviewFailed,
    ReviewAlreadyPending,
    ReviewNotPending,
    UserHasExistingReview,
)
from lp.code.interfaces.codereviewvote import ICodeReviewVoteReference
from lp.services.database.constants import DEFAULT
from lp.services.database.stormbase import StormBase


@implementer(ICodeReviewVoteReference)
class CodeReviewVoteReference(StormBase):
    """See `ICodeReviewVote`"""

    __storm_table__ = "CodeReviewVote"

    id = Int(primary=True)
    branch_merge_proposal_id = Int(
        name="branch_merge_proposal", allow_none=False
    )
    branch_merge_proposal = Reference(
        branch_merge_proposal_id, "BranchMergeProposal.id"
    )
    date_created = DateTime(
        tzinfo=timezone.utc, allow_none=False, default=DEFAULT
    )
    registrant_id = Int(name="registrant", allow_none=False)
    registrant = Reference(registrant_id, "Person.id")
    reviewer_id = Int(name="reviewer", allow_none=False)
    reviewer = Reference(reviewer_id, "Person.id")
    review_type = Unicode(default=None)
    comment_id = Int(name="vote_message", default=None)
    comment = Reference(comment_id, "CodeReviewComment.id")

    def __init__(
        self,
        branch_merge_proposal,
        registrant,
        reviewer,
        review_type=None,
        date_created=DEFAULT,
    ):
        self.branch_merge_proposal = branch_merge_proposal
        self.registrant = registrant
        self.reviewer = reviewer
        self.review_type = review_type
        self.date_created = date_created

    @property
    def is_pending(self):
        """See `ICodeReviewVote`"""
        # Reviews are pending if there is no associated comment.
        return self.comment is None

    def resetToPending(self):
        """See `ICodeReviewVote`"""
        if self.is_pending:
            raise ReviewAlreadyPending("The review is already pending.")
        # Only the slot is reopened, so the original vote stays in the
        # conversation.
        self.comment = None

    def _validatePending(self):
        """Raise if the review is not pending."""
        if not self.is_pending:
            raise ReviewNotPending("The review is not pending.")

    def _validateNoReviewForUser(self, user):
        """Make sure there isn't an existing review for the user."""
        bmp = self.branch_merge_proposal
        existing_review = bmp.getUsersVoteReference(user)
        if existing_review is not None:
            if existing_review.is_pending:
                error_str = "%s has already been asked to review this"
            else:
                error_str = "%s has already reviewed this"
            raise UserHasExistingReview(error_str % user.unique_displayname)

    def validateClaimReview(self, claimant):
        """See `ICodeReviewVote`"""
        self._validatePending()
        if not self.reviewer.is_team:
            raise ClaimReviewFailed("Cannot claim non-team reviews.")
        if not claimant.inTeam(self.reviewer):
            raise ClaimReviewFailed(
                "%s is not a member of %s"
                % (
                    claimant.unique_displayname,
                    self.reviewer.unique_displayname,
                )
            )
        self._validateNoReviewForUser(claimant)

    def claimReview(self, claimant):
        """See `ICodeReviewVote`"""
        if self.reviewer == claimant:
            return
        self.validateClaimReview(claimant)
        self.reviewer = claimant

    def validateReasignReview(self, reviewer):
        """See `ICodeReviewVote`"""
        self._validatePending()
        if not reviewer.is_team:
            self._validateNoReviewForUser(reviewer)

    def reassignReview(self, reviewer):
        """See `ICodeReviewVote`"""
        self.validateReasignReview(reviewer)
        self.reviewer = reviewer

    def destroySelf(self):
        """Delete this vote."""
        Store.of(self).remove(self)

    def delete(self):
        """See `ICodeReviewVote`"""
        if not self.is_pending:
            raise ReviewNotPending("The review is not pending.")
        self.destroySelf()
