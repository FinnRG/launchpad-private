# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test charm recipe build views."""

import re

import soupmatchers
import transaction
from fixtures import FakeLogger
from pymacaroons import Macaroon
from storm.locals import Store
from testtools.matchers import StartsWith
from zope.component import getUtility
from zope.security.interfaces import Unauthorized
from zope.security.management import checkPermission
from zope.security.proxy import removeSecurityProxy
from zope.testbrowser.browser import LinkNotFoundError

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.buildmaster.enums import BuildStatus
from lp.charms.interfaces.charmrecipe import CHARM_RECIPE_ALLOW_CREATE
from lp.charms.interfaces.charmrecipebuildjob import ICharmhubUploadJobSource
from lp.services.features.testing import FeatureFixture
from lp.services.job.interfaces.job import JobStatus
from lp.services.webapp import canonical_url
from lp.testing import (
    ANONYMOUS,
    BrowserTestCase,
    TestCaseWithFactory,
    login,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.pages import (
    extract_text,
    find_main_content,
    find_tags_by_class,
)
from lp.testing.views import create_initialized_view


class TestCanonicalUrlForCharmRecipeBuild(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))

    def test_canonical_url(self):
        owner = self.factory.makePerson(name="person")
        project = self.factory.makeProduct(name="charm-project")
        recipe = self.factory.makeCharmRecipe(
            registrant=owner, owner=owner, project=project, name="charm"
        )
        build = self.factory.makeCharmRecipeBuild(
            requester=owner, recipe=recipe
        )
        self.assertThat(
            canonical_url(build),
            StartsWith(
                "http://launchpad.test/~person/charm-project/+charm/charm/"
                "+build/"
            ),
        )


class TestCharmRecipeBuildView(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))

    def test_files(self):
        # CharmRecipeBuildView.files returns all the associated files.
        build = self.factory.makeCharmRecipeBuild(
            status=BuildStatus.FULLYBUILT
        )
        charm_file = self.factory.makeCharmFile(build=build)
        build_view = create_initialized_view(build, "+index")
        self.assertEqual(
            [charm_file.library_file.filename],
            [lfa.filename for lfa in build_view.files],
        )
        # Deleted files won't be included.
        self.assertFalse(charm_file.library_file.deleted)
        removeSecurityProxy(charm_file.library_file).content = None
        self.assertTrue(charm_file.library_file.deleted)
        build_view = create_initialized_view(build, "+index")
        self.assertEqual([], build_view.files)

    def test_revision_id(self):
        build = self.factory.makeCharmRecipeBuild()
        build.updateStatus(
            BuildStatus.FULLYBUILT, worker_status={"revision_id": "dummy"}
        )
        build_view = create_initialized_view(build, "+index")
        self.assertThat(
            build_view(),
            soupmatchers.HTMLContains(
                soupmatchers.Tag(
                    "revision ID",
                    "li",
                    attrs={"id": "revision-id"},
                    text=re.compile(r"^\s*Revision: dummy\s*$"),
                )
            ),
        )

    def test_store_upload_status_in_progress(self):
        build = self.factory.makeCharmRecipeBuild(
            status=BuildStatus.FULLYBUILT
        )
        getUtility(ICharmhubUploadJobSource).create(build)
        build_view = create_initialized_view(build, "+index")
        self.assertThat(
            build_view(),
            soupmatchers.HTMLContains(
                soupmatchers.Tag(
                    "store upload status",
                    "li",
                    attrs={"id": "store-upload-status"},
                    text=re.compile(r"^\s*Charmhub upload in progress\s*$"),
                )
            ),
        )

    def test_store_upload_status_completed(self):
        build = self.factory.makeCharmRecipeBuild(
            status=BuildStatus.FULLYBUILT
        )
        job = getUtility(ICharmhubUploadJobSource).create(build)
        naked_job = removeSecurityProxy(job)
        naked_job.job._status = JobStatus.COMPLETED
        build_view = create_initialized_view(build, "+index")
        self.assertThat(
            build_view(),
            soupmatchers.HTMLContains(
                soupmatchers.Tag(
                    "store upload status",
                    "li",
                    attrs={"id": "store-upload-status"},
                    text=re.compile(r"^\s*Uploaded to Charmhub\s*$"),
                )
            ),
        )

    def test_store_upload_status_failed(self):
        build = self.factory.makeCharmRecipeBuild(
            status=BuildStatus.FULLYBUILT
        )
        job = getUtility(ICharmhubUploadJobSource).create(build)
        naked_job = removeSecurityProxy(job)
        naked_job.job._status = JobStatus.FAILED
        naked_job.error_message = "Review failed."
        build_view = create_initialized_view(build, "+index")
        self.assertThat(
            build_view(),
            soupmatchers.HTMLContains(
                soupmatchers.Tag(
                    "store upload status",
                    "li",
                    attrs={"id": "store-upload-status"},
                    text=re.compile(
                        r"^\s*Charmhub upload failed:\s+" r"Review failed.\s*$"
                    ),
                )
            ),
        )

    def test_store_upload_status_release_failed(self):
        build = self.factory.makeCharmRecipeBuild(
            status=BuildStatus.FULLYBUILT
        )
        job = getUtility(ICharmhubUploadJobSource).create(build)
        naked_job = removeSecurityProxy(job)
        naked_job.job._status = JobStatus.FAILED
        naked_job.store_revision = 1
        naked_job.error_message = "Failed to publish"
        build_view = create_initialized_view(build, "+index")
        self.assertThat(
            build_view(),
            soupmatchers.HTMLContains(
                soupmatchers.Tag(
                    "store upload status",
                    "li",
                    attrs={"id": "store-upload-status"},
                    text=re.compile(
                        r"^\s*Charmhub release failed:\s+"
                        r"Failed to publish\s*$"
                    ),
                )
            ),
        )


class TestCharmRecipeBuildOperations(BrowserTestCase):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))
        self.useFixture(FakeLogger())
        self.build = self.factory.makeCharmRecipeBuild()
        self.build_url = canonical_url(self.build)
        self.requester = self.build.requester
        self.buildd_admin = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).buildd_admin]
        )
        self.launchpad_developer = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).launchpad_developers]
        )

    def test_retry_build(self):
        # The requester of a build can retry it.
        self.build.updateStatus(BuildStatus.FAILEDTOBUILD)
        transaction.commit()
        browser = self.getViewBrowser(self.build, user=self.requester)
        browser.getLink("Retry this build").click()
        self.assertEqual(self.build_url, browser.getLink("Cancel").url)
        browser.getControl("Retry build").click()
        self.assertEqual(self.build_url, browser.url)
        login(ANONYMOUS)
        self.assertEqual(BuildStatus.NEEDSBUILD, self.build.status)

    def test_retry_build_random_user(self):
        # An unrelated non-admin user cannot retry a build.
        self.build.updateStatus(BuildStatus.FAILEDTOBUILD)
        transaction.commit()
        user = self.factory.makePerson()
        browser = self.getViewBrowser(self.build, user=user)
        self.assertRaises(
            LinkNotFoundError, browser.getLink, "Retry this build"
        )
        self.assertRaises(
            Unauthorized,
            self.getUserBrowser,
            self.build_url + "/+retry",
            user=user,
        )

    def test_retry_build_wrong_state(self):
        # If the build isn't in an unsuccessful terminal state, you can't
        # retry it.
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        browser = self.getViewBrowser(self.build, user=self.requester)
        self.assertRaises(
            LinkNotFoundError, browser.getLink, "Retry this build"
        )

    def test_cancel_build(self):
        # The requester of a build can cancel it.
        self.build.queueBuild()
        transaction.commit()
        browser = self.getViewBrowser(self.build, user=self.requester)
        browser.getLink("Cancel build").click()
        self.assertEqual(self.build_url, browser.getLink("Cancel").url)
        browser.getControl("Cancel build").click()
        self.assertEqual(self.build_url, browser.url)
        login(ANONYMOUS)
        self.assertEqual(BuildStatus.CANCELLED, self.build.status)

    def test_cancel_build_random_user(self):
        # An unrelated non-admin user cannot cancel a build.
        self.build.queueBuild()
        transaction.commit()
        user = self.factory.makePerson()
        browser = self.getViewBrowser(self.build, user=user)
        self.assertRaises(LinkNotFoundError, browser.getLink, "Cancel build")
        self.assertRaises(
            Unauthorized,
            self.getUserBrowser,
            self.build_url + "/+cancel",
            user=user,
        )

    def test_cancel_build_wrong_state(self):
        # If the build isn't queued, you can't cancel it.
        browser = self.getViewBrowser(self.build, user=self.requester)
        self.assertRaises(LinkNotFoundError, browser.getLink, "Cancel build")

    def test_rescore_build(self):
        # A buildd admin can rescore a build.
        self.build.queueBuild()
        transaction.commit()
        browser = self.getViewBrowser(self.build, user=self.buildd_admin)
        browser.getLink("Rescore build").click()
        self.assertEqual(self.build_url, browser.getLink("Cancel").url)
        browser.getControl("Priority").value = "1024"
        browser.getControl("Rescore build").click()
        self.assertEqual(self.build_url, browser.url)
        login(ANONYMOUS)
        self.assertEqual(1024, self.build.buildqueue_record.lastscore)

    def test_launchpad_developer_can_rescore_build(self):
        self.build.queueBuild()
        builder = self.factory.makeBuilder()
        transaction.commit()
        browser = self.getViewBrowser(
            self.build, user=self.launchpad_developer
        )
        browser.getLink("Rescore build").click()
        browser.getControl("Priority").value = "1024"
        browser.getControl("Rescore build").click()
        self.assertEqual(self.build_url, browser.url)
        with person_logged_in(self.launchpad_developer):
            self.assertFalse(checkPermission("launchpad.Admin", builder))
        login(ANONYMOUS)
        self.assertEqual(1024, self.build.buildqueue_record.lastscore)

    def test_rescore_build_invalid_score(self):
        # Build scores can only take numbers.
        self.build.queueBuild()
        transaction.commit()
        browser = self.getViewBrowser(self.build, user=self.buildd_admin)
        browser.getLink("Rescore build").click()
        self.assertEqual(self.build_url, browser.getLink("Cancel").url)
        browser.getControl("Priority").value = "tentwentyfour"
        browser.getControl("Rescore build").click()
        self.assertEqual(
            "Invalid integer data",
            extract_text(find_tags_by_class(browser.contents, "message")[1]),
        )

    def test_rescore_build_not_admin(self):
        # A non-admin user cannot cancel a build.
        self.build.queueBuild()
        transaction.commit()
        user = self.factory.makePerson()
        browser = self.getViewBrowser(self.build, user=user)
        self.assertRaises(LinkNotFoundError, browser.getLink, "Rescore build")
        self.assertRaises(
            Unauthorized,
            self.getUserBrowser,
            self.build_url + "/+rescore",
            user=user,
        )

    def test_rescore_build_wrong_state(self):
        # If the build isn't NEEDSBUILD, you can't rescore it.
        self.build.queueBuild()
        with person_logged_in(self.requester):
            self.build.cancel()
        browser = self.getViewBrowser(self.build, user=self.buildd_admin)
        self.assertRaises(LinkNotFoundError, browser.getLink, "Rescore build")

    def test_rescore_build_wrong_state_stale_link(self):
        # An attempt to rescore a non-queued build from a stale link shows a
        # sensible error message.
        self.build.queueBuild()
        with person_logged_in(self.requester):
            self.build.cancel()
        browser = self.getViewBrowser(
            self.build, "+rescore", user=self.buildd_admin
        )
        self.assertEqual(self.build_url, browser.url)
        self.assertThat(
            browser.contents,
            soupmatchers.HTMLContains(
                soupmatchers.Tag(
                    "notification",
                    "div",
                    attrs={"class": "warning message"},
                    text="Cannot rescore this build because it is not queued.",
                )
            ),
        )

    def setUpStoreUpload(self):
        self.pushConfig("charms", charmhub_url="http://charmhub.example/")
        with person_logged_in(self.requester):
            self.build.recipe.store_name = self.factory.getUniqueUnicode()
            # CharmRecipe.can_upload_to_store only checks whether
            # "exchanged_encrypted" is present, so don't bother setting up
            # encryption keys here.
            self.build.recipe.store_secrets = {
                "exchanged_encrypted": Macaroon().serialize()
            }

    def test_store_upload(self):
        # A build not previously uploaded to Charmhub can be uploaded
        # manually.
        self.setUpStoreUpload()
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.factory.makeCharmFile(
            build=self.build,
            library_file=self.factory.makeLibraryFileAlias(db_only=True),
        )
        browser = self.getViewBrowser(self.build, user=self.requester)
        browser.getControl("Upload this charm to Charmhub").click()
        self.assertEqual(self.build_url, browser.url)
        login(ANONYMOUS)
        [job] = getUtility(ICharmhubUploadJobSource).iterReady()
        self.assertEqual(JobStatus.WAITING, job.job.status)
        self.assertEqual(self.build, job.build)
        self.assertEqual(
            "An upload has been scheduled and will run as soon as possible.",
            extract_text(find_tags_by_class(browser.contents, "message")[0]),
        )

    def test_store_upload_retry(self):
        # A build with a previously-failed Charmhub upload can have the
        # upload retried.
        self.setUpStoreUpload()
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.factory.makeCharmFile(
            build=self.build,
            library_file=self.factory.makeLibraryFileAlias(db_only=True),
        )
        old_job = getUtility(ICharmhubUploadJobSource).create(self.build)
        removeSecurityProxy(old_job).job._status = JobStatus.FAILED
        browser = self.getViewBrowser(self.build, user=self.requester)
        browser.getControl("Retry").click()
        self.assertEqual(self.build_url, browser.url)
        login(ANONYMOUS)
        [job] = getUtility(ICharmhubUploadJobSource).iterReady()
        self.assertEqual(JobStatus.WAITING, job.job.status)
        self.assertEqual(self.build, job.build)
        self.assertEqual(
            "An upload has been scheduled and will run as soon as possible.",
            extract_text(find_tags_by_class(browser.contents, "message")[0]),
        )

    def test_store_upload_error_notifies(self):
        # If a build cannot be scheduled for uploading to Charmhub, we issue
        # a notification.
        self.setUpStoreUpload()
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        browser = self.getViewBrowser(self.build, user=self.requester)
        browser.getControl("Upload this charm to Charmhub").click()
        self.assertEqual(self.build_url, browser.url)
        login(ANONYMOUS)
        self.assertEqual(
            [], list(getUtility(ICharmhubUploadJobSource).iterReady())
        )
        self.assertEqual(
            "Cannot upload this charm because it has no files.",
            extract_text(find_tags_by_class(browser.contents, "message")[0]),
        )

    def test_builder_history(self):
        Store.of(self.build).flush()
        self.build.updateStatus(
            BuildStatus.FULLYBUILT, builder=self.factory.makeBuilder()
        )
        title = self.build.title
        browser = self.getViewBrowser(self.build.builder, "+history")
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"Build history.*%s" % re.escape(title),
            extract_text(find_main_content(browser.contents)),
        )
        self.assertEqual(self.build_url, browser.getLink(title).url)

    def makeBuildingRecipe(self, information_type=InformationType.PUBLIC):
        builder = self.factory.makeBuilder()
        build = self.factory.makeCharmRecipeBuild(
            information_type=information_type
        )
        build.updateStatus(BuildStatus.BUILDING, builder=builder)
        build.queueBuild()
        build.buildqueue_record.builder = builder
        build.buildqueue_record.logtail = "tail of the log"
        return build

    def test_builder_index_public(self):
        build = self.makeBuildingRecipe()
        browser = self.getViewBrowser(build.builder, no_login=True)
        self.assertIn("tail of the log", browser.contents)
