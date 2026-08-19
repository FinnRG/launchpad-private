# Copyright 2009-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Webservice unit tests related to Launchpad Bugs."""

from lazr.restful.declarations import LAZR_WEBSERVICE_EXPORTED
from lazr.restful.fields import ReferenceChoice

from lp.app.enums import InformationType
from lp.bugs.interfaces.bugtask import (
    DB_INCOMPLETE_BUGTASK_STATUSES,
    BugTaskImportance,
    BugTaskStatus,
    BugTaskStatusSearch,
)
from lp.bugs.interfaces.webservice import IHasBugs
from lp.registry.interfaces.person import IPerson
from lp.services.webapp.interfaces import OAuthPermission
from lp.testing import TestCase, TestCaseWithFactory, api_url, person_logged_in
from lp.testing.layers import BaseLayer, DatabaseFunctionalLayer
from lp.testing.pages import LaunchpadWebServiceCaller, webservice_for_person


class TestSearchTasksCountsParameterSchema(TestCase):
    """Tests that count parameters do not mutate search parameters."""

    layer = BaseLayer

    def test_parameter_fields_are_independent(self):
        search_params = IHasBugs["searchTasks"].queryTaggedValue(
            LAZR_WEBSERVICE_EXPORTED
        )["params"]
        count_params = IHasBugs["searchTasksCounts"].queryTaggedValue(
            LAZR_WEBSERVICE_EXPORTED
        )["params"]

        self.assertIsNot(search_params["milestone"], count_params["milestone"])
        self.assertIsNot(
            search_params["milestone"].value_type,
            count_params["milestone"].value_type,
        )
        for params in (search_params, count_params):
            self.assertIsInstance(
                params["milestone"].value_type, ReferenceChoice
            )
            self.assertEqual(
                "Milestone", params["milestone"].value_type.vocabularyName
            )
            self.assertIs(IPerson, params["assignee"].schema)


class TestOmitTargetedParameter(TestCaseWithFactory):
    """Test all values for the omit_targeted search parameter."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.owner = self.factory.makePerson()
        with person_logged_in(self.owner):
            self.distro = self.factory.makeDistribution(name="mebuntu")
        self.release = self.factory.makeDistroSeries(
            name="inkanyamba", distribution=self.distro
        )
        self.bug = self.factory.makeBugTask(target=self.release)
        self.webservice = LaunchpadWebServiceCaller(
            "launchpad-library", "salgado-change-anything"
        )

    def test_omit_targeted_old_default_true(self):
        response = self.webservice.named_get(
            "/mebuntu/inkanyamba", "searchTasks", api_version="1.0"
        ).jsonBody()
        self.assertEqual(response["total_size"], 0)

    def test_omit_targeted_new_default_false(self):
        response = self.webservice.named_get(
            "/mebuntu/inkanyamba", "searchTasks", api_version="devel"
        ).jsonBody()
        self.assertEqual(response["total_size"], 1)


class TestProductSearchTasks(TestCaseWithFactory):
    """Tests Product bug task search and grouped count operations."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.owner = self.factory.makePerson()
        with person_logged_in(self.owner):
            self.product = self.factory.makeProduct()
        self.product_name = self.product.name
        self.bug = self.factory.makeBug(
            target=self.product,
            information_type=InformationType.PRIVATESECURITY,
        )
        self.webservice = LaunchpadWebServiceCaller(
            "launchpad-library", "salgado-change-anything"
        )

    def search(self, api_version, **kwargs):
        return self.webservice.named_get(
            "/%s" % self.product_name,
            "searchTasks",
            api_version=api_version,
            **kwargs,
        ).jsonBody()

    def test_linked_blueprints_in_devel(self):
        # Searching for linked Blueprints works in the devel API.
        self.search("devel", linked_blueprints="Show all bugs")

    def test_linked_blueprints_in_devel_2(self):
        # The linked_blueprints is considered. An error is returned if its
        # value is not a member of BugBlueprintSearch.
        self.assertRaises(
            ValueError, self.search, "devel", linked_blueprints="Teabags!"
        )

    def test_linked_blueprints_not_in_1_0(self):
        # Searching for linked Blueprints does not work in the 1.0 API. No
        # validation is performed for the linked_blueprints parameter, and
        # thus no error is returned when we pass rubbish.
        self.search("1.0", linked_blueprints="Teabags!")

    def test_linked_blueprints_not_in_beta(self):
        # Searching for linked Blueprints does not work in the beta API. No
        # validation is performed for the linked_blueprints parameter, and
        # thus no error is returned when we pass rubbish.
        self.search("beta", linked_blueprints="Teabags!")

    def test_search_returns_results(self):
        # A matching search returns results.
        response = self.search("devel", information_type="Private Security")
        self.assertEqual(response["total_size"], 1)

    def test_search_returns_no_results(self):
        # A non-matching search returns no results.
        response = self.search("devel", information_type="Private")
        self.assertEqual(response["total_size"], 0)

    def test_search_with_wrong_orderby(self):
        # Calling searchTasks() with a wrong order_by is a Bad Request.
        response = self.webservice.named_get(
            "/%s" % self.product_name,
            "searchTasks",
            api_version="devel",
            order_by="date_created",
        )
        self.assertEqual(400, response.status)
        self.assertRaisesWithContent(
            ValueError,
            "Unrecognized order_by: %r" % "date_created",
            response.jsonBody,
        )

    def test_search_incomplete_status_results(self):
        # The Incomplete status matches Incomplete with response and
        # Incomplete without response bug tasks.
        with person_logged_in(self.owner):
            self.factory.makeBug(
                target=self.product,
                status=BugTaskStatusSearch.INCOMPLETE_WITH_RESPONSE,
            )
            self.factory.makeBug(
                target=self.product,
                status=BugTaskStatusSearch.INCOMPLETE_WITHOUT_RESPONSE,
            )
        response = self.search("devel", status="Incomplete")
        self.assertEqual(response["total_size"], 2)

    def test_search_counts(self):
        response = self.webservice.named_get(
            "/%s" % self.product_name,
            "searchTasksCounts",
            api_version="devel",
        ).jsonBody()

        self.assertEqual(1, response["total"])
        self.assertEqual(1, response["by_status"]["NEW"])


class TestSearchTasksCountsPrivacy(TestCaseWithFactory):
    """Tests visibility filtering for grouped task counts."""

    layer = DatabaseFunctionalLayer

    def test_private_bug_visibility(self):
        owner = self.factory.makePerson()
        other_person = self.factory.makePerson()
        with person_logged_in(owner):
            product = self.factory.makeProduct(owner=owner)
            self.factory.makeBug(
                target=product,
                owner=owner,
                information_type=InformationType.PRIVATESECURITY,
            )
            product_url = api_url(product)
        owner_webservice = webservice_for_person(
            owner,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        other_webservice = webservice_for_person(
            other_person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )

        owner_response = owner_webservice.named_get(
            product_url, "searchTasksCounts"
        ).jsonBody()
        other_response = other_webservice.named_get(
            product_url, "searchTasksCounts"
        ).jsonBody()

        self.assertEqual(1, owner_response["total"])
        self.assertEqual(0, other_response["total"])


class TestDistroSeriesSearchTasksCounts(TestCaseWithFactory):
    """Tests for grouped task counts on a distro series."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.owner = self.factory.makePerson()
        with person_logged_in(self.owner):
            distribution = self.factory.makeDistribution(owner=self.owner)
            self.series = self.factory.makeDistroSeries(
                distribution=distribution
            )
            self.milestone = self.factory.makeMilestone(
                distroseries=self.series
            )
            self.series_url = api_url(self.series)
            self.milestone_url = api_url(self.milestone)
            self.owner_url = api_url(self.owner)
        self.webservice = LaunchpadWebServiceCaller(
            "launchpad-library", "salgado-change-anything"
        )

    def _make_task(self, status, importance, milestone=None):
        with person_logged_in(self.owner):
            task = self.factory.makeBugTask(
                owner=self.owner, target=self.series
            )
            if milestone is not None:
                task.transitionToMilestone(milestone, self.owner)
            task.transitionToStatus(status)
            task.transitionToImportance(importance)
        return task

    def _get_counts(self, **kwargs):
        return self.webservice.named_get(
            self.series_url,
            "searchTasksCounts",
            api_version="devel",
            **kwargs,
        ).jsonBody()

    def test_zero_result_response_shape(self):
        response = self._get_counts()

        self.assertEqual(
            {"total", "by_status", "by_importance"}, set(response)
        )
        self.assertEqual(0, response["total"])
        expected_statuses = {
            status.name
            for status in BugTaskStatusSearch.items
            if status not in DB_INCOMPLETE_BUGTASK_STATUSES
        }
        expected_importances = {
            importance.name for importance in BugTaskImportance.items
        }
        self.assertEqual(expected_statuses, set(response["by_status"]))
        self.assertEqual(expected_importances, set(response["by_importance"]))
        self.assertTrue(
            all(count == 0 for count in response["by_status"].values())
        )
        self.assertTrue(
            all(count == 0 for count in response["by_importance"].values())
        )

    def test_unknown_importance_is_counted(self):
        self._make_task(BugTaskStatus.NEW, BugTaskImportance.UNKNOWN)

        response = self._get_counts()

        self.assertEqual(1, response["total"])
        self.assertEqual(1, response["by_importance"]["UNKNOWN"])

    def test_returns_total_and_complete_groups(self):
        self._make_task(BugTaskStatus.NEW, BugTaskImportance.HIGH)
        self._make_task(BugTaskStatus.INCOMPLETE, BugTaskImportance.HIGH)
        self._make_task(BugTaskStatus.FIXRELEASED, BugTaskImportance.LOW)

        response = self._get_counts()

        self.assertEqual(
            {"total", "by_status", "by_importance"}, set(response)
        )
        self.assertEqual(2, response["total"])
        self.assertEqual(1, response["by_status"]["NEW"])
        self.assertEqual(1, response["by_status"]["INCOMPLETE"])
        self.assertEqual(0, response["by_status"]["FIXRELEASED"])
        self.assertEqual(2, response["by_importance"]["HIGH"])
        self.assertEqual(0, response["by_importance"]["LOW"])

    def test_status_filter_matches_search_tasks(self):
        self._make_task(BugTaskStatus.NEW, BugTaskImportance.HIGH)
        self._make_task(BugTaskStatus.FIXRELEASED, BugTaskImportance.LOW)

        response = self._get_counts(status="Fix Released")

        self.assertEqual(1, response["total"])
        self.assertEqual(1, response["by_status"]["FIXRELEASED"])
        self.assertEqual(1, response["by_importance"]["LOW"])

    def test_assignee_link_filter(self):
        task = self._make_task(BugTaskStatus.NEW, BugTaskImportance.HIGH)
        with person_logged_in(self.owner):
            task.transitionToAssignee(self.owner)
        self._make_task(BugTaskStatus.NEW, BugTaskImportance.LOW)

        response = self._get_counts(assignee=self.owner_url)

        self.assertEqual(1, response["total"])
        self.assertEqual(1, response["by_importance"]["HIGH"])

    def test_incomplete_statuses_are_combined(self):
        self._make_task(
            BugTaskStatusSearch.INCOMPLETE_WITH_RESPONSE,
            BugTaskImportance.HIGH,
        )
        self._make_task(
            BugTaskStatusSearch.INCOMPLETE_WITHOUT_RESPONSE,
            BugTaskImportance.LOW,
        )

        response = self._get_counts()

        self.assertEqual(2, response["by_status"]["INCOMPLETE"])
        self.assertNotIn("INCOMPLETE_WITH_RESPONSE", response["by_status"])
        self.assertNotIn("INCOMPLETE_WITHOUT_RESPONSE", response["by_status"])

    def test_tag_filter(self):
        tagged_task = self._make_task(
            BugTaskStatus.NEW, BugTaskImportance.HIGH
        )
        with person_logged_in(self.owner):
            tagged_task.bug.tags = ["included"]
        self._make_task(BugTaskStatus.NEW, BugTaskImportance.LOW)

        response = self._get_counts(tags="included")

        self.assertEqual(1, response["total"])
        self.assertEqual(1, response["by_importance"]["HIGH"])

    def test_omit_duplicates(self):
        original = self._make_task(BugTaskStatus.NEW, BugTaskImportance.HIGH)
        duplicate = self._make_task(BugTaskStatus.NEW, BugTaskImportance.LOW)
        with person_logged_in(self.owner):
            duplicate.bug.markAsDuplicate(original.bug)

        without_duplicates = self._get_counts()
        with_duplicates = self._get_counts(omit_duplicates=False)

        self.assertEqual(1, without_duplicates["total"])
        self.assertEqual(2, with_duplicates["total"])

    def test_milestone_link_filter(self):
        self._make_task(
            BugTaskStatus.INPROGRESS,
            BugTaskImportance.CRITICAL,
            milestone=self.milestone,
        )
        self._make_task(BugTaskStatus.NEW, BugTaskImportance.HIGH)

        response = self._get_counts(milestone=[self.milestone_url])

        self.assertEqual(1, response["total"])
        self.assertEqual(1, response["by_status"]["INPROGRESS"])
        self.assertEqual(1, response["by_importance"]["CRITICAL"])


class TestPersonSearchTasksCounts(TestCaseWithFactory):
    """Tests counts for the multi-alternative person task search."""

    layer = DatabaseFunctionalLayer

    def test_related_task_is_counted_once(self):
        person = self.factory.makePerson()
        with person_logged_in(person):
            product = self.factory.makeProduct(owner=person)
            self.factory.makeBug(
                target=product, owner=person
            ).default_bugtask.transitionToAssignee(person)
            person_url = api_url(person)
        webservice = LaunchpadWebServiceCaller(
            "launchpad-library", "salgado-change-anything"
        )

        response = webservice.named_get(
            person_url,
            "searchTasksCounts",
            api_version="devel",
        ).jsonBody()

        self.assertEqual(1, response["total"])
        self.assertEqual(1, response["by_status"]["NEW"])


class TestMaloneApplicationSearchTasks(TestCaseWithFactory):
    """Test the searchTasks operation on the top-level /bugs collection."""

    layer = DatabaseFunctionalLayer

    def test_global_search_by_tag(self):
        project1 = self.factory.makeProduct()
        project2 = self.factory.makeProduct()
        bug1 = self.factory.makeBug(target=project1, tags=["foo"])
        self.factory.makeBug(target=project1, tags=["bar"])
        bug3 = self.factory.makeBug(target=project2, tags=["foo"])
        self.factory.makeBug(target=project2, tags=["baz"])
        webservice = LaunchpadWebServiceCaller(
            "launchpad-library", "salgado-change-anything"
        )
        response = webservice.named_get(
            "/bugs", "searchTasks", api_version="devel", tags="foo"
        ).jsonBody()
        self.assertEqual(2, response["total_size"])
        self.assertContentEqual(
            [bug1.id, bug3.id],
            [
                int(entry["bug_link"].split("/")[-1])
                for entry in response["entries"]
            ],
        )


class TestGetBugData(TestCaseWithFactory):
    """Tests for the /bugs getBugData operation."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.owner = self.factory.makePerson()
        with person_logged_in(self.owner):
            self.product = self.factory.makeProduct()
        self.bug = self.factory.makeBug(
            target=self.product,
            information_type=InformationType.PRIVATESECURITY,
        )
        self.webservice = LaunchpadWebServiceCaller(
            "launchpad-library", "salgado-change-anything"
        )

    def search(self, api_version, **kwargs):
        return self.webservice.named_get(
            "/bugs", "getBugData", api_version=api_version, **kwargs
        ).jsonBody()

    def test_search_returns_results(self):
        # A matching search returns results.
        response = self.search("devel", bug_id=self.bug.id)
        self.assertEqual(self.bug.id, response[0]["id"])

    def test_search_returns_no_results(self):
        # A non-matching search returns no results.
        response = self.search("devel", bug_id=0)
        self.assertEqual(len(response), 0)
