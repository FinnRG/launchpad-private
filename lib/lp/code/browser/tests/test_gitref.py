# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Unit tests for GitRefView."""

import hashlib
import re
from datetime import datetime, timezone
from textwrap import dedent

import soupmatchers
from fixtures import FakeLogger
from storm.store import Store
from testtools.matchers import Equals, MatchesListwise, Not
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.code.enums import RevisionStatusResult
from lp.code.errors import GitRepositoryScanFault
from lp.code.interfaces.gitjob import IGitRefScanJobSource
from lp.code.interfaces.gitrepository import IGitRepositorySet
from lp.code.tests.helpers import GitHostingFixture
from lp.services.beautifulsoup import BeautifulSoup
from lp.services.job.runner import JobRunner
from lp.services.timeout import TimeoutError
from lp.services.utils import seconds_since_epoch
from lp.services.webapp.publisher import canonical_url
from lp.testing import (
    BrowserTestCase,
    StormStatementRecorder,
    TestCaseWithFactory,
    admin_logged_in,
    login_person,
    person_logged_in,
)
from lp.testing.dbuser import dbuser
from lp.testing.fakemethod import FakeMethod
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.matchers import HasQueryCount
from lp.testing.pages import (
    extract_text,
    find_main_content,
    find_tags_by_class,
)
from lp.testing.views import create_initialized_view, create_view


class TestGitRefNavigation(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_canonical_url_branch(self):
        [ref] = self.factory.makeGitRefs(paths=["refs/heads/master"])
        self.assertEqual(
            "%s/+ref/master" % canonical_url(ref.repository),
            canonical_url(ref),
        )

    def test_canonical_url_with_slash(self):
        [ref] = self.factory.makeGitRefs(paths=["refs/heads/with/slash"])
        self.assertEqual(
            "%s/+ref/with/slash" % canonical_url(ref.repository),
            canonical_url(ref),
        )

    def test_canonical_url_percent_encoded(self):
        [ref] = self.factory.makeGitRefs(paths=["refs/heads/with#hash"])
        self.assertEqual(
            "%s/+ref/with%%23hash" % canonical_url(ref.repository),
            canonical_url(ref),
        )

    def test_canonical_url_non_ascii(self):
        [ref] = self.factory.makeGitRefs(paths=["refs/heads/\N{SNOWMAN}"])
        self.assertEqual(
            "%s/+ref/%%E2%%98%%83" % canonical_url(ref.repository),
            canonical_url(ref),
        )

    def test_canonical_url_tag(self):
        [ref] = self.factory.makeGitRefs(paths=["refs/tags/1.0"])
        self.assertEqual(
            "%s/+ref/refs/tags/1.0" % canonical_url(ref.repository),
            canonical_url(ref),
        )


class MissingCommitsNote(soupmatchers.Tag):
    def __init__(self):
        super().__init__(
            "missing commits note",
            "div",
            text="Some recent commit information could not be fetched.",
        )


class TestGitRefView(BrowserTestCase):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.hosting_fixture = self.useFixture(GitHostingFixture())

    def _test_rendering(self, branch_name):
        repository = self.factory.makeGitRepository(
            owner=self.factory.makePerson(name="person"),
            target=self.factory.makeProduct(name="target"),
            name="git",
        )
        getUtility(IGitRepositorySet).setDefaultRepositoryForOwner(
            repository.owner, repository.target, repository, repository.owner
        )
        [ref] = self.factory.makeGitRefs(
            repository=repository, paths=["refs/heads/%s" % branch_name]
        )
        view = create_view(ref, "+index")
        # To test the breadcrumbs we need a correct traversal stack.
        view.request.traversed_objects = [repository, ref, view]
        view.initialize()
        breadcrumbs_tag = soupmatchers.Tag(
            "breadcrumbs", "ol", attrs={"class": "breadcrumbs"}
        )
        self.assertThat(
            view(),
            soupmatchers.HTMLContains(
                soupmatchers.Within(
                    breadcrumbs_tag,
                    soupmatchers.Tag(
                        "git collection breadcrumb",
                        "a",
                        text="Git",
                        attrs={"href": re.compile(r"/\+git$")},
                    ),
                ),
                soupmatchers.Within(
                    breadcrumbs_tag,
                    soupmatchers.Tag(
                        "repository breadcrumb",
                        "a",
                        text="lp:~person/target",
                        attrs={
                            "href": re.compile(r"/~person/target/\+git/git")
                        },
                    ),
                ),
                soupmatchers.Within(
                    breadcrumbs_tag,
                    soupmatchers.Tag(
                        "git ref breadcrumb",
                        "li",
                        text=re.compile(r"\s%s\s" % branch_name),
                    ),
                ),
            ),
        )

    def test_rendering(self):
        self._test_rendering("master")

    def test_rendering_non_ascii(self):
        self._test_rendering("\N{SNOWMAN}")

    def test_rendering_githost_failure(self):
        [ref] = self.factory.makeGitRefs(paths=["refs/heads/branch"])
        log = self.makeCommitLog()
        self.hosting_fixture.getLog.result = list(reversed(log))
        # XXX jugmac00 2022-03-14
        # This is a workaround for the limitation of `GitHostingFixture` not
        # implementing a proper `getCommits` method.
        # If we would not supply the following configuration file,
        # `CIBuild.requestBuildsForRefs`, which is unrelated to this test case,
        # would fail.
        configuration = dedent(
            """\
            pipeline: [test]
            jobs:
                test:
                    series: foo
                    architectures: ["bar"]
        """
        )
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": configuration})

        # Make code hosting fail.
        self.hosting_fixture.getLog = FakeMethod(
            failure=GitRepositoryScanFault(":-(")
        )
        with admin_logged_in():
            url = canonical_url(ref)
        # Visiting ref page should not crash, and we should be able to see
        # the error message telling us that git hosting is having problems.
        browser = self.getUserBrowser(url, ref.owner)

        error_msg = (
            "There was an error while fetching commit information from code "
            "hosting service. Please try again in a few minutes. If the "
            "problem persists, contact Launchpad support."
        )
        self.assertIn(
            error_msg, extract_text(find_main_content(browser.contents))
        )

    def test_revisionStatusReports(self):
        repository = self.factory.makeGitRepository()
        [ref] = self.factory.makeGitRefs(
            repository=repository, paths=["refs/heads/branch"]
        )
        log = self.makeCommitLog()

        # create status reports for 2 of the 5 commits available here
        report1 = self.factory.makeRevisionStatusReport(
            user=repository.owner,
            git_repository=repository,
            title="CI",
            commit_sha1=log[0]["sha1"],
            result_summary="120/120 tests passed",
            url="https://foo1.com",
            result=RevisionStatusResult.SUCCEEDED,
        )
        report2 = self.factory.makeRevisionStatusReport(
            user=repository.owner,
            git_repository=repository,
            title="Lint",
            commit_sha1=log[1]["sha1"],
            result_summary="Invalid import in test_file.py",
            url="https://foo2.com",
            result=RevisionStatusResult.FAILED,
            ci_build=self.factory.makeCIBuild(
                git_repository=repository, commit_sha1=log[1]["sha1"]
            ),
        )
        pending_report = self.factory.makeRevisionStatusReport(
            user=repository.owner,
            git_repository=repository,
            title="Build",
            commit_sha1=log[1]["sha1"],
        )

        self.hosting_fixture.getLog.result = list(log)
        # XXX jugmac00 2022-03-14
        # This is a workaround for the limitation of `GitHostingFixture` not
        # implementing a proper `getCommits` method.
        # If we would not supply the following configuration file,
        # `CIBuild.requestBuildsForRefs`, which is unrelated to this test case,
        # would fail.
        configuration = dedent(
            """\
            pipeline: [test]
            jobs:
                test:
                    series: foo
                    architectures: ["bar"]
        """
        )
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": configuration})
        view = create_initialized_view(
            ref, "+index", principal=repository.owner
        )
        with person_logged_in(repository.owner):
            contents = view.render()
        reports_section = find_tags_by_class(contents, "status-reports-table")
        with person_logged_in(repository.owner):
            self.assertThat(
                reports_section[0],
                soupmatchers.Within(
                    soupmatchers.Tag("first report title", "td"),
                    soupmatchers.Tag(
                        "first report link",
                        "a",
                        text=report1.title,
                        attrs={"href": report1.url},
                    ),
                ),
            )
            self.assertThat(
                reports_section[0],
                Not(
                    soupmatchers.Within(
                        soupmatchers.Tag("first report title", "td"),
                        soupmatchers.Tag(
                            "first report CI build link", "a", text="build"
                        ),
                    )
                ),
            )
            self.assertThat(
                reports_section[0],
                soupmatchers.Tag(
                    "first report summary", "td", text=report1.result_summary
                ),
            )
            self.assertThat(
                reports_section[1],
                soupmatchers.Within(
                    soupmatchers.Tag("second report title", "td"),
                    soupmatchers.Tag(
                        "second report link",
                        "a",
                        text=report2.title,
                        attrs={"href": report2.url},
                    ),
                ),
            )
            self.assertThat(
                reports_section[1],
                soupmatchers.Within(
                    soupmatchers.Tag("second report title", "td"),
                    soupmatchers.Tag(
                        "second report CI build link",
                        "a",
                        text="build",
                        attrs={
                            "href": canonical_url(
                                report2.ci_build, force_local_path=True
                            ),
                        },
                    ),
                ),
            )
            self.assertThat(
                reports_section[1],
                soupmatchers.Tag(
                    "second report summary", "td", text=report2.result_summary
                ),
            )
            self.assertThat(
                reports_section[1],
                soupmatchers.Within(
                    soupmatchers.Tag("pending report title", "td"),
                    soupmatchers.Tag(
                        "pending report title",
                        "span",
                        text=pending_report.title,
                    ),
                ),
            )

            # Ensure we don't display an empty expander for those commits
            # that do not have status reports created for them - means we
            # should only see 2 entries with class 'status-reports-table'
            # on the page: reports_section[0] and reports_section[1]
            self.assertEqual(2, len(reports_section))

    def test_revisionStatusReports_groups_are_not_limited_to_one_batch(self):
        [ref] = self.factory.makeGitRefs()
        log = self.makeCommitLog()
        for index in range(7):
            self.factory.makeRevisionStatusReport(
                git_repository=ref.repository,
                commit_sha1=log[0]["sha1"],
                title="check-%03d" % index,
                group_name="Other checks" if index < 5 else None,
                result=RevisionStatusResult.SUCCEEDED,
            )
        self.hosting_fixture.getLog.result = list(log)
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": ""})
        view = create_initialized_view(
            ref, "+index", principal=ref.repository.owner
        )
        with person_logged_in(ref.repository.owner):
            contents = view.render()
        [section] = find_tags_by_class(contents, "status-reports-table")
        self.assertEqual(7, len(section.find_all("tr")))
        self.assertIn("Other checks", str(section))
        self.assertIn("CI checks", str(section))

    def test_revisionStatusReports_history_is_limited(self):
        [ref] = self.factory.makeGitRefs()
        log = self.makeCommitLog()
        for index in range(8):
            self.factory.makeRevisionStatusReport(
                git_repository=ref.repository,
                commit_sha1=log[0]["sha1"],
                title="test",
                result_summary="attempt-%s" % index,
                result=RevisionStatusResult.SUCCEEDED,
            )
        self.hosting_fixture.getLog.result = list(log)
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": ""})
        view = create_initialized_view(
            ref, "+index", principal=ref.repository.owner
        )
        with person_logged_in(ref.repository.owner):
            contents = view.render()
        [section] = find_tags_by_class(contents, "status-reports-table")
        [history] = find_tags_by_class(str(section), "validation-history")
        self.assertEqual(5, len(history.find_all("tr")))
        self.assertEqual(6, len(section.find_all("tr")))
        self.assertIn(
            "Only the 5 most recent reports are shown.", extract_text(history)
        )
        self.assertIn("attempt-7", extract_text(section))
        self.assertNotIn("attempt-0", extract_text(section))
        self.assertNotIn("attempt-1", extract_text(section))
        self.assertEqual(
            ["attempt-%s" % index for index in range(6, 1, -1)],
            [
                row.find_all("td")[2].span.text
                for row in history.find_all("tr")
            ],
        )

    def test_revisionStatusReports_all_skipped(self):
        self.useFixture(FakeLogger())
        [ref] = self.factory.makeGitRefs()
        log = self.makeCommitLog()
        for _ in range(2):
            self.factory.makeRevisionStatusReport(
                user=ref.repository.owner,
                git_repository=ref.repository,
                commit_sha1=log[0]["sha1"],
                result=RevisionStatusResult.SKIPPED,
            )
        self.hosting_fixture.getLog.result = list(log)
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": ""})
        view = create_initialized_view(
            ref, "+index", principal=ref.repository.owner
        )
        with person_logged_in(ref.repository.owner):
            contents = view.render()
        reports_section = find_tags_by_class(contents, "status-reports-table")
        with person_logged_in(ref.repository.owner):
            self.assertThat(
                reports_section[0].div,
                soupmatchers.Tag(
                    "overall icon",
                    "img",
                    attrs={
                        "width": "14",
                        "height": "14",
                        "src": "/@@/yes-gray",
                        "title": "Skipped",
                    },
                ),
            )
            self.assertThat(
                reports_section[0].table.find_all("tr"),
                MatchesListwise(
                    [
                        soupmatchers.Tag(
                            "icon",
                            "img",
                            attrs={"src": "/@@/yes-gray", "title": "Skipped"},
                        )
                        for _ in range(2)
                    ]
                ),
            )

    def test_revisionStatusReports_all_succeeded_or_skipped(self):
        self.useFixture(FakeLogger())
        [ref] = self.factory.makeGitRefs()
        log = self.makeCommitLog()
        for result in (
            RevisionStatusResult.SUCCEEDED,
            RevisionStatusResult.SUCCEEDED,
            RevisionStatusResult.SKIPPED,
        ):
            self.factory.makeRevisionStatusReport(
                user=ref.repository.owner,
                git_repository=ref.repository,
                commit_sha1=log[0]["sha1"],
                result=result,
            )
        self.hosting_fixture.getLog.result = list(log)
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": ""})
        view = create_initialized_view(
            ref, "+index", principal=ref.repository.owner
        )
        with person_logged_in(ref.repository.owner):
            contents = view.render()
        reports_section = find_tags_by_class(contents, "status-reports-table")
        with person_logged_in(ref.repository.owner):
            self.assertThat(
                reports_section[0].div,
                soupmatchers.Tag(
                    "overall icon",
                    "img",
                    attrs={
                        "width": "14",
                        "height": "14",
                        "src": "/@@/yes",
                        "title": "Succeeded",
                    },
                ),
            )
            self.assertThat(
                reports_section[0].table.find_all("tr"),
                MatchesListwise(
                    [
                        soupmatchers.Tag(
                            "icon", "img", attrs={"src": src, "title": title}
                        )
                        for src, title in (
                            ("/@@/yes", "Succeeded"),
                            ("/@@/yes", "Succeeded"),
                            ("/@@/yes-gray", "Skipped"),
                        )
                    ]
                ),
            )

    def test_revisionStatusReports_failed(self):
        self.useFixture(FakeLogger())
        [ref] = self.factory.makeGitRefs()
        log = self.makeCommitLog()
        for result in (
            RevisionStatusResult.SUCCEEDED,
            RevisionStatusResult.FAILED,
        ):
            self.factory.makeRevisionStatusReport(
                user=ref.repository.owner,
                git_repository=ref.repository,
                commit_sha1=log[0]["sha1"],
                result=result,
            )
        self.hosting_fixture.getLog.result = list(log)
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": ""})
        view = create_initialized_view(
            ref, "+index", principal=ref.repository.owner
        )
        with person_logged_in(ref.repository.owner):
            contents = view.render()
        reports_section = find_tags_by_class(contents, "status-reports-table")
        with person_logged_in(ref.repository.owner):
            self.assertThat(
                reports_section[0].div,
                soupmatchers.Tag(
                    "overall icon",
                    "img",
                    attrs={
                        "width": "14",
                        "height": "14",
                        "src": "/@@/no",
                        "title": "Failed",
                    },
                ),
            )
            self.assertThat(
                reports_section[0].table.find_all("tr"),
                MatchesListwise(
                    [
                        soupmatchers.Tag(
                            "icon", "img", attrs={"src": src, "title": title}
                        )
                        for src, title in (
                            ("/@@/yes", "Succeeded"),
                            ("/@@/no", "Failed"),
                        )
                    ]
                ),
            )

    def test_revisionStatusReports_cancelled(self):
        self.useFixture(FakeLogger())
        [ref] = self.factory.makeGitRefs()
        log = self.makeCommitLog()
        for result in (
            RevisionStatusResult.SUCCEEDED,
            RevisionStatusResult.CANCELLED,
        ):
            self.factory.makeRevisionStatusReport(
                user=ref.repository.owner,
                git_repository=ref.repository,
                commit_sha1=log[0]["sha1"],
                result=result,
            )
        self.hosting_fixture.getLog.result = list(log)
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": ""})
        view = create_initialized_view(
            ref, "+index", principal=ref.repository.owner
        )
        with person_logged_in(ref.repository.owner):
            contents = view.render()
        reports_section = find_tags_by_class(contents, "status-reports-table")
        with person_logged_in(ref.repository.owner):
            self.assertThat(
                reports_section[0].div,
                soupmatchers.Tag(
                    "overall icon",
                    "img",
                    attrs={
                        "width": "14",
                        "height": "14",
                        "src": "/@@/no",
                        "title": "Failed",
                    },
                ),
            )
            self.assertThat(
                reports_section[0].table.find_all("tr"),
                MatchesListwise(
                    [
                        soupmatchers.Tag(
                            "icon", "img", attrs={"src": src, "title": title}
                        )
                        for src, title in (
                            ("/@@/yes", "Succeeded"),
                            ("/@@/build-failed", "Cancelled"),
                        )
                    ]
                ),
            )

    def test_revisionStatusReports_all_waiting_or_running(self):
        self.useFixture(FakeLogger())
        [ref] = self.factory.makeGitRefs()
        log = self.makeCommitLog()
        for result in (
            RevisionStatusResult.WAITING,
            RevisionStatusResult.RUNNING,
        ):
            self.factory.makeRevisionStatusReport(
                user=ref.repository.owner,
                git_repository=ref.repository,
                commit_sha1=log[0]["sha1"],
                result=result,
            )
        self.hosting_fixture.getLog.result = list(log)
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": ""})
        view = create_initialized_view(
            ref, "+index", principal=ref.repository.owner
        )
        with person_logged_in(ref.repository.owner):
            contents = view.render()
        reports_section = find_tags_by_class(contents, "status-reports-table")
        with person_logged_in(ref.repository.owner):
            self.assertThat(
                reports_section[0].div,
                soupmatchers.Tag(
                    "overall icon",
                    "img",
                    attrs={
                        "width": "14",
                        "height": "14",
                        "src": "/@@/processing",
                        "title": "In progress",
                    },
                ),
            )
            self.assertThat(
                reports_section[0].table.find_all("tr"),
                MatchesListwise(
                    [
                        soupmatchers.Tag(
                            "icon", "img", attrs={"src": src, "title": title}
                        )
                        for src, title in (
                            ("/@@/build-needed", "Waiting"),
                            ("/@@/processing", "Running"),
                        )
                    ]
                ),
            )

    def test_clone_instructions(self):
        [ref] = self.factory.makeGitRefs(paths=["refs/heads/branch"])
        username = ref.owner.name
        text = self.getMainText(ref, "+index", user=ref.owner)
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""
            git clone -b branch https://git.launchpad.test/.*
            git clone -b branch git\+ssh://{username}@git.launchpad.test/.*
            """.format(
                username=username
            ),
            text,
        )

    def test_push_directions_logged_in_cannot_push_individual(self):
        repo = self.factory.makeGitRepository()
        [ref] = self.factory.makeGitRefs(
            repository=repo, paths=["refs/heads/branch"]
        )
        login_person(self.user)
        view = create_initialized_view(ref, "+index", principal=self.user)
        git_push_url_text_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Push url text",
                "dt",
                text="To fork this repository and propose "
                "fixes from there, push to this repository:",
            )
        )

        git_push_url_hint_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Push url hint",
                "span",
                text=("git+ssh://%s@git.launchpad.test/" "~%s/%s")
                % (self.user.name, self.user.name, repo.target.name),
            )
        )
        with person_logged_in(self.user):
            rendered_view = view.render()
            self.assertThat(rendered_view, git_push_url_text_match)
            self.assertThat(rendered_view, git_push_url_hint_match)

    def test_push_directions_logged_in_cannot_push_individual_project(self):
        # Repository is the default for a project
        eric = self.factory.makePerson(name="eric")
        fooix = self.factory.makeProduct(name="fooix", owner=eric)
        repository = self.factory.makeGitRepository(
            owner=eric, target=fooix, name="fooix-repo"
        )
        [ref] = self.factory.makeGitRefs(
            repository=repository, paths=["refs/heads/branch"]
        )
        self.repository_set = getUtility(IGitRepositorySet)
        with person_logged_in(fooix.owner) as user:
            self.repository_set.setDefaultRepositoryForOwner(
                repository.owner, fooix, repository, user
            )
            self.repository_set.setDefaultRepository(fooix, repository)
        login_person(self.user)
        view = create_initialized_view(ref, "+index", principal=self.user)
        git_push_url_text_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Push url text",
                "dt",
                text="To fork this repository and propose "
                "fixes from there, push to this repository:",
            )
        )
        git_push_url_hint_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Push url hint",
                "span",
                text="git+ssh://%s@git.launchpad.test/~%s/%s"
                % (self.user.name, self.user.name, repository.target.name),
            )
        )

        with person_logged_in(self.user):
            rendered_view = view.render()
            self.assertThat(rendered_view, git_push_url_text_match)
            self.assertThat(rendered_view, git_push_url_hint_match)

    def test_push_directions_logged_in_cannot_push_individual_package(self):
        # Repository is the default for a package
        mint = self.factory.makeDistribution(name="mint")
        eric = self.factory.makePerson(name="eric")
        mint_choc = self.factory.makeDistributionSourcePackage(
            distribution=mint, sourcepackagename="choc"
        )
        repository = self.factory.makeGitRepository(
            owner=eric, target=mint_choc, name="choc-repo"
        )
        [ref] = self.factory.makeGitRefs(
            repository=repository, paths=["refs/heads/branch"]
        )
        dsp = repository.target
        self.repository_set = getUtility(IGitRepositorySet)
        with admin_logged_in():
            self.repository_set.setDefaultRepositoryForOwner(
                repository.owner, dsp, repository, repository.owner
            )
            self.repository_set.setDefaultRepository(dsp, repository)
        login_person(self.user)
        view = create_initialized_view(ref, "+index", principal=self.user)
        git_push_url_text_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Push url text",
                "dt",
                text="To fork this repository and propose "
                "fixes from there, push to this repository:",
            )
        )
        git_push_url_hint_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Push url hint",
                "span",
                text="git+ssh://%s@git.launchpad.test/~%s/%s/+source/%s"
                % (self.user.name, self.user.name, mint.name, mint_choc.name),
            )
        )

        with person_logged_in(self.user):
            rendered_view = view.render()
            self.assertThat(rendered_view, git_push_url_text_match)
            self.assertThat(rendered_view, git_push_url_hint_match)

    def test_push_directions_logged_in_cannot_push_personal_project(self):
        repository = self.factory.makeGitRepository(
            owner=self.user, target=self.user
        )
        [ref] = self.factory.makeGitRefs(
            repository=repository, paths=["refs/heads/branch"]
        )
        other_user = self.factory.makePerson()
        login_person(other_user)
        view = create_initialized_view(ref, "+index", principal=self.user)
        git_push_url_text_match = soupmatchers.Tag(
            "Push url text", "a", text=self.user.displayname
        )
        with person_logged_in(other_user):
            rendered_view = view.render()
            div = soupmatchers.Tag(
                "Push directions", "div", attrs={"id": "push-directions"}
            )
            self.assertThat(
                rendered_view,
                soupmatchers.HTMLContains(
                    soupmatchers.Within(div, git_push_url_text_match)
                ),
            )

    def test_merge_guidelines_personal(self):
        repository = self.factory.makeGitRepository(
            owner=self.user, target=self.user
        )
        [ref] = self.factory.makeGitRefs(
            repository=repository, paths=["refs/heads/branch"]
        )
        other_user = self.factory.makePerson()
        login_person(other_user)
        view = create_initialized_view(ref, "+index", principal=self.user)
        git_add_remote_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Git remote add text",
                "tt",
                attrs={"id": "remote-add"},
                text=(
                    "git remote add %s "
                    "git+ssh://%s@git.launchpad.test/~%s/+git/%s"
                )
                % (
                    self.user.name,
                    self.user.name,
                    self.user.name,
                    repository.name,
                ),
            )
        )
        git_remote_update_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Git remote update text",
                "tt",
                attrs={"id": "remote-update"},
                text=("git remote update %s" % self.user.name),
            )
        )
        git_merge_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Merge command text",
                "tt",
                attrs={"id": "merge-cmd"},
                text="git merge %s/branch" % self.user.name,
            )
        )

        with person_logged_in(self.user):
            rendered_view = view.render()
            self.assertThat(rendered_view, git_add_remote_match)
            self.assertThat(rendered_view, git_remote_update_match)
            self.assertThat(rendered_view, git_merge_match)

    def test_merge_guidelines_package(self):
        # Repository is the default for a package
        mint = self.factory.makeDistribution(name="mint")
        eric = self.factory.makePerson(name="eric")
        mint_choc = self.factory.makeDistributionSourcePackage(
            distribution=mint, sourcepackagename="choc"
        )
        repository = self.factory.makeGitRepository(
            owner=eric, target=mint_choc, name="choc-repo"
        )
        [ref] = self.factory.makeGitRefs(
            repository=repository, paths=["refs/heads/branch"]
        )
        dsp = repository.target
        self.repository_set = getUtility(IGitRepositorySet)
        with admin_logged_in():
            self.repository_set.setDefaultRepositoryForOwner(
                repository.owner, dsp, repository, repository.owner
            )
            self.repository_set.setDefaultRepository(dsp, repository)
        login_person(self.user)
        view = create_initialized_view(ref, "+index", principal=self.user)
        git_add_remote_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Git remote add text",
                "tt",
                attrs={"id": "remote-add"},
                text=(
                    "git remote add %s "
                    "git+ssh://%s@git.launchpad.test/%s/+source/%s"
                )
                % (eric.name, self.user.name, mint.name, mint_choc.name),
            )
        )
        git_remote_update_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Git remote update text",
                "tt",
                attrs={"id": "remote-update"},
                text=("git remote update %s" % eric.name),
            )
        )
        git_merge_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Merge command text",
                "tt",
                attrs={"id": "merge-cmd"},
                text="git merge %s/branch" % eric.name,
            )
        )

        with person_logged_in(self.user):
            rendered_view = view.render()
            self.assertThat(rendered_view, git_add_remote_match)
            self.assertThat(rendered_view, git_remote_update_match)
            self.assertThat(rendered_view, git_merge_match)

    def test_merge_guidelines_project(self):
        # Repository is the default for a project
        eric = self.factory.makePerson(name="eric")
        fooix = self.factory.makeProduct(name="fooix", owner=eric)
        repository = self.factory.makeGitRepository(
            owner=eric, target=fooix, name="fooix-repo"
        )
        [ref] = self.factory.makeGitRefs(
            repository=repository, paths=["refs/heads/branch"]
        )
        self.repository_set = getUtility(IGitRepositorySet)
        with person_logged_in(fooix.owner) as user:
            self.repository_set.setDefaultRepositoryForOwner(
                repository.owner, fooix, repository, user
            )
            self.repository_set.setDefaultRepository(fooix, repository)
        login_person(self.user)
        view = create_initialized_view(ref, "+index", principal=self.user)
        git_add_remote_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Git remote add text",
                "tt",
                attrs={"id": "remote-add"},
                text=(
                    "git remote add %s git+ssh://%s@git.launchpad.test/%s"
                    % (eric.name, self.user.name, fooix.name)
                ),
            )
        )
        git_remote_update_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Git remote update text",
                "tt",
                attrs={"id": "remote-update"},
                text=("git remote update %s" % eric.name),
            )
        )
        git_merge_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Merge command text",
                "tt",
                attrs={"id": "merge-cmd"},
                text="git merge %s/branch" % eric.name,
            )
        )

        with person_logged_in(self.user):
            rendered_view = view.render()
            self.assertThat(rendered_view, git_add_remote_match)
            self.assertThat(rendered_view, git_remote_update_match)
            self.assertThat(rendered_view, git_merge_match)

    def test_merge_guidelines_anonymous_view(self):
        # Merge guidelines are mainly intended for maintainers merging
        # contributions, they might be a bit noisy otherwise, therefore
        # we do not show them on anonymous views.
        # There is of course the permissions aspect involved here that you can
        # do a local merge using only read permissions on the source branch.
        team = self.factory.makeTeam()
        fooix = self.factory.makeProduct(name="fooix")
        repository = self.factory.makeGitRepository(
            owner=team, target=fooix, name="fooix-repo"
        )

        [ref] = self.factory.makeGitRefs(
            repository=repository, paths=["refs/heads/branch"]
        )
        with person_logged_in(self.user):
            view = create_initialized_view(ref, "+index", principal=self.user)
        git_add_remote_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Git remote add text", "tt", attrs={"id": "remote-add"}
            )
        )
        git_remote_update_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Git remote update text", "tt", attrs={"id": "remote-update"}
            )
        )
        git_merge_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Merge command text", "tt", attrs={"id": "merge-cmd"}
            )
        )

        rendered_view = view.render()
        self.assertThat(rendered_view, Not(git_add_remote_match))
        self.assertThat(rendered_view, Not(git_remote_update_match))
        self.assertThat(rendered_view, Not(git_merge_match))

    def makeCommitLog(self):
        authors = [self.factory.makePerson() for _ in range(5)]
        with admin_logged_in():
            author_emails = [author.preferredemail.email for author in authors]
        dates = [
            datetime(2015, 1, day + 1, tzinfo=timezone.utc) for day in range(5)
        ]
        return [
            {
                "sha1": hashlib.sha1(str(i).encode()).hexdigest(),
                "message": "Commit %d" % i,
                "author": {
                    "name": authors[i].display_name,
                    "email": author_emails[i],
                    "time": int(seconds_since_epoch(dates[i])),
                },
                "committer": {
                    "name": authors[i].display_name,
                    "email": author_emails[i],
                    "time": int(seconds_since_epoch(dates[i])),
                },
                "parents": [hashlib.sha1(str(i - 1).encode()).hexdigest()],
                "tree": hashlib.sha1(b"").hexdigest(),
            }
            for i in range(5)
        ]

    def scanRef(self, ref, tip, blobs=None):
        if blobs is not None:
            tip["blobs"] = blobs
        self.hosting_fixture.getRefs.result = {
            ref.path: {"object": {"sha1": tip["sha1"], "type": "commit"}},
        }
        self.hosting_fixture.getCommits.result = [tip]
        self.hosting_fixture.getProperties.result = {
            "default_branch": ref.path,
        }
        job = getUtility(IGitRefScanJobSource).create(
            removeSecurityProxy(ref.repository)
        )
        with dbuser("branchscanner"):
            JobRunner([job]).runAll()

    def test_recent_commits(self):
        [ref] = self.factory.makeGitRefs(paths=["refs/heads/branch"])
        log = self.makeCommitLog()
        self.hosting_fixture.getLog.result = list(reversed(log))
        # XXX jugmac00 2022-03-14
        # This is a workaround for the limitation of `GitHostingFixture` not
        # implementing a proper `getCommits` method.
        # If we would not supply the following configuration file,
        # `CIBuild.requestBuildsForRefs`, which is unrelated to this test case,
        # would fail.
        configuration = dedent(
            """\
            pipeline: [test]
            jobs:
                test:
                    series: foo
                    architectures: ["bar"]
        """
        )
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": configuration})
        view = create_initialized_view(ref, "+index")
        contents = view()
        expected_texts = list(
            reversed(
                [
                    "%.7s...\nby\n%s\non 2015-01-%02d"
                    % (log[i]["sha1"], log[i]["author"]["name"], i + 1)
                    for i in range(5)
                ]
            )
        )
        details = find_tags_by_class(contents, "commit-details")
        self.assertEqual(
            expected_texts, [extract_text(detail) for detail in details]
        )
        expected_urls = list(
            reversed(
                [
                    "https://git.launchpad.test/%s/commit/?id=%s"
                    % (ref.repository.shortened_path, log[i]["sha1"])
                    for i in range(5)
                ]
            )
        )
        self.assertEqual(
            expected_urls, [detail.a["href"] for detail in details]
        )
        self.assertThat(
            contents, Not(soupmatchers.HTMLContains(MissingCommitsNote()))
        )

    def test_recent_commits_with_merge(self):
        [ref] = self.factory.makeGitRefs(paths=["refs/heads/branch"])
        log = self.makeCommitLog()
        self.hosting_fixture.getLog.result = list(reversed(log))
        # XXX jugmac00 2022-03-14
        # This is a workaround for the limitation of `GitHostingFixture` not
        # implementing a proper `getCommits` method.
        # If we would not supply the following configuration file,
        # `CIBuild.requestBuildsForRefs`, which is unrelated to this test case,
        # would fail.
        configuration = dedent(
            """\
            pipeline: [test]
            jobs:
                test:
                    series: foo
                    architectures: ["bar"]
        """
        )
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": configuration})
        mp = self.factory.makeBranchMergeProposalForGit(target_ref=ref)
        merged_tip = dict(log[-1])
        merged_tip["sha1"] = hashlib.sha1(b"merged").hexdigest()
        self.scanRef(mp.merge_source, merged_tip)
        removeSecurityProxy(mp).markAsMerged(merged_revision_id=log[0]["sha1"])
        view = create_initialized_view(ref, "+index")
        contents = view()
        soup = BeautifulSoup(contents)
        details = soup.find_all(
            attrs={"class": re.compile(r"commit-details|commit-comment")}
        )
        expected_texts = list(
            reversed(
                [
                    "%.7s...\nby\n%s\non 2015-01-%02d"
                    % (log[i]["sha1"], log[i]["author"]["name"], i + 1)
                    for i in range(5)
                ]
            )
        )
        expected_texts.append(
            "Merged branch\n%s" % mp.merge_source.display_name
        )
        self.assertEqual(
            expected_texts, [extract_text(detail) for detail in details]
        )
        self.assertEqual(
            [canonical_url(mp), canonical_url(mp.merge_source)],
            [link["href"] for link in details[5].find_all("a")],
        )
        self.assertThat(
            contents, Not(soupmatchers.HTMLContains(MissingCommitsNote()))
        )

    def test_recent_commits_with_merge_from_deleted_ref(self):
        [ref] = self.factory.makeGitRefs(paths=["refs/heads/branch"])
        log = self.makeCommitLog()
        self.hosting_fixture.getLog.result = list(reversed(log))
        # XXX jugmac00 2022-03-14
        # This is a workaround for the limitation of `GitHostingFixture` not
        # implementing a proper `getCommits` method.
        # If we would not supply the following configuration file,
        # `CIBuild.requestBuildsForRefs`, which is unrelated to this test case,
        # would fail.
        configuration = dedent(
            """\
            pipeline: [test]
            jobs:
                test:
                    series: foo
                    architectures: ["bar"]
        """
        )
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": configuration})
        mp = self.factory.makeBranchMergeProposalForGit(target_ref=ref)
        merged_tip = dict(log[-1])
        merged_tip["sha1"] = hashlib.sha1(b"merged").hexdigest()
        self.scanRef(mp.merge_source, merged_tip)
        removeSecurityProxy(mp).markAsMerged(merged_revision_id=log[0]["sha1"])
        mp.source_git_repository.removeRefs([mp.source_git_path])
        view = create_initialized_view(ref, "+index")
        contents = view()
        soup = BeautifulSoup(contents)
        details = soup.find_all(
            attrs={"class": re.compile(r"commit-details|commit-comment")}
        )
        expected_texts = list(
            reversed(
                [
                    "%.7s...\nby\n%s\non 2015-01-%02d"
                    % (log[i]["sha1"], log[i]["author"]["name"], i + 1)
                    for i in range(5)
                ]
            )
        )
        expected_texts.append(
            "Merged branch\n%s" % mp.merge_source.display_name
        )
        self.assertEqual(
            expected_texts, [extract_text(detail) for detail in details]
        )
        self.assertEqual(
            [canonical_url(mp)],
            [link["href"] for link in details[5].find_all("a")],
        )
        self.assertThat(
            contents, Not(soupmatchers.HTMLContains(MissingCommitsNote()))
        )

    def test_recent_commits_with_invalid_author_email(self):
        # If an author's email address is syntactically invalid, then we
        # ignore the authorship information for that commit and do our best
        # to render what we have.
        [ref] = self.factory.makeGitRefs(paths=["refs/heads/branch"])
        log = self.makeCommitLog()
        log[4]["author"]["email"] = "“%s”" % log[4]["author"]["email"]
        self.hosting_fixture.getLog.result = list(reversed(log))
        # XXX jugmac00 2022-03-14
        # This is a workaround for the limitation of `GitHostingFixture` not
        # implementing a proper `getCommits` method.
        # If we would not supply the following configuration file,
        # `CIBuild.requestBuildsForRefs`, which is unrelated to this test case,
        # would fail.
        configuration = dedent(
            """\
            pipeline: [test]
            jobs:
                test:
                    series: foo
                    architectures: ["bar"]
        """
        )
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": configuration})
        view = create_initialized_view(ref, "+index")
        contents = view()
        expected_texts = ["%.7s...\non 2015-01-05" % log[4]["sha1"]]
        expected_texts.extend(
            reversed(
                [
                    "%.7s...\nby\n%s\non 2015-01-%02d"
                    % (log[i]["sha1"], log[i]["author"]["name"], i + 1)
                    for i in range(4)
                ]
            )
        )
        details = find_tags_by_class(contents, "commit-details")
        self.assertEqual(
            expected_texts, [extract_text(detail) for detail in details]
        )
        expected_urls = list(
            reversed(
                [
                    "https://git.launchpad.test/%s/commit/?id=%s"
                    % (ref.repository.shortened_path, log[i]["sha1"])
                    for i in range(5)
                ]
            )
        )
        self.assertEqual(
            expected_urls, [detail.a["href"] for detail in details]
        )
        self.assertThat(
            contents, Not(soupmatchers.HTMLContains(MissingCommitsNote()))
        )

    def test_show_merge_link_for_personal_repo(self):
        person = self.factory.makePerson()
        repo = self.factory.makeGitRepository(owner=person, target=person)
        [ref] = self.factory.makeGitRefs(
            repository=repo, paths=["refs/heads/branch"]
        )

        view = create_initialized_view(ref, "+index")
        self.assertTrue(view.show_merge_links)
        self.assertEqual(1, len(view.propose_merge_notes))

    def _test_all_commits_link(self, branch_name, encoded_branch_name=None):
        if encoded_branch_name is None:
            encoded_branch_name = branch_name
        [ref] = self.factory.makeGitRefs(paths=["refs/heads/%s" % branch_name])
        log = self.makeCommitLog()
        self.hosting_fixture.getLog.result = list(reversed(log))
        # XXX jugmac00 2022-03-14
        # This is a workaround for the limitation of `GitHostingFixture` not
        # implementing a proper `getCommits` method.
        # If we would not supply the following configuration file,
        # `CIBuild.requestBuildsForRefs`, which is unrelated to this test case,
        # would fail.
        configuration = dedent(
            """\
            pipeline: [test]
            jobs:
                test:
                    series: foo
                    architectures: ["bar"]
        """
        )
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": configuration})
        view = create_initialized_view(ref, "+index")
        recent_commits_tag = soupmatchers.Tag(
            "recent commits", "div", attrs={"id": "recent-commits"}
        )
        expected_url = "https://git.launchpad.test/%s/log/?h=%s" % (
            ref.repository.shortened_path,
            encoded_branch_name,
        )
        self.assertThat(
            view(),
            soupmatchers.HTMLContains(
                soupmatchers.Within(
                    recent_commits_tag,
                    soupmatchers.Tag(
                        "all commits link",
                        "a",
                        text="All commits",
                        attrs={"href": expected_url},
                    ),
                )
            ),
        )

    def test_all_commits_link(self):
        self._test_all_commits_link("branch")

    def test_all_commits_link_non_ascii(self):
        self._test_all_commits_link("\N{SNOWMAN}", "%E2%98%83")

    def test_query_count_landing_candidates(self):
        project = self.factory.makeProduct()
        [ref] = self.factory.makeGitRefs(target=project)
        for _ in range(10):
            self.factory.makeBranchMergeProposalForGit(target_ref=ref)
        [source] = self.factory.makeGitRefs(target=project)
        [prereq] = self.factory.makeGitRefs(target=project)
        self.factory.makeBranchMergeProposalForGit(
            source_ref=source, target_ref=ref, prerequisite_ref=prereq
        )
        Store.of(ref).flush()
        Store.of(ref).invalidate()
        view = create_view(ref, "+index")
        with StormStatementRecorder() as recorder:
            view.landing_candidates
        self.assertThat(recorder, HasQueryCount(Equals(13)))

    def test_query_count_landing_targets(self):
        project = self.factory.makeProduct()
        [ref] = self.factory.makeGitRefs(target=project)
        for _ in range(10):
            self.factory.makeBranchMergeProposalForGit(source_ref=ref)
        [target] = self.factory.makeGitRefs(target=project)
        [prereq] = self.factory.makeGitRefs(target=project)
        self.factory.makeBranchMergeProposalForGit(
            source_ref=ref, target_ref=target, prerequisite_ref=prereq
        )
        Store.of(ref).flush()
        Store.of(ref).invalidate()
        view = create_view(ref, "+index")
        with StormStatementRecorder() as recorder:
            view.landing_targets
        self.assertThat(recorder, HasQueryCount(Equals(13)))

    def test_timeout(self):
        # The page renders even if fetching commits times out.
        self.useFixture(FakeLogger())
        [ref] = self.factory.makeGitRefs()
        log = self.makeCommitLog()
        # XXX jugmac00 2022-03-14
        # This is a workaround for the limitation of `GitHostingFixture` not
        # implementing a proper `getCommits` method.
        # If we would not supply the following configuration file,
        # `CIBuild.requestBuildsForRefs`, which is unrelated to this test case,
        # would fail.
        configuration = dedent(
            """\
            pipeline: [test]
            jobs:
                test:
                    series: foo
                    architectures: ["bar"]
        """
        )
        self.scanRef(ref, log[-1], blobs={".launchpad.yaml": configuration})
        self.hosting_fixture.getLog.failure = TimeoutError
        view = create_initialized_view(ref, "+index")
        contents = view()
        soup = BeautifulSoup(contents)
        details = soup.find_all(
            attrs={"class": re.compile(r"commit-details|commit-comment")}
        )
        expected_text = "%.7s...\nby\n%s\non 2015-01-%02d" % (
            log[-1]["sha1"],
            log[-1]["author"]["name"],
            len(log),
        )
        self.assertEqual(
            [expected_text], [extract_text(detail) for detail in details]
        )
        self.assertThat(
            contents, soupmatchers.HTMLContains(MissingCommitsNote())
        )
