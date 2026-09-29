# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Webservice unit tests related to Launchpad Bugs."""

import io
import json
import re
from datetime import datetime, timedelta, timezone

import six
from lazr.lifecycle.interfaces import IDoNotSnapshot
from storm.store import Store
from testtools.matchers import Equals, LessThan
from zope.component import getMultiAdapter
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.bugs.browser.bugtask import get_comments_for_bugtask
from lp.bugs.interfaces.bug import IBug
from lp.registry.enums import BugSharingPolicy, ExternalPackageType
from lp.registry.interfaces.product import License
from lp.services.beautifulsoup import BeautifulSoup
from lp.services.webapp import snapshot
from lp.services.webapp.interfaces import OAuthPermission
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.testing import (
    ANONYMOUS,
    RequestTimelineCollector,
    TestCaseWithFactory,
    api_url,
    login,
    logout,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.matchers import HasQueryCount
from lp.testing.pages import LaunchpadWebServiceCaller, webservice_for_person
from lp.testing.sampledata import ADMIN_EMAIL, USER_EMAIL


class TestBugConstraints(TestCaseWithFactory):
    """Test constraints on bug inputs over the API."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        product = self.factory.makeProduct(name="foo")
        self.bug = self.factory.makeBug(target=product)
        self.bug_url = api_url(self.bug)
        self.webservice = webservice_for_person(
            product.owner, permission=OAuthPermission.WRITE_PUBLIC
        )

    def _update_bug(self, nick):
        return self.webservice.patch(
            self.bug_url, "application/json", json.dumps({"name": nick})
        )

    def test_numeric_nicknames_fail(self):
        response = self._update_bug("1.1")
        self.assertEqual(400, response.status)

    def test_non_numeric_nicknames_pass(self):
        response = self._update_bug("bunny")
        self.assertEqual(209, response.status)
        login(ANONYMOUS)
        self.assertEqual("bunny", self.bug.name)


class TestBugDescriptionRepresentation(TestCaseWithFactory):
    """Test ways of interacting with Bug webservice representations."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        TestCaseWithFactory.setUp(self)
        login(ADMIN_EMAIL)
        # Make two bugs, one whose description points to the other, so it will
        # get turned into a HTML link.
        self.bug_one = self.factory.makeBug(title="generic")
        self.bug_two = self.factory.makeBug(
            description="Useless bugs are useless. See Bug %d."
            % (self.bug_one.id)
        )

        self.webservice = LaunchpadWebServiceCaller(
            "launchpad-library", "salgado-change-anything"
        )

    def findBugDescription(self, response):
        """Find the bug description field in an XHTML document fragment."""
        soup = BeautifulSoup(response.body)
        dt = soup.find("dt", text="description")
        dd = dt.find_next_sibling("dd")
        return str(dd.contents.pop())

    def test_GET_xhtml_representation(self):
        response = self.webservice.get(
            "/bugs/" + str(self.bug_two.id), "application/xhtml+xml"
        )
        self.assertEqual(response.status, 200)

        self.assertEqual(
            self.findBugDescription(response),
            "<p>Useless bugs are useless. "
            'See <a class="bug-link" href="/bugs/%d">Bug %d</a>.</p>'
            % (self.bug_one.id, self.bug_one.id),
        )

    def test_PATCH_xhtml_representation(self):
        new_description = "See bug %d" % self.bug_one.id

        bug_two_json = self.webservice.get(
            "/bugs/%d" % self.bug_two.id
        ).jsonBody()

        response = self.webservice.patch(
            bug_two_json["self_link"],
            "application/json",
            json.dumps(dict(description=new_description)),
            headers=dict(accept="application/xhtml+xml"),
        )

        self.assertEqual(response.status, 209)

        self.assertEqual(
            self.findBugDescription(response),
            '<p>See <a class="bug-link" href="/bugs/%d">bug %d</a></p>'
            % (self.bug_one.id, self.bug_one.id),
        )


class TestBugCommentRepresentation(TestCaseWithFactory):
    """Test ways of interacting with BugComment webservice representations."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        TestCaseWithFactory.setUp(self)
        login("guilherme.salgado@canonical.com ")
        self.bug = self.factory.makeBug()
        commenter = self.factory.makePerson()
        self.bug.newMessage(commenter, "Comment Subject", "Comment content")
        comments = get_comments_for_bugtask(self.bug.bugtasks[0])
        self.comment = comments[1]
        comment_view = getMultiAdapter(
            (self.comment, LaunchpadTestRequest()), name="+box"
        )
        self.expected_comment_html = str(comment_view())
        self.message_path = "/%s/+bug/%s/comments/1" % (
            self.bug.bugtasks[0].product.name,
            self.bug.id,
        )
        self.webservice = LaunchpadWebServiceCaller(
            "launchpad-library", "salgado-change-anything"
        )

    def assertRenderedCommentsEqual(self, a_comment, another_comment):
        """Assert that two rendered comments are equal.

        It replaces parts that depend of the current time with fixed
        strings, so that two comments rendered at different times are
        still considered equal.
        """
        when_regexp = re.compile(r">\d+ .*? ago<")
        a_comment = when_regexp.sub(">WHEN<", a_comment)
        another_comment = when_regexp.sub(">WHEN<", another_comment)
        self.assertEqual(a_comment, another_comment)

    def test_GET_xhtml_representation(self):
        # The XHTML of a BugComment is exactly the same as how it's
        # rendered in the web UI. The existing +box view is re-used to
        # render it.
        response = self.webservice.get(
            self.message_path, "application/xhtml+xml"
        )

        self.assertEqual(response.status, 200)

        rendered_comment = six.ensure_text(response.body)
        self.assertRenderedCommentsEqual(
            rendered_comment, self.expected_comment_html
        )


class TestBugScaling(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def test_attachments_query_counts_constant(self):
        # XXX j.c.sackett 2010-09-02 bug=619017
        # This test was being thrown off by the reference bug. To get around
        # the problem, flush and invalidate are called on the bug storm cache
        # before each call to the webservice. When lp's storm is updated
        # to release the committed fix for this bug, please see about
        # updating this test.
        login(USER_EMAIL)
        self.bug = self.factory.makeBug()
        store = Store.of(self.bug)
        self.factory.makeBugAttachment(self.bug)
        self.factory.makeBugAttachment(self.bug)
        webservice = LaunchpadWebServiceCaller(
            "launchpad-library", "salgado-change-anything"
        )
        collector = RequestTimelineCollector()
        collector.register()
        self.addCleanup(collector.unregister)
        url = "/bugs/%d/attachments?ws.size=75" % self.bug.id
        # First request.
        store.flush()
        store.invalidate()
        response = webservice.get(url)
        self.assertThat(collector, HasQueryCount(LessThan(24)))
        with_2_count = collector.count
        self.assertEqual(response.status, 200)
        login(USER_EMAIL)
        for _ in range(5):
            self.factory.makeBugAttachment(self.bug)
        logout()
        # Second request.
        store.flush()
        store.invalidate()
        response = webservice.get(url)
        self.assertThat(collector, HasQueryCount(Equals(with_2_count)))

    def test_messages_query_counts_constant(self):
        # XXX Robert Collins 2010-09-15 bug=619017
        # This test may be thrown off by the reference bug. To get around the
        # problem, flush and invalidate are called on the bug storm cache
        # before each call to the webservice. When lp's storm is updated to
        # release the committed fix for this bug, please see about updating
        # this test.
        login(USER_EMAIL)
        bug = self.factory.makeBug()
        store = Store.of(bug)
        self.factory.makeBugComment(bug)
        self.factory.makeBugComment(bug)
        self.factory.makeBugComment(bug)
        webservice = LaunchpadWebServiceCaller(
            "launchpad-library", "salgado-change-anything"
        )
        collector = RequestTimelineCollector()
        collector.register()
        self.addCleanup(collector.unregister)
        url = "/bugs/%d/messages?ws.size=75" % bug.id
        # First request.
        store.flush()
        store.invalidate()
        response = webservice.get(url)
        self.assertThat(collector, HasQueryCount(LessThan(24)))
        with_2_count = collector.count
        self.assertEqual(response.status, 200)
        login(USER_EMAIL)
        for _ in range(50):
            self.factory.makeBugComment(bug)
        self.factory.makeBugAttachment(bug)
        logout()
        # Second request.
        store.flush()
        store.invalidate()
        response = webservice.get(url)
        self.assertThat(collector, HasQueryCount(Equals(with_2_count)))


class TestBugMessages(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp(USER_EMAIL)
        self.bug = self.factory.makeBug()
        self.bug_url = api_url(self.bug)
        self.message1 = self.factory.makeMessage()
        self.message2 = self.factory.makeMessage(parent=self.message1)
        # Only link message2 to the bug.
        self.bug.linkMessage(self.message2)
        self.webservice = webservice_for_person(self.bug.owner)

    def test_messages(self):
        # When one of the messages on a bug is linked to a parent that
        # isn't linked to the bug, the webservice should still include
        # that message in the bug's associated messages.
        ws_bug = self.getWebserviceJSON(self.webservice, self.bug_url)
        ws_messages = self.getWebserviceJSON(
            self.webservice, ws_bug["messages_collection_link"]
        )
        latest_message = ws_messages["entries"][-1]
        self.assertEqual(self.message2.subject, latest_message["subject"])

        # The parent_link for the latest message should be None
        # because the parent is not a member of this bug's messages
        # collection itself.
        self.assertIsNone(latest_message["parent_link"])


class TestPostBugWithLargeCollections(TestCaseWithFactory):
    """Ensure that large IBug collections cause OOPSes on POSTs for IBug.

    When a POST request on a bug is processed, a snapshot of the bug
    is created. This can lead to OOPSes as described in bugs 507642,
    505999, 534339: A snapshot of a database collection field is a
    shortlist() copy of the data and the creation of the snapshot fails
    if a collection contains more elements than the hard limit of the
    sortlist().

    Hence we do not include properties in the snapshot that may have
    a large number of elements: messages, bug subscriptions and
    (un)affected users.
    """

    layer = DatabaseFunctionalLayer

    def test_no_snapshots_for_large_collections(self):
        # Ensure that no snapshots are made of the properties comments,
        # bug subscriptions and (un)affected users.
        for field_name in (
            "subscriptions",
            "users_affected",
            "users_unaffected",
            "users_affected_with_dupes",
            "messages",
            "attachments",
            "activity",
        ):
            self.assertTrue(
                IDoNotSnapshot.providedBy(IBug[field_name]),
                "IBug.%s should not be included in snapshots, see bug 507642."
                % field_name,
            )

    def test_many_subscribers(self):
        # Many subscriptions do not cause an OOPS for IBug POSTs.
        bug = self.factory.makeBug()
        bug_url = api_url(bug)

        real_hard_limit_for_snapshot = snapshot.HARD_LIMIT_FOR_SNAPSHOT
        snapshot.HARD_LIMIT_FOR_SNAPSHOT = 3

        webservice = webservice_for_person(
            bug.owner, permission=OAuthPermission.WRITE_PUBLIC
        )
        try:
            login(ADMIN_EMAIL)
            for _ in range(snapshot.HARD_LIMIT_FOR_SNAPSHOT + 1):
                person = self.factory.makePerson()
                bug.subscribe(person, person)
            logout()

            # Adding one more subscriber through the web service
            # doesn't cause an OOPS.
            response = webservice.named_post(
                bug_url, "subscribe", person="/~name12"
            )
            self.assertEqual(200, response.status, response.body)
        finally:
            snapshot.HARD_LIMIT_FOR_SNAPSHOT = real_hard_limit_for_snapshot


class TestErrorHandling(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def test_add_duplicate_bugtask_for_project_gives_bad_request(self):
        bug = self.factory.makeBug()
        bug_url = api_url(bug)
        product = self.factory.makeProduct()
        product_url = api_url(product)
        self.factory.makeBugTask(bug=bug, target=product)

        webservice = webservice_for_person(
            bug.owner, permission=OAuthPermission.WRITE_PUBLIC
        )
        response = webservice.named_post(
            bug_url, "addTask", target=product_url
        )
        self.assertEqual(400, response.status)

    def test_add_invalid_bugtask_to_proprietary_bug_gives_bad_request(self):
        # Test we get an error when we attempt to invalidly add a bug task to
        # a proprietary bug. In this case, we cannot mark a proprietary bug
        # as affecting more than one project.
        owner = self.factory.makePerson()
        product1 = self.factory.makeProduct(
            bug_sharing_policy=BugSharingPolicy.PROPRIETARY
        )
        product2 = self.factory.makeProduct(
            bug_sharing_policy=BugSharingPolicy.PROPRIETARY
        )
        product2_url = api_url(product2)
        bug = self.factory.makeBug(
            target=product1,
            owner=owner,
            information_type=InformationType.PROPRIETARY,
        )
        bug_url = api_url(bug)

        webservice = webservice_for_person(
            owner, permission=OAuthPermission.WRITE_PRIVATE
        )
        response = webservice.named_post(
            bug_url, "addTask", target=product2_url
        )
        self.assertEqual(400, response.status)

    def test_add_attachment_with_bad_filename_raises_exception(self):
        # Test that addAttachment raises BadRequest when the filename given
        # contains slashes.
        owner = self.factory.makePerson()
        bug = self.factory.makeBug(owner=owner)
        bug_url = api_url(bug)

        webservice = webservice_for_person(
            owner, permission=OAuthPermission.WRITE_PUBLIC
        )
        response = webservice.named_post(
            bug_url,
            "addAttachment",
            comment="foo",
            data=io.BytesIO(b"foo"),
            filename="/home/foo/bar.txt",
        )
        self.assertEqual(400, response.status)


class BugSetTestCase(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def makeAPITarget(self, bug_policy):
        project = self.factory.makeProduct(
            licenses=[License.OTHER_PROPRIETARY]
        )
        target_url = api_url(project)
        with person_logged_in(project.owner):
            project.setBugSharingPolicy(bug_policy)
        return project, target_url

    def test_default_sharing_policy_proprietary(self):
        # Verify the path through user submission, to MaloneApplication to
        # BugSet, and back to the user creates a private bug according
        # to the project's bug sharing policy.
        target, target_url = self.makeAPITarget(
            BugSharingPolicy.PROPRIETARY_OR_PUBLIC
        )
        webservice = webservice_for_person(
            target.owner, permission=OAuthPermission.WRITE_PRIVATE
        )
        response = webservice.named_post(
            "/bugs",
            "createBug",
            target=target_url,
            title="title",
            description="desc",
        )
        self.assertEqual(201, response.status)
        ws_bug = webservice.get(response.getHeader("Location")).jsonBody()
        self.assertEqual("Proprietary", ws_bug["information_type"])

    def test_override_default_sharing_policy_proprietary(self):
        # A Proprietary bug can be created if the project's bug sharing policy
        # permits it.
        target, target_url = self.makeAPITarget(
            BugSharingPolicy.PUBLIC_OR_PROPRIETARY
        )
        webservice = webservice_for_person(
            target.owner, permission=OAuthPermission.WRITE_PRIVATE
        )
        response = webservice.named_post(
            "/bugs",
            "createBug",
            target=target_url,
            title="title",
            description="desc",
            information_type="Proprietary",
        )
        self.assertEqual(201, response.status)
        ws_bug = webservice.get(response.getHeader("Location")).jsonBody()
        self.assertEqual("Proprietary", ws_bug["information_type"])


class TestBugDateLastUpdated(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def make_old_bug(self):
        bug = self.factory.makeBug()
        one_year_ago = datetime.now(timezone.utc) - timedelta(days=365)
        removeSecurityProxy(bug).date_last_updated = one_year_ago
        owner = bug.owner
        with person_logged_in(owner):
            webservice = webservice_for_person(
                owner, permission=OAuthPermission.WRITE_PUBLIC
            )
        return (bug, owner, webservice)

    def test_subscribe_does_not_update(self):
        # Calling subscribe over the API does not update date_last_updated.
        (bug, owner, webservice) = self.make_old_bug()
        subscriber = self.factory.makePerson()
        date_last_updated = bug.date_last_updated
        api_sub = api_url(subscriber)
        bug_url = api_url(bug)
        logout()
        response = webservice.named_post(bug_url, "subscribe", person=api_sub)
        self.assertEqual(200, response.status)
        with person_logged_in(owner):
            self.assertEqual(date_last_updated, bug.date_last_updated)

    def test_change_status_does_update(self):
        # Changing the status of a bugtask does change date_last_updated.
        (bug, owner, webservice) = self.make_old_bug()
        task_url = api_url(bug.default_bugtask)
        date_last_updated = bug.date_last_updated
        logout()
        response = webservice.patch(
            task_url, "application/json", json.dumps(dict(status="Invalid"))
        )
        self.assertEqual(209, response.status)
        with person_logged_in(owner):
            self.assertNotEqual(date_last_updated, bug.date_last_updated)


class TestBugTaskExternalPackageAttributes(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_package_type_channel_and_metadata(self):
        externalpackage = self.factory.makeExternalPackage(
            packagetype=ExternalPackageType.SNAP,
            channel=("12.1", "stable", None),
        )
        bug = self.factory.makeBug(target=externalpackage)
        task = bug.default_bugtask
        removeSecurityProxy(task).metadata = {"key": "value"}
        user = self.factory.makePerson()
        task_url = api_url(task)
        logout()
        webservice = webservice_for_person(
            user, permission=OAuthPermission.READ_PUBLIC
        )
        response = webservice.get(task_url, api_version="devel")
        self.assertEqual(200, response.status)
        representation = response.jsonBody()
        self.assertEqual("Snap", representation["package_type"])
        self.assertEqual(["12.1", "stable", None], representation["channel"])
        self.assertEqual({"key": "value"}, representation["metadata"])

    def test_unset_values(self):
        task = self.factory.makeBug().default_bugtask
        user = self.factory.makePerson()
        task_url = api_url(task)
        logout()
        webservice = webservice_for_person(
            user, permission=OAuthPermission.READ_PUBLIC
        )
        representation = webservice.get(
            task_url, api_version="devel"
        ).jsonBody()
        self.assertIsNone(representation["package_type"])
        self.assertIsNone(representation["channel"])
        self.assertIsNone(representation["metadata"])
