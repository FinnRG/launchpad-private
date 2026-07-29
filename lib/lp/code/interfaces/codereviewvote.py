# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""CodeReviewVoteReference interface."""

__all__ = [
    "ICodeReviewVoteReference",
]

from lazr.restful.declarations import (
    REQUEST_USER,
    call_with,
    export_destructor_operation,
    export_write_operation,
    exported,
    exported_as_webservice_entry,
    operation_for_version,
    operation_parameters,
)
from lazr.restful.fields import Reference
from zope.interface import Interface
from zope.schema import Bool, Datetime, Int, TextLine

from lp import _
from lp.code.interfaces.branchmergeproposal import IBranchMergeProposal
from lp.code.interfaces.codereviewcomment import ICodeReviewComment
from lp.registry.interfaces.person import IPerson
from lp.services.fields import PersonChoice, PublicPersonChoice


class ICodeReviewVoteReferencePublic(Interface):
    """The public attributes for code review vote references."""

    id = Int(title=_("The ID of the vote reference"))

    branch_merge_proposal = exported(
        Reference(
            title=_("The merge proposal that is the subject of this vote"),
            required=True,
            schema=IBranchMergeProposal,
        )
    )

    date_created = exported(
        Datetime(title=_("Date Created"), required=True, readonly=True)
    )

    registrant = exported(
        PublicPersonChoice(
            title=_("The person who originally registered this vote"),
            required=True,
            vocabulary="ValidPersonOrTeam",
        )
    )

    reviewer = exported(
        PersonChoice(
            title=_("Reviewer"),
            required=True,
            description=_("A person who you want to review this."),
            vocabulary="ValidBranchReviewer",
        )
    )

    review_type = exported(
        TextLine(
            title=_("Review type"),
            required=False,
            description=_("Lowercase keywords describing the type of review."),
        )
    )

    comment = exported(
        Reference(
            title=_(
                "The code review comment that contains the most recent vote."
            ),
            required=True,
            schema=ICodeReviewComment,
        )
    )

    is_pending = exported(
        Bool(title=_("Is the pending?"), required=True, readonly=True)
    )


class ICodeReviewVoteReferenceEdit(Interface):
    """Method that require edit permissions."""

    def validateClaimReview(claimant):
        """Implements the validation for claiming a review.

        :raises ClaimReviewFailed: If the claimant already has a
            personal review, if the reviewer is not a team, if the
            claimant is not in the reviewer team, or if the review is
            not pending.
        """

    @call_with(claimant=REQUEST_USER)
    @export_write_operation()
    @operation_for_version("beta")
    def claimReview(claimant):
        """Change a pending review into a review for claimant.

        Pending team reviews can be claimed by members of that team.  This
        allows reviews to be moved of the general team todo list, and onto a
        personal todo list.

        :param claimant: The person claiming the team review.
        :raises ClaimReviewFailed: If the claimant already has a
            personal review, if the reviewer is not a team, if the
            claimant is not in the reviewer team, or if the review is
            not pending.
        """

    # XXX 2026-07-30 finnrg: Ideally this would reset back to the team for
    # claimed reviews, but that will require a new column (see LP: #1708172)
    @export_write_operation()
    @operation_for_version("devel")
    def resetToPending():
        """Reset a completed review back into a pending one.

        This asks the reviewer to look at the proposal again, typically
        because the source branch has changed since they voted.  The comment
        carrying their original vote is left untouched, so the vote remains
        visible in the conversation.

        :raises ReviewAlreadyPending: If the review is already pending.
        """

    def validateReasignReview(reviewer):
        """Implements the validation for reassignment.

        :raises ReviewNotPending: If the review is not pending.
        :raises ReassignReviewFailed: If the reviewer is an individual and
            already has a personal review.
        """

    @operation_parameters(
        reviewer=Reference(
            title=_("The person or team to assign to do the review."),
            schema=IPerson,
        )
    )
    @export_write_operation()
    @operation_for_version("beta")
    def reassignReview(reviewer):
        """Reassign a pending review to someone else.

        Pending reviews can be reassigned to someone else.

        :param reviewer: The person to assign the pending review to.
        :raises ReviewNotPending: If the review is not pending.
        :raises ReassignReviewFailed: If the reviewer is an individual and
            already has a personal review.
        """

    @export_destructor_operation()
    @operation_for_version("beta")
    def delete():
        """Delete the pending review.

        :raises ReviewNotPending: If the review is not pending.
        """


@exported_as_webservice_entry(as_of="beta")
class ICodeReviewVoteReference(
    ICodeReviewVoteReferencePublic, ICodeReviewVoteReferenceEdit
):
    """A reference to a vote on a IBranchMergeProposal.

    There is at most one reference to a vote for each reviewer on a given
    branch merge proposal.
    """
