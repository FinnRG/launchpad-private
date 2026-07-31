# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Communication with the Git hosting service."""

__all__ = [
    "GitHostingClient",
    "RefCopyOperation",
    "RequestExceptionWrapper",
]

import base64
import json
import sys
from urllib.parse import quote, urljoin

import requests
from lazr.restful.utils import get_current_browser_request
from six import ensure_text
from zope.interface import implementer

from lp.code.errors import (
    BranchMergeProposalConflicts,
    CannotRepackRepository,
    CannotRunGitGC,
    GitReferenceDeletionFault,
    GitRepositoryBlobNotFound,
    GitRepositoryCreationFault,
    GitRepositoryDeletionFault,
    GitRepositoryScanFault,
    GitTargetError,
)
from lp.code.interfaces.githosting import IGitHostingClient
from lp.services.config import config
from lp.services.timeline.requesttimeline import get_request_timeline
from lp.services.timeout import (
    TimeoutError,
    get_default_timeout_function,
    urlfetch,
)


class RequestExceptionWrapper(requests.RequestException):
    """A non-requests exception that occurred during a request."""


class RefCopyOperation:
    """A description of a ref (or commit) copy between repositories.

    This class is just a helper to define copy operations parameters on
    IGitHostingClient.copyRefs method.
    """

    def __init__(self, source_ref, target_repo, target_ref):
        self.source_ref = source_ref
        self.target_repo = target_repo
        self.target_ref = target_ref


@implementer(IGitHostingClient)
class GitHostingClient:
    """A client for the internal API provided by the Git hosting system."""

    def __init__(self):
        self.endpoint = config.codehosting.internal_git_api_endpoint

    def _request(self, method, path, **kwargs):
        """Make a request to the Git hosting API."""
        # Fetch the current timeout before starting the timeline action,
        # since making a database query inside this action will result in an
        # OverlappingActionError.
        get_default_timeout_function()()
        timeline = get_request_timeline(get_current_browser_request())
        action = timeline.start(
            "git-hosting-%s" % method, "%s %s" % (path, json.dumps(kwargs))
        )
        try:
            response = urlfetch(
                urljoin(self.endpoint, path), method=method, **kwargs
            )
        except TimeoutError:
            # Re-raise this directly so that it can be handled specially by
            # callers.
            raise
        except requests.RequestException:
            raise
        except Exception:
            _, val, tb = sys.exc_info()
            try:
                raise RequestExceptionWrapper(*val.args).with_traceback(tb)
            finally:
                # Avoid traceback reference cycles.
                del val, tb
        finally:
            action.finish()
        if response.content:
            return response.json()
        else:
            return None

    def _get(self, path, **kwargs):
        return self._request("get", path, **kwargs)

    def _post(self, path, **kwargs):
        return self._request("post", path, **kwargs)

    def _patch(self, path, **kwargs):
        return self._request("patch", path, **kwargs)

    def _delete(self, path, **kwargs):
        return self._request("delete", path, **kwargs)

    def create(self, path, clone_from=None, async_create=False):
        """See `IGitHostingClient`."""
        try:
            if clone_from:
                request = {"repo_path": path, "clone_from": clone_from}
            else:
                request = {"repo_path": path}
            if async_create:
                # XXX pappacena 2020-07-02: async forces to clone_refs
                # because it's only used in situations where this is
                # desirable for now. We might need to add "clone_refs" as
                # parameter in the future.
                request["async"] = True
                request["clone_refs"] = clone_from is not None
            self._post("/repo", json=request)
        except requests.RequestException as e:
            raise GitRepositoryCreationFault(
                "Failed to create Git repository: %s" % str(e), path
            )

    def getProperties(self, path):
        """See `IGitHostingClient`."""
        try:
            return self._get("/repo/%s" % path)
        except requests.RequestException as e:
            raise GitRepositoryScanFault(
                "Failed to get properties of Git repository: %s" % str(e)
            )

    def setProperties(self, path, **props):
        """See `IGitHostingClient`."""
        try:
            self._patch("/repo/%s" % path, json=props)
        except requests.RequestException as e:
            raise GitRepositoryScanFault(
                "Failed to set properties of Git repository: %s" % str(e)
            )

    def getRefs(self, path, exclude_prefixes=None):
        """See `IGitHostingClient`."""
        try:
            return self._get(
                "/repo/%s/refs" % path,
                params={"exclude_prefix": exclude_prefixes},
            )
        except requests.RequestException as e:
            raise GitRepositoryScanFault(
                "Failed to get refs from Git repository: %s" % str(e)
            )

    def _decodeBlob(self, encoded_blob):
        """Blobs are base64-decoded for transport.  Decode them."""
        try:
            blob = base64.b64decode(encoded_blob["data"].encode("UTF-8"))
            if len(blob) != encoded_blob["size"]:
                raise GitRepositoryScanFault(
                    "Unexpected size (%s vs %s)"
                    % (len(blob), encoded_blob["size"])
                )
            return blob
        except Exception as e:
            raise GitRepositoryScanFault(
                "Failed to get file from Git repository: %s" % str(e)
            )

    def getCommits(self, path, commit_oids, filter_paths=None, logger=None):
        """See `IGitHostingClient`."""
        commit_oids = list(commit_oids)
        try:
            if logger is not None:
                message = "Requesting commit details for %s" % commit_oids
                if filter_paths:
                    message += " (with paths: %s)" % filter_paths
                logger.info(message)
            json_data = {"commits": commit_oids}
            if filter_paths:
                json_data["filter_paths"] = filter_paths
            response = self._post("/repo/%s/commits" % path, json=json_data)
        except requests.RequestException as e:
            raise GitRepositoryScanFault(
                "Failed to get commit details from Git repository: %s" % str(e)
            )
        if filter_paths:
            for commit in response:
                blobs = commit.get("blobs", {})
                for path, encoded_blob in blobs.items():
                    blobs[path] = self._decodeBlob(encoded_blob)
        return response

    def getLog(self, path, start, limit=None, stop=None, logger=None):
        """See `IGitHostingClient`."""
        try:
            if logger is not None:
                logger.info(
                    "Requesting commit log for %s: "
                    "start %s, limit %s, stop %s" % (path, start, limit, stop)
                )
            return self._get(
                "/repo/%s/log/%s" % (path, quote(start)),
                params={"limit": limit, "stop": stop},
            )
        except requests.RequestException as e:
            raise GitRepositoryScanFault(
                "Failed to get commit log from Git repository: %s" % str(e)
            )

    def getDiff(
        self,
        path,
        old,
        new,
        common_ancestor=False,
        context_lines=None,
        logger=None,
    ):
        """See `IGitHostingClient`."""
        try:
            if logger is not None:
                logger.info(
                    "Requesting diff for %s from %s to %s" % (path, old, new)
                )
            separator = "..." if common_ancestor else ".."
            url = "/repo/%s/compare/%s%s%s" % (
                path,
                quote(old),
                separator,
                quote(new),
            )
            return self._get(url, params={"context_lines": context_lines})
        except requests.RequestException as e:
            raise GitRepositoryScanFault(
                "Failed to get diff from Git repository: %s" % str(e)
            )

    def getDiffStats(
        self,
        path,
        old,
        new,
        common_ancestor=False,
        logger=None,
    ):
        """See `IGitHostingClient`."""
        try:
            # Diff with empty tree case
            if old is None:
                old = ""

            # New commit cannot be None
            if new == "" or new is None:
                raise GitRepositoryScanFault("'new' commit cannot be None")

            if logger is not None:
                logger.info(
                    "Requesting diff for %s from %s to %s" % (path, old, new)
                )

            separator = "..." if common_ancestor else ".."
            url = "/repo/%s/compare/%s%s%s/stats" % (
                path,
                quote(old),
                separator,
                quote(new),
            )
            return self._get(url)
        except requests.RequestException as e:
            raise GitRepositoryScanFault(
                "Failed to get diff from Git repository: %s" % str(e)
            )

    def getMergeDiff(self, path, base, head, prerequisite=None, logger=None):
        """See `IGitHostingClient`."""
        try:
            if logger is not None:
                logger.info(
                    "Requesting merge diff for %s from %s to %s"
                    % (path, base, head)
                )
            url = "/repo/%s/compare-merge/%s:%s" % (
                path,
                quote(base),
                quote(head),
            )
            return self._get(url, params={"sha1_prerequisite": prerequisite})
        except requests.RequestException as e:
            raise GitRepositoryScanFault(
                "Failed to get merge diff from Git repository: %s" % str(e)
            )

    def requestMerge(
        self,
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
        """See `IGitHostingClient`."""

        if logger is not None:
            logger.info(
                "Requesting merge for %s from %s to %s" % repo,
                quote(source_branch),
                quote(target_branch),
            )

        if source_repo:
            repo = f"{repo}:{source_repo}"

        url = "/repo/%s/request-merge/%s:%s" % (
            repo,
            quote(target_branch),
            quote(source_branch),
        )

        json_data = {
            "committer_name": commiter.display_name,
            "committer_email": commiter.preferredemail.email,
            "commit_message": commit_message,
            "target_commit_sha1": target_commit_sha1,
            "source_commit_sha1": source_commit_sha1,
        }

        if logger is not None:
            logger.info("Sending request to turnip '%s'" % url)

        try:
            return self._post(url, json=json_data)
        except requests.RequestException as e:
            if e.response is not None and e.response.status_code == 404:
                raise GitRepositoryScanFault("Repository or branch not found")
            raise GitRepositoryScanFault(
                "Failed to merge from Git repository: %s" % str(e)
            )

    def merge(
        self,
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
        """See `IGitHostingClient`."""

        if logger is not None:
            logger.info(
                "Requesting merge for %s from %s to %s" % repo,
                quote(source_branch),
                quote(target_branch),
            )

        if source_repo:
            repo = f"{repo}:{source_repo}"

        url = "/repo/%s/merge/%s:%s" % (
            repo,
            quote(target_branch),
            quote(source_branch),
        )

        json_data = {
            "committer_name": commiter.display_name,
            "committer_email": commiter.preferredemail.email,
            "commit_message": commit_message,
            "target_commit_sha1": target_commit_sha1,
            "source_commit_sha1": source_commit_sha1,
        }

        if logger is not None:
            logger.info("Sending request to turnip '%s'" % url)

        try:
            return self._post(url, json=json_data)
        except requests.RequestException as e:
            if e.response is not None:
                if e.response.status_code == 404:
                    raise GitRepositoryScanFault(
                        "Repository or branch not found"
                    )
                elif e.response.status_code == 409:
                    raise BranchMergeProposalConflicts(
                        "Merge conflict detected"
                    )
            raise GitRepositoryScanFault(
                "Failed to merge from Git repository: %s" % str(e)
            )

    def detectMerges(
        self, path, target, sources, previous_target=None, logger=None
    ):
        """See `IGitHostingClient`."""
        sources = list(sources)
        stop = [] if previous_target is None else [previous_target]
        try:
            if logger is not None:
                logger.info(
                    "Detecting merges for %s from %s to %s%s"
                    % (
                        path,
                        sources,
                        target,
                        (
                            ""
                            if previous_target is None
                            else " (stopping at %s)" % previous_target
                        ),
                    )
                )
            return self._post(
                "/repo/%s/detect-merges/%s" % (path, quote(target)),
                json={"sources": sources, "stop": stop},
            )
        except requests.RequestException as e:
            raise GitRepositoryScanFault(
                "Failed to detect merges in Git repository: %s" % str(e)
            )

    def delete(self, path, logger=None):
        """See `IGitHostingClient`."""
        try:
            if logger is not None:
                logger.info("Deleting repository %s" % path)
            self._delete("/repo/%s" % path)
        except requests.RequestException as e:
            raise GitRepositoryDeletionFault(
                "Failed to delete Git repository: %s" % str(e)
            )

    def getBlob(self, path, filename, rev=None, logger=None):
        """See `IGitHostingClient`."""
        try:
            if logger is not None:
                logger.info(
                    "Fetching file %s from repository %s" % (filename, path)
                )
            url = "/repo/%s/blob/%s" % (path, quote(filename))
            response = self._get(url, params={"rev": rev})
        except requests.RequestException as e:
            if (
                e.response is not None
                and e.response.status_code == requests.codes.NOT_FOUND
            ):
                raise GitRepositoryBlobNotFound(path, filename, rev=rev)
            else:
                raise GitRepositoryScanFault(
                    "Failed to get file from Git repository: %s" % str(e)
                )
        return self._decodeBlob(response)

    def copyRefs(self, path, operations, logger=None):
        """See `IGitHostingClient`."""
        json_data = {
            "operations": [
                {
                    "from": i.source_ref,
                    "to": {"repo": i.target_repo, "ref": i.target_ref},
                }
                for i in operations
            ]
        }
        try:
            if logger is not None:
                logger.info(
                    "Copying refs from %s to %s targets"
                    % (path, len(operations))
                )
            url = "/repo/%s/refs-copy" % path
            self._post(url, json=json_data)
        except requests.RequestException as e:
            if (
                e.response is not None
                and e.response.status_code == requests.codes.NOT_FOUND
            ):
                raise GitTargetError(
                    "Could not find repository %s or one of its refs"
                    % ensure_text(path)
                )
            else:
                raise GitRepositoryScanFault(
                    "Could not copy refs: HTTP %s" % e.response.status_code
                )

    def deleteRefs(self, refs, logger=None):
        """See `IGitHostingClient`."""
        for path, ref in refs:
            try:
                if logger is not None:
                    logger.info("Delete from repo %s the ref %s" % (path, ref))
                url = "/repo/%s/%s" % (path, ref)
                self._delete(url)
            except requests.RequestException as e:
                raise GitReferenceDeletionFault(
                    "Error deleting %s from repo %s: HTTP %s"
                    % (ref, path, e.response.status_code)
                )

    def repackRepository(self, path, logger=None, no_reuse_delta=False):
        """See `IGitHostingClient`."""

        url = "/repo/%s/repack" % path
        try:
            if logger is not None:
                logger.info("Repacking repository %s" % (path))
            return self._post(url, json={"no_reuse_delta": no_reuse_delta})
        except requests.RequestException as e:
            if (
                e.response is not None
                and e.response.status_code == requests.codes.NOT_FOUND
            ):
                if logger:
                    logger.warning(
                        "Git repository %s not found." % ensure_text(path)
                    )
                return None
            else:
                raise CannotRepackRepository(
                    "Failed to repack Git repository %s: %s" % (path, str(e))
                )

    def collectGarbage(self, path, logger=None):
        """See `IGitHostingClient`."""

        url = "/repo/%s/gc" % path
        try:
            if logger is not None:
                logger.info("Running gc for repository %s" % (path))
            return self._post(url)
        except requests.RequestException as e:
            raise CannotRunGitGC(
                "Failed to run Git GC for repository %s: %s" % (path, str(e))
            )
