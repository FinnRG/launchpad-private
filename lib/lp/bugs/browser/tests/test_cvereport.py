# Copyright 2012-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""CVE related tests."""

import re
from functools import partial
from operator import attrgetter

from soupmatchers import HTMLContains, Tag

from lp.bugs.browser.cvereport import BugTaskCve
from lp.bugs.interfaces.bugtask import (
    RESOLVED_BUGTASK_STATUSES,
    UNRESOLVED_BUGTASK_STATUSES,
)
from lp.services.webapp.publisher import canonical_url
from lp.testing import (
    TestCaseWithFactory,
    login_person,
    person_logged_in,
    record_two_runs,
)
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.matchers import HasQueryCount
from lp.testing.views import create_initialized_view


class TestCVEReportView(TestCaseWithFactory):
    """Tests for CveReportView."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        """Create a few bugtasks and CVEs."""
        super().setUp()
        distroseries = self.factory.makeDistroSeries()
        self.resolved_bugtasks = []
        self.unresolved_bugtasks = []
        self.cves = {}
        self.getCVE = partial(next, self.cveGenerator())
        with person_logged_in(distroseries.owner):
            for status in RESOLVED_BUGTASK_STATUSES:
                tasks, cves = self.makeBugTasksWithCve(status, distroseries)
                self.resolved_bugtasks.append(tasks)
                self.cves[tasks[0].bug] = cves
            for status in UNRESOLVED_BUGTASK_STATUSES:
                tasks, cves = self.makeBugTasksWithCve(status, distroseries)
                self.unresolved_bugtasks.append(tasks)
                self.cves[tasks[0].bug] = cves
        self.view = create_initialized_view(distroseries, "+cve")

    def makeBugTasksWithCve(self, status, distroseries):
        """Return two bugtasks for one bug linked to CVE."""
        task = self.factory.makeBugTask(
            target=self.factory.makeSourcePackage(distroseries=distroseries)
        )
        task.transitionToStatus(status, distroseries.owner)
        bug = task.bug
        task_2 = self.factory.makeBugTask(
            target=self.factory.makeSourcePackage(distroseries=distroseries),
            bug=bug,
        )
        task_2.transitionToStatus(status, distroseries.owner)
        cve_1 = self.getCVE()
        cve_2 = self.getCVE()
        bug.linkCVE(cve_1, distroseries.owner)
        bug.linkCVE(cve_2, distroseries.owner)
        return [task, task_2], (cve_1, cve_2)

    def cveGenerator(self):
        """A generator returning five CVEs cyclically."""
        NUM_CVES = 5
        cves = [
            self.factory.makeCVE("2000-%04i" % count)
            for count in range(NUM_CVES)
        ]
        cve_index = 0
        while True:
            yield cves[cve_index]
            cve_index = (cve_index + 1) % NUM_CVES

    def test_distribution_cvereport(self):
        distribution = self.factory.makeDistribution()
        distroseries1 = self.factory.makeDistroSeries(
            distribution=distribution
        )
        distroseries2 = self.factory.makeDistroSeries(
            distribution=distribution
        )
        view = create_initialized_view(distribution, "+cve")

        self.assertFalse(view.open_cve_bugtasks)
        self.assertFalse(view.resolved_cve_bugtasks)
        self.assertThat(
            view.render(),
            HTMLContains(
                Tag(
                    "distroseries 1 link",
                    "a",
                    attrs={
                        "href": "{}/+cve".format(
                            canonical_url(distroseries1, force_local_path=True)
                        )
                    },
                    text=distroseries1.displayname,
                ),
                Tag(
                    "distroseries 2 link",
                    "a",
                    attrs={
                        "href": "{}/+cve".format(
                            canonical_url(distroseries2, force_local_path=True)
                        )
                    },
                    text=distroseries2.displayname,
                ),
            ),
        )

    def test_render(self):
        # The rendered page contains all expected CVE links.
        html_data = self.view.render()
        cve_links = re.findall(
            r'<a style="text-decoration: none" '
            r'href="http://bugs.launchpad.test/bugs/cve/CVE-\d{4}-\d{4}">'
            r'<img src="/@@/link" alt="" />'
            r'<span style="text-decoration: underline">CVE-\d{4}-\d{4}</span>'
            r"</a>",
            html_data,
        )
        # We have two CVEs per bug, and one bug per bugtask status.
        expected = 2 * (
            len(RESOLVED_BUGTASK_STATUSES) + len(UNRESOLVED_BUGTASK_STATUSES)
        )
        self.assertEqual(expected, len(cve_links))

    def test_open_resolved_cve_bugtasks(self):
        # The properties CVEReportView.open_cve_bugtasks and
        # CVEReportView.resolved_cve_bugtasks are lists of
        # BugTaskCve instances.
        for item in self.view.open_cve_bugtasks:
            self.assertIsInstance(item, BugTaskCve)
        for item in self.view.resolved_cve_bugtasks:
            self.assertIsInstance(item, BugTaskCve)

        open_bugs = [
            bugtaskcve.bug for bugtaskcve in self.view.open_cve_bugtasks
        ]
        expected_open_bugs = [
            task.bug for task, task_2 in self.unresolved_bugtasks
        ]
        self.assertEqual(expected_open_bugs, open_bugs)
        resolved_bugs = [
            bugtaskcve.bug for bugtaskcve in self.view.resolved_cve_bugtasks
        ]
        expected_resolved_bugs = [
            task.bug for task, task_2 in self.resolved_bugtasks
        ]
        self.assertEqual(expected_resolved_bugs, resolved_bugs)

        def unwrap_bugtask_listing_items(seq):
            return [[item1.bugtask, item2.bugtask] for item1, item2 in seq]

        open_bugtasks = [
            bugtaskcve.bugtasks for bugtaskcve in self.view.open_cve_bugtasks
        ]
        open_bugtasks = unwrap_bugtask_listing_items(open_bugtasks)
        self.assertEqual(self.unresolved_bugtasks, open_bugtasks)
        resolved_bugtasks = [
            bugtaskcve.bugtasks
            for bugtaskcve in self.view.resolved_cve_bugtasks
        ]
        resolved_bugtasks = unwrap_bugtask_listing_items(resolved_bugtasks)
        self.assertEqual(self.resolved_bugtasks, resolved_bugtasks)

        def get_cve_data_for_task(task):
            """Sort the CVEs for the given bug task by ID and return
            their display data."""
            cves = sorted(self.cves[task.bug], key=attrgetter("id"))
            return [
                {
                    "url": canonical_url(cve),
                    "displayname": cve.displayname,
                }
                for cve in cves
            ]

        open_cves = [
            bugtaskcve.cves for bugtaskcve in self.view.open_cve_bugtasks
        ]
        expected_open_cves = [
            get_cve_data_for_task(task)
            for task, task_2 in self.unresolved_bugtasks
        ]
        self.assertEqual(expected_open_cves, open_cves)

        resolved_cves = [
            bugtaskcve.cves for bugtaskcve in self.view.resolved_cve_bugtasks
        ]
        expected_resolved_cves = [
            get_cve_data_for_task(task)
            for task, task_2 in self.resolved_bugtasks
        ]
        self.assertEqual(expected_resolved_cves, resolved_cves)

    def test_renderCVELinks(self):
        # renderCVELinks() takes a sequence of items with CVE related
        # data and returns an HTML representation with links to
        # Launchpad pages for the CVEs.
        result = self.view.renderCVELinks(
            [
                {
                    "displayname": "CVE-2011-0123",
                    "url": "http://bugs.launchpad.test/bugs/cve/2011-0123",
                },
                {
                    "displayname": "CVE-2011-0456",
                    "url": "http://bugs.launchpad.test/bugs/cve/2011-0456",
                },
            ]
        )
        expected = (
            '<a style="text-decoration: none" '
            'href="http://bugs.launchpad.test/bugs/cve/2011-0123">'
            '<img src="/@@/link" alt="" />'
            '<span style="text-decoration: underline">CVE-2011-0123</span>'
            "</a><br />\n"
            '<a style="text-decoration: none" '
            'href="http://bugs.launchpad.test/bugs/cve/2011-0456">'
            '<img src="/@@/link" alt="" />'
            '<span style="text-decoration: underline">CVE-2011-0456</span>'
            "</a>"
        )
        self.assertEqual(expected, result)

    def test_cve_url_backward_compat_bare_sequence(self):
        """Browsing to a bare sequence URL resolves to the same CVE page
        as the canonical prefixed URL.

        /bugs/cve/2024-1234 and /bugs/cve/CVE-2024-1234 must both work.
        GetitemNavigation calls CveSet.__getitem__ which normalises bare
        sequences, so both URL forms traverse to the same object.
        """
        cve = self.factory.makeCVE(sequence="2024-9876")
        person = self.factory.makePerson()

        # canonical_url uses cve.sequence (prefixed form: CVE-2024-9876)
        prefixed_url = canonical_url(cve)
        # construct the bare-sequence form manually
        bare_url = prefixed_url.replace("/cve/CVE-", "/cve/")

        prefixed_browser = self.getUserBrowser(prefixed_url, person)
        bare_browser = self.getUserBrowser(bare_url, person)

        self.assertEqual(200, prefixed_browser.responseStatusCode)
        self.assertEqual(200, bare_browser.responseStatusCode)
        # Both URLs render the same page title
        self.assertEqual(
            prefixed_browser.title,
            bare_browser.title,
        )

    def test_query_count(self):
        cve = self.getCVE()
        cve_url = canonical_url(cve)
        person = self.factory.makePerson()

        def make_bug_and_tasks():
            bug = self.factory.makeBug()
            self.factory.makeBugTask(
                bug=bug, target=self.factory.makeProductSeries()
            )
            self.factory.makeBugTask(
                bug=bug, target=self.factory.makeSourcePackage()
            )
            bug.linkCVE(cve, person)

        recorder1, recorder2 = record_two_runs(
            lambda: self.getUserBrowser(cve_url, person),
            make_bug_and_tasks,
            2,
            10,
            login_method=lambda: login_person(person),
        )
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))
