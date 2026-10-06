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


class TestBugAttachmentVulnerabilityPatches(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def _add_attachment(
        self, bug, owner, url=None, vulnerability_patches=None
    ):
        # Add the data inline so no librarian file is needed.
        with person_logged_in(owner):
            return bug.addAttachment(
                owner=owner,
                data=None,
                comment=None,
                filename=None,
                url=url,
                is_patch=vulnerability_patches is not None,
                description="test-package",
                vulnerability_patches=vulnerability_patches,
            )

    def test_vulnerability_patches_exported(self):
        # The stored list of patch mappings round-trips unchanged over the API
        patches = [
            {
                "name": "upstream",
                "value": "https://example.com/patch.diff",
                "comment": "fixes the issue",
            }
        ]
        owner = self.factory.makePerson()
        bug = self.factory.makeBug(owner=owner)
        attachment = self._add_attachment(
            bug, owner, vulnerability_patches=patches
        )
        attachment_url = api_url(attachment)
        logout()
        webservice = webservice_for_person(
            owner, permission=OAuthPermission.READ_PUBLIC
        )
        response = webservice.get(attachment_url, api_version="devel")
        self.assertEqual(200, response.status)
        self.assertEqual(patches, response.jsonBody()["vulnerability_patches"])

    def test_vulnerability_patches_unset(self):
        # A plain attachment has no patches, and should be serialized as None.
        owner = self.factory.makePerson()
        bug = self.factory.makeBug(owner=owner)
        # A URL attachment avoids uploading a file to the librarian.
        attachment = self._add_attachment(
            bug, owner, url="https://example.com/attachment"
        )
        attachment_url = api_url(attachment)
        user = self.factory.makePerson()
        logout()
        webservice = webservice_for_person(
            user, permission=OAuthPermission.READ_PUBLIC
        )
        response = webservice.get(attachment_url, api_version="devel")
        self.assertEqual(200, response.status)
        self.assertIsNone(response.jsonBody()["vulnerability_patches"])

    def test_vulnerability_patches_writable(self):
        # Someone with launchpad.Edit on the attachment can replace the
        # patches through the API.
        owner = self.factory.makePerson()
        bug = self.factory.makeBug(owner=owner)
        attachment = self._add_attachment(
            bug,
            owner,
            vulnerability_patches=[
                {"name": "upstream", "value": "https://example.com/old.diff"}
            ],
        )
        attachment_url = api_url(attachment)
        new_patches = [
            {
                "name": "debdiff",
                "value": "https://example.com/new.diff",
                "comment": "updated",
            }
        ]
        logout()
        webservice = webservice_for_person(
            owner,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.patch(
            attachment_url,
            "application/json",
            json.dumps({"vulnerability_patches": new_patches}),
        )
        self.assertEqual(209, response.status)
        with person_logged_in(owner):
            self.assertEqual(new_patches, attachment.vulnerability_patches)

    def test_vulnerability_patches_write_requires_edit(self):
        # A user without launchpad.Edit on the attachment cannot change the
        # patches.
        owner = self.factory.makePerson()
        bug = self.factory.makeBug(owner=owner)
        original_patches = [
            {"name": "upstream", "value": "https://example.com/old.diff"}
        ]
        attachment = self._add_attachment(
            bug, owner, vulnerability_patches=original_patches
        )
        attachment_url = api_url(attachment)
        other = self.factory.makePerson()
        logout()
        webservice = webservice_for_person(
            other,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.patch(
            attachment_url,
            "application/json",
            json.dumps(
                {
                    "vulnerability_patches": [
                        {"name": "x", "value": "https://example.com/x.diff"}
                    ]
                }
            ),
        )
        self.assertEqual(401, response.status)
        with person_logged_in(owner):
            self.assertEqual(
                original_patches, attachment.vulnerability_patches
            )

    def test_vulnerability_patches_conflict_with_url_rejected(self):
        # Setting patches on an attachment that has a URL returns 400.
        owner = self.factory.makePerson()
        bug = self.factory.makeBug(owner=owner)
        attachment = self._add_attachment(
            bug, owner, url="https://example.com/attachment"
        )
        attachment_url = api_url(attachment)
        logout()
        webservice = webservice_for_person(
            owner,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.patch(
            attachment_url,
            "application/json",
            json.dumps(
                {
                    "vulnerability_patches": [
                        {"name": "x", "value": "https://example.com/x.diff"}
                    ]
                }
            ),
        )
        self.assertEqual(400, response.status)
        with person_logged_in(owner):
            self.assertIsNone(attachment.vulnerability_patches)


class TestBugPresencesWebservice(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_presences_and_break_fix_data_exported(self):
        # A bug's presences are exposed as a collection on devel, and each
        # presence exports its break_fix_data.
        owner = self.factory.makePerson()
        bug = self.factory.makeBug(owner=owner)
        break_fix = [{"break": "abcdef123456", "fix": "654321fedcba"}]
        with person_logged_in(owner):
            self.factory.makeBugPresence(bug=bug, break_fix_data=break_fix)
        bug_url = api_url(bug)
        logout()
        webservice = webservice_for_person(
            owner,
            permission=OAuthPermission.READ_PUBLIC,
            default_api_version="devel",
        )
        response = webservice.get(bug_url)
        self.assertEqual(200, response.status)
        presences = webservice.get(
            response.jsonBody()["presences_collection_link"]
        ).jsonBody()
        self.assertEqual(1, len(presences["entries"]))
        self.assertEqual(break_fix, presences["entries"][0]["break_fix_data"])

    def _get_presence_representation(self, owner, presence_url):
        webservice = webservice_for_person(
            owner,
            permission=OAuthPermission.READ_PUBLIC,
            default_api_version="devel",
        )
        return webservice, webservice.get(presence_url).jsonBody()

    def test_presence_exports_source_package_name(self):
        # The source package name is exported as a plain string, and the
        # other targets are unset.
        owner = self.factory.makePerson()
        bug = self.factory.makeBug(owner=owner)
        spn = self.factory.makeSourcePackageName()
        with person_logged_in(owner):
            presence = self.factory.makeBugPresence(
                bug=bug, source_package_name=spn
            )
        presence_url = api_url(presence)
        logout()
        _, representation = self._get_presence_representation(
            owner, presence_url
        )
        self.assertEqual(spn.name, representation["source_package_name"])
        self.assertIsNone(representation["product_link"])
        self.assertIsNone(representation["distribution_link"])
        self.assertIsNone(representation["git_repository_link"])

    def test_presence_exports_product_distribution_and_git_repository(self):
        # Product, distribution and git repository are exported as links.
        owner = self.factory.makePerson()
        bug = self.factory.makeBug(owner=owner)
        targets = {
            "product": self.factory.makeProduct(),
            "distribution": self.factory.makeDistribution(),
            "git_repository": self.factory.makeGitRepository(),
        }
        target_urls = {}
        presence_urls = {}
        with person_logged_in(owner):
            for name, target in targets.items():
                presence = self.factory.makeBugPresence(
                    bug=bug, **{name: target}
                )
                target_urls[name] = api_url(target)
                presence_urls[name] = api_url(presence)
        logout()
        for name, presence_url in presence_urls.items():
            webservice, representation = self._get_presence_representation(
                owner, presence_url
            )
            self.assertEqual(
                webservice.getAbsoluteUrl(target_urls[name]),
                representation[name + "_link"],
            )
            self.assertIsNone(representation["source_package_name"])

    def _make_bug_with_moderator(self):
        # The product owner has a bug role, so they hold launchpad.Moderate.
        owner = self.factory.makePerson()
        product = self.factory.makeProduct(owner=owner)
        bug = self.factory.makeBug(target=product, owner=owner)
        return owner, bug

    def _write_webservice(self, person):
        return webservice_for_person(
            person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )

    def _add_presence(self, webservice, bug_url, spn):
        return webservice.named_post(
            bug_url,
            "addPresence",
            source_package_name=spn.name,
            break_fix_data=[{"break": "abc", "fix": "def"}],
        )

    def test_add_presence(self):
        # A user with launchpad.Moderate can add a presence via the API,
        # passing the source package by name.
        owner, bug = self._make_bug_with_moderator()
        spn = self.factory.makeSourcePackageName()
        bug_url = api_url(bug)
        logout()
        webservice = self._write_webservice(owner)
        response = self._add_presence(webservice, bug_url, spn)
        self.assertEqual(201, response.status)
        presence = webservice.get(response.getHeader("Location")).jsonBody()
        self.assertEqual(
            [{"break": "abc", "fix": "def"}], presence["break_fix_data"]
        )

    def test_add_presence_denied_to_unrelated_user(self):
        # An ordinary user, even on a public bug, cannot add a presence.
        owner, bug = self._make_bug_with_moderator()
        spn = self.factory.makeSourcePackageName()
        bug_url = api_url(bug)
        other = self.factory.makePerson()
        logout()
        response = self._add_presence(
            self._write_webservice(other), bug_url, spn
        )
        self.assertEqual(401, response.status)

    def test_add_presence_denied_to_bug_reporter_without_role(self):
        # Reporting the bug does not grant the right to add presences.
        product = self.factory.makeProduct()
        reporter = self.factory.makePerson()
        bug = self.factory.makeBug(target=product, owner=reporter)
        spn = self.factory.makeSourcePackageName()
        bug_url = api_url(bug)
        logout()
        response = self._add_presence(
            self._write_webservice(reporter), bug_url, spn
        )
        self.assertEqual(401, response.status)

    def test_add_presence_requires_exactly_one_target(self):
        # Providing no target is rejected with 400.
        owner, bug = self._make_bug_with_moderator()
        bug_url = api_url(bug)
        logout()
        webservice = self._write_webservice(owner)
        response = webservice.named_post(
            bug_url,
            "addPresence",
            break_fix_data=[{"break": "abc", "fix": "def"}],
        )
        self.assertEqual(400, response.status)

    def test_add_presence_rejects_bad_break_fix_data(self):
        # Break-fix entries without 'break' and 'fix' keys are rejected.
        owner, bug = self._make_bug_with_moderator()
        spn = self.factory.makeSourcePackageName()
        bug_url = api_url(bug)
        logout()
        webservice = self._write_webservice(owner)
        response = webservice.named_post(
            bug_url,
            "addPresence",
            source_package_name=spn.name,
            break_fix_data=[{"wrong": "key"}],
        )
        self.assertEqual(400, response.status)

    def test_remove_presence(self):
        # A user with launchpad.Moderate can remove a presence via the API.
        owner, bug = self._make_bug_with_moderator()
        with person_logged_in(owner):
            presence = self.factory.makeBugPresence(
                bug=bug, break_fix_data=[{"break": "a", "fix": "b"}]
            )
        bug_url = api_url(bug)
        presence_url = api_url(presence)
        logout()
        webservice = self._write_webservice(owner)
        response = webservice.named_post(
            bug_url, "removePresence", bug_presence=presence_url
        )
        self.assertEqual(200, response.status)
        with person_logged_in(owner):
            self.assertEqual(0, bug.presences.count())

    def test_remove_presence_denied_to_unrelated_user(self):
        # An ordinary user cannot remove a presence, and it is kept.
        owner, bug = self._make_bug_with_moderator()
        with person_logged_in(owner):
            presence = self.factory.makeBugPresence(
                bug=bug, break_fix_data=[{"break": "a", "fix": "b"}]
            )
        bug_url = api_url(bug)
        presence_url = api_url(presence)
        other = self.factory.makePerson()
        logout()
        response = self._write_webservice(other).named_post(
            bug_url, "removePresence", bug_presence=presence_url
        )
        self.assertEqual(401, response.status)
        with person_logged_in(owner):
            self.assertEqual(1, bug.presences.count())

    def test_remove_presence_from_other_bug_rejected(self):
        # A presence belonging to another bug cannot be removed by passing
        # its reference to this bug's removePresence, even by a moderator.
        owner, bug = self._make_bug_with_moderator()
        other_bug = self.factory.makeBug(owner=owner)
        with person_logged_in(owner):
            presence = self.factory.makeBugPresence(
                bug=other_bug, break_fix_data=[{"break": "a", "fix": "b"}]
            )
        bug_url = api_url(bug)
        presence_url = api_url(presence)
        logout()
        webservice = self._write_webservice(owner)
        response = webservice.named_post(
            bug_url, "removePresence", bug_presence=presence_url
        )
        self.assertEqual(400, response.status)
        with person_logged_in(owner):
            self.assertEqual(1, other_bug.presences.count())

    def test_traverse_nonexistent_presence_not_found(self):
        # Traversing to a +presence id that does not exist returns 404.
        owner, bug = self._make_bug_with_moderator()
        bug_url = api_url(bug)
        logout()
        webservice = self._write_webservice(owner)
        response = webservice.get("%s/+presence/0" % bug_url)
        self.assertEqual(404, response.status)
