# Copyright 2012-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""CVE related tests."""

from datetime import datetime, timedelta, timezone

from testtools.matchers import MatchesStructure
from testtools.testcase import ExpectedException
from zope.component import getUtility
from zope.security.interfaces import ForbiddenAttribute, Unauthorized
from zope.security.management import checkPermission
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.bugs.interfaces.bugtasksearch import BugTaskSearchParams
from lp.bugs.interfaces.cve import CveStatus, ICveSet
from lp.services.webapp.interfaces import OAuthPermission
from lp.testing import (
    TestCaseWithFactory,
    admin_logged_in,
    api_url,
    login_person,
    person_logged_in,
    verifyObject,
)
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.pages import webservice_for_person


class TestCveSet(TestCaseWithFactory):
    """Tests for CveSet."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        """Create a few bugtasks and CVEs."""
        super().setUp()
        self.distroseries = self.factory.makeDistroSeries()
        self.bugs = []
        self.cves = []
        self.cve_index = 0
        with person_logged_in(self.distroseries.owner):
            for _ in range(4):
                task = self.factory.makeBugTask(target=self.distroseries)
                bug = task.bug
                self.bugs.append(bug)
                cve = self.makeCVE()
                self.cves.append(cve)
                bug.linkCVE(cve, self.distroseries.owner)

    def makeCVE(self):
        """Create a CVE."""
        self.cve_index += 1
        return self.factory.makeCVE("2000-%04i" % self.cve_index)

    def test_CveSet_implements_ICveSet(self):
        cveset = getUtility(ICveSet)
        self.assertTrue(verifyObject(ICveSet, cveset))

    def test_getBugCvesForBugTasks(self):
        # ICveSet.getBugCvesForBugTasks() returns tuples (bug, cve)
        # for the given bugtasks.
        bugtasks = self.distroseries.searchTasks(
            BugTaskSearchParams(self.distroseries.owner, has_cve=True)
        )
        bug_cves = getUtility(ICveSet).getBugCvesForBugTasks(bugtasks)
        found_bugs = [bug for bug, cve in bug_cves]
        found_cves = [cve for bug, cve in bug_cves]
        self.assertEqual(self.bugs, found_bugs)
        self.assertEqual(self.cves, found_cves)

    def test_getBugCvesForBugTasks_with_mapper(self):
        # ICveSet.getBugCvesForBugTasks() takes a function f as an
        # optional argeument. This function is applied to each CVE
        # related to the given bugs; the method return a sequence of
        # tuples (bug, f(cve)).
        def cve_name(cve):
            return cve.displayname

        bugtasks = self.distroseries.searchTasks(
            BugTaskSearchParams(self.distroseries.owner, has_cve=True)
        )
        bug_cves = getUtility(ICveSet).getBugCvesForBugTasks(
            bugtasks, cve_name
        )
        found_bugs = [bug for bug, cve in bug_cves]
        cve_data = [cve for bug, cve in bug_cves]
        self.assertEqual(self.bugs, found_bugs)
        expected = [
            "CVE-2000-0001",
            "CVE-2000-0002",
            "CVE-2000-0003",
            "CVE-2000-0004",
        ]
        self.assertEqual(expected, cve_data)

    def test_getBugCveCount(self):
        login_person(self.factory.makePerson())

        base = getUtility(ICveSet).getBugCveCount()
        bug1 = self.factory.makeBug()
        bug2 = self.factory.makeBug()
        cve1 = self.factory.makeCVE(sequence="2099-1234")
        cve2 = self.factory.makeCVE(sequence="2099-2468")
        self.assertEqual(base, getUtility(ICveSet).getBugCveCount())
        cve1.linkBug(bug1)
        self.assertEqual(base + 1, getUtility(ICveSet).getBugCveCount())
        cve1.linkBug(bug2)
        self.assertEqual(base + 2, getUtility(ICveSet).getBugCveCount())
        cve2.linkBug(bug1)
        self.assertEqual(base + 3, getUtility(ICveSet).getBugCveCount())
        cve1.unlinkBug(bug1)
        cve1.unlinkBug(bug2)
        cve2.unlinkBug(bug1)
        self.assertEqual(base, getUtility(ICveSet).getBugCveCount())


class TestCveSetGetFilteredCves(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.cves = getUtility(ICveSet)

    def test_getFilteredCves_default_arguments(self):
        # 5 cves will be returned as there are not vulnerabilities created
        # and limit = config.launchpad.default_batch_size = 5
        result = self.cves.getFilteredCves()
        self.assertEqual(5, len(list(result)))

    def test_getFilteredCves_in_distribution(self):
        distribution1 = self.factory.makeDistribution()
        distribution2 = self.factory.makeDistribution()

        # No result as there is no cve with a vulnerability for distribution1
        result = self.cves.getFilteredCves(in_distribution=[distribution1])
        self.assertEqual(0, len(list(result)))

        cve = self.factory.makeCVE(sequence="2099-9876")
        self.factory.makeVulnerability(distribution1, cve=cve)

        # There is 1 cve with a vulnerability for distribution
        result = self.cves.getFilteredCves(in_distribution=[distribution1])
        cves = list(result)
        self.assertEqual(1, len(cves))
        self.assertEqual("CVE-2099-9876", cves[0])

        # No result as there is no cve with a vulnerability for distribution2
        result = self.cves.getFilteredCves(in_distribution=[distribution2])
        self.assertEqual(0, len(list(result)))

    def test_getFilteredCves_not_in_distribution(self):
        distribution1 = self.factory.makeDistribution()
        distribution2 = self.factory.makeDistribution()

        # 10 cves as they don't have a vulnerability for distributions yet
        result = self.cves.getFilteredCves(
            not_in_distribution=[distribution1, distribution2],
            limit=20,
        )
        self.assertEqual(10, len(list(result)))

        cve = self.factory.makeCVE(sequence="2099-9876")

        # There are 11 cve without a vulnerability for distributions
        result = self.cves.getFilteredCves(
            not_in_distribution=[distribution1, distribution2],
            limit=20,
        )
        self.assertEqual(11, len(list(result)))

        # Creating a vulnerability for distribution1 will make this cve not to
        # appear in the search
        self.factory.makeVulnerability(distribution1, cve=cve)

        # There are 10 cve without a vulnerability for any of distributions
        result = self.cves.getFilteredCves(
            not_in_distribution=[distribution1, distribution2],
            limit=20,
        )
        self.assertEqual(10, len(list(result)))

    def test_getFilteredCves_both_in_distribution(self):
        distribution1 = self.factory.makeDistribution()
        distribution2 = self.factory.makeDistribution()
        distribution3 = self.factory.makeDistribution()

        # No result as there are no cves that:
        #   - has a vulnerability for distribution1 and distribution2
        #   - has no vulnerability for distribution3
        result = self.cves.getFilteredCves(
            in_distribution=[distribution1, distribution2],
            not_in_distribution=[distribution3],
        )
        self.assertEqual(0, len(list(result)))

        cve = self.factory.makeCVE(sequence="2099-9876")
        self.factory.makeVulnerability(distribution1, cve=cve)

        # No result as we only created the vulnerability for distribution1
        result = self.cves.getFilteredCves(
            in_distribution=[distribution1, distribution2],
            not_in_distribution=[distribution3],
        )
        self.assertEqual(0, len(list(result)))

        # 1 result as we created a vulnerability for distribution1 and
        # distribution2
        self.factory.makeVulnerability(distribution2, cve=cve)
        result = self.cves.getFilteredCves(
            in_distribution=[distribution1, distribution2],
            not_in_distribution=[distribution3],
        )
        cves = list(result)
        self.assertEqual(1, len(cves))
        self.assertEqual("CVE-2099-9876", cves[0])

        # No result as we created a vulnerability for distribution3 for the cve
        self.factory.makeVulnerability(distribution3, cve=cve)
        result = self.cves.getFilteredCves(
            in_distribution=[distribution1, distribution2],
            not_in_distribution=[distribution3],
        )
        self.assertEqual(0, len(list(result)))

    def test_getFilteredCves_modified_since(self):
        modified_since = datetime.utcnow() - timedelta(days=1)
        self.factory.makeCVE(sequence="2099-9876")

        result = self.cves.getFilteredCves(modified_since=modified_since)
        cves = list(result)
        self.assertEqual(1, len(cves))
        self.assertEqual("CVE-2099-9876", cves[0])

    def test_getFilteredCves_offset(self):
        self.factory.makeCVE(sequence="2099-9876")

        result = self.cves.getFilteredCves(offset=1, limit=20)
        cves = list(result)
        self.assertEqual(10, len(cves))

        # Using offset = 1 we skipped the latest modified cve
        self.assertNotIn("CVE-2099-9876", cves)

    def test_getFilteredCves_limit(self):
        result = self.cves.getFilteredCves(limit=5)
        self.assertEqual(5, len(list(result)))

    def test_getFilteredCves_order_by(self):
        """Test that getFilteredCves orders by descending datemodified."""

        # Get all cves and sort them by desc datemodified
        cves = self.cves.getAll()
        cves = sorted(cves, key=lambda cve: cve.datemodified, reverse=True)
        sequences = [cve.sequence for cve in cves]

        result = self.cves.getFilteredCves(limit=10)
        self.assertEqual(sequences, list(result))


class TestCveSetAdvancedSearchWebService(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()
        self.distribution1 = self.factory.makeDistribution(
            owner=self.person, name="distribution1"
        )
        self.distribution2 = self.factory.makeDistribution(
            owner=self.person, name="distribution2"
        )

        self.api_base = "http://api.launchpad.test/devel"
        self.cves = getUtility(ICveSet)
        self.cves_url = self.api_base + api_url(self.cves)

    def test_advancedSearch_default_arguments(self):
        webservice = webservice_for_person(
            self.person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_get(
            self.cves_url,
            "advancedSearch",
        )

        self.assertEqual(200, response.status)

        # config.launchpad.default_batch_size = 5
        self.assertEqual(5, len(response.jsonBody()["cves"]))

    def test_advancedSearch_in_distribution(self):
        cve = self.factory.makeCVE(sequence="2099-9876")
        self.factory.makeVulnerability(self.distribution1, cve=cve)

        in_distribution = [api_url(self.distribution1)]
        webservice = webservice_for_person(
            self.person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_get(
            self.cves_url,
            "advancedSearch",
            in_distribution=in_distribution,
        )

        self.assertEqual(200, response.status)
        self.assertEqual(1, len(response.jsonBody()["cves"]))
        self.assertEqual("CVE-2099-9876", response.jsonBody()["cves"][0])

    def test_advancedSearch_not_in_distribution(self):
        not_in_distribution = [api_url(self.distribution1)]
        webservice = webservice_for_person(
            self.person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_get(
            self.cves_url,
            "advancedSearch",
            not_in_distribution=not_in_distribution,
            limit=20,
        )

        self.assertEqual(200, response.status)
        self.assertEqual(10, len(response.jsonBody()["cves"]))

    def test_advancedSearch_modified_since(self):
        self.factory.makeCVE(sequence="2099-9876")

        webservice = webservice_for_person(
            self.person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_get(
            self.cves_url,
            "advancedSearch",
            modified_since=(datetime.utcnow() - timedelta(days=1)).isoformat(),
        )

        self.assertEqual(200, response.status)
        self.assertEqual(1, len(response.jsonBody()["cves"]))

    def test_advancedSearch_modified_since_str(self):
        self.factory.makeCVE(sequence="2099-9876")

        webservice = webservice_for_person(
            self.person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_get(
            self.cves_url,
            "advancedSearch",
            modified_since="2025-10-08T13:43:26",
        )

        self.assertEqual(200, response.status)
        self.assertEqual(1, len(response.jsonBody()["cves"]))

    def test_advancedSearch_modified_since_wrong(self):
        self.factory.makeCVE(sequence="2099-9876")

        webservice = webservice_for_person(
            self.person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_get(
            self.cves_url,
            "advancedSearch",
            modified_since="not a date",
        )

        self.assertEqual(400, response.status)
        self.assertEqual(
            b"modified_since: Value doesn't look like a date.", response.body
        )

    def test_advancedSearch_offset(self):
        self.factory.makeCVE(sequence="2099-9876")

        webservice = webservice_for_person(
            self.person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_get(
            self.cves_url,
            "advancedSearch",
            offset=1,
            limit=20,
        )

        self.assertEqual(200, response.status)
        self.assertEqual(10, len(response.jsonBody()["cves"]))

        # Using offset = 1 we skipped the latest modified cve
        self.assertNotIn("CVE-2099-9876", response.jsonBody()["cves"])

    def test_advancedSearch_limit(self):
        webservice = webservice_for_person(
            self.person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_get(
            self.cves_url,
            "advancedSearch",
            limit=5,
        )

        self.assertEqual(200, response.status)
        self.assertEqual(5, len(response.jsonBody()["cves"]))

    def test_advancedSearch_unauthenticated(self):
        in_distribution = [api_url(self.distribution1)]

        webservice = webservice_for_person(
            None,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_get(
            self.cves_url,
            "advancedSearch",
            in_distribution=in_distribution,
        )

        self.assertEqual(401, response.status)
        self.assertEqual(
            b"Only authenticated users can use this endpoint",
            response.body,
        )

    def test_advancedSearch_unauthorized(self):
        in_distribution = [api_url(self.distribution1)]
        person = self.factory.makePerson()

        webservice = webservice_for_person(
            person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_get(
            self.cves_url,
            "advancedSearch",
            in_distribution=in_distribution,
        )

        self.assertEqual(401, response.status)
        self.assertEqual(
            b"Only security admins can use distribution1 as a filter",
            response.body,
        )


class TestBugLinks(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_link_and_unlink(self):
        login_person(self.factory.makePerson())

        bug1 = self.factory.makeBug()
        bug2 = self.factory.makeBug()
        cve1 = self.factory.makeCVE(sequence="2099-1234")
        cve2 = self.factory.makeCVE(sequence="2099-2468")
        self.assertContentEqual([], bug1.cves)
        self.assertContentEqual([], bug2.cves)
        self.assertContentEqual([], cve1.bugs)
        self.assertContentEqual([], cve2.bugs)

        cve1.linkBug(bug1)
        cve2.linkBug(bug1)
        cve1.linkBug(bug2)
        self.assertContentEqual([bug1, bug2], cve1.bugs)
        self.assertContentEqual([bug1], cve2.bugs)
        self.assertContentEqual([cve1, cve2], bug1.cves)
        self.assertContentEqual([cve1], bug2.cves)

        cve1.unlinkBug(bug1)
        self.assertContentEqual([bug2], cve1.bugs)
        self.assertContentEqual([bug1], cve2.bugs)
        self.assertContentEqual([cve2], bug1.cves)
        self.assertContentEqual([cve1], bug2.cves)

        cve1.unlinkBug(bug2)
        self.assertContentEqual([], cve1.bugs)
        self.assertContentEqual([bug1], cve2.bugs)
        self.assertContentEqual([cve2], bug1.cves)
        self.assertContentEqual([], bug2.cves)


class TestCve(TestCaseWithFactory):
    """Tests for Cve fields and methods."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.cve = self.factory.makeCVE("2025-9999")

    def test_cveset_new_method_optional_parameters(self):
        cve = getUtility(ICveSet).new(
            sequence="2099-1234",
            description="A critical vulnerability",
            status=CveStatus.CANDIDATE,
        )
        self.assertThat(
            cve,
            MatchesStructure.byEquality(
                sequence="2099-1234",
                status=CveStatus.CANDIDATE,
                description="A critical vulnerability",
                date_made_public=None,
                discovered_by=None,
                cvss=None,
                metadata=None,
            ),
        )

    def test_cveset_new_method_parameters(self):
        today = datetime.now(tz=timezone.utc)
        cvss = {"nvd": ["CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"]}
        metadata = {"example key": "example value"}
        cve = getUtility(ICveSet).new(
            sequence="2099-1234",
            description="A critical vulnerability",
            status=CveStatus.CANDIDATE,
            date_made_public=today,
            discovered_by="A person",
            cvss=cvss,
            metadata=metadata,
        )
        self.assertThat(
            cve,
            MatchesStructure.byEquality(
                sequence="2099-1234",
                status=CveStatus.CANDIDATE,
                description="A critical vulnerability",
                date_made_public=today,
                discovered_by="A person",
                cvss=cvss,
                metadata=metadata,
            ),
        )

    def test_cve_date_made_public_invalid_values(self):
        invalid_values = [
            "",
            "abcd",
            {"a": 1},
            [1, "a", "2", "b"],
            "2022-01-01",
        ]
        cve = self.factory.makeCVE(
            sequence="2099-1234",
            description="A critical vulnerability",
            cvestate=CveStatus.CANDIDATE,
        )
        for invalid_value in invalid_values:
            with ExpectedException(TypeError, "Expected datetime,.*"):
                removeSecurityProxy(cve).date_made_public = invalid_value

    def test_getDistributionVulnerability(self):
        cve = self.factory.makeCVE(sequence="2099-1234")
        distribution = self.factory.makeDistribution(
            information_type=InformationType.PROPRIETARY
        )
        vulnerability = self.factory.makeVulnerability(
            distribution=distribution,
            cve=cve,
            information_type=InformationType.PROPRIETARY,
        )

        # getDistributionVulnerability returns the vulnerability although we
        # are not logged in
        self.assertEqual(
            vulnerability, cve.getDistributionVulnerability(distribution)
        )

        # As we are not logged as an user, cve.vulnerabilities is empty
        self.assertEqual(len(list(cve.vulnerabilities)), 0)

        # Admin can see the PROPRIETARY vulnerability
        with admin_logged_in():
            self.assertEqual(vulnerability, cve.vulnerabilities[0])

    def test_cve_permissions_anonymous(self):
        """Test that anonymous user cannot view, edit or delete."""
        self.assertFalse(checkPermission("launchpad.View", self.cve))
        self.assertFalse(checkPermission("launchpad.Edit", self.cve))
        self.assertFalse(checkPermission("launchpad.Delete", self.cve))

    def test_cve_permissions_authenticated(self):
        """Test that logged in user can view but not edit or delete."""
        with person_logged_in(self.factory.makePerson()):
            self.assertTrue(checkPermission("launchpad.View", self.cve))
            self.assertFalse(checkPermission("launchpad.Edit", self.cve))
            self.assertFalse(checkPermission("launchpad.Delete", self.cve))

    def test_cve_permissions_admin(self):
        """Test that admin can view but not edit or delete."""
        with admin_logged_in():
            self.assertTrue(checkPermission("launchpad.View", self.cve))
            self.assertFalse(checkPermission("launchpad.Edit", self.cve))
            self.assertFalse(checkPermission("launchpad.Delete", self.cve))

    def test_cve_readonly(self):
        """Test that app code cannot update Cve attributes, but
        InternalScripts can."""
        failure_regex = ".*InternalScriptsOnly"

        with ExpectedException(Unauthorized, failure_regex):
            self.cve.sequence = "2099-9876"
        with ExpectedException(Unauthorized, failure_regex):
            self.cve.status = CveStatus.DEPRECATED
        with ExpectedException(Unauthorized, failure_regex):
            self.cve.description = "example"
        with ExpectedException(Unauthorized, failure_regex):
            self.cve.datecreated = datetime.utcnow()
        with ExpectedException(Unauthorized, failure_regex):
            self.cve.datemodified = datetime.utcnow()
        with ExpectedException(Unauthorized, failure_regex):
            self.cve.references = []
        with ExpectedException(Unauthorized, failure_regex):
            self.cve.date_made_public = datetime.utcnow()
        with ExpectedException(Unauthorized, failure_regex):
            self.cve.discovered_by = "example person"
        with ExpectedException(Unauthorized, failure_regex):
            self.cve.cvss = {"example authority": ["example score"]}
        with ExpectedException(Unauthorized, failure_regex):
            self.cve.metadata = {"meta": "data"}

        # It is forbidden to use cve._cvss
        with ExpectedException(ForbiddenAttribute):
            self.cve._cvss = {"example authority": ["example score"]}


class TestCveWebService(TestCaseWithFactory):
    """Tests for Cve fields and methods exposed over the webservice."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.cve = self.factory.makeCVE("2025-9999")

    def test_references_exported_over_webservice(self):
        """The references collection is readable via the webservice."""
        self.cve.createReference(
            source="XF", content="example content", url="http://example.com"
        )
        person = self.factory.makePerson()
        webservice = webservice_for_person(
            person,
            permission=OAuthPermission.READ_PUBLIC,
            default_api_version="devel",
        )

        response = webservice.get(api_url(self.cve) + "/references")

        self.assertEqual(200, response.status)
        entries = response.jsonBody()["entries"]
        self.assertEqual(1, len(entries))
        self.assertEqual("XF", entries[0]["source"])
        self.assertEqual("example content", entries[0]["content"])
        self.assertEqual("http://example.com", entries[0]["url"])
