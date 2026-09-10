# Copyright 2019-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for OCI image building recipe functionality."""

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
from lp.oci.interfaces.ocirecipe import OCI_RECIPE_ALLOW_CREATE
from lp.oci.interfaces.ocirecipebuild import (
    CannotScheduleRegistryUpload,
    IOCIFileSet,
    IOCIRecipeBuild,
    IOCIRecipeBuildSet,
    OCIRecipeBuildRegistryUploadStatus,
)
from lp.oci.interfaces.ocirecipebuildjob import IOCIRegistryUploadJobSource
from lp.oci.model.ocirecipebuild import OCIRecipeBuildSet
from lp.oci.tests.helpers import OCIConfigHelperMixin
from lp.registry.enums import PersonVisibility, TeamMembershipPolicy
from lp.registry.interfaces.series import SeriesStatus
from lp.services.authserver.xmlrpc import AuthServerAPIView
from lp.services.config import config
from lp.services.features.testing import FeatureFixture
from lp.services.job.interfaces.job import JobStatus
from lp.services.librarian.browser import ProxiedLibraryFileAlias
from lp.services.macaroons.interfaces import (
    BadMacaroonContext,
    IMacaroonIssuer,
)
from lp.services.macaroons.testing import MacaroonTestMixin
from lp.services.propertycache import clear_property_cache
from lp.services.webapp.interfaces import OAuthPermission
from lp.services.webapp.publisher import canonical_url
from lp.services.webhooks.testing import LogsScheduledWebhooks
from lp.testing import (
    ANONYMOUS,
    StormStatementRecorder,
    TestCaseWithFactory,
    admin_logged_in,
    api_url,
    login,
    logout,
    person_logged_in,
)
from lp.testing.dbuser import dbuser
from lp.testing.layers import (
    DatabaseFunctionalLayer,
    LaunchpadFunctionalLayer,
    LaunchpadZopelessLayer,
)
from lp.testing.matchers import HasQueryCount
from lp.testing.pages import webservice_for_person
from lp.xmlrpc.interfaces import IPrivateApplication


class TestOCIFileSet(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))

    def test_implements_interface(self):
        file_set = getUtility(IOCIFileSet)
        self.assertProvides(file_set, IOCIFileSet)

    def test_getByLayerDigest(self):
        digest = "test_digest"
        oci_file = self.factory.makeOCIFile(layer_file_digest=digest)
        for _ in range(3):
            self.factory.makeOCIFile()

        result = getUtility(IOCIFileSet).getByLayerDigest(digest)
        self.assertEqual(oci_file, result)

    def test_getByLayerDigest_not_matching(self):
        result = getUtility(IOCIFileSet).getByLayerDigest("not existing")
        self.assertIsNone(result)


class TestOCIRecipeBuild(OCIConfigHelperMixin, TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        self.build = self.factory.makeOCIRecipeBuild()

    def test_implements_interface(self):
        with admin_logged_in():
            self.assertProvides(self.build, IOCIRecipeBuild)
            self.assertProvides(self.build, IPackageBuild)
            self.assertProvides(self.build, IPrivacy)

    def test_addFile(self):
        lfa = self.factory.makeLibraryFileAlias()
        self.build.addFile(lfa)
        result_lfa = self.build.getFileByName(lfa.filename)
        self.assertEqual(result_lfa, lfa)

    def test_getFileByName(self):
        files = [self.factory.makeOCIFile(build=self.build) for x in range(3)]
        result = self.build.getFileByName(files[0].library_file.filename)
        self.assertEqual(files[0].library_file, result)

    def test_getFileByName_missing(self):
        self.assertRaises(NotFoundError, self.build.getFileByName, "missing")

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

    def test_getLayerFileByDigest(self):
        files = [
            self.factory.makeOCIFile(
                build=self.build, layer_file_digest=str(x)
            )
            for x in range(3)
        ]
        result, _, _ = self.build.getLayerFileByDigest(
            files[0].layer_file_digest
        )
        self.assertEqual(result, files[0])

    def test_getLayerFileByDigest_missing(self):
        [
            self.factory.makeOCIFile(
                build=self.build, layer_file_digest=str(x)
            )
            for x in range(3)
        ]
        self.assertRaises(
            NotFoundError, self.build.getLayerFileByDigest, "missing"
        )

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
            build = self.factory.makeOCIRecipeBuild(status=status)
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
        build = self.factory.makeOCIRecipeBuild(distro_arch_series=das)
        self.assertFalse(build.can_be_retried)

    def test_can_be_cancelled(self):
        # For all states that can be cancelled, can_be_cancelled returns True.
        ok_cases = [
            BuildStatus.BUILDING,
            BuildStatus.NEEDSBUILD,
        ]
        for status in BuildStatus.items:
            build = self.factory.makeOCIRecipeBuild()
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
        build = self.factory.makeOCIRecipeBuild()
        build.updateStatus(BuildStatus.BUILDING, date_started=now)
        build.updateStatus(BuildStatus.FAILEDTOBUILD)
        build.gotFailure()
        with person_logged_in(build.recipe.owner):
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
        # Previous successful builds of the same OCI recipe are used for
        # estimates.
        oci_build = self.factory.makeOCIRecipeBuild(
            status=BuildStatus.FULLYBUILT, duration=timedelta(seconds=335)
        )
        for _ in range(3):
            self.factory.makeOCIRecipeBuild(
                requester=oci_build.requester,
                recipe=oci_build.recipe,
                status=BuildStatus.FAILEDTOBUILD,
                duration=timedelta(seconds=20),
            )
        self.assertEqual(335, oci_build.estimateDuration().seconds)

    def test_queueBuild(self):
        # OCIRecipeBuild can create the queue entry for itself.
        bq = self.build.queueBuild()
        self.assertProvides(bq, IBuildQueue)
        self.assertEqual(
            self.build.build_farm_job, removeSecurityProxy(bq)._build_farm_job
        )
        self.assertEqual(self.build, bq.specific_build)
        self.assertEqual(self.build.virtualized, bq.virtualized)
        self.assertIsNotNone(bq.processor)
        self.assertEqual(bq, self.build.buildqueue_record)

    def test_is_private(self):
        # An OCIRecipeBuild is private if its recipe or owner is.
        self.assertFalse(self.build.is_private)
        self.assertFalse(self.build.private)
        private_team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.MODERATED,
            visibility=PersonVisibility.PRIVATE,
        )
        with person_logged_in(private_team.teamowner):
            build = self.factory.makeOCIRecipeBuild(
                requester=private_team.teamowner,
                owner=private_team,
                information_type=InformationType.USERDATA,
            )
            self.assertTrue(build.is_private)
            self.assertTrue(build.private)

    def test_updateStatus_triggers_webhooks(self):
        # Updating the status of an OCIRecipeBuild triggers webhooks on the
        # corresponding OCIRecipe.
        logger = self.useFixture(FakeLogger())
        hook = self.factory.makeWebhook(
            target=self.build.recipe, event_types=["oci-recipe:build:0.1"]
        )
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        expected_payload = {
            "recipe_build": Equals(
                canonical_url(self.build, force_local_path=True)
            ),
            "action": Equals("status-changed"),
            "recipe": Equals(
                canonical_url(self.build.recipe, force_local_path=True)
            ),
            "build_request": Is(None),
            "status": Equals("Successfully built"),
            "registry_upload_status": Equals("Unscheduled"),
        }
        self.assertThat(
            logger.output,
            LogsScheduledWebhooks(
                [(hook, "oci-recipe:build:0.1", MatchesDict(expected_payload))]
            ),
        )

        delivery = hook.deliveries.one()
        self.assertThat(
            delivery,
            MatchesStructure(
                event_type=Equals("oci-recipe:build:0.1"),
                payload=MatchesDict(expected_payload),
            ),
        )
        with dbuser(config.IWebhookDeliveryJobSource.dbuser):
            self.assertEqual(
                "<WebhookDeliveryJob for webhook %d on %r>"
                % (hook.id, hook.target),
                repr(delivery),
            )

    def test_updateStatus_no_change_does_not_trigger_webhooks(self):
        # An updateStatus call that doesn't change the build's status
        # attribute does not trigger webhooks.
        logger = self.useFixture(FakeLogger())
        hook = self.factory.makeWebhook(
            target=self.build.recipe, event_types=["oci-recipe:build:0.1"]
        )
        self.build.updateStatus(BuildStatus.BUILDING)
        expected_logs = [
            (
                hook,
                "oci-recipe:build:0.1",
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

        self.build.updateStatus(BuildStatus.BUILDING)
        expected_logs = [
            (
                hook,
                "oci-recipe:build:0.1",
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

    def test_updateStatus_failure_does_not_trigger_registry_uploads(self):
        # A failed OCIRecipeBuild does not trigger registry uploads.
        self.setConfig()
        self.factory.makeOCIPushRule(self.build.recipe)
        with dbuser(config.builddmaster.dbuser):
            self.build.updateStatus(BuildStatus.FAILEDTOBUILD)
        self.assertContentEqual([], self.build.registry_upload_jobs)

    def test_updateStatus_fullybuilt_not_configured(self):
        # A completed OCIRecipeBuild does not trigger registry uploads if
        # the recipe is not properly configured for that.
        logger = self.useFixture(FakeLogger())
        with dbuser(config.builddmaster.dbuser):
            self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.assertEqual(0, len(list(self.build.registry_upload_jobs)))
        self.assertIn(
            "%r is not configured for upload to registries."
            % (self.build.recipe),
            logger.output.splitlines(),
        )

    def test_updateStatus_fullybuilt_triggers_registry_uploads(self):
        # A completed OCIRecipeBuild triggers registry uploads.
        self.setConfig()
        logger = self.useFixture(FakeLogger())
        self.factory.makeOCIPushRule(self.build.recipe)
        with dbuser(config.builddmaster.dbuser):
            self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.assertEqual(1, len(list(self.build.registry_upload_jobs)))
        self.assertIn(
            "Scheduling upload of %r to registries." % self.build,
            logger.output.splitlines(),
        )

    def test_updateStatus_fullybuilt_distro_triggers_registry_uploads(self):
        # A completed OCIRecipeBuild with distribution credentials triggers
        # registry uploads.
        self.setConfig()
        logger = self.useFixture(FakeLogger())
        distribution = self.factory.makeDistribution()
        distribution.oci_registry_credentials = (
            self.factory.makeOCIRegistryCredentials()
        )
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        recipe = self.factory.makeOCIRecipe(oci_project=oci_project)
        build = self.factory.makeOCIRecipeBuild(recipe=recipe)
        oci_project.setOfficialRecipeStatus(recipe, True)
        with dbuser(config.builddmaster.dbuser):
            build.updateStatus(BuildStatus.FULLYBUILT)
        self.assertEqual(1, len(list(build.registry_upload_jobs)))
        self.assertIn(
            "Scheduling upload of %r to registries." % build,
            logger.output.splitlines(),
        )

    def test_eta(self):
        # OCIRecipeBuild.eta returns a non-None value when it should, or
        # None when there's no start time.
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
        # OCIRecipeBuild.estimate returns True until the job is completed.
        self.build.queueBuild()
        self.factory.makeBuilder(processors=[self.build.processor])
        self.build.updateStatus(BuildStatus.BUILDING)
        self.assertTrue(self.build.estimate)
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        clear_property_cache(self.build)
        self.assertFalse(self.build.estimate)

    def test_registry_upload_status_unscheduled(self):
        build = self.factory.makeOCIRecipeBuild(status=BuildStatus.FULLYBUILT)
        self.assertEqual(
            OCIRecipeBuildRegistryUploadStatus.UNSCHEDULED,
            build.registry_upload_status,
        )

    def test_registry_upload_status_pending(self):
        build = self.factory.makeOCIRecipeBuild(status=BuildStatus.FULLYBUILT)
        getUtility(IOCIRegistryUploadJobSource).create(build)
        self.assertEqual(
            OCIRecipeBuildRegistryUploadStatus.PENDING,
            build.registry_upload_status,
        )

    def test_registry_upload_status_uploaded(self):
        build = self.factory.makeOCIRecipeBuild(status=BuildStatus.FULLYBUILT)
        job = getUtility(IOCIRegistryUploadJobSource).create(build)
        naked_job = removeSecurityProxy(job)
        naked_job.job._status = JobStatus.COMPLETED
        self.assertEqual(
            OCIRecipeBuildRegistryUploadStatus.UPLOADED,
            build.registry_upload_status,
        )

    def test_registry_upload_status_failed_to_upload(self):
        build = self.factory.makeOCIRecipeBuild(status=BuildStatus.FULLYBUILT)
        job = getUtility(IOCIRegistryUploadJobSource).create(build)
        naked_job = removeSecurityProxy(job)
        naked_job.job._status = JobStatus.FAILED
        self.assertEqual(
            OCIRecipeBuildRegistryUploadStatus.FAILEDTOUPLOAD,
            build.registry_upload_status,
        )

    def test_registry_upload_error_summary_no_job(self):
        build = self.factory.makeOCIRecipeBuild(status=BuildStatus.FULLYBUILT)
        self.assertIsNone(build.registry_upload_error_summary)

    def test_registry_upload_error_summary_job_no_error(self):
        build = self.factory.makeOCIRecipeBuild(status=BuildStatus.FULLYBUILT)
        getUtility(IOCIRegistryUploadJobSource).create(build)
        self.assertIsNone(build.registry_upload_error_summary)

    def test_registry_upload_error_summary_job_error(self):
        build = self.factory.makeOCIRecipeBuild(status=BuildStatus.FULLYBUILT)
        job = getUtility(IOCIRegistryUploadJobSource).create(build)
        removeSecurityProxy(job).error_summary = "Boom"
        self.assertEqual("Boom", build.registry_upload_error_summary)

    def test_registry_upload_errors_no_job(self):
        build = self.factory.makeOCIRecipeBuild(status=BuildStatus.FULLYBUILT)
        self.assertEqual([], build.registry_upload_errors)

    def test_registry_upload_errors_job_no_error(self):
        build = self.factory.makeOCIRecipeBuild(status=BuildStatus.FULLYBUILT)
        getUtility(IOCIRegistryUploadJobSource).create(build)
        self.assertEqual([], build.registry_upload_errors)

    def test_registry_upload_errors_job_error(self):
        build = self.factory.makeOCIRecipeBuild(status=BuildStatus.FULLYBUILT)
        job = getUtility(IOCIRegistryUploadJobSource).create(build)
        removeSecurityProxy(job).errors = [
            {"code": "BOOM", "message": "Boom", "detail": "It went boom"},
        ]
        self.assertEqual(
            [{"code": "BOOM", "message": "Boom", "detail": "It went boom"}],
            build.registry_upload_errors,
        )

    def test_scheduleRegistryUpload(self):
        # A build not previously uploaded to a registry can be uploaded
        # manually.
        self.setConfig()
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.factory.makeOCIPushRule(recipe=self.build.recipe)
        self.factory.makeOCIFile(
            build=self.build,
            library_file=self.factory.makeLibraryFileAlias(db_only=True),
        )
        self.build.scheduleRegistryUpload()
        [job] = getUtility(IOCIRegistryUploadJobSource).iterReady()
        self.assertEqual(JobStatus.WAITING, job.job.status)
        self.assertEqual(self.build, job.build)

    def test_scheduleRegistryUpload_not_configured(self):
        # A build that is not properly configured cannot be uploaded to
        # registries.
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.assertRaisesWithContent(
            CannotScheduleRegistryUpload,
            "Cannot upload this build to registries because the recipe is not "
            "properly configured.",
            self.build.scheduleRegistryUpload,
        )
        self.assertEqual(
            [], list(getUtility(IOCIRegistryUploadJobSource).iterReady())
        )

    def test_scheduleRegistryUpload_no_files(self):
        # A build with no files cannot be uploaded to registries.
        self.setConfig()
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.factory.makeOCIPushRule(recipe=self.build.recipe)
        self.assertRaisesWithContent(
            CannotScheduleRegistryUpload,
            "Cannot upload this build because it has no files.",
            self.build.scheduleRegistryUpload,
        )
        self.assertEqual(
            [], list(getUtility(IOCIRegistryUploadJobSource).iterReady())
        )

    def test_scheduleRegistryUpload_already_in_progress(self):
        # A build with an upload already in progress will not have another
        # one created.
        self.setConfig()
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.factory.makeOCIPushRule(recipe=self.build.recipe)
        self.factory.makeOCIFile(
            build=self.build,
            library_file=self.factory.makeLibraryFileAlias(db_only=True),
        )
        old_job = getUtility(IOCIRegistryUploadJobSource).create(self.build)
        self.assertRaisesWithContent(
            CannotScheduleRegistryUpload,
            "An upload of this build is already in progress.",
            self.build.scheduleRegistryUpload,
        )
        self.assertEqual(
            [old_job],
            list(getUtility(IOCIRegistryUploadJobSource).iterReady()),
        )

    def test_scheduleRegistryUpload_already_uploaded(self):
        # A build with an upload that has already completed will not have
        # another one created.
        self.setConfig()
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.factory.makeOCIPushRule(recipe=self.build.recipe)
        self.factory.makeOCIFile(
            build=self.build,
            library_file=self.factory.makeLibraryFileAlias(db_only=True),
        )
        old_job = getUtility(IOCIRegistryUploadJobSource).create(self.build)
        removeSecurityProxy(old_job).job._status = JobStatus.COMPLETED
        self.assertRaisesWithContent(
            CannotScheduleRegistryUpload,
            "Cannot upload this build because it has already been uploaded.",
            self.build.scheduleRegistryUpload,
        )
        self.assertEqual(
            [], list(getUtility(IOCIRegistryUploadJobSource).iterReady())
        )

    def test_scheduleRegistryUpload_triggers_webhooks(self):
        # Scheduling a registry upload triggers webhooks on the
        # corresponding recipe.
        self.setConfig()
        logger = self.useFixture(FakeLogger())
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        self.factory.makeOCIFile(
            build=self.build,
            library_file=self.factory.makeLibraryFileAlias(db_only=True),
        )
        self.factory.makeOCIPushRule(recipe=self.build.recipe)
        hook = self.factory.makeWebhook(
            target=self.build.recipe, event_types=["oci-recipe:build:0.1"]
        )
        self.build.scheduleRegistryUpload()
        expected_payload = {
            "recipe_build": Equals(
                canonical_url(self.build, force_local_path=True)
            ),
            "action": Equals("status-changed"),
            "recipe": Equals(
                canonical_url(self.build.recipe, force_local_path=True)
            ),
            "build_request": Is(None),
            "status": Equals("Successfully built"),
            "registry_upload_status": Equals("Pending"),
        }
        delivery = hook.deliveries.one()
        self.assertThat(
            delivery,
            MatchesStructure(
                event_type=Equals("oci-recipe:build:0.1"),
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
                    [
                        (
                            hook,
                            "oci-recipe:build:0.1",
                            MatchesDict(expected_payload),
                        )
                    ]
                ),
            )


class TestOCIRecipeBuildSet(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))

    def test_implements_interface(self):
        target = OCIRecipeBuildSet()
        with admin_logged_in():
            self.assertProvides(target, IOCIRecipeBuildSet)

    def test_new(self):
        requester = self.factory.makePerson()
        distribution = self.factory.makeDistribution()
        distroseries = self.factory.makeDistroSeries(
            distribution=distribution, status=SeriesStatus.CURRENT
        )
        processor = getUtility(IProcessorSet).getByName("386")
        distro_arch_series = self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag="i386",
            processor=processor,
        )
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        recipe = self.factory.makeOCIRecipe(oci_project=oci_project)
        target = getUtility(IOCIRecipeBuildSet).new(
            requester, recipe, distro_arch_series
        )
        with admin_logged_in():
            self.assertProvides(target, IOCIRecipeBuild)
            self.assertEqual(distro_arch_series, target.distro_arch_series)

    def test_new_oci_feature_flag_enabled(self):
        requester = self.factory.makePerson()
        distribution = self.factory.makeDistribution()
        distroseries = self.factory.makeDistroSeries(
            distribution=distribution, status=SeriesStatus.CURRENT
        )
        processor = getUtility(IProcessorSet).getByName("386")
        self.useFixture(
            FeatureFixture(
                {
                    "oci.build_series.%s"
                    % distribution.name: distroseries.name,
                    OCI_RECIPE_ALLOW_CREATE: "on",
                }
            )
        )
        distro_arch_series = self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag="i386",
            processor=processor,
        )
        oci_project = self.factory.makeOCIProject(pillar=distribution)
        recipe = self.factory.makeOCIRecipe(oci_project=oci_project)
        target = getUtility(IOCIRecipeBuildSet).new(
            requester, recipe, distro_arch_series
        )
        with admin_logged_in():
            self.assertProvides(target, IOCIRecipeBuild)
            self.assertEqual(distro_arch_series, target.distro_arch_series)

    def test_getByID(self):
        builds = [self.factory.makeOCIRecipeBuild() for x in range(3)]
        result = getUtility(IOCIRecipeBuildSet).getByID(builds[1].id)
        self.assertEqual(result, builds[1])

    def test_getByBuildFarmJob(self):
        builds = [self.factory.makeOCIRecipeBuild() for x in range(3)]
        result = getUtility(IOCIRecipeBuildSet).getByBuildFarmJob(
            builds[1].build_farm_job
        )
        self.assertEqual(result, builds[1])

    def test_getByBuildFarmJobs(self):
        builds = [self.factory.makeOCIRecipeBuild() for x in range(3)]
        self.assertContentEqual(
            builds,
            getUtility(IOCIRecipeBuildSet).getByBuildFarmJobs(
                [build.build_farm_job for build in builds]
            ),
        )

    def test_getByBuildFarmJobs_empty(self):
        self.assertContentEqual(
            [], getUtility(IOCIRecipeBuildSet).getByBuildFarmJobs([])
        )

    def test_virtualized_recipe_requires(self):
        recipe = self.factory.makeOCIRecipe(require_virtualized=True)
        target = self.factory.makeOCIRecipeBuild(recipe=recipe)
        self.assertTrue(target.virtualized)

    def test_virtualized_processor_requires(self):
        recipe = self.factory.makeOCIRecipe(require_virtualized=False)
        distro_arch_series = self.factory.makeDistroArchSeries(
            distroseries=self.factory.makeDistroSeries(
                distribution=recipe.oci_project.distribution
            )
        )
        distro_arch_series.processor.supports_nonvirtualized = False
        target = self.factory.makeOCIRecipeBuild(
            distro_arch_series=distro_arch_series, recipe=recipe
        )
        self.assertTrue(target.virtualized)

    def test_virtualized_no_support(self):
        recipe = self.factory.makeOCIRecipe(require_virtualized=False)
        distro_arch_series = self.factory.makeDistroArchSeries(
            distroseries=self.factory.makeDistroSeries(
                distribution=recipe.oci_project.distribution
            )
        )
        distro_arch_series.processor.supports_nonvirtualized = True
        target = self.factory.makeOCIRecipeBuild(
            recipe=recipe, distro_arch_series=distro_arch_series
        )
        self.assertFalse(target.virtualized)


class TestOCIRecipeBuildWebservice(OCIConfigHelperMixin, TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.setConfig()
        self.person = self.factory.makePerson()
        self.webservice = webservice_for_person(
            self.person, permission=OAuthPermission.WRITE_PRIVATE
        )
        self.webservice.default_api_version = "devel"
        login(ANONYMOUS)

    def getURL(self, obj):
        return self.webservice.getAbsoluteUrl(api_url(obj))

    def test_properties(self):
        # The basic properties of an OCIRecipeBuild are sensible.
        db_build = self.factory.makeOCIRecipeBuild(requester=self.person)
        build_url = api_url(db_build)
        logout()
        build = self.webservice.get(build_url).jsonBody()
        with person_logged_in(self.person):
            self.assertThat(
                build,
                ContainsDict(
                    {
                        "requester_link": Equals(self.getURL(self.person)),
                        "recipe_link": Equals(self.getURL(db_build.recipe)),
                        "distro_arch_series_link": Equals(
                            self.getURL(db_build.distro_arch_series)
                        ),
                        "arch_tag": Equals(
                            db_build.distro_arch_series.architecturetag
                        ),
                        "score": Is(None),
                        "can_be_rescored": Is(False),
                        "can_be_cancelled": Is(False),
                    }
                ),
            )

    def test_public(self):
        # An OCIRecipeBuild with a public recipe and repository is itself
        # public.
        db_build = self.factory.makeOCIRecipeBuild()
        build_url = api_url(db_build)
        unpriv_webservice = webservice_for_person(
            self.factory.makePerson(), permission=OAuthPermission.WRITE_PUBLIC
        )
        unpriv_webservice.default_api_version = "devel"
        logout()
        self.assertEqual(200, self.webservice.get(build_url).status)
        self.assertEqual(200, unpriv_webservice.get(build_url).status)

    def test_private_recipe(self):
        # An OCIRecipeBuild with a private recipe is private.
        db_team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.MODERATED, owner=self.person
        )
        with person_logged_in(self.person):
            db_build = self.factory.makeOCIRecipeBuild(
                requester=self.person,
                owner=db_team,
                information_type=InformationType.USERDATA,
            )
            build_url = api_url(db_build)
        unpriv_webservice = webservice_for_person(
            self.factory.makePerson(), permission=OAuthPermission.WRITE_PUBLIC
        )
        unpriv_webservice.default_api_version = "devel"
        logout()
        response = self.webservice.get(build_url)
        self.assertEqual(200, response.status)
        self.assertEqual(401, unpriv_webservice.get(build_url).status)

    def test_private_recipe_owner(self):
        # An OCIRecipeBuild with a private recipe owner is private.
        db_team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.MODERATED,
            owner=self.person,
            visibility=PersonVisibility.PRIVATE,
        )
        with person_logged_in(self.person):
            db_build = self.factory.makeOCIRecipeBuild(
                requester=self.person,
                owner=db_team,
                information_type=InformationType.USERDATA,
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

    def test_cancel(self):
        # The owner of a build can cancel it.
        db_build = self.factory.makeOCIRecipeBuild(requester=self.person)
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
        db_build = self.factory.makeOCIRecipeBuild(requester=self.person)
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
        self.assertEqual(4010, build["score"])
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
        db_build = self.factory.makeOCIRecipeBuild(requester=self.person)
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
        db_build = self.factory.makeOCIRecipeBuild(requester=self.person)
        db_files = [self.factory.makeOCIFile(build=db_build) for i in range(2)]
        build_url = api_url(db_build)
        file_urls = [
            ProxiedLibraryFileAlias(file.library_file, db_build).http_url
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


class TestOCIRecipeBuildMacaroonIssuer(
    MacaroonTestMixin, OCIConfigHelperMixin, TestCaseWithFactory
):
    """Test OCIRecipeBuild macaroon issuing and verification."""

    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.setConfig()
        self.pushConfig(
            "launchpad", internal_macaroon_secret_key="some-secret"
        )

    def getPrivateBuild(self):
        build = self.factory.makeOCIRecipeBuild()
        build.recipe.git_ref.repository.transitionToInformationType(
            InformationType.PRIVATESECURITY, build.recipe.registrant
        )
        return build

    def test_issueMacaroon_refuses_public_ocirecipebuild(self):
        build = self.factory.makeOCIRecipeBuild()
        issuer = getUtility(IMacaroonIssuer, "oci-recipe-build")
        self.assertRaises(
            BadMacaroonContext,
            removeSecurityProxy(issuer).issueMacaroon,
            build,
        )

    def test_issueMacaroon_good(self):
        build = self.getPrivateBuild()
        issuer = getUtility(IMacaroonIssuer, "oci-recipe-build")
        macaroon = removeSecurityProxy(issuer).issueMacaroon(build)
        self.assertThat(
            macaroon,
            MatchesStructure(
                location=Equals("launchpad.test"),
                identifier=Equals("oci-recipe-build"),
                caveats=MatchesListwise(
                    [
                        MatchesStructure.byEquality(
                            caveat_id="lp.oci-recipe-build %s" % build.id
                        ),
                    ]
                ),
            ),
        )

    def test_issueMacaroon_via_authserver(self):
        build = self.getPrivateBuild()
        private_root = getUtility(IPrivateApplication)
        authserver = AuthServerAPIView(private_root.authserver, TestRequest())
        macaroon = Macaroon.deserialize(
            authserver.issueMacaroon(
                "oci-recipe-build", "OCIRecipeBuild", build.id
            )
        )
        self.assertThat(
            macaroon,
            MatchesStructure(
                location=Equals("launchpad.test"),
                identifier=Equals("oci-recipe-build"),
                caveats=MatchesListwise(
                    [
                        MatchesStructure.byEquality(
                            caveat_id="lp.oci-recipe-build %s" % build.id
                        ),
                    ]
                ),
            ),
        )

    def test_verifyMacaroon_good(self):
        build = self.getPrivateBuild()
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(
            getUtility(IMacaroonIssuer, "oci-recipe-build")
        )
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonVerifies(
            issuer, macaroon, build.recipe.git_ref.repository
        )

    def test_verifyMacaroon_good_no_context(self):
        build = self.getPrivateBuild()
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(
            getUtility(IMacaroonIssuer, "oci-recipe-build")
        )
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonVerifies(
            issuer, macaroon, None, require_context=False
        )
        self.assertMacaroonVerifies(
            issuer,
            macaroon,
            build.recipe.git_ref.repository,
            require_context=False,
        )

    def test_verifyMacaroon_no_context_but_require_context(self):
        build = self.getPrivateBuild()
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(
            getUtility(IMacaroonIssuer, "oci-recipe-build")
        )
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonDoesNotVerify(
            ["Expected macaroon verification context but got None."],
            issuer,
            macaroon,
            None,
        )

    def test_verifyMacaroon_wrong_location(self):
        build = self.getPrivateBuild()
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(
            getUtility(IMacaroonIssuer, "oci-recipe-build")
        )
        macaroon = Macaroon(
            location="another-location", key=issuer._root_secret
        )
        self.assertMacaroonDoesNotVerify(
            ["Macaroon has unknown location 'another-location'."],
            issuer,
            macaroon,
            build.recipe.git_ref.repository,
        )
        self.assertMacaroonDoesNotVerify(
            ["Macaroon has unknown location 'another-location'."],
            issuer,
            macaroon,
            build.recipe.git_ref.repository,
            require_context=False,
        )

    def test_verifyMacaroon_wrong_key(self):
        build = self.getPrivateBuild()
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(
            getUtility(IMacaroonIssuer, "oci-recipe-build")
        )
        macaroon = Macaroon(
            location=config.vhost.mainsite.hostname, key="another-secret"
        )
        self.assertMacaroonDoesNotVerify(
            ["Signatures do not match"],
            issuer,
            macaroon,
            build.recipe.git_ref.repository,
        )
        self.assertMacaroonDoesNotVerify(
            ["Signatures do not match"],
            issuer,
            macaroon,
            build.recipe.git_ref.repository,
            require_context=False,
        )

    def test_verifyMacaroon_not_building(self):
        build = self.getPrivateBuild()
        issuer = removeSecurityProxy(
            getUtility(IMacaroonIssuer, "oci-recipe-build")
        )
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonDoesNotVerify(
            ["Caveat check for 'lp.oci-recipe-build %s' failed." % build.id],
            issuer,
            macaroon,
            build.recipe.git_ref.repository,
        )

    def test_verifyMacaroon_wrong_build(self):
        build = self.getPrivateBuild()
        build.updateStatus(BuildStatus.BUILDING)
        other_build = self.getPrivateBuild()
        other_build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(
            getUtility(IMacaroonIssuer, "oci-recipe-build")
        )
        macaroon = issuer.issueMacaroon(other_build)
        self.assertMacaroonDoesNotVerify(
            [
                "Caveat check for 'lp.oci-recipe-build %s' failed."
                % other_build.id
            ],
            issuer,
            macaroon,
            build.recipe.git_ref.repository,
        )

    def test_verifyMacaroon_wrong_repository(self):
        build = self.getPrivateBuild()
        other_repository = self.factory.makeGitRepository()
        build.updateStatus(BuildStatus.BUILDING)
        issuer = removeSecurityProxy(
            getUtility(IMacaroonIssuer, "oci-recipe-build")
        )
        macaroon = issuer.issueMacaroon(build)
        self.assertMacaroonDoesNotVerify(
            ["Caveat check for 'lp.oci-recipe-build %s' failed." % build.id],
            issuer,
            macaroon,
            other_repository,
        )
