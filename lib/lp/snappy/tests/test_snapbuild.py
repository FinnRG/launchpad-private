# Copyright 2015-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test snap package build features."""

from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

from fixtures import FakeLogger
from pymacaroons import Macaroon
from testtools.matchers import (
    ContainsDict,
    Equals,
    Is,
    MatchesDict,
    MatchesListwise,
    MatchesStructure,
)
from zope.component import getUtility
from zope.publisher.xmlrpc import TestRequest
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.app.errors import NotFoundError
from lp.app.interfaces.launchpad import ILaunchpadCelebrities, IPrivacy
from lp.buildmaster.enums import BuildStatus
from lp.buildmaster.interfaces.buildqueue import IBuildQueue
from lp.buildmaster.interfaces.packagebuild import IPackageBuild
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.registry.enums import PersonVisibility, TeamMembershipPolicy
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.series import SeriesStatus
from lp.services.authserver.xmlrpc import AuthServerAPIView
from lp.services.config import config
from lp.services.job.interfaces.job import JobStatus
from lp.services.librarian.browser import ProxiedLibraryFileAlias
from lp.services.macaroons.interfaces import IMacaroonIssuer
from lp.services.macaroons.testing import MacaroonTestMixin
from lp.services.propertycache import clear_property_cache
from lp.services.webapp.interfaces import OAuthPermission
from lp.services.webapp.publisher import canonical_url
from lp.services.webhooks.testing import LogsScheduledWebhooks
from lp.snappy.interfaces.snapbuild import (
    CannotScheduleStoreUpload,
    ISnapBuild,
    ISnapBuildSet,
    SnapBuildStoreUploadStatus,
)
from lp.snappy.interfaces.snapbuildjob import ISnapStoreUploadJobSource
from lp.soyuz.enums import ArchivePurpose
from lp.testing import (
    ANONYMOUS,
    StormStatementRecorder,
    TestCaseWithFactory,
    api_url,
    login,
    logout,
    person_logged_in,
)
from lp.testing.dbuser import dbuser
from lp.testing.layers import LaunchpadFunctionalLayer, LaunchpadZopelessLayer
from lp.testing.mail_helpers import pop_notifications
from lp.testing.matchers import HasQueryCount
from lp.testing.pages import webservice_for_person
from lp.xmlrpc.interfaces import IPrivateApplication

expected_body = """\
 * Snap Package: snap-1
 * Archive: distro
 * Distroseries: distro unstable
 * Architecture: i386
 * Pocket: UPDATES
 * State: Failed to build
 * Duration: 10 minutes
 * Build Log: %s
 * Upload Log: %s
 * Builder: http://launchpad.test/builders/bob
"""


class TestSnapBuild(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.pushConfig(
            "snappy",
            store_url="http://sca.example/",
            store_upload_url="http://updown.example/",
        )
        self.build = self.factory.makeSnapBuild()

    def test_implements_interfaces(self):
        # SnapBuild implements IPackageBuild, ISnapBuild, and IPrivacy.
        self.assertProvides(self.build, IPackageBuild)
        self.assertProvides(self.build, ISnapBuild)
        self.assertProvides(self.build, IPrivacy)

    def test___repr__(self):
        # SnapBuild has an informative __repr__.
        self.assertEqual(
            "<SnapBuild ~%s/+snap/%s/+build/%s>"
            % (
                self.build.snap.owner.name,
                self.build.snap.name,
                self.build.id,
            ),
            repr(self.build),
        )

    def test_title(self):
        # SnapBuild has an informative title.
        das = self.build.distro_arch_series
        self.assertIsNone(self.build.snap.store_name)
        self.assertEqual(
            "%s build of %s snap package in %s %s"
            % (
                das.architecturetag,
                self.build.snap.name,
                das.distroseries.distribution.name,
                das.distroseries.getSuite(self.build.pocket),
            ),
            self.build.title,
        )
        self.build.snap.store_name = self.build.snap.name
        self.assertEqual(
            "%s build of %s snap package in %s %s"
            % (
                das.architecturetag,
                self.build.snap.name,
                das.distroseries.distribution.name,
                das.distroseries.getSuite(self.build.pocket),
            ),
            self.build.title,
        )
        self.build.snap.store_name = self.factory.getUniqueUnicode()
        self.assertEqual(
            "%s build of %s snap package (%s) in %s %s"
            % (
                das.architecturetag,
                self.build.snap.name,
                self.build.snap.store_name,
                das.distroseries.distribution.name,
                das.distroseries.getSuite(self.build.pocket),
            ),
            self.build.title,
        )

    def test_queueBuild(self):
        # SnapBuild can create the queue entry for itself.
        bq = self.build.queueBuild()
        self.assertProvides(bq, IBuildQueue)
        self.assertEqual(
            self.build.build_farm_job, removeSecurityProxy(bq)._build_farm_job
        )
        self.assertEqual(self.build, bq.specific_build)
        self.assertEqual(self.build.virtualized, bq.virtualized)
        self.assertIsNotNone(bq.processor)
        self.assertEqual(bq, self.build.buildqueue_record)

    def test_current_component_primary(self):
        # SnapBuilds for primary archives always build in multiverse for the
        # time being.
        self.assertEqual(ArchivePurpose.PRIMARY, self.build.archive.purpose)
        self.assertEqual("multiverse", self.build.current_component.name)

    def test_current_component_ppa(self):
        # PPAs only have indices for main, so SnapBuilds for PPAs always
        # build in main.
        build = self.factory.makeSnapBuild(archive=self.factory.makeArchive())
        self.assertEqual("main", build.current_component.name)

    def test_is_private(self):
        # A SnapBuild is private iff its Snap or owner or archive are.
        self.assertFalse(self.build.is_private)
        self.assertFalse(self.build.private)
        private_team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.MODERATED,
            visibility=PersonVisibility.PRIVATE,
        )
        with person_logged_in(private_team.teamowner):
            build = self.factory.makeSnapBuild(
                requester=private_team.teamowner,
                owner=private_team,
                private=True,
            )
            self.assertTrue(build.is_private)
            self.assertTrue(build.private)
        private_archive = self.factory.makeArchive(private=True)
        with person_logged_in(private_archive.owner):
            build = self.factory.makeSnapBuild(archive=private_archive)
            self.assertTrue(build.is_private)
            self.assertTrue(build.private)

    def test_can_be_retried(self):
        ok_cases = [
            BuildStatus.FAILEDTOBUILD,
            BuildStatus.MANUALDEPWAIT,
            BuildStatus.CHROOTWAIT,
            BuildStatus.FAILEDTOUPLOAD,
            BuildStatus.CANCELLED,
            BuildStatus.SUPERSEDED,
        ]
        for status in BuildStatus.items:
            build = self.factory.makeSnapBuild(status=status)
            if status in ok_cases:
                self.assertTrue(build.can_be_retried)
            else:
                self.assertFalse(build.can_be_retried)

    def test_can_be_retried_obsolete_series(self):
        # Builds for obsolete series cannot be retried.
        distroseries = self.factory.makeDistroSeries(
            status=SeriesStatus.OBSOLETE
        )
        das = self.factory.makeDistroArchSeries(distroseries=distroseries)
        build = self.factory.makeSnapBuild(distroarchseries=das)
        self.assertFalse(build.can_be_retried)

    def test_can_be_cancelled(self):
        # For all states that can be cancelled, can_be_cancelled returns True.
        ok_cases = [
            BuildStatus.BUILDING,
            BuildStatus.NEEDSBUILD,
        ]
        for status in BuildStatus.items:
            build = self.factory.makeSnapBuild()
            build.queueBuild()
            build.updateStatus(status)
            if status in ok_cases:
                self.assertTrue(build.can_be_cancelled)
            else:
                self.assertFalse(build.can_be_cancelled)

    def test_retry_resets_state(self):
        # Retrying a build resets most of the state attributes, but does
        # not modify the first dispatch time.
        now = datetime.now(timezone.utc)
        build = self.factory.makeSnapBuild()
        build.updateStatus(BuildStatus.BUILDING, date_started=now)
        build.updateStatus(BuildStatus.FAILEDTOBUILD)
        build.gotFailure()
        with person_logged_in(build.snap.owner):
            build.retry()
        self.assertEqual(BuildStatus.NEEDSBUILD, build.status)
        self.assertEqual(now, build.date_first_dispatched)
        self.assertIsNone(build.log)
        self.assertIsNone(build.upload_log)
        self.assertEqual(0, build.failure_count)

    def test_cancel_not_in_progress(self):
        # The cancel() method for a pending build leaves it in the CANCELLED
        # state.
        self.build.queueBuild()
        self.build.cancel()
        self.assertEqual(BuildStatus.CANCELLED, self.build.status)
        self.assertIsNone(self.build.buildqueue_record)

    def test_cancel_in_progress(self):
        # The cancel() method for a building build leaves it in the
        # CANCELLING state.
        bq = self.build.queueBuild()
        bq.markAsBuilding(self.factory.makeBuilder())
        self.build.cancel()
        self.assertEqual(BuildStatus.CANCELLING, self.build.status)
        self.assertEqual(bq, self.build.buildqueue_record)

    def test_estimateDuration(self):
        # Without previous builds, the default time estimate is 30m.
        self.assertEqual(1800, self.build.estimateDuration().seconds)

    def test_estimateDuration_with_history(self):
        # Previous successful builds of the same snap package are used for
        # estimates.
        self.factory.makeSnapBuild(
            requester=self.build.requester,
            snap=self.build.snap,
            distroarchseries=self.build.distro_arch_series,
            status=BuildStatus.FULLYBUILT,
            duration=timedelta(seconds=335),
        )
        for _ in range(3):
            self.factory.makeSnapBuild(
                requester=self.build.requester,
                snap=self.build.snap,
                distroarchseries=self.build.distro_arch_series,
                status=BuildStatus.FAILEDTOBUILD,
                duration=timedelta(seconds=20),
            )
        self.assertEqual(335, self.build.estimateDuration().seconds)

    def test_build_cookie(self):
        build = self.factory.makeSnapBuild()
        self.assertEqual("SNAPBUILD-%d" % build.id, build.build_cookie)

    def test_getFileByName_logs(self):
        # getFileByName returns the logs when requested by name.
        self.build.setLog(
            self.factory.makeLibraryFileAlias(filename="buildlog.txt.gz")
        )
        self.assertEqual(
            self.build.log, self.build.getFileByName("buildlog.txt.gz")
        )
        self.assertRaises(NotFoundError, self.build.getFileByName, "foo")
        self.build.storeUploadLog("uploaded")
        self.assertEqual(
            self.build.upload_log,
            self.build.getFileByName(self.build.upload_log.filename),
        )

    def test_getFileByName_uploaded_files(self):
        # getFileByName returns uploaded files when requested by name.
        filenames = ("ubuntu.squashfs", "ubuntu.manifest", "foo_log.txt")
        lfas = []
        for filename in filenames:
            lfa = self.factory.makeLibraryFileAlias(filename=filename)
            lfas.append(lfa)
            self.build.addFile(lfa)
        self.assertContentEqual(
            lfas, [row[1] for row in self.build.getFiles()]
        )
        for filename, lfa in zip(filenames, lfas):
            self.assertEqual(lfa, self.build.getFileByName(filename))
        self.assertRaises(NotFoundError, self.build.getFileByName, "missing")

    def test_verifySuccessfulUpload(self):
        self.assertFalse(self.build.verifySuccessfulUpload())
        self.factory.makeSnapFile(snapbuild=self.build)
        self.assertTrue(self.build.verifySuccessfulUpload())

    def test_updateStatus_stores_revision_id(self):
        # If the builder reports a revision_id, updateStatus saves it.
        self.assertIsNone(self.build.revision_id)
        self.build.updateStatus(BuildStatus.BUILDING, worker_status={})
        self.assertIsNone(self.build.revision_id)
        self.build.updateStatus(
            BuildStatus.BUILDING, worker_status={"revision_id": "dummy"}
        )
        self.assertEqual("dummy", self.build.revision_id)

    def test_updateStatus_triggers_webhooks(self):
        # Updating the status of a SnapBuild triggers webhooks on the
        # corresponding Snap.
        logger = self.useFixture(FakeLogger())
        hook = self.factory.makeWebhook(
            target=self.build.snap, event_types=["snap:build:0.1"]
        )
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        expected_payload = {
            "snap_build": Equals(
                canonical_url(self.build, force_local_path=True)
            ),
            "action": Equals("status-changed"),
            "snap": Equals(
                canonical_url(self.build.snap, force_local_path=True)
            ),
            "build_request": Is(None),
            "status": Equals("Successfully built"),
            "store_upload_status": Equals("Unscheduled"),
        }
        delivery = hook.deliveries.one()
        self.assertThat(
            delivery,
            MatchesStructure(
                event_type=Equals("snap:build:0.1"),
                payload=MatchesDict(expected_payload),
            ),
        )
        with dbuser(config.IWebhookDeliveryJobSource.dbuser):
            self.assertEqual(
                "<WebhookDeliveryJob for webhook %d on %r>"
                % (hook.id, hook.target),
                repr(delivery),
            )
            self.assertThat(
                logger.output,
                LogsScheduledWebhooks(
                    [(hook, "snap:build:0.1", MatchesDict(expected_payload))]
                ),
            )

    def test_updateStatus_triggers_fullybuilt_subscope_webhook(self):
        # A webhook scoped to "snap:build:0.1::fullybuilt" only receives
        # deliveries once the build reaches BuildStatus.FULLYBUILT, not on
        # other status changes.
        hook = self.factory.makeWebhook(
            target=self.build.snap,
            event_types=["snap:build:0.1::fullybuilt"],
        )
        self.build.updateStatus(BuildStatus.BUILDING)
        self.assertEqual(0, hook.deliveries.count())
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.assertEqual(1, hook.deliveries.count())
        # IWebhookSet.trigger() always delivers using the parent event
        # type, so subscope subscribers still see the same event_type and
        # payload shape as parent-scope subscribers.
        delivery = hook.deliveries.one()
        self.assertThat(
            delivery,
            MatchesStructure(
                event_type=Equals("snap:build:0.1"),
                payload=ContainsDict(
                    {
                        "action": Equals("status-changed"),
                        "status": Equals("Successfully built"),
                    }
                ),
            ),
        )

    def test_updateStatus_parent_scope_webhook_still_sees_every_status(self):
        # Adding the "fullybuilt" subscope does not change the behaviour
        # of webhooks that remain subscribed to the parent
        # "snap:build:0.1" scope: they keep receiving every status
        # change, including intermediate ones.
        hook = self.factory.makeWebhook(
            target=self.build.snap, event_types=["snap:build:0.1"]
        )
        self.build.updateStatus(BuildStatus.BUILDING)
        self.assertEqual(1, hook.deliveries.count())
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.assertEqual(2, hook.deliveries.count())

    def test_updateStatus_no_change_does_not_trigger_webhooks(self):
        # An updateStatus call that changes details such as the revision_id
        # but that doesn't change the build's status attribute does not
        # trigger webhooks.
        logger = self.useFixture(FakeLogger())
        hook = self.factory.makeWebhook(
            target=self.build.snap, event_types=["snap:build:0.1"]
        )
        builder = self.factory.makeBuilder()
        self.build.updateStatus(BuildStatus.BUILDING)
        expected_logs = [
            (
                hook,
                "snap:build:0.1",
                ContainsDict(
                    {
                        "action": Equals("status-changed"),
                        "status": Equals("Currently building"),
                    }
                ),
            )
        ]
        self.assertEqual(1, hook.deliveries.count())
        self.assertThat(logger.output, LogsScheduledWebhooks(expected_logs))
        self.build.updateStatus(
            BuildStatus.BUILDING,
            builder=builder,
            worker_status={"revision_id": "1"},
        )
        self.assertEqual(1, hook.deliveries.count())
        self.assertThat(logger.output, LogsScheduledWebhooks(expected_logs))
        self.build.updateStatus(BuildStatus.UPLOADING)
        expected_logs.append(
            (
                hook,
                "snap:build:0.1",
                ContainsDict(
                    {
                        "action": Equals("status-changed"),
                        "status": Equals("Uploading build"),
                    }
                ),
            )
        )
        self.assertEqual(2, hook.deliveries.count())
        self.assertThat(logger.output, LogsScheduledWebhooks(expected_logs))

    def test_updateStatus_failure_does_not_trigger_store_uploads(self):
        # A failed SnapBuild does not trigger store uploads.
        self.build.snap.store_series = self.factory.makeSnappySeries()
        self.build.snap.store_name = self.factory.getUniqueUnicode()
        self.build.snap.store_upload = True
        self.build.snap.store_secrets = {"root": Macaroon().serialize()}
        with dbuser(config.builddmaster.dbuser):
            self.build.updateStatus(BuildStatus.FAILEDTOBUILD)
        self.assertContentEqual([], self.build.store_upload_jobs)

    def test_updateStatus_fullybuilt_not_configured(self):
        # A completed SnapBuild does not trigger store uploads if the snap
        # is not properly configured for that.
        logger = self.useFixture(FakeLogger())
        with dbuser(config.builddmaster.dbuser):
            self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.assertEqual(0, len(list(self.build.store_upload_jobs)))
        self.assertIn(
            "<Snap ~%s/+snap/%s> is not configured for upload to the "
            "store." % (self.build.snap.owner.name, self.build.snap.name),
            logger.output.splitlines(),
        )

    def test_updateStatus_fullybuilt_triggers_store_uploads(self):
        # A completed SnapBuild triggers store uploads.
        logger = self.useFixture(FakeLogger())
        self.build.snap.store_series = self.factory.makeSnappySeries()
        self.build.snap.store_name = self.factory.getUniqueUnicode()
        self.build.snap.store_upload = True
        self.build.snap.store_secrets = {"root": Macaroon().serialize()}
        with dbuser(config.builddmaster.dbuser):
            self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.assertEqual(1, len(list(self.build.store_upload_jobs)))
        self.assertIn(
            "Scheduling upload of <SnapBuild ~%s/+snap/%s/+build/%d> to the "
            "store."
            % (
                self.build.snap.owner.name,
                self.build.snap.name,
                self.build.id,
            ),
            logger.output.splitlines(),
        )

    def test_notify_fullybuilt(self):
        # notify does not send mail when a SnapBuild completes normally.
        person = self.factory.makePerson(name="person")
        build = self.factory.makeSnapBuild(
            requester=person, status=BuildStatus.FULLYBUILT
        )
        build.notify()
        self.assertEqual(0, len(pop_notifications()))

    def test_notify_packagefail(self):
        # notify sends mail when a SnapBuild fails.
        person = self.factory.makePerson(name="person")
        distribution = self.factory.makeDistribution(name="distro")
        distroseries = self.factory.makeDistroSeries(
            distribution=distribution, name="unstable"
        )
        processor = getUtility(IProcessorSet).getByName("386")
        distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag="i386",
            processor=processor,
        )
        build = self.factory.makeSnapBuild(
            name="snap-1",
            requester=person,
            owner=person,
            distroarchseries=distroarchseries,
            date_created=datetime(2014, 4, 25, 10, 38, 0, tzinfo=timezone.utc),
            status=BuildStatus.FAILEDTOBUILD,
            builder=self.factory.makeBuilder(name="bob"),
            duration=timedelta(minutes=10),
        )
        build.setLog(self.factory.makeLibraryFileAlias())
        build.notify()
        [notification] = pop_notifications()
        self.assertEqual(
            config.canonical.noreply_from_address, notification["From"]
        )
        self.assertEqual(
            "Person <%s>" % person.preferredemail.email, notification["To"]
        )
        subject = notification["Subject"].replace("\n ", " ")
        self.assertEqual(
            "[Snap build #%d] i386 build of snap-1 snap package in distro "
            "unstable-updates" % build.id,
            subject,
        )
        self.assertEqual(
            "Requester", notification["X-Launchpad-Message-Rationale"]
        )
        self.assertEqual(person.name, notification["X-Launchpad-Message-For"])
        self.assertEqual(
            "snap-build-status", notification["X-Launchpad-Notification-Type"]
        )
        self.assertEqual(
            "FAILEDTOBUILD", notification["X-Launchpad-Build-State"]
        )
        body, footer = (
            notification.get_payload(decode=True).decode().split("\n-- \n")
        )
        self.assertEqual(expected_body % (build.log_url, ""), body)
        self.assertEqual(
            "http://launchpad.test/~person/+snap/snap-1/+build/%d\n"
            "You are the requester of the build.\n" % build.id,
            footer,
        )

    def test_notify_subscribers(self):
        # notify also sends mail to snap subscribers.
        person = self.factory.makePerson(name="person")
        subscriber = self.factory.makePerson(name="subscriber")
        build = self.factory.makeSnapBuild(
            name="snap-1",
            requester=person,
            owner=person,
            status=BuildStatus.FAILEDTOBUILD,
        )
        build.snap.subscribe(subscriber, subscriber)
        build.setLog(self.factory.makeLibraryFileAlias())
        build.notify()
        notifications = pop_notifications()
        self.assertEqual(2, len(notifications))
        recipients = [n["To"] for n in notifications]
        self.assertIn("Person <%s>" % person.preferredemail.email, recipients)
        self.assertIn(
            "Subscriber <%s>" % subscriber.preferredemail.email, recipients
        )
        for notification in notifications:
            if notification["To"] == "Subscriber <%s>" % (
                subscriber.preferredemail.email
            ):
                self.assertEqual(
                    "Subscriber", notification["X-Launchpad-Message-Rationale"]
                )
                self.assertEqual(
                    subscriber.name, notification["X-Launchpad-Message-For"]
                )

    def addFakeBuildLog(self, build):
        build.setLog(self.factory.makeLibraryFileAlias("mybuildlog.txt"))

    def test_log_url(self):
        # The log URL for a snap package build will use the archive context.
        self.addFakeBuildLog(self.build)
        self.assertEqual(
            "http://launchpad.test/~%s/+snap/%s/+build/%d/+files/"
            "mybuildlog.txt"
            % (
                self.build.snap.owner.name,
                self.build.snap.name,
                self.build.id,
            ),
            self.build.log_url,
        )

    def test_eta(self):
        # SnapBuild.eta returns a non-None value when it should, or None
        # when there's no start time.
        self.build.queueBuild()
        self.assertIsNone(self.build.eta)
        self.factory.makeBuilder(processors=[self.build.processor])
        clear_property_cache(self.build)
        self.assertIsNotNone(self.build.eta)

    def test_eta_cached(self):
        # The expensive completion time estimate is cached.
        self.build.queueBuild()
        self.build.eta
        with StormStatementRecorder() as recorder:
            self.build.eta
        self.assertThat(recorder, HasQueryCount(Equals(0)))

    def test_estimate(self):
        # SnapBuild.estimate returns True until the job is completed.
        self.build.queueBuild()
        self.factory.makeBuilder(processors=[self.build.processor])
        self.build.updateStatus(BuildStatus.BUILDING)
        self.assertTrue(self.build.estimate)
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        clear_property_cache(self.build)
        self.assertFalse(self.build.estimate)

    def setUpStoreUpload(self):
        self.pushConfig(
            "snappy",
            store_url="http://sca.example/",
            store_upload_url="http://updown.example/",
        )
        self.build.snap.store_series = self.factory.makeSnappySeries(
            usable_distro_series=[self.build.snap.distro_series]
        )
        self.build.snap.store_name = self.factory.getUniqueUnicode()
        self.build.snap.store_secrets = {"root": Macaroon().serialize()}

    def test_store_upload_status_unscheduled(self):
        build = self.factory.makeSnapBuild(status=BuildStatus.FULLYBUILT)
        self.assertEqual(
            SnapBuildStoreUploadStatus.UNSCHEDULED, build.store_upload_status
        )

    def test_store_upload_status_pending(self):
        build = self.factory.makeSnapBuild(status=BuildStatus.FULLYBUILT)
        getUtility(ISnapStoreUploadJobSource).create(build)
        self.assertEqual(
            SnapBuildStoreUploadStatus.PENDING, build.store_upload_status
        )

    def test_store_upload_status_uploaded(self):
        build = self.factory.makeSnapBuild(status=BuildStatus.FULLYBUILT)
        job = getUtility(ISnapStoreUploadJobSource).create(build)
        naked_job = removeSecurityProxy(job)
        naked_job.job._status = JobStatus.COMPLETED
        self.assertEqual(
            SnapBuildStoreUploadStatus.UPLOADED, build.store_upload_status
        )

    def test_store_upload_status_failed_to_upload(self):
        build = self.factory.makeSnapBuild(status=BuildStatus.FULLYBUILT)
        job = getUtility(ISnapStoreUploadJobSource).create(build)
        naked_job = removeSecurityProxy(job)
        naked_job.job._status = JobStatus.FAILED
        self.assertEqual(
            SnapBuildStoreUploadStatus.FAILEDTOUPLOAD,
            build.store_upload_status,
        )

    def test_store_upload_status_failed_to_release(self):
        build = self.factory.makeSnapBuild(status=BuildStatus.FULLYBUILT)
        job = getUtility(ISnapStoreUploadJobSource).create(build)
        naked_job = removeSecurityProxy(job)
        naked_job.job._status = JobStatus.FAILED
        naked_job.store_url = "http://sca.example/dev/click-apps/1/rev/1/"
        self.assertEqual(
            SnapBuildStoreUploadStatus.FAILEDTORELEASE,
            build.store_upload_status,
        )

    def test_store_upload_error_messages_no_job(self):
        build = self.factory.makeSnapBuild(status=BuildStatus.FULLYBUILT)
        self.assertEqual([], build.store_upload_error_messages)

    def test_store_upload_error_messages_job_no_error(self):
        build = self.factory.makeSnapBuild(status=BuildStatus.FULLYBUILT)
        getUtility(ISnapStoreUploadJobSource).create(build)
        self.assertEqual([], build.store_upload_error_messages)

    def test_store_upload_error_messages_job_error_messages(self):
        build = self.factory.makeSnapBuild(status=BuildStatus.FULLYBUILT)
        job = getUtility(ISnapStoreUploadJobSource).create(build)
        removeSecurityProxy(job).error_messages = [
            {"message": "Scan failed.", "link": "link1"},
        ]
        self.assertEqual(
            [{"message": "Scan failed.", "link": "link1"}],
            build.store_upload_error_messages,
        )

    def test_store_upload_error_messages_job_error_message(self):
        build = self.factory.makeSnapBuild(status=BuildStatus.FULLYBUILT)
        job = getUtility(ISnapStoreUploadJobSource).create(build)
        removeSecurityProxy(job).error_message = "Boom"
        self.assertEqual(
            [{"message": "Boom"}], build.store_upload_error_messages
        )

    def test_scheduleStoreUpload(self):
        # A build not previously uploaded to the store can be uploaded
        # manually.
        self.setUpStoreUpload()
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.factory.makeSnapFile(
            snapbuild=self.build,
            libraryfile=self.factory.makeLibraryFileAlias(db_only=True),
        )
        self.build.scheduleStoreUpload()
        [job] = getUtility(ISnapStoreUploadJobSource).iterReady()
        self.assertEqual(JobStatus.WAITING, job.job.status)
        self.assertEqual(self.build, job.snapbuild)

    def test_scheduleStoreUpload_not_configured(self):
        # A build that is not properly configured cannot be uploaded to the
        # store.
        self.setUpStoreUpload()
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.build.snap.store_name = None
        self.assertRaisesWithContent(
            CannotScheduleStoreUpload,
            "Cannot upload this package to the store because it is not "
            "properly configured.",
            self.build.scheduleStoreUpload,
        )
        self.assertEqual(
            [], list(getUtility(ISnapStoreUploadJobSource).iterReady())
        )

    def test_scheduleStoreUpload_no_files(self):
        # A build with no files cannot be uploaded to the store.
        self.setUpStoreUpload()
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.assertRaisesWithContent(
            CannotScheduleStoreUpload,
            "Cannot upload this package because it has no files.",
            self.build.scheduleStoreUpload,
        )
        self.assertEqual(
            [], list(getUtility(ISnapStoreUploadJobSource).iterReady())
        )

    def test_scheduleStoreUpload_already_in_progress(self):
        # A build with an upload already in progress will not have another
        # one created.
        self.setUpStoreUpload()
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.factory.makeSnapFile(
            snapbuild=self.build,
            libraryfile=self.factory.makeLibraryFileAlias(db_only=True),
        )
        old_job = getUtility(ISnapStoreUploadJobSource).create(self.build)
        self.assertRaisesWithContent(
            CannotScheduleStoreUpload,
            "An upload of this package is already in progress.",
            self.build.scheduleStoreUpload,
        )
        self.assertEqual(
            [old_job], list(getUtility(ISnapStoreUploadJobSource).iterReady())
        )

    def test_scheduleStoreUpload_already_uploaded(self):
        # A build with an upload that has already completed will not have
        # another one created.
        self.setUpStoreUpload()
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.factory.makeSnapFile(
            snapbuild=self.build,
            libraryfile=self.factory.makeLibraryFileAlias(db_only=True),
        )
        old_job = getUtility(ISnapStoreUploadJobSource).create(self.build)
        removeSecurityProxy(old_job).job._status = JobStatus.COMPLETED
        self.assertRaisesWithContent(
            CannotScheduleStoreUpload,
            "Cannot upload this package because it has already been uploaded.",
            self.build.scheduleStoreUpload,
        )
        self.assertEqual(
            [], list(getUtility(ISnapStoreUploadJobSource).iterReady())
        )

    def test_scheduleStoreUpload_triggers_webhooks(self):
        # Scheduling a store upload triggers webhooks on the corresponding
        # snap.
        logger = self.useFixture(FakeLogger())
        self.setUpStoreUpload()
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.factory.makeSnapFile(
            snapbuild=self.build,
            libraryfile=self.factory.makeLibraryFileAlias(db_only=True),
        )
        hook = self.factory.makeWebhook(
            target=self.build.snap, event_types=["snap:build:0.1"]
        )
        self.build.scheduleStoreUpload()
        expected_payload = {
            "snap_build": Equals(
                canonical_url(self.build, force_local_path=True)
            ),
            "action": Equals("status-changed"),
            "snap": Equals(
                canonical_url(self.build.snap, force_local_path=True)
            ),
            "build_request": Is(None),
            "status": Equals("Successfully built"),
            "store_upload_status": Equals("Pending"),
        }
        delivery = hook.deliveries.one()
        self.assertThat(
            delivery,
            MatchesStructure(
                event_type=Equals("snap:build:0.1"),
                payload=MatchesDict(expected_payload),
            ),
        )
        with dbuser(config.IWebhookDeliveryJobSource.dbuser):
            self.assertEqual(
                "<WebhookDeliveryJob for webhook %d on %r>"
                % (hook.id, hook.target),
                repr(delivery),
            )
            self.assertThat(
                logger.output,
                LogsScheduledWebhooks(
                    [(hook, "snap:build:0.1", MatchesDict(expected_payload))]
                ),
            )

    def test_can_have_target_architectures(self):
        build = self.factory.makeSnapBuild(target_architectures=["amd64"])
        self.assertEqual(build.target_architectures, ["amd64"])


class TestSnapBuildSet(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def test_getByBuildFarmJob_works(self):
        build = self.factory.makeSnapBuild()
        self.assertEqual(
            build,
            getUtility(ISnapBuildSet).getByBuildFarmJob(build.build_farm_job),
        )

    def test_getByBuildFarmJob_returns_None_when_missing(self):
        bpb = self.factory.makeBinaryPackageBuild()
        self.assertIsNone(
            getUtility(ISnapBuildSet).getByBuildFarmJob(bpb.build_farm_job)
        )

    def test_getByBuildFarmJobs_works(self):
        builds = [self.factory.makeSnapBuild() for i in range(10)]
        self.assertContentEqual(
            builds,
            getUtility(ISnapBuildSet).getByBuildFarmJobs(
                [build.build_farm_job for build in builds]
            ),
        )

    def test_getByBuildFarmJobs_works_empty(self):
        self.assertContentEqual(
            [], getUtility(ISnapBuildSet).getByBuildFarmJobs([])
        )


class TestSnapBuildWebservice(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()
        self.webservice = webservice_for_person(
            self.person, permission=OAuthPermission.WRITE_PRIVATE
        )
        self.webservice.default_api_version = "devel"
        login(ANONYMOUS)

    def getURL(self, obj):
        return self.webservice.getAbsoluteUrl(api_url(obj))

    def test_properties(self):
        # The basic properties of a SnapBuild are sensible.
        db_build = self.factory.makeSnapBuild(
            requester=self.person,
            date_created=datetime(2014, 4, 25, 10, 38, 0, tzinfo=timezone.utc),
        )
        build_url = api_url(db_build)
        logout()
        build = self.webservice.get(build_url).jsonBody()
        with person_logged_in(self.person):
            self.assertEqual(self.getURL(self.person), build["requester_link"])
            self.assertEqual(self.getURL(db_build.snap), build["snap_link"])
            self.assertEqual(
                self.getURL(db_build.archive), build["archive_link"]
            )
            self.assertEqual(
                self.getURL(db_build.distro_arch_series),
                build["distro_arch_series_link"],
            )
            self.assertEqual(
                db_build.distro_arch_series.architecturetag, build["arch_tag"]
            )
            self.assertEqual("Updates", build["pocket"])
            self.assertIsNone(build["snap_base_link"])
            self.assertIsNone(build["channels"])
            self.assertIsNone(build["score"])
            self.assertFalse(build["can_be_rescored"])
            self.assertFalse(build["can_be_cancelled"])

    def test_build_request(self):
        # The build_request property is exposed.
        snap = self.factory.makeSnap()
        request = self.factory.makeSnapBuildRequest(snap=snap)
        db_build = self.factory.makeSnapBuild(
            requester=self.person, snap=snap, build_request=request
        )
        build_url = api_url(db_build)
        logout()
        build = self.webservice.get(build_url).jsonBody()
        with person_logged_in(self.person):
            self.assertEqual(self.getURL(request), build["build_request_link"])

    def test_public(self):
        # A SnapBuild with a public Snap and archive is itself public.
        db_build = self.factory.makeSnapBuild()
        build_url = api_url(db_build)
        unpriv_webservice = webservice_for_person(
            self.factory.makePerson(), permission=OAuthPermission.WRITE_PUBLIC
        )
        unpriv_webservice.default_api_version = "devel"
        logout()
        self.assertEqual(200, self.webservice.get(build_url).status)
        self.assertEqual(200, unpriv_webservice.get(build_url).status)

    def test_private_snap(self):
        # A SnapBuild with a private Snap is private.
        db_team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.MODERATED,
            owner=self.person,
            visibility=PersonVisibility.PRIVATE,
        )
        with person_logged_in(self.person):
            db_build = self.factory.makeSnapBuild(
                requester=self.person, owner=db_team, private=True
            )
            build_url = api_url(db_build)
        unpriv_webservice = webservice_for_person(
            self.factory.makePerson(), permission=OAuthPermission.WRITE_PUBLIC
        )
        unpriv_webservice.default_api_version = "devel"
        logout()
        response = self.webservice.get(build_url)
        self.assertEqual(200, response.status)
        # 404 since we aren't allowed to know that the private team exists.
        self.assertEqual(404, unpriv_webservice.get(build_url).status)

    def test_private_archive(self):
        # A SnapBuild with a private archive is private.
        db_archive = self.factory.makeArchive(owner=self.person, private=True)
        with person_logged_in(self.person):
            db_build = self.factory.makeSnapBuild(archive=db_archive)
            build_url = api_url(db_build)
        unpriv_webservice = webservice_for_person(
            self.factory.makePerson(), permission=OAuthPermission.WRITE_PUBLIC
        )
        unpriv_webservice.default_api_version = "devel"
        logout()
        self.assertEqual(200, self.webservice.get(build_url).status)
        self.assertEqual(401, unpriv_webservice.get(build_url).status)

    def test_cancel(self):
        # The owner of a build can cancel it.
        db_build = self.factory.makeSnapBuild(requester=self.person)
        db_build.queueBuild()
        build_url = api_url(db_build)
        unpriv_webservice = webservice_for_person(
            self.factory.makePerson(), permission=OAuthPermission.WRITE_PUBLIC
        )
        unpriv_webservice.default_api_version = "devel"
        logout()
        build = self.webservice.get(build_url).jsonBody()
        self.assertTrue(build["can_be_cancelled"])
        response = unpriv_webservice.named_post(build["self_link"], "cancel")
        self.assertEqual(401, response.status)
        response = self.webservice.named_post(build["self_link"], "cancel")
        self.assertEqual(200, response.status)
        build = self.webservice.get(build_url).jsonBody()
        self.assertFalse(build["can_be_cancelled"])
        with person_logged_in(self.person):
            self.assertEqual(BuildStatus.CANCELLED, db_build.status)

    def test_rescore(self):
        # Buildd administrators can rescore builds.
        db_build = self.factory.makeSnapBuild(requester=self.person)
        db_build.queueBuild()
        build_url = api_url(db_build)
        buildd_admin = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).buildd_admin]
        )
        buildd_admin_webservice = webservice_for_person(
            buildd_admin, permission=OAuthPermission.WRITE_PUBLIC
        )
        buildd_admin_webservice.default_api_version = "devel"
        logout()
        build = self.webservice.get(build_url).jsonBody()
        self.assertEqual(2510, build["score"])
        self.assertTrue(build["can_be_rescored"])
        response = self.webservice.named_post(
            build["self_link"], "rescore", score=5000
        )
        self.assertEqual(401, response.status)
        response = buildd_admin_webservice.named_post(
            build["self_link"], "rescore", score=5000
        )
        self.assertEqual(200, response.status)
        build = self.webservice.get(build_url).jsonBody()
        self.assertEqual(5000, build["score"])

    def assertCanOpenRedirectedUrl(self, browser, url):
        browser.open(url)
        self.assertEqual(303, browser.responseStatusCode)
        urlopen(browser.headers["Location"]).close()

    def test_logs(self):
        # API clients can fetch the build and upload logs.
        db_build = self.factory.makeSnapBuild(requester=self.person)
        db_build.setLog(self.factory.makeLibraryFileAlias("buildlog.txt.gz"))
        db_build.storeUploadLog("uploaded")
        build_url = api_url(db_build)
        logout()
        build = self.webservice.get(build_url).jsonBody()
        browser = self.getNonRedirectingBrowser(user=self.person)
        browser.raiseHttpErrors = False
        self.assertIsNotNone(build["build_log_url"])
        self.assertCanOpenRedirectedUrl(browser, build["build_log_url"])
        self.assertIsNotNone(build["upload_log_url"])
        self.assertCanOpenRedirectedUrl(browser, build["upload_log_url"])

    def test_getFileUrls(self):
        # API clients can fetch files attached to builds.
        db_build = self.factory.makeSnapBuild(requester=self.person)
        db_files = [
            self.factory.makeSnapFile(snapbuild=db_build) for i in range(2)
        ]
        build_url = api_url(db_build)
        file_urls = [
            ProxiedLibraryFileAlias(file.libraryfile, db_build).http_url
            for file in db_files
        ]
        logout()
        response = self.webservice.named_get(build_url, "getFileUrls")
        self.assertEqual(200, response.status)
        self.assertContentEqual(file_urls, response.jsonBody())
        browser = self.getNonRedirectingBrowser(user=self.person)
        browser.raiseHttpErrors = False
        for file_url in file_urls:
            self.assertCanOpenRedirectedUrl(browser, file_url)

    def test_build_metadata_url(self):
        # API clients can fetch the metadata from the build, generated by the
        # fetch service
        db_build = self.factory.makeSnapBuild(requester=self.person)
        metadata_filename = f"{db_build.build_cookie}_metadata.json"
        with person_logged_in(self.person):
            file_1 = self.factory.makeLibraryFileAlias(
                content="some_json",
                filename="test_file.json",
            )
            db_build.addFile(file_1)
            metadata_file = self.factory.makeLibraryFileAlias(
                content="some_json",
                filename=metadata_filename,
            )
            db_build.addFile(metadata_file)
            file_2 = self.factory.makeLibraryFileAlias(
                content="some_json",
                filename="another_test_file.tar",
            )
            db_build.addFile(file_2)

        build_url = api_url(db_build)
        logout()

        build = self.webservice.get(build_url).jsonBody()
        self.assertIsNotNone(build["build_metadata_url"])
        self.assertEndsWith(build["build_metadata_url"], metadata_filename)

    def test_build_metadata_url_no_metadata_file(self):
        # The attribute `build_metadata_url` returns None when metadata file
        # does not exist.
        db_build = self.factory.makeSnapBuild(requester=self.person)
        with person_logged_in(self.person):
            file_1 = self.factory.makeLibraryFileAlias(
                content="some_json",
                filename="test_file.json",
            )
            db_build.addFile(file_1)
            file_2 = self.factory.makeLibraryFileAlias(
                content="some_json",
                filename="another_test_file.tar",
            )
            db_build.addFile(file_2)

        build_url = api_url(db_build)
        logout()

        build = self.webservice.get(build_url).jsonBody()
        self.assertIsNone(build["build_metadata_url"])


class TestSnapBuildMacaroonIssuer(MacaroonTestMixin, TestCaseWithFactory):
    """Test SnapBuild macaroon issuing and verification."""

    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.pushConfig(
            "launchpad", internal_macaroon_secret_key="some-secret"
        )

    def test_issueMacaroon_good(self):
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(private=True)
        )
        issuer = getUtility(IMacaroonIssuer, "snap-build")
        macaroon = removeSecurityProxy(issuer).issueMacaroon(build)
        self.assertThat(
            macaroon,
            MatchesStructure(
                location=Equals("launchpad.test"),
                identifier=Equals("snap-build"),
                caveats=MatchesListwise(
                    [
                        MatchesStructure.byEquality(
                            caveat_id="lp.snap-build %s" % build.id
                        ),
                    ]
                ),
            ),
        )

    def test_issueMacaroon_via_authserver(self):
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(private=True)
        )
        private_root = getUtility(IPrivateApplication)
        authserver = AuthServerAPIView(private_root.authserver, TestRequest())
        macaroon = Macaroon.deserialize(
            authserver.issueMacaroon("snap-build", "SnapBuild", build.id)
        )
        self.assertThat(
            macaroon,
            MatchesStructure(
                location=Equals("launchpad.test"),
                identifier=Equals("snap-build"),
                caveats=MatchesListwise(
                    [
                        MatchesStructure.byEquality(
                            caveat_id="lp.snap-build %s" % build.id
                        ),
                    ]
                ),
            ),
        )

    def test_verifyMacaroon_good_repository(self):
        [ref] = self.factory.makeGitRefs(
            information_type=InformationType.USERDATA
        )
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(git_ref=ref, private=True)
        )
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(getUtility(IMacaroonIssuer, "snap-build"))
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonVerifies(issuer, macaroon, ref.repository)

    def test_verifyMacaroon_good_direct_archive(self):
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(private=True),
            archive=self.factory.makeArchive(private=True),
        )
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(getUtility(IMacaroonIssuer, "snap-build"))
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonVerifies(issuer, macaroon, build.archive)

    def test_verifyMacaroon_good_indirect_archive(self):
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(private=True),
            archive=self.factory.makeArchive(private=True),
        )
        dependency = self.factory.makeArchive(
            distribution=build.archive.distribution, private=True
        )
        build.archive.addArchiveDependency(
            dependency, PackagePublishingPocket.RELEASE
        )
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(getUtility(IMacaroonIssuer, "snap-build"))
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonVerifies(issuer, macaroon, dependency)

    def test_verifyMacaroon_good_snap_base_archive(self):
        snap_base = self.factory.makeSnapBase()
        dependency = self.factory.makeArchive(private=True)
        snap_base.addArchiveDependency(
            dependency, PackagePublishingPocket.RELEASE
        )
        build = self.factory.makeSnapBuild(snap_base=snap_base)
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(getUtility(IMacaroonIssuer, "snap-build"))
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonVerifies(issuer, macaroon, dependency)

    def test_verifyMacaroon_good_no_context(self):
        [ref] = self.factory.makeGitRefs(
            information_type=InformationType.USERDATA
        )
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(git_ref=ref, private=True)
        )
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(getUtility(IMacaroonIssuer, "snap-build"))
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonVerifies(
            issuer, macaroon, None, require_context=False
        )
        self.assertMacaroonVerifies(
            issuer, macaroon, ref.repository, require_context=False
        )

    def test_verifyMacaroon_no_context_but_require_context(self):
        [ref] = self.factory.makeGitRefs(
            information_type=InformationType.USERDATA
        )
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(git_ref=ref, private=True)
        )
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(getUtility(IMacaroonIssuer, "snap-build"))
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonDoesNotVerify(
            ["Expected macaroon verification context but got None."],
            issuer,
            macaroon,
            None,
        )

    def test_verifyMacaroon_wrong_location(self):
        [ref] = self.factory.makeGitRefs(
            information_type=InformationType.USERDATA
        )
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(git_ref=ref, private=True)
        )
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(getUtility(IMacaroonIssuer, "snap-build"))
        macaroon = Macaroon(
            location="another-location", key=issuer._root_secret
        )
        self.assertMacaroonDoesNotVerify(
            ["Macaroon has unknown location 'another-location'."],
            issuer,
            macaroon,
            ref.repository,
        )
        self.assertMacaroonDoesNotVerify(
            ["Macaroon has unknown location 'another-location'."],
            issuer,
            macaroon,
            ref.repository,
            require_context=False,
        )

    def test_verifyMacaroon_wrong_key(self):
        [ref] = self.factory.makeGitRefs(
            information_type=InformationType.USERDATA
        )
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(git_ref=ref, private=True)
        )
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(getUtility(IMacaroonIssuer, "snap-build"))
        macaroon = Macaroon(
            location=config.vhost.mainsite.hostname, key="another-secret"
        )
        self.assertMacaroonDoesNotVerify(
            ["Signatures do not match"], issuer, macaroon, ref.repository
        )
        self.assertMacaroonDoesNotVerify(
            ["Signatures do not match"],
            issuer,
            macaroon,
            ref.repository,
            require_context=False,
        )

    def test_verifyMacaroon_refuses_branch(self):
        branch = self.factory.makeAnyBranch(
            information_type=InformationType.USERDATA
        )
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(branch=branch, private=True)
        )
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(getUtility(IMacaroonIssuer, "snap-build"))
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonDoesNotVerify(
            ["Cannot handle context %r." % branch], issuer, macaroon, branch
        )

    def test_verifyMacaroon_not_building(self):
        [ref] = self.factory.makeGitRefs(
            information_type=InformationType.USERDATA
        )
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(git_ref=ref, private=True)
        )
        issuer = removeSecurityProxy(getUtility(IMacaroonIssuer, "snap-build"))
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonDoesNotVerify(
            ["Caveat check for 'lp.snap-build %s' failed." % build.id],
            issuer,
            macaroon,
            ref.repository,
        )

    def test_verifyMacaroon_wrong_build(self):
        [ref] = self.factory.makeGitRefs(
            information_type=InformationType.USERDATA
        )
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(git_ref=ref, private=True)
        )
        build.updateStatus(BuildStatus.BUILDING)
        other_build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(private=True)
        )
        other_build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(getUtility(IMacaroonIssuer, "snap-build"))
        macaroon = issuer.issueMacaroon(other_build)
        self.assertMacaroonDoesNotVerify(
            ["Caveat check for 'lp.snap-build %s' failed." % other_build.id],
            issuer,
            macaroon,
            ref.repository,
        )

    def test_verifyMacaroon_wrong_repository(self):
        [ref] = self.factory.makeGitRefs(
            information_type=InformationType.USERDATA
        )
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(git_ref=ref, private=True)
        )
        other_repository = self.factory.makeGitRepository()
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(getUtility(IMacaroonIssuer, "snap-build"))
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonDoesNotVerify(
            ["Caveat check for 'lp.snap-build %s' failed." % build.id],
            issuer,
            macaroon,
            other_repository,
        )

    def test_verifyMacaroon_wrong_archive(self):
        build = self.factory.makeSnapBuild(
            snap=self.factory.makeSnap(private=True),
            archive=self.factory.makeArchive(private=True),
        )
        other_archive = self.factory.makeArchive(
            distribution=build.archive.distribution, private=True
        )
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(getUtility(IMacaroonIssuer, "snap-build"))
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonDoesNotVerify(
            ["Caveat check for 'lp.snap-build %s' failed." % build.id],
            issuer,
            macaroon,
            other_archive,
        )
