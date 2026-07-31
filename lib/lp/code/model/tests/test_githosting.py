# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Unit tests for `GitHostingClient`.

We don't currently do integration testing against a real hosting service,
but we at least check that we're sending the right requests.
"""

import base64
import json
import re
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlsplit

import responses
from lazr.restful.utils import get_current_browser_request
from testtools.matchers import (
    AfterPreprocessing,
    Equals,
    MatchesListwise,
    MatchesStructure,
)
from zope.component import getUtility
from zope.interface import implementer
from zope.security.proxy import removeSecurityProxy

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
from lp.code.model.githosting import RefCopyOperation
from lp.code.tests.helpers import GitHostingFixture
from lp.services.job.interfaces.job import IRunnableJob, JobStatus
from lp.services.job.model.job import Job
from lp.services.job.runner import BaseRunnableJob, JobRunner
from lp.services.timeline.requesttimeline import get_request_timeline
from lp.services.timeout import (
    get_default_timeout_function,
    set_default_timeout_function,
)
from lp.services.webapp.url import urlappend
from lp.testing import TestCase, TestCaseWithFactory
from lp.testing.layers import ZopelessDatabaseLayer


class MatchesURL(AfterPreprocessing):
    """Matches a URL, disregarding the order of query string parameters."""

    def __init__(self, url):
        split_url = urlsplit(url)
        query_matcher = AfterPreprocessing(
            lambda qs: sorted(parse_qsl(qs)),
            MatchesListwise(
                [Equals(pair) for pair in sorted(parse_qsl(split_url.query))]
            ),
        )
        super().__init__(
            urlsplit,
            MatchesStructure(
                scheme=Equals(split_url.scheme),
                netloc=Equals(split_url.netloc),
                path=Equals(split_url.path),
                query=query_matcher,
                fragment=Equals(split_url.fragment),
            ),
        )


class TestGitHostingClient(TestCase):
    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()
        self.client = getUtility(IGitHostingClient)
        self.endpoint = removeSecurityProxy(self.client).endpoint
        self.requests = []

    @contextmanager
    def mockRequests(self, method, set_default_timeout=True, **kwargs):
        with responses.RequestsMock() as requests_mock:
            requests_mock.add(method, re.compile(r".*"), **kwargs)
            original_timeout_function = get_default_timeout_function()
            if set_default_timeout:
                set_default_timeout_function(lambda: 60.0)
            try:
                yield
            finally:
                set_default_timeout_function(original_timeout_function)
            self.requests = [call.request for call in requests_mock.calls]

    def assertRequest(self, url_suffix, json_data=None, method=None, **kwargs):
        [request] = self.requests
        self.assertThat(
            request,
            MatchesStructure(
                url=MatchesURL(urlappend(self.endpoint, url_suffix)),
                method=Equals(method),
                **{key: Equals(value) for key, value in kwargs.items()},
            ),
        )
        if json_data is not None:
            self.assertEqual(json_data, json.loads(request.body))
        timeline = get_request_timeline(get_current_browser_request())
        action = timeline.actions[-1]
        self.assertEqual("git-hosting-%s" % method.lower(), action.category)
        self.assertEqual(
            "/" + url_suffix.split("?", 1)[0], action.detail.split(" ", 1)[0]
        )

    def test_create(self):
        with self.mockRequests("POST"):
            self.client.create("123")
        self.assertRequest(
            "repo", method="POST", json_data={"repo_path": "123"}
        )

    def test_create_clone_from(self):
        with self.mockRequests("POST"):
            self.client.create("123", clone_from="122")
        self.assertRequest(
            "repo",
            method="POST",
            json_data={"repo_path": "123", "clone_from": "122"},
        )

    def test_create_async(self):
        with self.mockRequests("POST"):
            self.client.create("123", clone_from="122", async_create=True)
        self.assertRequest(
            "repo",
            method="POST",
            json_data={
                "repo_path": "123",
                "clone_from": "122",
                "async": True,
                "clone_refs": True,
            },
        )

    def test_create_failure(self):
        with self.mockRequests("POST", status=400):
            self.assertRaisesWithContent(
                GitRepositoryCreationFault,
                "Failed to create Git repository: "
                "400 Client Error: Bad Request",
                self.client.create,
                "123",
            )

    def test_getProperties(self):
        with self.mockRequests(
            "GET", json={"default_branch": "refs/heads/master"}
        ):
            props = self.client.getProperties("123")
        self.assertEqual({"default_branch": "refs/heads/master"}, props)
        self.assertRequest("repo/123", method="GET")

    def test_getProperties_failure(self):
        with self.mockRequests("GET", status=400):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get properties of Git repository: "
                "400 Client Error: Bad Request",
                self.client.getProperties,
                "123",
            )

    def test_setProperties(self):
        with self.mockRequests("PATCH"):
            self.client.setProperties("123", default_branch="refs/heads/a")
        self.assertRequest(
            "repo/123",
            method="PATCH",
            json_data={"default_branch": "refs/heads/a"},
        )

    def test_setProperties_failure(self):
        with self.mockRequests("PATCH", status=400):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to set properties of Git repository: "
                "400 Client Error: Bad Request",
                self.client.setProperties,
                "123",
                default_branch="refs/heads/a",
            )

    def test_getRefs(self):
        with self.mockRequests("GET", json={"refs/heads/master": {}}):
            refs = self.client.getRefs("123")
        self.assertEqual({"refs/heads/master": {}}, refs)
        self.assertRequest("repo/123/refs", method="GET")

    def test_getRefs_exclude_prefixes(self):
        with self.mockRequests("GET", json={"refs/heads/master": {}}):
            refs = self.client.getRefs(
                "123", exclude_prefixes=["refs/changes/", "refs/pull/"]
            )
        self.assertEqual({"refs/heads/master": {}}, refs)
        self.assertRequest(
            "repo/123/refs"
            "?exclude_prefix=refs%2Fchanges%2F&exclude_prefix=refs%2Fpull%2F",
            method="GET",
        )

    def test_getRefs_failure(self):
        with self.mockRequests("GET", status=400):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get refs from Git repository: "
                "400 Client Error: Bad Request",
                self.client.getRefs,
                "123",
            )

    def test_getCommits(self):
        with self.mockRequests("POST", json=[{"sha1": "0"}]):
            commits = self.client.getCommits("123", ["0"])
        self.assertEqual([{"sha1": "0"}], commits)
        self.assertRequest(
            "repo/123/commits", method="POST", json_data={"commits": ["0"]}
        )

    def test_getCommits_filter_paths(self):
        commit_json = {
            "sha1": "0",
            "blobs": {
                ".launchpad.yaml": b"foo",
                "debian/.launchpad.yaml": b"bar",
            },
        }
        encoded_commit_json = {
            "sha1": "0",
            "blobs": {
                ".launchpad.yaml": {"size": 3, "data": "Zm9v"},
                "debian/.launchpad.yaml": {"size": 3, "data": "YmFy"},
            },
        }
        with self.mockRequests("POST", json=[encoded_commit_json]):
            commits = self.client.getCommits(
                "123",
                ["0"],
                filter_paths=[".launchpad.yaml", "debian/.launchpad.yaml"],
            )
        self.assertEqual([commit_json], commits)
        self.assertRequest(
            "repo/123/commits",
            method="POST",
            json_data={
                "commits": ["0"],
                "filter_paths": [".launchpad.yaml", "debian/.launchpad.yaml"],
            },
        )

    def test_getCommits_filter_paths_no_data(self):
        commit_json = {"sha1": "0", "blobs": {".launchpad.yaml": {"size": 1}}}
        with self.mockRequests("POST", json=[commit_json]):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get file from Git repository: 'data'",
                self.client.getCommits,
                "123",
                ["0"],
                filter_paths=[".launchpad.yaml"],
            )

    def test_getCommits_filter_paths_no_size(self):
        commit_json = {
            "sha1": "0",
            "blobs": {".launchpad.yaml": {"data": "data"}},
        }
        with self.mockRequests("POST", json=[commit_json]):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get file from Git repository: 'size'",
                self.client.getCommits,
                "123",
                ["0"],
                filter_paths=[".launchpad.yaml"],
            )

    def test_getCommits_filter_paths_bad_encoding(self):
        commit_json = {
            "sha1": "0",
            "blobs": {".launchpad.yaml": {"data": "xy", "size": 2}},
        }
        with self.mockRequests("POST", json=[commit_json]):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get file from Git repository: Incorrect padding",
                self.client.getCommits,
                "123",
                ["0"],
                filter_paths=[".launchpad.yaml"],
            )

    def test_getCommits_filter_paths_wrong_size(self):
        blob = b"".join(bytes((i,)) for i in range(256))
        commit_json = {
            "sha1": "0",
            "blobs": {
                ".launchpad.yaml": {
                    "data": base64.b64encode(blob).decode(),
                    "size": 0,
                },
            },
        }
        with self.mockRequests("POST", json=[commit_json]):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get file from Git repository: Unexpected size"
                " (256 vs 0)",
                self.client.getCommits,
                "123",
                ["0"],
                filter_paths=[".launchpad.yaml"],
            )

    def test_getCommits_failure(self):
        with self.mockRequests("POST", status=400):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get commit details from Git repository: "
                "400 Client Error: Bad Request",
                self.client.getCommits,
                "123",
                ["0"],
            )

    def test_getLog(self):
        with self.mockRequests("GET", json=[{"sha1": "0"}]):
            log = self.client.getLog("123", "refs/heads/master")
        self.assertEqual([{"sha1": "0"}], log)
        self.assertRequest("repo/123/log/refs/heads/master", method="GET")

    def test_getLog_limit_stop(self):
        with self.mockRequests("GET", json=[{"sha1": "0"}]):
            log = self.client.getLog(
                "123", "refs/heads/master", limit=10, stop="refs/heads/old"
            )
        self.assertEqual([{"sha1": "0"}], log)
        self.assertRequest(
            "repo/123/log/refs/heads/master?limit=10&stop=refs%2Fheads%2Fold",
            method="GET",
        )

    def test_getLog_failure(self):
        with self.mockRequests("GET", status=400):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get commit log from Git repository: "
                "400 Client Error: Bad Request",
                self.client.getLog,
                "123",
                "refs/heads/master",
            )

    def test_getDiff(self):
        with self.mockRequests("GET", json={"patch": ""}):
            diff = self.client.getDiff("123", "a", "b")
        self.assertEqual({"patch": ""}, diff)
        self.assertRequest("repo/123/compare/a..b", method="GET")

    def test_getDiff_common_ancestor(self):
        with self.mockRequests("GET", json={"patch": ""}):
            diff = self.client.getDiff("123", "a", "b", common_ancestor=True)
        self.assertEqual({"patch": ""}, diff)
        self.assertRequest("repo/123/compare/a...b", method="GET")

    def test_getDiff_context_lines(self):
        with self.mockRequests("GET", json={"patch": ""}):
            diff = self.client.getDiff("123", "a", "b", context_lines=4)
        self.assertEqual({"patch": ""}, diff)
        self.assertRequest(
            "repo/123/compare/a..b?context_lines=4", method="GET"
        )

    def test_getDiff_failure(self):
        with self.mockRequests("GET", status=400):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get diff from Git repository: "
                "400 Client Error: Bad Request",
                self.client.getDiff,
                "123",
                "a",
                "b",
            )

    def test_getDiffStats(self):
        with self.mockRequests("GET", json={"files": {}}):
            stats = self.client.getDiffStats("123", "a", "b")
        self.assertEqual({"files": {}}, stats)
        self.assertRequest("repo/123/compare/a..b/stats", method="GET")

    def test_getDiffStats_common_ancestor(self):
        with self.mockRequests("GET", json={"files": {}}):
            stats = self.client.getDiffStats(
                "123", "a", "b", common_ancestor=True
            )
        self.assertEqual({"files": {}}, stats)
        self.assertRequest("repo/123/compare/a...b/stats", method="GET")

    def test_getDiffStats_empty_tree(self):
        with self.mockRequests("GET", json={"files": {}}):
            stats = self.client.getDiffStats("123", None, "b")
        self.assertEqual({"files": {}}, stats)
        self.assertRequest("repo/123/compare/..b/stats", method="GET")

    def test_getDiffStats_no_new_commit(self):
        self.assertRaisesWithContent(
            GitRepositoryScanFault,
            "'new' commit cannot be None",
            self.client.getDiffStats,
            "123",
            "a",
            None,
        )

    def test_getDiffStats_failure(self):
        with self.mockRequests("GET", status=400):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get diff from Git repository: "
                "400 Client Error: Bad Request",
                self.client.getDiffStats,
                "123",
                "a",
                "b",
            )

    def test_getMergeDiff(self):
        with self.mockRequests("GET", json={"patch": ""}):
            diff = self.client.getMergeDiff("123", "a", "b")
        self.assertEqual({"patch": ""}, diff)
        self.assertRequest("repo/123/compare-merge/a:b", method="GET")

    def test_getMergeDiff_prerequisite(self):
        with self.mockRequests("GET", json={"patch": ""}):
            diff = self.client.getMergeDiff("123", "a", "b", prerequisite="c")
        self.assertEqual({"patch": ""}, diff)
        self.assertRequest(
            "repo/123/compare-merge/a:b?sha1_prerequisite=c", method="GET"
        )

    def test_getMergeDiff_unpaired_surrogate(self):
        # pygit2 tries to decode the diff as UTF-8 with errors="replace".
        # In some cases this can result in unpaired surrogates, which older
        # versions of json/simplejson don't like.
        # This is u"卷。".encode("GBK").decode("UTF-8", errors="replace") on
        # Python 2.  Python 3 decodes this differently, but we don't mind
        # how we get there as long as the input contains an unpaired
        # surrogate.
        body = json.dumps({"patch": "\uFFFD\uD863"})
        with self.mockRequests("GET", body=body):
            diff = self.client.getMergeDiff("123", "a", "b")
        self.assertEqual({"patch": "\uFFFD\uD863"}, diff)
        self.assertRequest("repo/123/compare-merge/a:b", method="GET")

    def test_getMergeDiff_failure(self):
        with self.mockRequests("GET", status=400):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get merge diff from Git repository: "
                "400 Client Error: Bad Request",
                self.client.getMergeDiff,
                "123",
                "a",
                "b",
            )

    def test_detectMerges(self):
        with self.mockRequests("POST", json={"b": "0"}):
            merges = self.client.detectMerges("123", "a", ["b", "c"])
        self.assertEqual({"b": "0"}, merges)
        self.assertRequest(
            "repo/123/detect-merges/a",
            method="POST",
            json_data={"sources": ["b", "c"], "stop": []},
        )

    def test_detectMerges_previous_target(self):
        with self.mockRequests("POST", json={"b": "0"}):
            merges = self.client.detectMerges(
                "123", "a", ["b", "c"], previous_target="d"
            )
        self.assertEqual({"b": "0"}, merges)
        self.assertRequest(
            "repo/123/detect-merges/a",
            method="POST",
            json_data={"sources": ["b", "c"], "stop": ["d"]},
        )

    def test_detectMerges_failure(self):
        with self.mockRequests("POST", status=400):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to detect merges in Git repository: "
                "400 Client Error: Bad Request",
                self.client.detectMerges,
                "123",
                "a",
                ["b", "c"],
            )

    def test_delete(self):
        with self.mockRequests("DELETE"):
            self.client.delete("123")
        self.assertRequest("repo/123", method="DELETE")

    def test_delete_failed(self):
        with self.mockRequests("DELETE", status=400):
            self.assertRaisesWithContent(
                GitRepositoryDeletionFault,
                "Failed to delete Git repository: "
                "400 Client Error: Bad Request",
                self.client.delete,
                "123",
            )

    def test_getBlob(self):
        blob = b"".join(bytes((i,)) for i in range(256))
        payload = {
            "data": base64.b64encode(blob).decode("UTF-8"),
            "size": len(blob),
        }
        with self.mockRequests("GET", json=payload):
            response = self.client.getBlob("123", "dir/path/file/name")
        self.assertEqual(blob, response)
        self.assertRequest("repo/123/blob/dir/path/file/name", method="GET")

    def test_getBlob_revision(self):
        blob = b"".join(bytes((i,)) for i in range(256))
        payload = {
            "data": base64.b64encode(blob).decode("UTF-8"),
            "size": len(blob),
        }
        with self.mockRequests("GET", json=payload):
            response = self.client.getBlob("123", "dir/path/file/name", "dev")
        self.assertEqual(blob, response)
        self.assertRequest(
            "repo/123/blob/dir/path/file/name?rev=dev", method="GET"
        )

    def test_getBlob_not_found(self):
        with self.mockRequests("GET", status=404):
            self.assertRaisesWithContent(
                GitRepositoryBlobNotFound,
                "Repository 123 has no file dir/path/file/name",
                self.client.getBlob,
                "123",
                "dir/path/file/name",
            )

    def test_getBlob_revision_not_found(self):
        with self.mockRequests("GET", status=404):
            self.assertRaisesWithContent(
                GitRepositoryBlobNotFound,
                "Repository 123 has no file dir/path/file/name "
                "at revision dev",
                self.client.getBlob,
                "123",
                "dir/path/file/name",
                "dev",
            )

    def test_getBlob_failure(self):
        with self.mockRequests("GET", status=400):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get file from Git repository: "
                "400 Client Error: Bad Request",
                self.client.getBlob,
                "123",
                "dir/path/file/name",
            )

    def test_getBlob_url_quoting(self):
        blob = b"".join(bytes((i,)) for i in range(256))
        payload = {
            "data": base64.b64encode(blob).decode("UTF-8"),
            "size": len(blob),
        }
        with self.mockRequests("GET", json=payload):
            self.client.getBlob("123", "dir/+file name?.txt", "+rev/ no?")
        self.assertRequest(
            "repo/123/blob/dir/%2Bfile%20name%3F.txt?rev=%2Brev%2F+no%3F",
            method="GET",
        )

    def test_getBlob_no_data(self):
        with self.mockRequests("GET", json={"size": 1}):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get file from Git repository: 'data'",
                self.client.getBlob,
                "123",
                "dir/path/file/name",
            )

    def test_getBlob_no_size(self):
        with self.mockRequests("GET", json={"data": "data"}):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get file from Git repository: 'size'",
                self.client.getBlob,
                "123",
                "dir/path/file/name",
            )

    def test_getBlob_bad_encoding(self):
        with self.mockRequests("GET", json={"data": "xy", "size": 2}):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get file from Git repository: Incorrect padding",
                self.client.getBlob,
                "123",
                "dir/path/file/name",
            )

    def test_getBlob_wrong_size(self):
        blob = b"".join(bytes((i,)) for i in range(256))
        payload = {"data": base64.b64encode(blob).decode("UTF-8"), "size": 0}
        with self.mockRequests("GET", json=payload):
            self.assertRaisesWithContent(
                GitRepositoryScanFault,
                "Failed to get file from Git repository: Unexpected size"
                " (256 vs 0)",
                self.client.getBlob,
                "123",
                "dir/path/file/name",
            )

    def getCopyRefOperations(self):
        return [
            RefCopyOperation("1a2b3c4", "999", "refs/merge/123"),
            RefCopyOperation("9a8b7c6", "666", "refs/merge/989"),
        ]

    def test_copyRefs(self):
        with self.mockRequests("POST", status=202):
            self.client.copyRefs("123", self.getCopyRefOperations())
        self.assertRequest(
            "repo/123/refs-copy",
            {
                "operations": [
                    {
                        "from": "1a2b3c4",
                        "to": {"repo": "999", "ref": "refs/merge/123"},
                    },
                    {
                        "from": "9a8b7c6",
                        "to": {"repo": "666", "ref": "refs/merge/989"},
                    },
                ]
            },
            "POST",
        )

    def test_copyRefs_refs_not_found(self):
        with self.mockRequests("POST", status=404):
            self.assertRaisesWithContent(
                GitTargetError,
                "Could not find repository 123 or one of its refs",
                self.client.copyRefs,
                "123",
                self.getCopyRefOperations(),
            )

    def test_deleteRef(self):
        with self.mockRequests("DELETE", status=200):
            self.client.deleteRefs([("123", "refs/merge/123")])
        self.assertRequest("repo/123/refs/merge/123", method="DELETE")

    def test_deleteRef_refs_request_error(self):
        with self.mockRequests("DELETE", status=500):
            self.assertRaisesWithContent(
                GitReferenceDeletionFault,
                "Error deleting refs/merge/123 from repo 123: HTTP 500",
                self.client.deleteRefs,
                [("123", "refs/merge/123")],
            )

    def test_works_in_job(self):
        # `GitHostingClient` is usable from a running job.
        @implementer(IRunnableJob)
        class GetRefsJob(BaseRunnableJob):
            def __init__(self, testcase):
                super().__init__()
                self.job = Job()
                self.testcase = testcase

            def run(self):
                with self.testcase.mockRequests(
                    "GET",
                    json={"refs/heads/master": {}},
                    set_default_timeout=False,
                ):
                    self.refs = self.testcase.client.getRefs("123")
                # We must make this assertion inside the job, since the job
                # runner creates a separate timeline.
                self.testcase.assertRequest("repo/123/refs", method="GET")

        job = GetRefsJob(self)
        JobRunner([job]).runAll()
        self.assertEqual(JobStatus.COMPLETED, job.job.status)
        self.assertEqual({"refs/heads/master": {}}, job.refs)

    def test_repack(self):
        with self.mockRequests("POST", status=200):
            repack = self.client.repackRepository("/repo/123")
        self.assertEqual(None, repack)
        [request] = self.requests
        self.assertEqual({"no_reuse_delta": False}, json.loads(request.body))

    def test_repack_no_reuse_delta(self):
        with self.mockRequests("POST", status=200):
            repack = self.client.repackRepository(
                "/repo/123", no_reuse_delta=True
            )
        self.assertEqual(None, repack)
        [request] = self.requests
        self.assertEqual({"no_reuse_delta": True}, json.loads(request.body))

    def test_repack_failure(self):
        with self.mockRequests("POST", status=400):
            self.assertRaisesWithContent(
                CannotRepackRepository,
                "Failed to repack Git repository /repo/123: "
                "400 Client Error: Bad Request",
                self.client.repackRepository,
                "/repo/123",
            )

    def test_repack_failure_404(self):
        with self.mockRequests("POST", status=404):
            self.assertIsNone(self.client.repackRepository("/repo/123"))

    def test_git_gc(self):
        with self.mockRequests("POST", status=200):
            gc = self.client.collectGarbage("/repo/123")
        self.assertEqual(None, gc)

    def test_git_gc_failure(self):
        with self.mockRequests("POST", status=400):
            self.assertRaisesWithContent(
                CannotRunGitGC,
                "Failed to run Git GC for repository /repo/123: "
                "400 Client Error: Bad Request",
                self.client.collectGarbage,
                "/repo/123",
            )


class TestGitHostingClientMerge(TestCaseWithFactory):
    """Test the merge method of GitHostingClient."""

    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()
        self.client = getUtility(IGitHostingClient)
        self.requests = []

    @contextmanager
    def mockRequests(self, method, set_default_timeout=True, **kwargs):
        with responses.RequestsMock() as requests_mock:
            requests_mock.add(method, re.compile(r".*"), **kwargs)
            original_timeout_function = get_default_timeout_function()
            if set_default_timeout:
                set_default_timeout_function(lambda: 60.0)
            try:
                yield
            finally:
                set_default_timeout_function(original_timeout_function)
            self.requests = [call.request for call in requests_mock.calls]

    def test_merge_success(self):
        """Test successful merge"""
        person = self.factory.makePerson()

        with self.mockRequests("POST", json={"merge_commit": "dummy commit"}):
            response = self.client.merge(
                1,  # repository id
                "target-branch",
                "target_commit_sha1",
                "source-branch",
                "source_commit_sha1",
                person,
                "Test commit message",
            )

        self.assertIn("merge_commit", response)
        self.assertEqual(
            "http://git.launchpad.test:19417/repo/1/merge/"
            "target-branch:source-branch",
            self.requests[0].url,
        )

    def test_cross_repo_merge_success(self):
        """Test successful merge for cross repo branches"""
        person = self.factory.makePerson()

        with self.mockRequests("POST", json={"merge_commit": "dummy commit"}):
            response = self.client.merge(
                1,  # repository id
                "target-branch",
                "target_commit_sha1",
                "source-branch",
                "source_commit_sha1",
                person,
                "Test commit message",
                source_repo=2,  # source repository id
            )

        self.assertIn("merge_commit", response)
        self.assertEqual(
            "http://git.launchpad.test:19417/repo/1:2/merge/"
            "target-branch:source-branch",
            self.requests[0].url,
        )

    def test_merge_conflict(self):
        """Test merge with conflicts"""

        repository = self.factory.makeGitRepository()
        person = self.factory.makePerson()

        with self.mockRequests("POST", status=409):
            self.assertRaises(
                BranchMergeProposalConflicts,
                self.client.merge,
                repository.id,
                "target-branch",
                "target_commit_sha1",
                "source-branch",
                "source_commit_sha1",
                person,
                "Test commit message",
            )

    def test_merge_not_found(self):
        """Test merge with non-existent repository"""

        person = self.factory.makePerson()
        with self.mockRequests("POST", status=404):
            self.assertRaises(
                GitRepositoryScanFault,
                self.client.merge,
                "non-existent",
                "target-branch",
                "target_commit_sha1",
                "source-branch",
                "source_commit_sha1",
                person,
                "Test commit message",
            )

    def test_merge_network_error(self):
        """Test merge with network error"""
        self.hosting_fixture = self.useFixture(GitHostingFixture())
        self.hosting_fixture.merge.failure = GitRepositoryScanFault(
            "Network error"
        )
        self.client = getUtility(IGitHostingClient)

        repository = self.factory.makeGitRepository()
        person = self.factory.makePerson()

        self.assertRaises(
            GitRepositoryScanFault,
            self.client.merge,
            repository.id,
            "target-branch",
            "target_commit_sha1",
            "source-branch",
            "source_commit_sha1",
            person,
            "Test commit message",
        )


class TestGitHostingClientRequestMerge(TestCaseWithFactory):
    """Test the request-merge method of GitHostingClient."""

    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()
        self.client = getUtility(IGitHostingClient)
        self.requests = []

    @contextmanager
    def mockRequests(self, method, set_default_timeout=True, **kwargs):
        with responses.RequestsMock() as requests_mock:
            requests_mock.add(method, re.compile(r".*"), **kwargs)
            original_timeout_function = get_default_timeout_function()
            if set_default_timeout:
                set_default_timeout_function(lambda: 60.0)
            try:
                yield
            finally:
                set_default_timeout_function(original_timeout_function)
            self.requests = [call.request for call in requests_mock.calls]

    def test_requestMerge_success(self):
        """Test successful requestMerge"""
        person = self.factory.makePerson()

        with self.mockRequests(
            "POST", json={"queued": True, "already_merged": False}
        ):
            response = self.client.requestMerge(
                1,  # repository id
                "target-branch",
                "target_commit_sha1",
                "source-branch",
                "source_commit_sha1",
                person,
                "Test commit message",
            )

        self.assertTrue(response["queued"])
        self.assertEqual(
            "http://git.launchpad.test:19417/repo/1/request-merge/"
            "target-branch:source-branch",
            self.requests[0].url,
        )

    def test_request_cross_repo_merge_success(self):
        """Test successful requestMerge for cross repo branches"""
        person = self.factory.makePerson()

        with self.mockRequests(
            "POST", json={"queued": True, "already_merged": False}
        ):
            response = self.client.requestMerge(
                1,  # repository id
                "target-branch",
                "target_commit_sha1",
                "source-branch",
                "source_commit_sha1",
                person,
                "Test commit message",
                source_repo=2,  # source repository id
            )

        self.assertTrue(response["queued"])
        self.assertEqual(
            "http://git.launchpad.test:19417/repo/1:2/request-merge/"
            "target-branch:source-branch",
            self.requests[0].url,
        )

    def test_requestMerge_not_found(self):
        """Test requestMerge with non-existent repository"""

        person = self.factory.makePerson()
        with self.mockRequests("POST", status=404):
            self.assertRaises(
                GitRepositoryScanFault,
                self.client.requestMerge,
                "non-existent",
                "target-branch",
                "target_commit_sha1",
                "source-branch",
                "source_commit_sha1",
                person,
                "Test commit message",
            )

    def test_requestMerge_network_error(self):
        """Test requestMerge with network error"""
        self.hosting_fixture = self.useFixture(GitHostingFixture())
        self.hosting_fixture.requestMerge.failure = GitRepositoryScanFault(
            "Network error"
        )
        self.client = getUtility(IGitHostingClient)

        repository = self.factory.makeGitRepository()
        person = self.factory.makePerson()

        self.assertRaises(
            GitRepositoryScanFault,
            self.client.requestMerge,
            repository.id,
            "target-branch",
            "target_commit_sha1",
            "source-branch",
            "source_commit_sha1",
            person,
            "Test commit message",
        )
