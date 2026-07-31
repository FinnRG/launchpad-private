# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interface for communication with the Git hosting service."""

__all__ = [
    "IGitHostingClient",
]

from zope.interface import Interface


class IGitHostingClient(Interface):
    """Interface for the internal API provided by the Git hosting service."""

    def create(path, clone_from=None, async_create=False):
        """Create a Git repository.

        :param path: Physical path of the new repository on the hosting
            service.
        :param clone_from: If not None, clone the new repository from this
            other physical path.
        :param async_create: Do not block the call until the repository is
            actually created.
        """

    def getProperties(path):
        """Get properties of this repository.

        :param path: Physical path of the repository on the hosting service.
        :return: A dict of properties.
        """

    def setProperties(path, **props):
        """Set properties of this repository.

        :param path: Physical path of the repository on the hosting service.
        :param props: Properties to set.
        """

    def getRefs(path, exclude_prefixes=None):
        """Get all refs in this repository.

        :param path: Physical path of the repository on the hosting service.
        :param exclude_prefixes: An optional list of ref prefixes to exclude.
        :return: A dict mapping ref paths to dicts representing the objects
            they point to.
        """

    def getCommits(path, commit_oids, filter_paths=None, logger=None):
        """Get details of a list of commits.

        :param path: Physical path of the repository on the hosting service.
        :param commit_oids: A list of commit OIDs.
        :param filter_paths: If given, only return commits that modify paths
            in this list, and include the contents of the files at those
            paths in the response.
        :param logger: An optional logger.
        :return: A list of dicts each of which represents one of the
            requested commits.  Non-existent commits will be omitted.
        """

    def getLog(path, start, limit=None, stop=None, logger=None):
        """Get commit log information.

        :param path: Physical path of the repository on the hosting service.
        :param start: The commit to start listing from.
        :param limit: If not None, return no more than this many commits.
        :param stop: If not None, ignore this commit and its ancestors.
        :param logger: An optional logger.
        :return: A list of dicts each of which represents a commit from the
            start commit's history.
        """

    def getDiff(
        path, old, new, common_ancestor=False, context_lines=None, logger=None
    ):
        """Get the diff between two commits.

        :param path: Physical path of the repository on the hosting service.
        :param old: The OID of the old commit.
        :param new: The OID of the new commit.
        :param common_ancestor: If True, return the symmetric or common
            ancestor diff, equivalent to
            `git diff $(git-merge-base OLD NEW) NEW`.
        :param context_lines: Include this number of lines of context around
            each hunk.
        :param logger: An optional logger.
        :return: A dict mapping 'commits' to a list of commits between 'old'
            and 'new' (formatted as with `getCommits`) and 'patch' to the
            text of the diff between 'old' and 'new'.
        """

    def getMergeDiff(path, base, head, logger=None):
        """Get the merge preview diff between two commits.

        :param path: Physical path of the repository on the hosting service.
        :param base: The OID of the base commit.
        :param head: The OID of the commit that we want to merge into
            'base'.
        :param logger: An optional logger.
        :return: A dict mapping 'commits' to a list of commits between
            'base' and 'head' (formatted as with `getCommits`), 'patch' to
            the text of the diff between 'base' and 'head', and 'conflicts'
            to a list of conflicted paths.
        """

    def getDiffStats(path, old, new, common_ancestor=False, logger=None):
        """Get the diff between two commits.

        :param path: Physical path of the repository on the hosting service.
        :param old: The OID of the old commit. Can be empty to get stat
            against empty tree diff
        :param new: The OID of the new commit.
        :param common_ancestor: If True, return the symmetric or common
            ancestor diff, equivalent to
            `git diff $(git-merge-base OLD NEW) NEW`.
        :param logger: An optional logger.
        :return: A dict with diff stats for the existing diff.
        """

    def detectMerges(path, target, sources, previous_target=None, logger=None):
        """Detect merges of any of 'sources' into 'target'.

        :param path: Physical path of the repository on the hosting service.
        :param target: The OID of the merge proposal target commit.
        :param sources: The OIDs of the merge proposal source commits.
        :param previous_target: If not None, stop walking back through
            history upon reaching this commit.
        :param logger: An optional logger.
        :return: A dict mapping merged commit OIDs from 'sources' to the
            first commit OID in the left-hand (first parent only) history of
            'target' that is a descendant of the corresponding source
            commit.  Unmerged commits are omitted.
        """

    def delete(path, logger=None):
        """Delete a repository.

        :param path: Physical path of the repository on the hosting service.
        :param logger: An optional logger.
        """

    def getBlob(path, filename, rev=None, logger=None):
        """Get a blob by file name from a repository.

        :param path: Physical path of the repository on the hosting service.
        :param filename: Relative path of a file in the repository.
        :param rev: An optional revision. Defaults to 'HEAD'.
        :param logger: An optional logger.
        :return: A binary string with the blob content.
        """

    def copyRefs(path, operations, logger=None):
        """Executes the copy of refs or commits between different
        repositories.

        :param path: Physical path of the repository on the hosting service.
        :param operations: A list of RefCopyOperation objects describing
                           source and target of the copy.
        :param logger: An optional logger.
        """

    def deleteRefs(refs, logger=None):
        """Deletes a list of repository paths and references.

        :param refs: A list of tuples like (repo_path, ref_name) to be deleted.
        :param logger: An optional logger.
        """

    def repackRepository(path, logger=None, no_reuse_delta=False):
        """Repack a Git repository.

        :param path: Physical path of the new repository on the hosting
            service.
        :param logger: An optional logger.
        :param no_reuse_delta: If True, request the hosting service to force a
            full delta recomputation instead of reusing
            existing deltas.
        """

    def collectGarbage(path, logger=None):
        """Run Git GC for a repository.

        :param path: Physical path of the new repository on the hosting
            service.
        :param logger: An optional logger.
        """

    def requestMerge(
        repo,
        target_branch,
        target_commit_sha1,
        source_branch,
        source_commit_sha1,
        commiter,
        commit_message,
        source_repo=None,
        logger=None,
    ):
        """Request a merge from source_branch to target_branch

        :param repo: name of the target repository on the hosting service.
        :param target_branch: name of target branch.
        :param target_commit_sha1: commit set as target in target branch
        :param source_branch: name of source branch, to be merged into target
        :param source_commit_sha1: latest commit in source branch
        :param committer: person that requests the merge
        :param commit_message: custom message for merge commit
        :param source_repo: name of the source repository on the hosting
        service (if any)
        :param logger: An optional logger.
        """

    def merge(
        repo,
        target_branch,
        target_commit_sha1,
        source_branch,
        source_commit_sha1,
        commiter,
        commit_message,
        source_repo=None,
        logger=None,
    ):
        """To be deprecated in favour of requestMerge

        :param repo: name of the target repository on the hosting service.
        :param target_branch: name of target branch.
        :param target_commit_sha1: commit set as target in target branch
        :param source_branch: name of source branch, to be merged into target
        :param source_commit_sha1: latest commit in source branch
        :param committer: person that requests the merge
        :param commit_message: custom message for merge commit
        :param source_repo: name of the source repository on the hosting
        service (if any)
        :param logger: An optional logger.
        """
