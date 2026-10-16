# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Connect binary publication to merge proposal validation requests."""

__all__ = ["request_autopkgtests_on_publication"]

import logging

from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.code.enums import BranchMergeProposalStatus, RevisionStatusResult
from lp.code.interfaces.autopkgtest import IRequestAutopkgtestJobSource
from lp.services.database.interfaces import IPrimaryStore
from lp.services.features import getFeatureFlag
from lp.soyuz.model.mergeproposalvalidation import MergeProposalValidation

logger = logging.getLogger(__name__)


def request_autopkgtests_on_publication(publication, event):
    """Save request work in the publishing transaction, without network I/O."""
    if not getFeatureFlag("autopkgtest.request.enabled"):
        return
    publication = removeSecurityProxy(publication)
    if not publication.archive.is_ppa or publication.archive.private:
        return
    binary_build = publication.build
    if binary_build is None:
        return
    source = binary_build.source_package_release
    git_build = source.source_package_git_build
    if git_build is None or git_build.git_repository.private:
        return
    runs = IPrimaryStore(MergeProposalValidation).find(
        MergeProposalValidation,
        archive=publication.archive,
        commit_sha1=git_build.commit_sha1,
    )
    matches = []
    for run in runs:
        proposal = run.branch_merge_proposal
        if (
            run.status
            not in (
                RevisionStatusResult.WAITING,
                RevisionStatusResult.RUNNING,
            )
            or run.date_finished is not None
            or proposal.queue_status
            in (
                BranchMergeProposalStatus.MERGED,
                BranchMergeProposalStatus.REJECTED,
                BranchMergeProposalStatus.SUPERSEDED,
            )
            or proposal.source_git_repository != git_build.git_repository
            or proposal.target_git_repository is None
            or proposal.target_git_repository.private
        ):
            continue
        matches.append(run)
    if len(matches) > 1:
        logger.error(
            "Ambiguous validation runs for binary publication %s; "
            "no autopkgtest job queued.",
            publication.id,
        )
        return
    if matches:
        getUtility(IRequestAutopkgtestJobSource).create(
            matches[0], publication
        )
