# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Webservice tests for distribution target milestones."""

__all__ = []

from datetime import date

from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.services.webapp.interfaces import OAuthPermission
from lp.services.webapp.publisher import canonical_url
from lp.testing import (
    ANONYMOUS,
    TestCaseWithFactory,
    api_url,
    login,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.pages import webservice_for_person


class TestMilestoneSearchWebservice(TestCaseWithFactory):
    """Tests for distribution target ``searchMilestones`` operations."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        login(ANONYMOUS)

    def getWebservice(self, person=None, version="devel"):
        if person is None:
            return webservice_for_person(None, default_api_version=version)
        return webservice_for_person(
            person,
            default_api_version=version,
            permission=OAuthPermission.READ_PUBLIC,
        )

    def searchMilestones(self, target, person=None, **parameters):
        login(ANONYMOUS)
        target_url = api_url(target)
        webservice = self.getWebservice(person)
        response = webservice.named_get(
            target_url, "searchMilestones", **parameters
        )
        return webservice, response

    def makeOrderedMilestones(self, distroseries):
        return {
            "alpha": self.factory.makeMilestone(
                distroseries=distroseries,
                name="alpha",
                dateexpected=date(2026, 1, 2),
            ),
            "beta": self.factory.makeMilestone(
                distroseries=distroseries,
                name="beta",
                dateexpected=date(2026, 1, 1),
                active=False,
            ),
            "gamma": self.factory.makeMilestone(
                distroseries=distroseries,
                name="gamma",
                dateexpected=date(2026, 1, 1),
            ),
        }

    def assertNames(self, expected, response):
        self.assertEqual(200, response.status)
        self.assertEqual(
            expected,
            [entry["name"] for entry in response.jsonBody()["entries"]],
        )

    def assertCollectionResourceType(self, response):
        self.assertEqual(
            "http://api.launchpad.test/devel/#milestone-page-resource",
            response.jsonBody()["resource_type_link"],
        )

    def test_operation_is_available_on_supported_targets_in_devel(self):
        distroseries = self.factory.makeDistroSeries()
        self.factory.makeMilestone(distroseries=distroseries, name="alpha")
        targets = (distroseries, distroseries.distribution)
        target_urls = [api_url(target) for target in targets]

        for target in targets:
            _webservice, response = self.searchMilestones(target)
            self.assertNames(["alpha"], response)
            self.assertCollectionResourceType(response)

        for version in ("beta", "1.0"):
            webservice = self.getWebservice(version=version)
            for target_url in target_urls:
                response = webservice.named_get(target_url, "searchMilestones")
                self.assertEqual(400, response.status, version)

    def test_public_visibility_is_scoped_to_the_series(self):
        distroseries = self.factory.makeDistroSeries()
        other_series = self.factory.makeDistroSeries(
            distribution=distroseries.distribution
        )
        self.factory.makeMilestone(distroseries=distroseries, name="included")
        self.factory.makeMilestone(distroseries=other_series, name="excluded")
        unrelated_person = self.factory.makePerson()

        for person in (None, unrelated_person):
            _webservice, response = self.searchMilestones(
                distroseries, person=person
            )
            self.assertNames(["included"], response)

    def test_distribution_includes_milestones_from_each_series(self):
        distroseries = self.factory.makeDistroSeries()
        distribution = distroseries.distribution
        other_series = self.factory.makeDistroSeries(distribution=distribution)
        other_distribution = self.factory.makeDistribution()
        self.factory.makeMilestone(
            distroseries=distroseries, name="included-one"
        )
        self.factory.makeMilestone(
            distroseries=other_series, name="included-two"
        )
        self.factory.makeMilestone(
            distribution=other_distribution, name="excluded"
        )

        _webservice, response = self.searchMilestones(
            distribution, order_by=["name"]
        )

        self.assertNames(["included-one", "included-two"], response)
        self.assertCollectionResourceType(response)

    def test_private_distribution_visibility(self):
        owner = self.factory.makePerson()
        unrelated_person = self.factory.makePerson()
        with person_logged_in(owner):
            distribution = self.factory.makeDistribution(
                owner=owner,
                information_type=InformationType.PROPRIETARY,
            )
            distroseries = self.factory.makeDistroSeries(
                distribution=distribution
            )
            self.factory.makeMilestone(
                distroseries=distroseries, name="private"
            )
            distribution_url = api_url(distribution)

        owner_webservice = webservice_for_person(
            owner,
            default_api_version="devel",
            permission=OAuthPermission.READ_PRIVATE,
        )
        response = owner_webservice.named_get(
            distribution_url, "searchMilestones"
        )
        self.assertNames(["private"], response)

        unrelated_webservice = webservice_for_person(
            unrelated_person,
            default_api_version="devel",
            permission=OAuthPermission.READ_PRIVATE,
        )
        response = unrelated_webservice.named_get(
            distribution_url, "searchMilestones"
        )
        self.assertEqual(404, response.status)

    def test_default_matches_existing_collections(self):
        distroseries = self.factory.makeDistroSeries()
        self.makeOrderedMilestones(distroseries)
        self.factory.makeMilestone(distroseries=distroseries, name="undated")
        expected_all = [
            milestone.name for milestone in distroseries.all_milestones
        ]
        expected_active = [
            milestone.name for milestone in distroseries.milestones
        ]
        distroseries_url = api_url(distroseries)
        webservice, response = self.searchMilestones(distroseries)

        self.assertNames(expected_all, response)
        distroseries_entry = webservice.get(distroseries_url).jsonBody()
        all_collection = webservice.get(
            distroseries_entry["all_milestones_collection_link"]
        ).jsonBody()
        active_collection = webservice.get(
            distroseries_entry["active_milestones_collection_link"]
        ).jsonBody()
        self.assertEqual(
            expected_all,
            [entry["name"] for entry in all_collection["entries"]],
        )
        self.assertEqual(
            expected_active,
            [entry["name"] for entry in active_collection["entries"]],
        )
        self.assertIn("beta", expected_all)
        self.assertNotIn("beta", expected_active)

    def test_active_filter(self):
        distroseries = self.factory.makeDistroSeries()
        self.makeOrderedMilestones(distroseries)

        _webservice, response = self.searchMilestones(
            distroseries, is_active=True, order_by=["name"]
        )
        self.assertNames(["alpha", "gamma"], response)
        _webservice, response = self.searchMilestones(
            distroseries, is_active=False, order_by=["name"]
        )
        self.assertNames(["beta"], response)
        _webservice, response = self.searchMilestones(
            distroseries, order_by=["name"]
        )
        self.assertNames(["alpha", "beta", "gamma"], response)

    def test_all_sort_keys_in_both_directions(self):
        distroseries = self.factory.makeDistroSeries()
        self.makeOrderedMilestones(distroseries)
        cases = (
            (["name"], ["alpha", "beta", "gamma"]),
            (["-name"], ["gamma", "beta", "alpha"]),
            (["date_targeted"], ["beta", "gamma", "alpha"]),
            (["-date_targeted"], ["alpha", "beta", "gamma"]),
            (
                ["date_targeted", "-name"],
                ["gamma", "beta", "alpha"],
            ),
        )

        for order_by, expected in cases:
            _webservice, response = self.searchMilestones(
                distroseries, order_by=order_by
            )
            self.assertNames(expected, response)

    def test_name_order_is_natural(self):
        # Version-like names order by number, not by character, so 1.2
        # comes before 1.10 as it does in the default order.
        distroseries = self.factory.makeDistroSeries()
        for name in ("1.10", "1.2", "1.9"):
            self.factory.makeMilestone(
                distroseries=distroseries,
                name=name,
                dateexpected=date(2026, 1, 1),
            )

        _webservice, response = self.searchMilestones(
            distroseries, order_by=["name"]
        )
        self.assertNames(["1.2", "1.9", "1.10"], response)
        _webservice, response = self.searchMilestones(
            distroseries, order_by=["-name"]
        )
        self.assertNames(["1.10", "1.9", "1.2"], response)
        _webservice, response = self.searchMilestones(
            distroseries, order_by=["date_targeted"]
        )
        self.assertNames(["1.2", "1.9", "1.10"], response)

    def test_paging_is_stable_when_name_keys_tie(self):
        # "1.2" and "1.02" order equally by name, so a further stable
        # tiebreaker has to keep page boundaries consistent.
        distroseries = self.factory.makeDistroSeries()
        for name in ("1.2", "1.02"):
            self.factory.makeMilestone(
                distroseries=distroseries,
                name=name,
                dateexpected=date(2026, 1, 1),
            )

        for order_by in ([], ["name"], ["date_targeted"]):
            message = "order_by=%r" % (order_by,)
            paged_names = []
            for start in (0, 1):
                _webservice, response = self.searchMilestones(
                    distroseries,
                    order_by=order_by,
                    **{"ws.start": start, "ws.size": 1},
                )
                self.assertEqual(200, response.status, message)
                body = response.jsonBody()
                self.assertEqual(start, body["start"], message)
                paged_names.extend(entry["name"] for entry in body["entries"])
            self.assertEqual(["1.2", "1.02"], paged_names, message)

    def test_unknown_sort_key_is_rejected(self):
        distroseries = self.factory.makeDistroSeries()
        _webservice, response = self.searchMilestones(
            distroseries, order_by=["unknown"]
        )

        self.assertEqual(400, response.status)
        self.assertIn(b"Unrecognized order_by: 'unknown'", response.body)

    def test_empty_result_is_a_collection(self):
        distroseries = self.factory.makeDistroSeries()
        _webservice, response = self.searchMilestones(distroseries)

        self.assertEqual(200, response.status)
        body = response.jsonBody()
        self.assertEqual(0, body["start"])
        self.assertEqual([], body["entries"])
        self.assertCollectionResourceType(response)

    def test_results_are_batched_with_stable_order(self):
        distroseries = self.factory.makeDistroSeries()
        self.makeOrderedMilestones(distroseries)
        webservice, response = self.searchMilestones(
            distroseries,
            order_by=["date_targeted"],
            **{"ws.size": 1},
        )

        body = response.jsonBody()
        self.assertEqual(0, body["start"])
        self.assertEqual(["beta"], [body["entries"][0]["name"]])
        self.assertCollectionResourceType(response)
        self.assertIsNotNone(body["next_collection_link"])
        next_page = webservice.get(body["next_collection_link"]).jsonBody()
        self.assertEqual(1, next_page["start"])
        self.assertEqual(
            "http://api.launchpad.test/devel/#milestone-page-resource",
            next_page["resource_type_link"],
        )
        self.assertEqual(
            ["gamma"], [entry["name"] for entry in next_page["entries"]]
        )

    def test_entries_use_existing_milestone_serialization(self):
        distroseries = self.factory.makeDistroSeries()
        milestone = self.factory.makeMilestone(
            distroseries=distroseries,
            name="alpha",
            dateexpected=date(2026, 4, 25),
        )
        naked_milestone = removeSecurityProxy(milestone)
        naked_milestone.code_name = "wombat"
        naked_milestone.summary = "Milestone summary."
        milestone_url = api_url(milestone)
        distroseries_url = api_url(distroseries)
        distribution_url = api_url(distroseries.distribution)
        milestone_web_link = canonical_url(milestone)
        milestone_title = milestone.title
        webservice, response = self.searchMilestones(distroseries)

        self.assertEqual(200, response.status)
        [entry] = response.jsonBody()["entries"]
        self.assertEqual(
            {
                "code_name",
                "date_targeted",
                "http_etag",
                "is_active",
                "name",
                "official_bug_tags",
                "release_link",
                "resource_type_link",
                "self_link",
                "series_target_link",
                "summary",
                "target_link",
                "title",
                "web_link",
            },
            set(entry),
        )
        self.assertEqual("alpha", entry["name"])
        self.assertEqual("wombat", entry["code_name"])
        self.assertEqual("Milestone summary.", entry["summary"])
        self.assertEqual("2026-04-25", entry["date_targeted"])
        self.assertTrue(entry["is_active"])
        self.assertEqual(milestone_title, entry["title"])
        self.assertEqual([], entry["official_bug_tags"])
        self.assertIsNone(entry["release_link"])
        self.assertEqual(
            webservice.getAbsoluteUrl(milestone_url),
            entry["self_link"],
        )
        self.assertEqual(milestone_web_link, entry["web_link"])
        self.assertEqual(
            webservice.getAbsoluteUrl(distribution_url),
            entry["target_link"],
        )
        self.assertEqual(
            webservice.getAbsoluteUrl(distroseries_url),
            entry["series_target_link"],
        )
        self.assertEqual(
            "http://api.launchpad.test/devel/#milestone",
            entry["resource_type_link"],
        )
        self.assertEqual(entry, webservice.get(entry["self_link"]).jsonBody())
