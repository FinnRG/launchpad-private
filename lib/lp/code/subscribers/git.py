# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Event subscribers for Git repositories."""

from zope.component import getUtility

from lp.code.interfaces.cibuild import ICIBuildSet
from lp.soyuz.interfaces.mergeproposalvalidation import (
    ICreateValidationArchiveJobSource,
)


def _request_ci_builds(repository, event):
    getUtility(ICIBuildSet).requestBuildsForRefs(
        repository, event.paths, logger=event.logger
    )


def refs_created(repository, event):
    """Some references in a Git repository have been created."""
    _request_ci_builds(repository, event)


def refs_updated(repository, event):
    """Some references in a Git repository have been updated."""
    # Remember the previous target commit IDs so that detectMerges can know
    # how far back in history it needs to search.
    previous_targets = {
        bmp.id: bmp.target_git_commit_sha1
        for bmp in repository.getActiveLandingCandidates(event.paths)
    }

    repository.updateMergeCommitIDs(event.paths)
    repository.updateLandingTargets(event.paths)
    repository.markRecipesStale(event.paths)
    repository.markSnapsStale(event.paths)
    repository.markCharmRecipesStale(event.paths)
    repository.detectMerges(event.paths, previous_targets, logger=event.logger)
    for merge_proposal in repository.getActiveLandingTargets(event.paths):
        getUtility(ICreateValidationArchiveJobSource).create(merge_proposal)
    _request_ci_builds(repository, event)
