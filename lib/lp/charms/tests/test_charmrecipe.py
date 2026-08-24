# Copyright 2021-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test charm recipes."""

import base64
import json
from datetime import datetime, timedelta, timezone
from textwrap import dedent
from unittest import TestCase
from urllib.parse import urlsplit

import iso8601
import responses
import transaction
from fixtures import FakeLogger
from nacl.public import PrivateKey
from pymacaroons import Macaroon
from pymacaroons.serializers import JsonSerializer
from storm.exceptions import LostObjectError
from storm.locals import Store
from testtools.matchers import (
    AfterPreprocessing,
    ContainsDict,
    Equals,
    GreaterThan,
    Is,
    LessThan,
    MatchesAll,
    MatchesDict,
    MatchesListwise,
    MatchesSetwise,
    MatchesStructure,
)
from testtools.testcase import ExpectedException
from zope.component import getUtility
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.buildmaster.builderproxy import FetchServicePolicy
from lp.buildmaster.enums import (
    BuildBaseImageType,
    BuildQueueStatus,
    BuildStatus,
)
from lp.buildmaster.interfaces.buildqueue import IBuildQueue
from lp.buildmaster.interfaces.processor import (
    IProcessorSet,
    ProcessorNotFound,
)
from lp.buildmaster.model.buildfarmjob import BuildFarmJob
from lp.buildmaster.model.buildqueue import BuildQueue
from lp.charms.adapters.buildarch import CraftPlatformsBuildPlanError
from lp.charms.interfaces.charmrecipe import (
    CHARM_RECIPE_ALLOW_CREATE,
    CHARM_RECIPE_BUILD_DISTRIBUTION,
    CHARM_RECIPE_PRIVATE_FEATURE_FLAG,
    CHARM_RECIPE_USSO_FEATURE_FLAG,
    CHARM_RECIPE_WEBHOOKS_FEATURE_FLAG,
    BadCharmRecipeSearchContext,
    CannotAuthorizeCharmhubUploads,
    CharmRecipeBuildAlreadyPending,
    CharmRecipeBuildDisallowedArchitecture,
    CharmRecipeBuildRequestStatus,
    CharmRecipeFeatureDisabled,
    CharmRecipePrivateFeatureDisabled,
    ICharmRecipe,
    ICharmRecipeSet,
    ICharmRecipeView,
    NoSourceForCharmRecipe,
)
from lp.charms.interfaces.charmrecipebuild import (
    ICharmRecipeBuild,
    ICharmRecipeBuildSet,
)
from lp.charms.interfaces.charmrecipebuildjob import ICharmhubUploadJobSource
from lp.charms.interfaces.charmrecipejob import (
    ICharmRecipeRequestBuildsJobSource,
)
from lp.charms.model.charmrecipe import CharmRecipeSet, is_unified_format
from lp.charms.model.charmrecipebuild import CharmFile
from lp.charms.model.charmrecipebuildjob import CharmRecipeBuildJob
from lp.charms.model.charmrecipejob import CharmRecipeJob
from lp.code.errors import GitRepositoryBlobNotFound
from lp.code.tests.helpers import GitHostingFixture
from lp.registry.enums import PersonVisibility, TeamMembershipPolicy
from lp.registry.interfaces.series import SeriesStatus
from lp.services.config import config
from lp.services.crypto.interfaces import IEncryptedContainer
from lp.services.database.constants import ONE_DAY_AGO, UTC_NOW
from lp.services.database.interfaces import IStore
from lp.services.database.sqlbase import (
    flush_database_caches,
    get_transaction_timestamp,
)
from lp.services.features.testing import FeatureFixture
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.runner import JobRunner
from lp.services.log.logger import BufferLogger
from lp.services.webapp.interfaces import OAuthPermission
from lp.services.webapp.publisher import canonical_url
from lp.services.webapp.snapshot import notify_modified
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
    record_two_runs,
)
from lp.testing.dbuser import dbuser
from lp.testing.layers import (
    DatabaseFunctionalLayer,
    LaunchpadFunctionalLayer,
    LaunchpadZopelessLayer,
)
from lp.testing.matchers import DoesNotSnapshot, HasQueryCount
from lp.testing.pages import webservice_for_person


class TestCharmRecipeFormatDetector(TestCase):
    """Detect whether a configuration file uses the unified format.

    For more information refer to the docstring of `is_unified_format`.
    """

    def test_is_unified_format_with_old_format(self):
        # 'bases' is only used with the old configuration format
        d = {
            "bases": {
                "build-on": [
                    {
                        "name": "ubuntu",
                        "channel": "20.04",
                        "architectures": ["sparc"],
                    }
                ]
            },
        }
        self.assertFalse(is_unified_format(d))

    def test_is_unified_format_with_new_syntax(self):
        # 'base', 'build-base', and 'platforms' were introduced with the
        # 'unified' configuration
        d = {
            "base": "ubuntu@24.04",
            "build-base": "ubuntu@24.04",
            "platforms": {"amd64": [{"build-on": "riscv64"}]},
        }
        self.assertTrue(is_unified_format(d))


class TestCharmRecipeFeatureFlags(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def test_feature_flag_disabled(self):
        # Without a feature flag, we wil not create any charm recipes.
        self.assertRaises(
            CharmRecipeFeatureDisabled, self.factory.makeCharmRecipe
        )

    def test_private_feature_flag_disabled(self):
        # Without a private feature flag, we wil not create new private
        # charm recipes.
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))
        self.assertRaises(
            CharmRecipePrivateFeatureDisabled,
            self.factory.makeCharmRecipe,
            information_type=InformationType.PROPRIETARY,
        )


class TestCharmRecipe(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))

    def test_implements_interfaces(self):
        # CharmRecipe implements ICharmRecipe.
        recipe = self.factory.makeCharmRecipe()
        with admin_logged_in():
            self.assertProvides(recipe, ICharmRecipe)

    def test___repr__(self):
        # CharmRecipe objects have an informative __repr__.
        recipe = self.factory.makeCharmRecipe()
        self.assertEqual(
            "<CharmRecipe ~%s/%s/+charm/%s>"
            % (recipe.owner.name, recipe.project.name, recipe.name),
            repr(recipe),
        )

    def test_avoids_problematic_snapshots(self):
        self.assertThat(
            self.factory.makeCharmRecipe(),
            DoesNotSnapshot(
                [
                    "pending_build_requests",
                    "failed_build_requests",
                    "builds",
                    "completed_builds",
                    "pending_builds",
                ],
                ICharmRecipeView,
            ),
        )

    def test_initial_date_last_modified(self):
        # The initial value of date_last_modified is date_created.
        recipe = self.factory.makeCharmRecipe(date_created=ONE_DAY_AGO)
        self.assertEqual(recipe.date_created, recipe.date_last_modified)

    def test_modifiedevent_sets_date_last_modified(self):
        # When a CharmRecipe receives an object modified event, the last
        # modified date is set to UTC_NOW.
        recipe = self.factory.makeCharmRecipe(date_created=ONE_DAY_AGO)
        with notify_modified(removeSecurityProxy(recipe), ["name"]):
            pass
        self.assertSqlAttributeEqualsDate(
            recipe, "date_last_modified", UTC_NOW
        )

    def test_build_path_strips_trailing_slashes(self):
        # A trailing slash in the build path is stripped, since it would
        # otherwise lead to confusing build failures.
        recipe = self.factory.makeCharmRecipe(build_path="charm/foo/")
        self.assertEqual("charm/foo", recipe.build_path)
        removeSecurityProxy(recipe).build_path = "charm/bar//"
        self.assertEqual("charm/bar", recipe.build_path)

    def test_build_path_only_slashes_becomes_none(self):
        # A build path consisting only of slashes reduces to None, so it is
        # not joined into the charmcraft.yaml lookup path.
        recipe = self.factory.makeCharmRecipe(build_path="/")
        self.assertIsNone(recipe.build_path)

    def test_build_path_empty_becomes_none(self):
        # An empty build path is normalised to None for the same reason.
        recipe = self.factory.makeCharmRecipe(build_path="")
        self.assertIsNone(recipe.build_path)

    def test_getCharmcraftYaml_build_path_trailing_slash_no_double_slash(self):
        # A trailing slash in build_path must not produce a "//" in the
        # charmcraft.yaml lookup path; Git resolves such a path to a missing
        # blob, which previously masked the real charmcraft.yaml.
        requested = []

        def getBlob(path, filename, *args, **kwargs):
            requested.append(filename)
            if filename == "charm/foo/charmcraft.yaml":
                return b"name: test-charm"
            raise GitRepositoryBlobNotFound("dummy", filename)

        self.useFixture(GitHostingFixture()).getBlob = getBlob
        [git_ref] = self.factory.makeGitRefs()
        recipe = self.factory.makeCharmRecipe(
            git_ref=git_ref, build_path="charm/foo/"
        )
        self.assertEqual(
            {"name": "test-charm"},
            getUtility(ICharmRecipeSet).getCharmcraftYaml(recipe),
        )
        self.assertEqual(["charm/foo/charmcraft.yaml"], requested)
        self.assertNotIn("charm/foo//charmcraft.yaml", requested)

    def test__default_distribution_default(self):
        # If the CHARM_RECIPE_BUILD_DISTRIBUTION feature rule is not set, we
        # default to Ubuntu.
        recipe = self.factory.makeCharmRecipe()
        self.assertEqual(
            "ubuntu", removeSecurityProxy(recipe)._default_distribution.name
        )

    def test__default_distribution_feature_rule(self):
        # If the CHARM_RECIPE_BUILD_DISTRIBUTION feature rule is set, we
        # default to the distribution with the given name.
        distro_name = "mydistro"
        distribution = self.factory.makeDistribution(name=distro_name)
        recipe = self.factory.makeCharmRecipe()
        with FeatureFixture({CHARM_RECIPE_BUILD_DISTRIBUTION: distro_name}):
            self.assertEqual(
                distribution, removeSecurityProxy(recipe)._default_distribution
            )

    def test__default_distribution_feature_rule_nonexistent(self):
        # If we mistakenly set the rule to a non-existent distribution,
        # things break explicitly.
        recipe = self.factory.makeCharmRecipe()
        with FeatureFixture({CHARM_RECIPE_BUILD_DISTRIBUTION: "nonexistent"}):
            expected_msg = (
                "'nonexistent' is not a valid value for feature rule '%s'"
                % CHARM_RECIPE_BUILD_DISTRIBUTION
            )
            self.assertRaisesWithContent(
                ValueError,
                expected_msg,
                getattr,
                removeSecurityProxy(recipe),
                "_default_distribution",
            )

    def test__default_distro_series_feature_rule(self):
        # If the appropriate per-distribution feature rule is set, we
        # default to the named distro series.
        distro_name = "mydistro"
        distribution = self.factory.makeDistribution(name=distro_name)
        distro_series_name = "myseries"
        distro_series = self.factory.makeDistroSeries(
            distribution=distribution, name=distro_series_name
        )
        self.factory.makeDistroSeries(distribution=distribution)
        recipe = self.factory.makeCharmRecipe()
        with FeatureFixture(
            {
                CHARM_RECIPE_BUILD_DISTRIBUTION: distro_name,
                "charm.default_build_series.%s"
                % distro_name: (distro_series_name),
            }
        ):
            self.assertEqual(
                distro_series,
                removeSecurityProxy(recipe)._default_distro_series,
            )

    def test__default_distro_series_no_feature_rule(self):
        # If the appropriate per-distribution feature rule is not set, we
        # default to the distribution's current series.
        distro_name = "mydistro"
        distribution = self.factory.makeDistribution(name=distro_name)
        self.factory.makeDistroSeries(
            distribution=distribution, status=SeriesStatus.SUPPORTED
        )
        current_series = self.factory.makeDistroSeries(
            distribution=distribution, status=SeriesStatus.DEVELOPMENT
        )
        recipe = self.factory.makeCharmRecipe()
        with FeatureFixture({CHARM_RECIPE_BUILD_DISTRIBUTION: distro_name}):
            self.assertEqual(
                current_series,
                removeSecurityProxy(recipe)._default_distro_series,
            )

    def makeBuildableDistroArchSeries(
        self,
        architecturetag=None,
        processor=None,
        supports_virtualized=True,
        supports_nonvirtualized=True,
        **kwargs,
    ):
        if architecturetag is None:
            architecturetag = self.factory.getUniqueUnicode("arch")
        if processor is None:
            try:
                processor = getUtility(IProcessorSet).getByName(
                    architecturetag
                )
            except ProcessorNotFound:
                processor = self.factory.makeProcessor(
                    name=architecturetag,
                    supports_virtualized=supports_virtualized,
                    supports_nonvirtualized=supports_nonvirtualized,
                )
        das = self.factory.makeDistroArchSeries(
            architecturetag=architecturetag, processor=processor, **kwargs
        )
        # Add both a chroot and a LXD image to test that
        # getAllowedArchitectures doesn't get confused by multiple
        # PocketChroot rows for a single DistroArchSeries.
        fake_chroot = self.factory.makeLibraryFileAlias(
            filename="fake_chroot.tar.gz", db_only=True
        )
        das.addOrUpdateChroot(fake_chroot)
        fake_lxd = self.factory.makeLibraryFileAlias(
            filename="fake_lxd.tar.gz", db_only=True
        )
        das.addOrUpdateChroot(fake_lxd, image_type=BuildBaseImageType.LXD)
        return das

    def test_requestBuild(self):
        # requestBuild creates a new CharmRecipeBuild.
        recipe = self.factory.makeCharmRecipe()
        das = self.makeBuildableDistroArchSeries()
        build_request = self.factory.makeCharmRecipeBuildRequest(recipe=recipe)
        build = recipe.requestBuild(build_request, das)
        self.assertTrue(ICharmRecipeBuild.providedBy(build))
        self.assertThat(
            build,
            MatchesStructure(
                requester=Equals(recipe.owner.teamowner),
                distro_arch_series=Equals(das),
                channels=Is(None),
                status=Equals(BuildStatus.NEEDSBUILD),
            ),
        )
        store = Store.of(build)
        store.flush()
        build_queue = store.find(
            BuildQueue,
            BuildQueue._build_farm_job_id
            == removeSecurityProxy(build).build_farm_job_id,
        ).one()
        self.assertProvides(build_queue, IBuildQueue)
        self.assertEqual(recipe.require_virtualized, build_queue.virtualized)
        self.assertEqual(BuildQueueStatus.WAITING, build_queue.status)

    def test_requestBuild_score(self):
        # Build requests have a relatively low queue score (2510).
        recipe = self.factory.makeCharmRecipe()
        das = self.makeBuildableDistroArchSeries()
        build_request = self.factory.makeCharmRecipeBuildRequest(recipe=recipe)
        build = recipe.requestBuild(build_request, das)
        queue_record = build.buildqueue_record
        queue_record.score()
        self.assertEqual(2510, queue_record.lastscore)

    def test_requestBuild_channels(self):
        # requestBuild can select non-default channels.
        recipe = self.factory.makeCharmRecipe()
        das = self.makeBuildableDistroArchSeries()
        build_request = self.factory.makeCharmRecipeBuildRequest(recipe=recipe)
        build = recipe.requestBuild(
            build_request, das, channels={"charmcraft": "edge"}
        )
        self.assertEqual({"charmcraft": "edge"}, build.channels)

    def test_requestBuild_rejects_repeats(self):
        # requestBuild refuses if there is already a pending build.
        recipe = self.factory.makeCharmRecipe()
        distro_serieses = [self.factory.makeDistroSeries() for _ in range(2)]
        arches = [
            self.makeBuildableDistroArchSeries(distroseries=distro_serieses[0])
            for _ in range(2)
        ]
        arches.append(
            self.makeBuildableDistroArchSeries(
                distroseries=distro_serieses[1],
                architecturetag=arches[0].architecturetag,
                processor=arches[0].processor,
            )
        )
        build_request = self.factory.makeCharmRecipeBuildRequest(recipe=recipe)
        old_build = recipe.requestBuild(build_request, arches[0])
        self.assertRaises(
            CharmRecipeBuildAlreadyPending,
            recipe.requestBuild,
            build_request,
            arches[0],
        )
        # We can build for a different distroarchseries.
        recipe.requestBuild(build_request, arches[1])
        # We can build for a distroarchseries in a different distroseries
        # for the same processor.
        recipe.requestBuild(build_request, arches[2])
        # channels=None and channels={} are treated as equivalent, but
        # anything else allows a new build.
        self.assertRaises(
            CharmRecipeBuildAlreadyPending,
            recipe.requestBuild,
            build_request,
            arches[0],
            channels={},
        )
        recipe.requestBuild(
            build_request, arches[0], channels={"core": "edge"}
        )
        self.assertRaises(
            CharmRecipeBuildAlreadyPending,
            recipe.requestBuild,
            build_request,
            arches[0],
            channels={"core": "edge"},
        )
        # Changing the status of the old build allows a new build.
        old_build.updateStatus(BuildStatus.BUILDING)
        old_build.updateStatus(BuildStatus.FULLYBUILT)
        recipe.requestBuild(build_request, arches[0])

    def test_requestBuild_virtualization(self):
        # New builds are virtualized if any of the processor or recipe
        # require it.
        recipe = self.factory.makeCharmRecipe()
        distro_series = self.factory.makeDistroSeries()
        dases = {}
        for proc_nonvirt in True, False:
            das = self.makeBuildableDistroArchSeries(
                distroseries=distro_series,
                supports_virtualized=True,
                supports_nonvirtualized=proc_nonvirt,
            )
            dases[proc_nonvirt] = das
        for proc_nonvirt, recipe_virt, build_virt in (
            (True, False, False),
            (True, True, True),
            (False, False, True),
            (False, True, True),
        ):
            das = dases[proc_nonvirt]
            recipe = self.factory.makeCharmRecipe(
                require_virtualized=recipe_virt
            )
            build_request = self.factory.makeCharmRecipeBuildRequest(
                recipe=recipe
            )
            build = recipe.requestBuild(build_request, das)
            self.assertEqual(build_virt, build.virtualized)

    def test_requestBuild_fetch_service(self):
        # Activate fetch service for a charm recipe.
        recipe = self.factory.makeCharmRecipe(use_fetch_service=True)
        self.assertEqual(True, recipe.use_fetch_service)
        distro_series = self.factory.makeDistroSeries()
        das = self.makeBuildableDistroArchSeries(
            distroseries=distro_series,
        )
        build_request = self.factory.makeCharmRecipeBuildRequest(recipe=recipe)
        build = recipe.requestBuild(build_request, das)
        self.assertEqual(True, build.recipe.use_fetch_service)
        self.assertEqual(
            FetchServicePolicy.STRICT, recipe.fetch_service_policy
        )

    def test_requestBuild_nonvirtualized(self):
        # A non-virtualized processor can build a charm recipe iff the
        # recipe has require_virtualized set to False.
        recipe = self.factory.makeCharmRecipe()
        distro_series = self.factory.makeDistroSeries()
        das = self.makeBuildableDistroArchSeries(
            distroseries=distro_series,
            supports_virtualized=False,
            supports_nonvirtualized=True,
        )
        build_request = self.factory.makeCharmRecipeBuildRequest(recipe=recipe)
        self.assertRaises(
            CharmRecipeBuildDisallowedArchitecture,
            recipe.requestBuild,
            build_request,
            das,
        )
        with admin_logged_in():
            recipe.require_virtualized = False
        recipe.requestBuild(build_request, das)

    def test_requestBuild_triggers_webhooks(self):
        # Requesting a build triggers webhooks.
        self.useFixture(
            FeatureFixture(
                {
                    CHARM_RECIPE_ALLOW_CREATE: "on",
                    CHARM_RECIPE_WEBHOOKS_FEATURE_FLAG: "on",
                }
            )
        )
        logger = self.useFixture(FakeLogger())
        recipe = self.factory.makeCharmRecipe()
        das = self.makeBuildableDistroArchSeries()
        build_request = self.factory.makeCharmRecipeBuildRequest(recipe=recipe)
        hook = self.factory.makeWebhook(
            target=recipe, event_types=["charm-recipe:build:0.1"]
        )
        build = recipe.requestBuild(build_request, das)
        expected_payload = {
            "recipe_build": Equals(
                canonical_url(build, force_local_path=True)
            ),
            "action": Equals("created"),
            "recipe": Equals(canonical_url(recipe, force_local_path=True)),
            "build_request": Equals(
                canonical_url(build_request, force_local_path=True)
            ),
            "status": Equals("Needs building"),
            "store_upload_status": Equals("Unscheduled"),
        }
        with person_logged_in(recipe.owner):
            delivery = hook.deliveries.one()
            self.assertThat(
                delivery,
                MatchesStructure(
                    event_type=Equals("charm-recipe:build:0.1"),
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
                                "charm-recipe:build:0.1",
                                MatchesDict(expected_payload),
                            )
                        ]
                    ),
                )

    def test_requestBuild_with_platform_name(self):
        recipe = self.factory.makeCharmRecipe()
        das = self.makeBuildableDistroArchSeries()
        build_request = self.factory.makeCharmRecipeBuildRequest(recipe=recipe)
        build = recipe.requestBuild(
            build_request, das, craft_platform="ubuntu-amd64"
        )
        self.assertEqual("ubuntu-amd64", build.craft_platform)

    def test_requestBuilds(self):
        # requestBuilds schedules a job and returns a corresponding
        # CharmRecipeBuildRequest.
        recipe = self.factory.makeCharmRecipe()
        now = get_transaction_timestamp(IStore(recipe))
        with person_logged_in(recipe.owner.teamowner):
            request = recipe.requestBuilds(recipe.owner.teamowner)
        self.assertThat(
            request,
            MatchesStructure(
                date_requested=Equals(now),
                date_finished=Is(None),
                recipe=Equals(recipe),
                status=Equals(CharmRecipeBuildRequestStatus.PENDING),
                error_message=Is(None),
                channels=Is(None),
                architectures=Is(None),
            ),
        )
        [job] = getUtility(ICharmRecipeRequestBuildsJobSource).iterReady()
        self.assertThat(
            job,
            MatchesStructure(
                job_id=Equals(request.id),
                job=MatchesStructure.byEquality(status=JobStatus.WAITING),
                recipe=Equals(recipe),
                requester=Equals(recipe.owner.teamowner),
                channels=Is(None),
                architectures=Is(None),
            ),
        )

    def test_requestBuilds_with_channels(self):
        # If asked to build using particular snap channels, requestBuilds
        # passes those through to the job.
        recipe = self.factory.makeCharmRecipe()
        now = get_transaction_timestamp(IStore(recipe))
        with person_logged_in(recipe.owner.teamowner):
            request = recipe.requestBuilds(
                recipe.owner.teamowner, channels={"charmcraft": "edge"}
            )
        self.assertThat(
            request,
            MatchesStructure(
                date_requested=Equals(now),
                date_finished=Is(None),
                recipe=Equals(recipe),
                status=Equals(CharmRecipeBuildRequestStatus.PENDING),
                error_message=Is(None),
                channels=MatchesDict({"charmcraft": Equals("edge")}),
                architectures=Is(None),
            ),
        )
        [job] = getUtility(ICharmRecipeRequestBuildsJobSource).iterReady()
        self.assertThat(
            job,
            MatchesStructure(
                job_id=Equals(request.id),
                job=MatchesStructure.byEquality(status=JobStatus.WAITING),
                recipe=Equals(recipe),
                requester=Equals(recipe.owner.teamowner),
                channels=Equals({"charmcraft": "edge"}),
                architectures=Is(None),
            ),
        )

    def test_requestBuilds_with_architectures(self):
        # If asked to build for particular architectures, requestBuilds
        # passes those through to the job.
        recipe = self.factory.makeCharmRecipe()
        now = get_transaction_timestamp(IStore(recipe))
        with person_logged_in(recipe.owner.teamowner):
            request = recipe.requestBuilds(
                recipe.owner.teamowner, architectures={"amd64", "i386"}
            )
        self.assertThat(
            request,
            MatchesStructure(
                date_requested=Equals(now),
                date_finished=Is(None),
                recipe=Equals(recipe),
                status=Equals(CharmRecipeBuildRequestStatus.PENDING),
                error_message=Is(None),
                channels=Is(None),
                architectures=MatchesSetwise(Equals("amd64"), Equals("i386")),
            ),
        )
        [job] = getUtility(ICharmRecipeRequestBuildsJobSource).iterReady()
        self.assertThat(
            job,
            MatchesStructure(
                job_id=Equals(request.id),
                job=MatchesStructure.byEquality(status=JobStatus.WAITING),
                recipe=Equals(recipe),
                requester=Equals(recipe.owner.teamowner),
                channels=Is(None),
                architectures=MatchesSetwise(Equals("amd64"), Equals("i386")),
            ),
        )

    def test_requestBuilds_with_processor_objects(self):
        # 'architectures' may be passed as Processor objects or strings
        # to 'requestBuilds'. Processor instances must be converted
        # to its tag strings before being stored in the job metadata.
        recipe = self.factory.makeCharmRecipe()
        amd64 = getUtility(IProcessorSet).getByName("amd64")

        with person_logged_in(recipe.owner.teamowner):
            request = recipe.requestBuilds(
                recipe.owner.teamowner, architectures={amd64}
            )
        self.assertThat(
            request.architectures,
            MatchesSetwise(Equals("amd64")),
        )
        [job] = getUtility(ICharmRecipeRequestBuildsJobSource).iterReady()
        self.assertThat(
            job.architectures,
            MatchesSetwise(Equals("amd64")),
        )

    def makeRequestBuildsJob(
        self, distro_series_version, arch_tags, git_ref=None
    ):
        recipe = self.factory.makeCharmRecipe(git_ref=git_ref)
        distro_series = self.factory.makeDistroSeries(
            distribution=getUtility(ILaunchpadCelebrities).ubuntu,
            version=distro_series_version,
        )
        for arch_tag in arch_tags:
            self.makeBuildableDistroArchSeries(
                distroseries=distro_series, architecturetag=arch_tag
            )
        return getUtility(ICharmRecipeRequestBuildsJobSource).create(
            recipe, recipe.owner.teamowner, {"charmcraft": "edge"}
        )

    def assertRequestedBuildsMatch(
        self, builds, job, distro_series_version, arch_tags, channels
    ):
        self.assertThat(
            builds,
            MatchesSetwise(
                *(
                    MatchesStructure(
                        requester=Equals(job.requester),
                        recipe=Equals(job.recipe),
                        distro_arch_series=MatchesStructure(
                            distroseries=MatchesStructure.byEquality(
                                version=distro_series_version
                            ),
                            architecturetag=Equals(arch_tag),
                        ),
                        channels=Equals(channels),
                    )
                    for arch_tag in arch_tags
                )
            ),
        )

    def test_requestBuildsFromJob_restricts_explicit_list(self):
        # requestBuildsFromJob limits builds targeted at an explicit list of
        # architectures to those allowed for the recipe.
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
                    bases:
                      - build-on:
                          - name: ubuntu
                            channel: "20.04"
                            architectures: [sparc]
                      - build-on:
                          - name: ubuntu
                            channel: "20.04"
                            architectures: [i386]
                      - build-on:
                          - name: ubuntu
                            channel: "20.04"
                            architectures: [avr]
                    """
                )
            )
        )
        job = self.makeRequestBuildsJob("20.04", ["sparc", "avr", "mips64el"])
        self.assertEqual(
            get_transaction_timestamp(IStore(job.recipe)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request, channels=removeSecurityProxy(job.channels)
            )
        self.assertRequestedBuildsMatch(
            builds, job, "20.04", ["sparc", "avr"], job.channels
        )

    def test_requestBuildsFromJob_no_explicit_bases(self):
        # If the recipe doesn't specify any bases, requestBuildsFromJob
        # requests builds for all configured architectures for the default
        # series.
        self.useFixture(
            FeatureFixture(
                {
                    CHARM_RECIPE_ALLOW_CREATE: "on",
                    CHARM_RECIPE_BUILD_DISTRIBUTION: "ubuntu",
                    "charm.default_build_series.ubuntu": "20.04",
                }
            )
        )
        self.useFixture(GitHostingFixture(blob="name: foo\n"))
        old_distro_series = self.factory.makeDistroSeries(
            distribution=getUtility(ILaunchpadCelebrities).ubuntu,
            version="18.04",
        )
        for arch_tag in ("mips64el", "riscv64"):
            self.makeBuildableDistroArchSeries(
                distroseries=old_distro_series, architecturetag=arch_tag
            )
        job = self.makeRequestBuildsJob("20.04", ["mips64el", "riscv64"])
        self.assertEqual(
            get_transaction_timestamp(IStore(job.recipe)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request, channels=removeSecurityProxy(job.channels)
            )
        self.assertRequestedBuildsMatch(
            builds, job, "20.04", ["mips64el", "riscv64"], job.channels
        )

    def test_requestBuildsFromJob_unified_charmcraft_yaml_invalid_short_base(
        self,
    ):
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
                    base: ubuntu-24.04
                    platforms:
                        ubuntu-amd64:
                            build-on: [amd64]
                            build-for: [amd64]
                    """
                )
            )
        )
        job = self.makeRequestBuildsJob("24.04", ["amd64", "riscv64", "arm64"])

        self.assertEqual(
            get_transaction_timestamp(IStore(job.recipe)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            with ExpectedException(
                CraftPlatformsBuildPlanError,
                r"Failed to compute the build plan for base=ubuntu-24\.04, "
                r"build base=None, platforms=\{'ubuntu-amd64': \{'build-on': "
                r"\['amd64'\], 'build-for': \['amd64'\]\}\}: Invalid base "
                r"string 'ubuntu-24\.04'\. Format should be "
                r"'<distribution>@<series>'",
            ):
                job.recipe.requestBuildsFromJob(
                    job.build_request,
                    channels=removeSecurityProxy(job.channels),
                )

    def test_requestBuildsFromJob_unified_charmcraft_yaml_platforms_missing(
        self,
    ):
        # If the recipe has no platforms specified, craft-platforms builds
        # for all default architectures without an option of cross-compiling.
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
                    base: ubuntu@24.04
                    """
                )
            )
        )
        job = self.makeRequestBuildsJob("24.04", ["amd64", "riscv64", "arm64"])

        self.assertEqual(
            get_transaction_timestamp(IStore(job.recipe)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request,
                channels=removeSecurityProxy(job.channels),
            )
        self.assertRequestedBuildsMatch(
            builds, job, "24.04", ["amd64", "riscv64", "arm64"], job.channels
        )

    def test_requestBuildsFromJob_unified_charmcraft_yaml_fully_expanded(self):
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
                    base: ubuntu@24.04
                    platforms:
                        ubuntu-amd64:
                            build-on: [amd64]
                            build-for: [amd64]
                    """
                )
            )
        )
        job = self.makeRequestBuildsJob("24.04", ["amd64", "riscv64", "arm64"])
        self.assertEqual(
            get_transaction_timestamp(IStore(job.recipe)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request, channels=removeSecurityProxy(job.channels)
            )
        self.assertRequestedBuildsMatch(
            builds, job, "24.04", ["amd64"], job.channels
        )

    def test_requestBuildsFromJob_unified_charmcraft_yaml_multi_platforms(
        self,
    ):
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
                    base: ubuntu@24.04
                    platforms:
                        ubuntu-amd64:
                            build-on: [amd64]
                            build-for: [amd64]
                        ubuntu-arm64:
                            build-on: [arm64]
                            build-for: [arm64]
                    """
                )
            )
        )
        job = self.makeRequestBuildsJob("24.04", ["amd64", "riscv64", "arm64"])
        self.assertEqual(
            get_transaction_timestamp(IStore(job.recipe)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request, channels=removeSecurityProxy(job.channels)
            )
        self.assertRequestedBuildsMatch(
            builds, job, "24.04", ["amd64", "arm64"], job.channels
        )

    def test_requestBuildsFromJob_unified_charmcraft_yaml_arch_as_str(self):
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
                    base: ubuntu@24.04
                    platforms:
                        ubuntu-amd64:
                            build-on: amd64
                            build-for: amd64
                    """
                )
            )
        )
        job = self.makeRequestBuildsJob("24.04", ["amd64", "riscv64", "arm64"])
        self.assertEqual(
            get_transaction_timestamp(IStore(job.recipe)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request, channels=removeSecurityProxy(job.channels)
            )
        self.assertRequestedBuildsMatch(
            builds, job, "24.04", ["amd64"], job.channels
        )

    def test_requestBuildsFromJob_unified_charmcraft_yaml_base_short_form(
        self,
    ):
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
                    base: ubuntu@24.04
                    platforms:
                        ubuntu-amd64:
                            build-on: [amd64]
                            build-for: [amd64]
                    """
                )
            )
        )
        job = self.makeRequestBuildsJob("24.04", ["amd64", "riscv64", "arm64"])
        self.assertEqual(
            get_transaction_timestamp(IStore(job.recipe)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request, channels=removeSecurityProxy(job.channels)
            )
        self.assertRequestedBuildsMatch(
            builds, job, "24.04", ["amd64"], job.channels
        )

    def test_requestBuildsFromJob_unified_charmcraft_yaml_unknown_arch(self):
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
                    base: ubuntu@24.04
                    platforms:
                        foobar:
                    """
                )
            )
        )
        job = self.makeRequestBuildsJob("24.04", ["amd64", "riscv64", "arm64"])
        self.assertEqual(
            get_transaction_timestamp(IStore(job.recipe)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            with ExpectedException(
                CraftPlatformsBuildPlanError,
                "Failed to compute the build plan for base=ubuntu@24.04, "
                "build base=None, platforms={'foobar': None}: Platform name "
                "'foobar' is not a valid Debian architecture. Specify a "
                "build-on and build-for.",
            ):
                job.recipe.requestBuildsFromJob(
                    job.build_request,
                    channels=removeSecurityProxy(job.channels),
                )

    def test_requestBuildsFromJob_unified_charmcraft_yaml_platforms_short_form(
        self,
    ):
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
                    base: ubuntu@24.04
                    platforms:
                        amd64:
                    """
                )
            )
        )
        job = self.makeRequestBuildsJob("24.04", ["amd64", "riscv64", "arm64"])
        self.assertEqual(
            get_transaction_timestamp(IStore(job.recipe)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request, channels=removeSecurityProxy(job.channels)
            )
        self.assertRequestedBuildsMatch(
            builds, job, "24.04", ["amd64"], job.channels
        )

    def test_requestBuildsFromJob_unified_charmcraft_yaml_2_platforms_short(
        self,
    ):
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
                    base: ubuntu@24.04
                    platforms:
                        amd64:
                        arm64:
                    """
                )
            )
        )
        job = self.makeRequestBuildsJob("24.04", ["amd64", "riscv64", "arm64"])
        self.assertEqual(
            get_transaction_timestamp(IStore(job.recipe)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request, channels=removeSecurityProxy(job.channels)
            )
        self.assertRequestedBuildsMatch(
            builds, job, "24.04", ["amd64", "arm64"], job.channels
        )

    def test_requestBuildsFromJob_no_charmcraft_yaml(self):
        # If the recipe has no charmcraft.yaml file, requestBuildsFromJob
        # treats this as equivalent to a charmcraft.yaml file that doesn't
        # specify any bases: that is, it requests builds for all configured
        # architectures for the default series.
        self.useFixture(
            FeatureFixture(
                {
                    CHARM_RECIPE_ALLOW_CREATE: "on",
                    CHARM_RECIPE_BUILD_DISTRIBUTION: "ubuntu",
                    "charm.default_build_series.ubuntu": "20.04",
                }
            )
        )
        self.useFixture(GitHostingFixture()).getBlob.failure = (
            GitRepositoryBlobNotFound("placeholder", "charmcraft.yaml")
        )
        old_distro_series = self.factory.makeDistroSeries(
            distribution=getUtility(ILaunchpadCelebrities).ubuntu,
            version="18.04",
        )
        for arch_tag in ("mips64el", "riscv64"):
            self.makeBuildableDistroArchSeries(
                distroseries=old_distro_series, architecturetag=arch_tag
            )
        job = self.makeRequestBuildsJob("20.04", ["mips64el", "riscv64"])
        self.assertEqual(
            get_transaction_timestamp(IStore(job.recipe)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request, channels=removeSecurityProxy(job.channels)
            )
        self.assertRequestedBuildsMatch(
            builds, job, "20.04", ["mips64el", "riscv64"], job.channels
        )

    def test_requestBuildsFromJob_architectures_parameter(self):
        # If an explicit set of architectures was given as a parameter,
        # requestBuildsFromJob intersects those with any other constraints
        # when requesting builds.
        self.useFixture(GitHostingFixture(blob="name: foo\n"))
        job = self.makeRequestBuildsJob(
            "20.04", ["avr", "mips64el", "riscv64"]
        )
        self.assertEqual(
            get_transaction_timestamp(IStore(job.recipe)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request,
                channels=removeSecurityProxy(job.channels),
                architectures={"avr", "riscv64"},
            )
        self.assertRequestedBuildsMatch(
            builds, job, "20.04", ["avr", "riscv64"], job.channels
        )

    def test_requestBuildsFromJob_charm_base_architectures(self):
        # requestBuildsFromJob intersects the architectures supported by the
        # charm base with any other constraints.
        self.useFixture(GitHostingFixture(blob="name: foo\n"))
        job = self.makeRequestBuildsJob("20.04", ["sparc", "i386", "avr"])
        distroseries = getUtility(ILaunchpadCelebrities).ubuntu.getSeries(
            "20.04"
        )
        with admin_logged_in():
            self.factory.makeCharmBase(
                distro_series=distroseries,
                build_snap_channels={"charmcraft": "stable/launchpad-buildd"},
                processors=[
                    distroseries[arch_tag].processor
                    for arch_tag in ("sparc", "avr")
                ],
            )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request, channels=removeSecurityProxy(job.channels)
            )
        self.assertRequestedBuildsMatch(
            builds, job, "20.04", ["sparc", "avr"], job.channels
        )

    def test_requestBuildsFromJob_charm_base_build_channels_by_arch(self):
        # If the charm base declares different build channels for specific
        # architectures, then requestBuildsFromJob uses those when
        # requesting builds for those architectures.
        self.useFixture(GitHostingFixture(blob="name: foo\n"))
        job = self.makeRequestBuildsJob("20.04", ["avr", "riscv64"])
        distroseries = getUtility(ILaunchpadCelebrities).ubuntu.getSeries(
            "20.04"
        )
        with admin_logged_in():
            self.factory.makeCharmBase(
                distro_series=distroseries,
                build_snap_channels={
                    "core20": "stable",
                    "_byarch": {"riscv64": {"core20": "candidate"}},
                },
            )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request, channels=removeSecurityProxy(job.channels)
            )
        self.assertThat(
            builds,
            MatchesSetwise(
                *(
                    MatchesStructure(
                        requester=Equals(job.requester),
                        recipe=Equals(job.recipe),
                        distro_arch_series=MatchesStructure(
                            distroseries=MatchesStructure.byEquality(
                                version="20.04"
                            ),
                            architecturetag=Equals(arch_tag),
                        ),
                        channels=Equals(channels),
                    )
                    for arch_tag, channels in (
                        ("avr", {"charmcraft": "edge", "core20": "stable"}),
                        (
                            "riscv64",
                            {"charmcraft": "edge", "core20": "candidate"},
                        ),
                    )
                )
            ),
        )

    def test_requestBuildsFromJob_triggers_webhooks(self):
        # requestBuildsFromJob triggers webhooks, and the payload includes a
        # link to the build request.
        self.useFixture(
            FeatureFixture(
                {
                    CHARM_RECIPE_ALLOW_CREATE: "on",
                    CHARM_RECIPE_BUILD_DISTRIBUTION: "ubuntu",
                    "charm.default_build_series.ubuntu": "20.04",
                    CHARM_RECIPE_WEBHOOKS_FEATURE_FLAG: "on",
                }
            )
        )
        self.useFixture(GitHostingFixture(blob="name: foo\n"))
        logger = self.useFixture(FakeLogger())
        job = self.makeRequestBuildsJob("20.04", ["mips64el", "riscv64"])
        hook = self.factory.makeWebhook(
            target=job.recipe, event_types=["charm-recipe:build:0.1"]
        )
        with person_logged_in(job.requester):
            builds = job.recipe.requestBuildsFromJob(
                job.build_request, channels=removeSecurityProxy(job.channels)
            )
            self.assertEqual(2, len(builds))
            payload_matchers = [
                MatchesDict(
                    {
                        "recipe_build": Equals(
                            canonical_url(build, force_local_path=True)
                        ),
                        "action": Equals("created"),
                        "recipe": Equals(
                            canonical_url(job.recipe, force_local_path=True)
                        ),
                        "build_request": Equals(
                            canonical_url(
                                job.build_request, force_local_path=True
                            )
                        ),
                        "status": Equals("Needs building"),
                        "store_upload_status": Equals("Unscheduled"),
                    }
                )
                for build in builds
            ]
            self.assertThat(
                hook.deliveries,
                MatchesSetwise(
                    *(
                        MatchesStructure(
                            event_type=Equals("charm-recipe:build:0.1"),
                            payload=payload_matcher,
                        )
                        for payload_matcher in payload_matchers
                    )
                ),
            )
            self.assertThat(
                logger.output,
                LogsScheduledWebhooks(
                    [
                        (hook, "charm-recipe:build:0.1", payload_matcher)
                        for payload_matcher in payload_matchers
                    ]
                ),
            )

    def test_requestAutoBuilds(self):
        # requestAutoBuilds creates a new build request with appropriate
        # parameters.
        recipe = self.factory.makeCharmRecipe()
        now = get_transaction_timestamp(IStore(recipe))
        with person_logged_in(recipe.owner.teamowner):
            request = recipe.requestAutoBuilds()
        self.assertThat(
            request,
            MatchesStructure(
                date_requested=Equals(now),
                date_finished=Is(None),
                recipe=Equals(recipe),
                status=Equals(CharmRecipeBuildRequestStatus.PENDING),
                error_message=Is(None),
                channels=Is(None),
                architectures=Is(None),
            ),
        )
        [job] = getUtility(ICharmRecipeRequestBuildsJobSource).iterReady()
        self.assertThat(
            job,
            MatchesStructure(
                job_id=Equals(request.id),
                job=MatchesStructure.byEquality(status=JobStatus.WAITING),
                recipe=Equals(recipe),
                requester=Equals(recipe.owner),
                channels=Is(None),
                architectures=Is(None),
            ),
        )

    def test_requestAutoBuilds_channels(self):
        # requestAutoBuilds honours CharmRecipe.auto_build_channels.
        recipe = self.factory.makeCharmRecipe(
            auto_build_channels={"charmcraft": "edge"}
        )
        now = get_transaction_timestamp(IStore(recipe))
        with person_logged_in(recipe.owner.teamowner):
            request = recipe.requestAutoBuilds()
        self.assertThat(
            request,
            MatchesStructure(
                date_requested=Equals(now),
                date_finished=Is(None),
                recipe=Equals(recipe),
                status=Equals(CharmRecipeBuildRequestStatus.PENDING),
                error_message=Is(None),
                channels=Equals({"charmcraft": "edge"}),
                architectures=Is(None),
            ),
        )
        [job] = getUtility(ICharmRecipeRequestBuildsJobSource).iterReady()
        self.assertThat(
            job,
            MatchesStructure(
                job_id=Equals(request.id),
                job=MatchesStructure.byEquality(status=JobStatus.WAITING),
                recipe=Equals(recipe),
                requester=Equals(recipe.owner),
                channels=Equals({"charmcraft": "edge"}),
                architectures=Is(None),
            ),
        )

    def test_delete_without_builds(self):
        # A charm recipe with no builds can be deleted.
        owner = self.factory.makePerson()
        project = self.factory.makeProduct()
        recipe = self.factory.makeCharmRecipe(
            registrant=owner, owner=owner, project=project, name="condemned"
        )
        self.assertTrue(
            getUtility(ICharmRecipeSet).exists(owner, project, "condemned")
        )
        with person_logged_in(recipe.owner):
            recipe.destroySelf()
        self.assertFalse(
            getUtility(ICharmRecipeSet).exists(owner, project, "condemned")
        )

    def test_related_webhooks_deleted(self):
        owner = self.factory.makePerson()
        recipe = self.factory.makeCharmRecipe(registrant=owner, owner=owner)
        webhook = self.factory.makeWebhook(target=recipe)
        with person_logged_in(recipe.owner):
            webhook.ping()
            recipe.destroySelf()
            transaction.commit()
            self.assertRaises(LostObjectError, getattr, webhook, "target")


class TestCharmRecipeAuthorization(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))
        self.pushConfig("charms", charmhub_url="http://charmhub.example/")
        self.pushConfig(
            "launchpad", candid_service_root="https://candid.test/"
        )

    @responses.activate
    def assertBeginsAuthorization(self, recipe, **kwargs):
        root_macaroon = Macaroon(version=2)
        root_macaroon.add_third_party_caveat(
            "https://candid.test/", "", "identity"
        )
        root_macaroon_raw = root_macaroon.serialize(JsonSerializer())
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens",
            json={"macaroon": root_macaroon_raw},
        )
        self.assertEqual(root_macaroon_raw, recipe.beginAuthorization())
        tokens_matcher = MatchesStructure(
            url=Equals("http://charmhub.example/v1/tokens"),
            method=Equals("POST"),
            body=AfterPreprocessing(
                json.loads,
                Equals(
                    {
                        "description": (
                            f"{recipe.store_name} for launchpad.test"
                        ),
                        "packages": [
                            {
                                "type": "charm",
                                "name": recipe.store_name,
                            },
                        ],
                        "permissions": [
                            "package-manage-releases",
                            "package-manage-revisions",
                            "package-view-revisions",
                        ],
                    }
                ),
            ),
        )
        self.assertThat(
            responses.calls,
            MatchesListwise([MatchesStructure(request=tokens_matcher)]),
        )
        self.assertEqual(
            {"root": root_macaroon_raw, "auth_method": "candid"},
            recipe.store_secrets,
        )

    def test_beginAuthorization(self):
        recipe = self.factory.makeCharmRecipe(
            store_upload=True, store_name=self.factory.getUniqueUnicode()
        )
        with person_logged_in(recipe.registrant):
            self.assertBeginsAuthorization(recipe)

    def test_beginAuthorization_unauthorized(self):
        # A user without edit access cannot authorize charm recipe uploads.
        recipe = self.factory.makeCharmRecipe(
            store_upload=True, store_name=self.factory.getUniqueUnicode()
        )
        with person_logged_in(self.factory.makePerson()):
            self.assertRaises(
                Unauthorized, getattr, recipe, "beginAuthorization"
            )

    @responses.activate
    def test_beginAuthorization_usso(self):
        # With the Ubuntu SSO feature flag on, beginAuthorization uses the
        # new endpoint and returns the Ubuntu SSO caveat identifier.
        self.useFixture(
            FeatureFixture(
                {
                    CHARM_RECIPE_ALLOW_CREATE: "on",
                    CHARM_RECIPE_USSO_FEATURE_FLAG: "on",
                }
            )
        )
        recipe = self.factory.makeCharmRecipe(
            store_upload=True, store_name=self.factory.getUniqueUnicode()
        )
        root_macaroon = Macaroon(version=2)
        root_macaroon.add_third_party_caveat(
            urlsplit(config.launchpad.openid_provider_root).netloc,
            "",
            "dummy",
        )
        root_macaroon_raw = root_macaroon.serialize()
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/usso",
            json={"macaroon": root_macaroon_raw},
        )
        with person_logged_in(recipe.registrant):
            sso_caveat_id = recipe.beginAuthorization()
        tokens_matcher = MatchesStructure(
            url=Equals("http://charmhub.example/v1/tokens/usso"),
            method=Equals("POST"),
            body=AfterPreprocessing(
                json.loads,
                Equals(
                    {
                        "ttl": 31536000,
                        "packages": [
                            {"type": "charm", "name": recipe.store_name},
                        ],
                        "permissions": [
                            "package-manage-releases",
                            "package-manage-revisions",
                            "package-view-revisions",
                        ],
                    }
                ),
            ),
        )
        self.assertThat(
            responses.calls,
            MatchesListwise([MatchesStructure(request=tokens_matcher)]),
        )
        self.assertEqual(
            root_macaroon.third_party_caveats()[0].caveat_id, sso_caveat_id
        )
        self.assertEqual(
            {"root": root_macaroon_raw, "auth_method": "usso"},
            recipe.store_secrets,
        )

    @responses.activate
    def test_beginAuthorization_usso_no_sso_caveat(self):
        # A macaroon with no Ubuntu SSO caveat is rejected.
        self.useFixture(
            FeatureFixture(
                {
                    CHARM_RECIPE_ALLOW_CREATE: "on",
                    CHARM_RECIPE_USSO_FEATURE_FLAG: "on",
                }
            )
        )
        recipe = self.factory.makeCharmRecipe(
            store_upload=True, store_name=self.factory.getUniqueUnicode()
        )
        root_macaroon_raw = Macaroon(version=2).serialize()
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/usso",
            json={"macaroon": root_macaroon_raw},
        )
        with person_logged_in(recipe.registrant):
            self.assertRaisesWithContent(
                CannotAuthorizeCharmhubUploads,
                "Macaroon has no SSO caveats.",
                recipe.beginAuthorization,
            )

    @responses.activate
    def test_beginAuthorization_usso_multiple_sso_caveats(self):
        # A macaroon with more than one Ubuntu SSO caveat is rejected.
        self.useFixture(
            FeatureFixture(
                {
                    CHARM_RECIPE_ALLOW_CREATE: "on",
                    CHARM_RECIPE_USSO_FEATURE_FLAG: "on",
                }
            )
        )
        recipe = self.factory.makeCharmRecipe(
            store_upload=True, store_name=self.factory.getUniqueUnicode()
        )
        root_macaroon = Macaroon(version=2)
        sso_location = urlsplit(config.launchpad.openid_provider_root).netloc
        root_macaroon.add_third_party_caveat(sso_location, "", "dummy")
        root_macaroon.add_third_party_caveat(sso_location, "", "dummy2")
        root_macaroon_raw = root_macaroon.serialize()
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/usso",
            json={"macaroon": root_macaroon_raw},
        )
        with person_logged_in(recipe.registrant):
            self.assertRaisesWithContent(
                CannotAuthorizeCharmhubUploads,
                "Macaroon has multiple SSO caveats.",
                recipe.beginAuthorization,
            )

    @responses.activate
    def test_beginAuthorization_usso_team_scope_member(self):
        # A team scoped Ubuntu SSO rule drives a team member down the
        # Ubuntu SSO path.
        team = self.factory.makeTeam()
        member = self.factory.makePerson(member_of=[team])
        self.useFixture(
            FeatureFixture(
                {CHARM_RECIPE_ALLOW_CREATE: "on"},
                full_feature_rules=[
                    dict(
                        flag=CHARM_RECIPE_USSO_FEATURE_FLAG,
                        scope="team:%s" % team.name,
                        priority=999,
                        value="on",
                    ),
                ],
            )
        )
        root_macaroon = Macaroon(version=2)
        root_macaroon.add_third_party_caveat(
            urlsplit(config.launchpad.openid_provider_root).netloc,
            "",
            "dummy",
        )
        root_macaroon_raw = root_macaroon.serialize()
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/usso",
            json={"macaroon": root_macaroon_raw},
        )
        with person_logged_in(member):
            recipe = self.factory.makeCharmRecipe(
                registrant=member,
                owner=member,
                store_upload=True,
                store_name=self.factory.getUniqueUnicode(),
            )
            sso_caveat_id = recipe.beginAuthorization()
        self.assertEqual(
            "http://charmhub.example/v1/tokens/usso",
            responses.calls[-1].request.url,
        )
        self.assertEqual(
            root_macaroon.third_party_caveats()[0].caveat_id, sso_caveat_id
        )
        self.assertEqual("usso", recipe.store_secrets["auth_method"])

    @responses.activate
    def test_beginAuthorization_candid_team_scope_non_member(self):
        # A team scoped Ubuntu SSO rule leaves a person outside the team on
        # the Candid path.
        team = self.factory.makeTeam()
        non_member = self.factory.makePerson()
        self.useFixture(
            FeatureFixture(
                {CHARM_RECIPE_ALLOW_CREATE: "on"},
                full_feature_rules=[
                    dict(
                        flag=CHARM_RECIPE_USSO_FEATURE_FLAG,
                        scope="team:%s" % team.name,
                        priority=999,
                        value="on",
                    ),
                ],
            )
        )
        root_macaroon = Macaroon(version=2)
        root_macaroon.add_third_party_caveat(
            "https://candid.test/", "", "identity"
        )
        root_macaroon_raw = root_macaroon.serialize(JsonSerializer())
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens",
            json={"macaroon": root_macaroon_raw},
        )
        with person_logged_in(non_member):
            recipe = self.factory.makeCharmRecipe(
                registrant=non_member,
                owner=non_member,
                store_upload=True,
                store_name=self.factory.getUniqueUnicode(),
            )
            recipe.beginAuthorization()
        self.assertEqual(
            "http://charmhub.example/v1/tokens",
            responses.calls[-1].request.url,
        )
        self.assertEqual("candid", recipe.store_secrets["auth_method"])

    @responses.activate
    def test_completeAuthorization(self):
        private_key = PrivateKey.generate()
        self.pushConfig(
            "charms",
            charmhub_secrets_public_key=base64.b64encode(
                bytes(private_key.public_key)
            ).decode(),
        )
        root_macaroon = Macaroon(version=2)
        root_macaroon_raw = root_macaroon.serialize(JsonSerializer())
        unbound_discharge_macaroon = Macaroon(version=2)
        unbound_discharge_macaroon_raw = unbound_discharge_macaroon.serialize(
            JsonSerializer()
        )
        discharge_macaroon_raw = root_macaroon.prepare_for_request(
            unbound_discharge_macaroon
        ).serialize(JsonSerializer())
        exchanged_macaroon = Macaroon(version=2)
        exchanged_macaroon_raw = exchanged_macaroon.serialize(JsonSerializer())
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/exchange",
            json={"macaroon": exchanged_macaroon_raw},
        )
        # completeAuthorization falls back to the Candid exchange endpoint if
        # the authorization has no auth_method in store_secrets, maintaining
        # backwards-compatibility.
        recipe = self.factory.makeCharmRecipe(
            store_upload=True,
            store_name=self.factory.getUniqueUnicode(),
            store_secrets={"root": root_macaroon_raw},
        )
        with person_logged_in(recipe.registrant):
            recipe.completeAuthorization(unbound_discharge_macaroon_raw)
            self.pushConfig(
                "charms",
                charmhub_secrets_private_key=base64.b64encode(
                    bytes(private_key)
                ).decode(),
            )
            container = getUtility(IEncryptedContainer, "charmhub-secrets")
            self.assertThat(
                recipe.store_secrets,
                MatchesDict(
                    {
                        "exchanged_encrypted": AfterPreprocessing(
                            lambda data: container.decrypt(data).decode(),
                            Equals(exchanged_macaroon_raw),
                        ),
                    }
                ),
            )
        exchange_matcher = MatchesStructure(
            url=Equals("http://charmhub.example/v1/tokens/exchange"),
            method=Equals("POST"),
            headers=ContainsDict(
                {
                    "Macaroons": AfterPreprocessing(
                        lambda v: json.loads(
                            base64.b64decode(v.encode()).decode()
                        ),
                        Equals(
                            [
                                json.loads(m)
                                for m in (
                                    root_macaroon_raw,
                                    discharge_macaroon_raw,
                                )
                            ]
                        ),
                    ),
                }
            ),
            body=AfterPreprocessing(json.loads, Equals({})),
        )
        self.assertThat(
            responses.calls,
            MatchesListwise([MatchesStructure(request=exchange_matcher)]),
        )

    def test_completeAuthorization_without_beginAuthorization(self):
        recipe = self.factory.makeCharmRecipe(
            store_upload=True, store_name=self.factory.getUniqueUnicode()
        )
        discharge_macaroon = Macaroon(version=2)
        with person_logged_in(recipe.registrant):
            self.assertRaisesWithContent(
                CannotAuthorizeCharmhubUploads,
                "beginAuthorization must be called before "
                "completeAuthorization.",
                recipe.completeAuthorization,
                discharge_macaroon.serialize(JsonSerializer()),
            )

    def test_completeAuthorization_unauthorized(self):
        root_macaroon = Macaroon(version=2)
        recipe = self.factory.makeCharmRecipe(
            store_upload=True,
            store_name=self.factory.getUniqueUnicode(),
            store_secrets={"root": root_macaroon.serialize(JsonSerializer())},
        )
        with person_logged_in(self.factory.makePerson()):
            self.assertRaises(
                Unauthorized, getattr, recipe, "completeAuthorization"
            )

    def test_completeAuthorization_malformed_discharge_macaroon(self):
        root_macaroon = Macaroon(version=2)
        recipe = self.factory.makeCharmRecipe(
            store_upload=True,
            store_name=self.factory.getUniqueUnicode(),
            store_secrets={"root": root_macaroon.serialize(JsonSerializer())},
        )
        with person_logged_in(recipe.registrant):
            self.assertRaisesWithContent(
                CannotAuthorizeCharmhubUploads,
                "Discharge macaroon is invalid.",
                recipe.completeAuthorization,
                "nonsense",
            )

    @responses.activate
    def test_completeAuthorization_usso(self):
        # An authorization recorded as "usso" is completed against the new
        # Ubuntu SSO exchange endpoint and stores the resulting token.
        private_key = PrivateKey.generate()
        self.pushConfig(
            "charms",
            charmhub_secrets_public_key=base64.b64encode(
                bytes(private_key.public_key)
            ).decode(),
        )
        root_macaroon = Macaroon(version=2)
        root_macaroon_raw = root_macaroon.serialize()
        unbound_discharge_macaroon = Macaroon(version=2)
        unbound_discharge_macaroon_raw = unbound_discharge_macaroon.serialize()
        discharge_macaroon_raw = root_macaroon.prepare_for_request(
            unbound_discharge_macaroon
        ).serialize()
        exchanged_macaroon = Macaroon(version=2)
        exchanged_macaroon_raw = exchanged_macaroon.serialize(JsonSerializer())
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/usso/exchange",
            json={"macaroon": exchanged_macaroon_raw},
        )
        store_name = self.factory.getUniqueUnicode()
        recipe = self.factory.makeCharmRecipe(
            store_upload=True,
            store_name=store_name,
            store_secrets={"root": root_macaroon_raw, "auth_method": "usso"},
        )
        with person_logged_in(recipe.registrant):
            recipe.completeAuthorization(unbound_discharge_macaroon_raw)
            self.pushConfig(
                "charms",
                charmhub_secrets_private_key=base64.b64encode(
                    bytes(private_key)
                ).decode(),
            )
            container = getUtility(IEncryptedContainer, "charmhub-secrets")
            self.assertThat(
                recipe.store_secrets,
                MatchesDict(
                    {
                        "exchanged_encrypted": AfterPreprocessing(
                            lambda data: container.decrypt(data).decode(),
                            Equals(exchanged_macaroon_raw),
                        ),
                    }
                ),
            )
        exchange_matcher = MatchesStructure(
            url=Equals("http://charmhub.example/v1/tokens/usso/exchange"),
            method=Equals("POST"),
            headers=ContainsDict(
                {
                    "Authorization": Equals(
                        'Macaroon root="%s", discharge="%s"'
                        % (root_macaroon_raw, discharge_macaroon_raw)
                    ),
                }
            ),
            body=AfterPreprocessing(
                json.loads,
                Equals(
                    {
                        "client-description": "%s for %s"
                        % (store_name, config.vhost.mainsite.hostname)
                    }
                ),
            ),
        )
        self.assertThat(
            responses.calls,
            MatchesListwise([MatchesStructure(request=exchange_matcher)]),
        )

    @responses.activate
    def test_completeAuthorization_candid_auth_method_flag_on(self):
        # completeAuthorization follows the auth_method recorded at
        # beginAuthorization instead of the live feature flag. Even with the
        # USSO flag on, a "candid" auth_method still exchanges via the Candid
        # endpoint. This ensures that the authorizations which are already in
        # progress are not broken.
        self.useFixture(
            FeatureFixture(
                {
                    CHARM_RECIPE_ALLOW_CREATE: "on",
                    CHARM_RECIPE_USSO_FEATURE_FLAG: "on",
                }
            )
        )
        private_key = PrivateKey.generate()
        self.pushConfig(
            "charms",
            charmhub_secrets_public_key=base64.b64encode(
                bytes(private_key.public_key)
            ).decode(),
        )
        root_macaroon = Macaroon(version=2)
        root_macaroon_raw = root_macaroon.serialize(JsonSerializer())
        unbound_discharge_macaroon = Macaroon(version=2)
        unbound_discharge_macaroon_raw = unbound_discharge_macaroon.serialize(
            JsonSerializer()
        )
        exchanged_macaroon_raw = Macaroon(version=2).serialize(
            JsonSerializer()
        )
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/exchange",
            json={"macaroon": exchanged_macaroon_raw},
        )
        recipe = self.factory.makeCharmRecipe(
            store_upload=True,
            store_name=self.factory.getUniqueUnicode(),
            store_secrets={"root": root_macaroon_raw, "auth_method": "candid"},
        )
        with person_logged_in(recipe.registrant):
            recipe.completeAuthorization(unbound_discharge_macaroon_raw)
        self.assertEqual(
            "http://charmhub.example/v1/tokens/exchange",
            responses.calls[-1].request.url,
        )

    @responses.activate
    def test_completeAuthorization_usso_auth_method_flag_off(self):
        # The mirror case with the USSO flag off. A "usso" auth_method is
        # recorded, and the exchange still goes to the Ubuntu SSO endpoint.
        private_key = PrivateKey.generate()
        self.pushConfig(
            "charms",
            charmhub_secrets_public_key=base64.b64encode(
                bytes(private_key.public_key)
            ).decode(),
        )
        root_macaroon = Macaroon(version=2)
        root_macaroon_raw = root_macaroon.serialize()
        unbound_discharge_macaroon = Macaroon(version=2)
        unbound_discharge_macaroon_raw = unbound_discharge_macaroon.serialize()
        exchanged_macaroon_raw = Macaroon(version=2).serialize(
            JsonSerializer()
        )
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/usso/exchange",
            json={"macaroon": exchanged_macaroon_raw},
        )
        recipe = self.factory.makeCharmRecipe(
            store_upload=True,
            store_name=self.factory.getUniqueUnicode(),
            store_secrets={"root": root_macaroon_raw, "auth_method": "usso"},
        )
        with person_logged_in(recipe.registrant):
            recipe.completeAuthorization(unbound_discharge_macaroon_raw)
        self.assertEqual(
            "http://charmhub.example/v1/tokens/usso/exchange",
            responses.calls[-1].request.url,
        )


class TestCharmRecipeDeleteWithBuilds(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))

    def test_delete_with_builds(self):
        # A charm recipe with build requests and builds can be deleted.
        # Doing so deletes all its build requests, their builds, and their
        # files, and any associated build jobs too.
        owner = self.factory.makePerson()
        project = self.factory.makeProduct()
        distroseries = self.factory.makeDistroSeries()
        processor = self.factory.makeProcessor(supports_virtualized=True)
        das = self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag=processor.name,
            processor=processor,
        )
        das.addOrUpdateChroot(
            self.factory.makeLibraryFileAlias(
                filename="fake_chroot.tar.gz", db_only=True
            )
        )
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
                    bases:
                      - build-on:
                          - name: "%s"
                            channel: "%s"
                            architectures: [%s]
                    """
                    % (
                        distroseries.distribution.name,
                        distroseries.name,
                        processor.name,
                    )
                )
            )
        )
        [git_ref] = self.factory.makeGitRefs()
        condemned_recipe = self.factory.makeCharmRecipe(
            registrant=owner,
            owner=owner,
            project=project,
            name="condemned",
            git_ref=git_ref,
        )
        other_recipe = self.factory.makeCharmRecipe(
            registrant=owner, owner=owner, project=project, git_ref=git_ref
        )
        self.assertTrue(
            getUtility(ICharmRecipeSet).exists(owner, project, "condemned")
        )
        with person_logged_in(owner):
            requests = []
            jobs = []
            for recipe in (condemned_recipe, other_recipe):
                requests.append(recipe.requestBuilds(owner))
                jobs.append(removeSecurityProxy(requests[-1])._job)
            with dbuser(config.ICharmRecipeRequestBuildsJobSource.dbuser):
                JobRunner(jobs).runAll()
            for job in jobs:
                self.assertEqual(JobStatus.COMPLETED, job.job.status)
            [build] = requests[0].builds
            [other_build] = requests[1].builds
            charm_file = self.factory.makeCharmFile(build=build)
            other_charm_file = self.factory.makeCharmFile(build=other_build)
            charm_build_job = getUtility(ICharmhubUploadJobSource).create(
                build
            )
            other_build_job = getUtility(ICharmhubUploadJobSource).create(
                other_build
            )
        store = Store.of(condemned_recipe)
        store.flush()
        job_ids = [job.job_id for job in jobs]
        build_id = build.id
        build_queue_id = build.buildqueue_record.id
        build_farm_job_id = removeSecurityProxy(build).build_farm_job_id
        charm_build_job_id = charm_build_job.job_id
        charm_file_id = removeSecurityProxy(charm_file).id
        with person_logged_in(condemned_recipe.owner):
            condemned_recipe.destroySelf()
        flush_database_caches()
        # The deleted recipe, its build requests, its builds and the build job
        # are gone.
        self.assertFalse(
            getUtility(ICharmRecipeSet).exists(owner, project, "condemned")
        )
        self.assertIsNone(store.get(CharmRecipeJob, job_ids[0]))
        self.assertIsNone(getUtility(ICharmRecipeBuildSet).getByID(build_id))
        self.assertIsNone(store.get(BuildQueue, build_queue_id))
        self.assertIsNone(store.get(BuildFarmJob, build_farm_job_id))
        self.assertIsNone(store.get(CharmFile, charm_file_id))
        self.assertIsNone(store.get(CharmRecipeBuildJob, charm_build_job_id))
        # Unrelated build requests, build jobs and builds are still present.
        self.assertIsNotNone(
            store.get(CharmRecipeBuildJob, other_build_job.job_id)
        )
        self.assertEqual(
            removeSecurityProxy(jobs[1]).context,
            store.get(CharmRecipeJob, job_ids[1]),
        )
        self.assertEqual(
            other_build,
            getUtility(ICharmRecipeBuildSet).getByID(other_build.id),
        )
        self.assertIsNotNone(other_build.buildqueue_record)
        self.assertIsNotNone(
            store.get(CharmFile, removeSecurityProxy(other_charm_file).id)
        )


class TestCharmRecipeSet(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))

    def test_class_implements_interfaces(self):
        # The CharmRecipeSet class implements ICharmRecipeSet.
        self.assertProvides(getUtility(ICharmRecipeSet), ICharmRecipeSet)

    def makeCharmRecipeComponents(self, git_ref=None):
        """Return a dict of values that can be used to make a charm recipe.

        Suggested use: provide as kwargs to ICharmRecipeSet.new.

        :param git_ref: An `IGitRef`, or None.
        """
        registrant = self.factory.makePerson()
        components = {
            "registrant": registrant,
            "owner": self.factory.makeTeam(owner=registrant),
            "project": self.factory.makeProduct(),
            "name": self.factory.getUniqueUnicode("charm-name"),
        }
        if git_ref is None:
            git_ref = self.factory.makeGitRefs()[0]
        components["git_ref"] = git_ref
        return components

    def test_creation_git(self):
        # The metadata entries supplied when a charm recipe is created for a
        # Git branch are present on the new object.
        [ref] = self.factory.makeGitRefs()
        components = self.makeCharmRecipeComponents(git_ref=ref)
        recipe = getUtility(ICharmRecipeSet).new(**components)
        self.assertEqual(components["registrant"], recipe.registrant)
        self.assertEqual(components["owner"], recipe.owner)
        self.assertEqual(components["project"], recipe.project)
        self.assertEqual(components["name"], recipe.name)
        self.assertEqual(ref.repository, recipe.git_repository)
        self.assertEqual(ref.path, recipe.git_path)
        self.assertEqual(ref, recipe.git_ref)
        self.assertIsNone(recipe.build_path)
        self.assertFalse(recipe.auto_build)
        self.assertIsNone(recipe.auto_build_channels)
        self.assertTrue(recipe.require_virtualized)
        self.assertFalse(recipe.private)
        self.assertFalse(recipe.store_upload)
        self.assertIsNone(recipe.store_name)
        self.assertIsNone(recipe.store_secrets)
        self.assertEqual([], recipe.store_channels)
        self.assertFalse(recipe.use_fetch_service)
        self.assertEqual(
            FetchServicePolicy.STRICT, recipe.fetch_service_policy
        )

    def test_creation_git_url(self):
        # A charm recipe can be backed directly by a URL for an external Git
        # repository, rather than a Git repository hosted in Launchpad.
        ref = self.factory.makeGitRefRemote()
        components = self.makeCharmRecipeComponents(git_ref=ref)
        charm_recipe = getUtility(ICharmRecipeSet).new(**components)
        self.assertIsNone(charm_recipe.git_repository)
        self.assertEqual(ref.repository_url, charm_recipe.git_repository_url)
        self.assertEqual(ref.path, charm_recipe.git_path)
        self.assertEqual(ref, charm_recipe.git_ref)

    def test_creation_no_source(self):
        # Attempting to create a charm recipe without a Git repository
        # fails.
        registrant = self.factory.makePerson()
        self.assertRaises(
            NoSourceForCharmRecipe,
            getUtility(ICharmRecipeSet).new,
            registrant,
            registrant,
            self.factory.makeProduct(),
            self.factory.getUniqueUnicode("charm-name"),
        )

    def test_getByName(self):
        owner = self.factory.makePerson()
        project = self.factory.makeProduct()
        project_recipe = self.factory.makeCharmRecipe(
            registrant=owner, owner=owner, project=project, name="proj-charm"
        )
        self.factory.makeCharmRecipe(
            registrant=owner, owner=owner, name="proj-charm"
        )

        self.assertEqual(
            project_recipe,
            getUtility(ICharmRecipeSet).getByName(
                owner, project, "proj-charm"
            ),
        )

    def test_findByOwner(self):
        # ICharmRecipeSet.findByOwner returns all charm recipes with the
        # given owner.
        owners = [self.factory.makePerson() for i in range(2)]
        recipes = []
        for owner in owners:
            for _ in range(2):
                recipes.append(
                    self.factory.makeCharmRecipe(registrant=owner, owner=owner)
                )
        recipe_set = getUtility(ICharmRecipeSet)
        self.assertContentEqual(recipes[:2], recipe_set.findByOwner(owners[0]))
        self.assertContentEqual(recipes[2:], recipe_set.findByOwner(owners[1]))

    def test_findByPerson(self):
        # ICharmRecipeSet.findByPerson returns all charm recipes with the
        # given owner or based on repositories with the given owner.
        owners = [self.factory.makePerson() for i in range(2)]
        recipes = []
        for owner in owners:
            recipes.append(
                self.factory.makeCharmRecipe(registrant=owner, owner=owner)
            )
            [ref] = self.factory.makeGitRefs(owner=owner)
            recipes.append(self.factory.makeCharmRecipe(git_ref=ref))
        recipe_set = getUtility(ICharmRecipeSet)
        self.assertContentEqual(
            recipes[:2], recipe_set.findByPerson(owners[0])
        )
        self.assertContentEqual(
            recipes[2:], recipe_set.findByPerson(owners[1])
        )

    def test_findByProject(self):
        # ICharmRecipeSet.findByProject returns all charm recipes based on
        # repositories for the given project, and charm recipes associated
        # directly with the project.
        projects = [self.factory.makeProduct() for i in range(2)]
        recipes = []
        for project in projects:
            [ref] = self.factory.makeGitRefs(target=project)
            recipes.append(self.factory.makeCharmRecipe(git_ref=ref))
            recipes.append(self.factory.makeCharmRecipe(project=project))
        [ref] = self.factory.makeGitRefs(target=None)
        recipes.append(self.factory.makeCharmRecipe(git_ref=ref))
        recipe_set = getUtility(ICharmRecipeSet)
        self.assertContentEqual(
            recipes[:2], recipe_set.findByProject(projects[0])
        )
        self.assertContentEqual(
            recipes[2:4], recipe_set.findByProject(projects[1])
        )

    def test_findByGitRepository(self):
        # ICharmRecipeSet.findByGitRepository returns all charm recipes with
        # the given Git repository.
        repositories = [self.factory.makeGitRepository() for i in range(2)]
        recipes = []
        for repository in repositories:
            for _ in range(2):
                [ref] = self.factory.makeGitRefs(repository=repository)
                recipes.append(self.factory.makeCharmRecipe(git_ref=ref))
        recipe_set = getUtility(ICharmRecipeSet)
        self.assertContentEqual(
            recipes[:2], recipe_set.findByGitRepository(repositories[0])
        )
        self.assertContentEqual(
            recipes[2:], recipe_set.findByGitRepository(repositories[1])
        )

    def test_findByGitRepository_paths(self):
        # ICharmRecipeSet.findByGitRepository can restrict by reference
        # paths.
        repositories = [self.factory.makeGitRepository() for i in range(2)]
        recipes = []
        for repository in repositories:
            for _ in range(3):
                [ref] = self.factory.makeGitRefs(repository=repository)
                recipes.append(self.factory.makeCharmRecipe(git_ref=ref))
        recipe_set = getUtility(ICharmRecipeSet)
        self.assertContentEqual(
            [], recipe_set.findByGitRepository(repositories[0], paths=[])
        )
        self.assertContentEqual(
            [recipes[0]],
            recipe_set.findByGitRepository(
                repositories[0], paths=[recipes[0].git_ref.path]
            ),
        )
        self.assertContentEqual(
            recipes[:2],
            recipe_set.findByGitRepository(
                repositories[0],
                paths=[recipes[0].git_ref.path, recipes[1].git_ref.path],
            ),
        )

    def test_findByGitRef(self):
        # ICharmRecipeSet.findByGitRef returns all charm recipes with the
        # given Git reference.
        repositories = [self.factory.makeGitRepository() for i in range(2)]
        refs = []
        recipes = []
        for _ in repositories:
            refs.extend(
                self.factory.makeGitRefs(
                    paths=["refs/heads/master", "refs/heads/other"]
                )
            )
            recipes.append(self.factory.makeCharmRecipe(git_ref=refs[-2]))
            recipes.append(self.factory.makeCharmRecipe(git_ref=refs[-1]))
        recipe_set = getUtility(ICharmRecipeSet)
        for ref, recipe in zip(refs, recipes):
            self.assertContentEqual([recipe], recipe_set.findByGitRef(ref))

    def test_findByContext(self):
        # ICharmRecipeSet.findByContext returns all charm recipes with the
        # given context.
        person = self.factory.makePerson()
        project = self.factory.makeProduct()
        repository = self.factory.makeGitRepository(
            owner=person, target=project
        )
        refs = self.factory.makeGitRefs(
            repository=repository,
            paths=["refs/heads/master", "refs/heads/other"],
        )
        other_repository = self.factory.makeGitRepository()
        other_refs = self.factory.makeGitRefs(
            repository=other_repository,
            paths=["refs/heads/master", "refs/heads/other"],
        )
        recipes = []
        recipes.append(self.factory.makeCharmRecipe(git_ref=refs[0]))
        recipes.append(self.factory.makeCharmRecipe(git_ref=refs[1]))
        recipes.append(
            self.factory.makeCharmRecipe(
                registrant=person, owner=person, git_ref=other_refs[0]
            )
        )
        recipes.append(
            self.factory.makeCharmRecipe(
                project=project, git_ref=other_refs[1]
            )
        )
        recipe_set = getUtility(ICharmRecipeSet)
        self.assertContentEqual(recipes[:3], recipe_set.findByContext(person))
        self.assertContentEqual(
            [recipes[0], recipes[1], recipes[3]],
            recipe_set.findByContext(project),
        )
        self.assertContentEqual(
            recipes[:2], recipe_set.findByContext(repository)
        )
        self.assertContentEqual(
            [recipes[0]], recipe_set.findByContext(refs[0])
        )
        self.assertRaises(
            BadCharmRecipeSearchContext,
            recipe_set.findByContext,
            self.factory.makeDistribution(),
        )

    def test__findStaleRecipes(self):
        # Stale; not built automatically.
        self.factory.makeCharmRecipe(is_stale=True)
        # Not stale; built automatically.
        self.factory.makeCharmRecipe(auto_build=True, is_stale=False)
        # Stale; built automatically.
        stale_daily = self.factory.makeCharmRecipe(
            auto_build=True, is_stale=True
        )
        self.assertContentEqual(
            [stale_daily], CharmRecipeSet._findStaleRecipes()
        )

    def test__findStaleRecipes_distinct(self):
        # If a charm recipe has two build requests, it only returns one
        # recipe.
        recipe = self.factory.makeCharmRecipe(auto_build=True, is_stale=True)
        for _ in range(2):
            build_request = self.factory.makeCharmRecipeBuildRequest(
                recipe=recipe
            )
            removeSecurityProxy(
                removeSecurityProxy(build_request)._job
            ).job.date_created = datetime.now(timezone.utc) - timedelta(days=2)
        self.assertContentEqual([recipe], CharmRecipeSet._findStaleRecipes())

    def test_makeAutoBuilds(self):
        # ICharmRecipeSet.makeAutoBuilds requests builds of
        # appropriately-configured recipes where possible.
        self.assertEqual([], getUtility(ICharmRecipeSet).makeAutoBuilds())
        recipe = self.factory.makeCharmRecipe(auto_build=True, is_stale=True)
        logger = BufferLogger()
        [build_request] = getUtility(ICharmRecipeSet).makeAutoBuilds(
            logger=logger
        )
        self.assertThat(
            build_request,
            MatchesStructure(
                recipe=Equals(recipe),
                status=Equals(CharmRecipeBuildRequestStatus.PENDING),
                requester=Equals(recipe.owner),
                channels=Is(None),
            ),
        )
        expected_log_entries = [
            "DEBUG Scheduling builds of charm recipe %s/%s/%s"
            % (recipe.owner.name, recipe.project.name, recipe.name),
        ]
        self.assertEqual(
            expected_log_entries, logger.getLogBuffer().splitlines()
        )
        self.assertFalse(recipe.is_stale)

    def test_makeAutoBuild_skips_for_unexpected_exceptions(self):
        # scheduling builds need to be unaffected by one erroring
        recipe = self.factory.makeCharmRecipe(auto_build=True, is_stale=True)
        logger = BufferLogger()
        recipe = removeSecurityProxy(recipe)

        # currently there is no expected way that `makeAutoBuilds` could fail
        # so we fake it
        def fake_requestAutoBuilds_with_side_effect(logger=None):
            raise Exception("something unexpected went wrong")

        recipe.requestAutoBuilds = fake_requestAutoBuilds_with_side_effect

        build_requests = getUtility(ICharmRecipeSet).makeAutoBuilds(
            logger=logger
        )

        self.assertEqual([], build_requests)
        self.assertEqual(
            ["ERROR something unexpected went wrong"],
            logger.getLogBuffer().splitlines(),
        )

    def test_makeAutoBuild_skips_and_no_logger_enabled(self):
        # This is basically the same test case as
        # `test_makeAutoBuild_skips_for_unexpected_exceptions`
        # but we particularly test with no logger enabled.
        recipe = self.factory.makeCharmRecipe(auto_build=True, is_stale=True)
        recipe = removeSecurityProxy(recipe)

        def fake_requestAutoBuilds_with_side_effect(logger=None):
            raise Exception("something unexpected went wrong")

        recipe.requestAutoBuilds = fake_requestAutoBuilds_with_side_effect

        build_requests = getUtility(ICharmRecipeSet).makeAutoBuilds()

        self.assertEqual([], build_requests)

    def test_makeAutoBuilds_skips_if_requested_recently(self):
        # ICharmRecipeSet.makeAutoBuilds skips recipes that have been built
        # recently.
        recipe = self.factory.makeCharmRecipe(auto_build=True, is_stale=True)
        self.factory.makeCharmRecipeBuildRequest(
            requester=recipe.owner, recipe=recipe
        )
        logger = BufferLogger()
        build_requests = getUtility(ICharmRecipeSet).makeAutoBuilds(
            logger=logger
        )
        self.assertEqual([], build_requests)
        self.assertEqual([], logger.getLogBuffer().splitlines())

    def test_makeAutoBuilds_skips_if_requested_recently_matching_channels(
        self,
    ):
        # ICharmRecipeSet.makeAutoBuilds only considers recent build
        # requests to match a recipe if they match its auto_build_channels.
        recipe1 = self.factory.makeCharmRecipe(auto_build=True, is_stale=True)
        recipe2 = self.factory.makeCharmRecipe(
            auto_build=True,
            auto_build_channels={"charmcraft": "edge"},
            is_stale=True,
        )
        # Create some build requests with mismatched channels.
        self.factory.makeCharmRecipeBuildRequest(
            recipe=recipe1,
            requester=recipe1.owner,
            channels={"charmcraft": "edge"},
        )
        self.factory.makeCharmRecipeBuildRequest(
            recipe=recipe2,
            requester=recipe2.owner,
            channels={"charmcraft": "stable"},
        )

        logger = BufferLogger()
        build_requests = getUtility(ICharmRecipeSet).makeAutoBuilds(
            logger=logger
        )
        self.assertThat(
            build_requests,
            MatchesSetwise(
                MatchesStructure(
                    recipe=Equals(recipe1),
                    status=Equals(CharmRecipeBuildRequestStatus.PENDING),
                    requester=Equals(recipe1.owner),
                    channels=Is(None),
                ),
                MatchesStructure(
                    recipe=Equals(recipe2),
                    status=Equals(CharmRecipeBuildRequestStatus.PENDING),
                    requester=Equals(recipe2.owner),
                    channels=Equals({"charmcraft": "edge"}),
                ),
            ),
        )
        log_entries = logger.getLogBuffer().splitlines()
        self.assertEqual(2, len(log_entries))
        for recipe in recipe1, recipe2:
            self.assertIn(
                "DEBUG Scheduling builds of charm recipe %s/%s/%s"
                % (recipe.owner.name, recipe.project.name, recipe.name),
                log_entries,
            )
            self.assertFalse(recipe.is_stale)

        # Mark the two recipes stale and try again.  There are now matching
        # build requests so we don't try to request more.
        for recipe in recipe1, recipe2:
            removeSecurityProxy(recipe).is_stale = True
            IStore(recipe).flush()
        logger = BufferLogger()
        build_requests = getUtility(ICharmRecipeSet).makeAutoBuilds(
            logger=logger
        )
        self.assertEqual([], build_requests)
        self.assertEqual([], logger.getLogBuffer().splitlines())

    def test_makeAutoBuilds_skips_non_stale_recipes(self):
        # ICharmRecipeSet.makeAutoBuilds skips recipes that are not stale.
        self.factory.makeCharmRecipe(auto_build=True, is_stale=False)
        self.assertEqual([], getUtility(ICharmRecipeSet).makeAutoBuilds())

    def test_makeAutoBuilds_with_older_build_request(self):
        # If a previous build request is not recent and the recipe is stale,
        # ICharmRecipeSet.makeAutoBuilds requests builds.
        recipe = self.factory.makeCharmRecipe(auto_build=True, is_stale=True)
        one_day_ago = datetime.now(timezone.utc) - timedelta(days=1)
        build_request = self.factory.makeCharmRecipeBuildRequest(
            recipe=recipe, requester=recipe.owner
        )
        removeSecurityProxy(
            removeSecurityProxy(build_request)._job
        ).job.date_created = one_day_ago
        [build_request] = getUtility(ICharmRecipeSet).makeAutoBuilds()
        self.assertThat(
            build_request,
            MatchesStructure(
                recipe=Equals(recipe),
                status=Equals(CharmRecipeBuildRequestStatus.PENDING),
                requester=Equals(recipe.owner),
                channels=Is(None),
            ),
        )

    def test_makeAutoBuilds_with_older_and_newer_build_requests(self):
        # If builds of a recipe have been requested twice, and the most recent
        # request is too recent, ICharmRecipeSet.makeAutoBuilds does not
        # request builds.
        recipe = self.factory.makeCharmRecipe(auto_build=True, is_stale=True)
        for timediff in timedelta(days=1), timedelta(minutes=30):
            date_created = datetime.now(timezone.utc) - timediff
            build_request = self.factory.makeCharmRecipeBuildRequest(
                recipe=recipe, requester=recipe.owner
            )
            removeSecurityProxy(
                removeSecurityProxy(build_request)._job
            ).job.date_created = date_created
        self.assertEqual([], getUtility(ICharmRecipeSet).makeAutoBuilds())

    def test_detachFromGitRepository(self):
        # ICharmRecipeSet.detachFromGitRepository clears the given Git
        # repository from all charm recipes.
        repositories = [self.factory.makeGitRepository() for i in range(2)]
        recipes = []
        paths = []
        refs = []
        for repository in repositories:
            for _ in range(2):
                [ref] = self.factory.makeGitRefs(repository=repository)
                paths.append(ref.path)
                refs.append(ref)
                recipes.append(
                    self.factory.makeCharmRecipe(
                        git_ref=ref, date_created=ONE_DAY_AGO
                    )
                )
        getUtility(ICharmRecipeSet).detachFromGitRepository(repositories[0])
        self.assertEqual(
            [None, None, repositories[1], repositories[1]],
            [recipe.git_repository for recipe in recipes],
        )
        self.assertEqual(
            [None, None, paths[2], paths[3]],
            [recipe.git_path for recipe in recipes],
        )
        self.assertEqual(
            [None, None, refs[2], refs[3]],
            [recipe.git_ref for recipe in recipes],
        )
        for recipe in recipes[:2]:
            self.assertSqlAttributeEqualsDate(
                recipe, "date_last_modified", UTC_NOW
            )

    def test_admins_can_update_admin_only_fields(self):
        # Test the admin fields can be updated by an admin

        [ref] = self.factory.makeGitRefs()
        charm = self.factory.makeCharmRecipe(
            git_ref=ref, use_fetch_service=True
        )

        admin_fields = {
            "require_virtualized": True,
        }

        for field_name, field_value in admin_fields.items():
            # exception is not raised when an admin does the same
            with admin_logged_in():
                setattr(charm, field_name, field_value)

    def test_non_admins_cannot_update_admin_only_fields(self):
        # The admin fields cannot be updated by a non admin

        [ref] = self.factory.makeGitRefs()
        charm = self.factory.makeCharmRecipe(
            git_ref=ref, use_fetch_service=True
        )
        person = self.factory.makePerson()
        admin_fields = {
            "require_virtualized": True,
        }

        for field_name, field_value in admin_fields.items():
            # exception is raised when a non admin updates the fields
            with person_logged_in(person):
                self.assertRaises(
                    Unauthorized,
                    setattr,
                    charm,
                    field_name,
                    field_value,
                )

    def test_use_fetch_service(self):
        [ref] = self.factory.makeGitRefs()
        charm = self.factory.makeCharmRecipe(
            git_ref=ref, use_fetch_service=False
        )
        self.assertEqual(False, charm.use_fetch_service)
        self.assertEqual(FetchServicePolicy.STRICT, charm.fetch_service_policy)
        with person_logged_in(charm.owner):
            charm.use_fetch_service = True
            charm.fetch_service_policy = FetchServicePolicy.PERMISSIVE
        self.assertEqual(True, charm.use_fetch_service)
        self.assertEqual(
            FetchServicePolicy.PERMISSIVE, charm.fetch_service_policy
        )

    def test_fetch_service_is_opt_in(self):
        # The fetch service not used by default and is opt-in only
        [ref] = self.factory.makeGitRefs()
        charm = self.factory.makeCharmRecipe(git_ref=ref)
        self.assertEqual(False, charm.use_fetch_service)
        self.assertEqual(FetchServicePolicy.STRICT, charm.fetch_service_policy)


class TestCharmRecipeWebservice(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(
            FeatureFixture(
                {
                    CHARM_RECIPE_ALLOW_CREATE: "on",
                    CHARM_RECIPE_BUILD_DISTRIBUTION: "ubuntu",
                    CHARM_RECIPE_PRIVATE_FEATURE_FLAG: "on",
                }
            )
        )
        self.pushConfig("charms", charmhub_url="http://charmhub.example/")
        self.pushConfig(
            "launchpad", candid_service_root="https://candid.test/"
        )
        self.person = self.factory.makePerson(displayname="Test Person")
        self.webservice = webservice_for_person(
            self.person, permission=OAuthPermission.WRITE_PUBLIC
        )
        self.webservice.default_api_version = "devel"
        login(ANONYMOUS)

    def getURL(self, obj):
        return self.webservice.getAbsoluteUrl(api_url(obj))

    def makeCharmRecipe(
        self,
        owner=None,
        project=None,
        name=None,
        git_ref=None,
        private=False,
        webservice=None,
        **kwargs,
    ):
        if owner is None:
            owner = self.person
        if project is None:
            project = self.factory.makeProduct(owner=owner)
        if name is None:
            name = self.factory.getUniqueUnicode()
        if git_ref is None:
            [git_ref] = self.factory.makeGitRefs()
        if webservice is None:
            webservice = self.webservice
        transaction.commit()
        owner_url = api_url(owner)
        project_url = api_url(project)
        git_ref_url = api_url(git_ref)
        logout()
        information_type = (
            InformationType.PROPRIETARY if private else InformationType.PUBLIC
        )
        response = webservice.named_post(
            "/+charm-recipes",
            "new",
            owner=owner_url,
            project=project_url,
            name=name,
            git_ref=git_ref_url,
            information_type=information_type.title,
            **kwargs,
        )
        self.assertEqual(201, response.status)
        return webservice.get(response.getHeader("Location")).jsonBody()

    def getCollectionLinks(self, entry, member):
        """Return a list of self_link attributes of entries in a collection."""
        collection = self.webservice.get(
            entry["%s_collection_link" % member]
        ).jsonBody()
        return [entry["self_link"] for entry in collection["entries"]]

    def test_new_git(self):
        # Charm recipe creation based on a Git branch works.
        team = self.factory.makeTeam(
            owner=self.person,
            membership_policy=TeamMembershipPolicy.RESTRICTED,
        )
        project = self.factory.makeProduct(owner=team)
        [ref] = self.factory.makeGitRefs()
        recipe = self.makeCharmRecipe(
            owner=team, project=project, name="test-charm", git_ref=ref
        )
        with person_logged_in(self.person):
            self.assertThat(
                recipe,
                ContainsDict(
                    {
                        "registrant_link": Equals(self.getURL(self.person)),
                        "owner_link": Equals(self.getURL(team)),
                        "project_link": Equals(self.getURL(project)),
                        "name": Equals("test-charm"),
                        "git_ref_link": Equals(self.getURL(ref)),
                        "build_path": Is(None),
                        "require_virtualized": Is(True),
                    }
                ),
            )

    def test_new_store_options(self):
        # The store-related options in CharmRecipe.new work.
        store_name = self.factory.getUniqueUnicode()
        recipe = self.makeCharmRecipe(
            store_upload=True, store_name=store_name, store_channels=["edge"]
        )
        with person_logged_in(self.person):
            self.assertThat(
                recipe,
                ContainsDict(
                    {
                        "store_upload": Is(True),
                        "store_name": Equals(store_name),
                        "store_channels": Equals(["edge"]),
                    }
                ),
            )

    def test_duplicate(self):
        # An attempt to create a duplicate charm recipe fails.
        team = self.factory.makeTeam(
            owner=self.person,
            membership_policy=TeamMembershipPolicy.RESTRICTED,
        )
        project = self.factory.makeProduct(owner=team)
        name = self.factory.getUniqueUnicode()
        [git_ref] = self.factory.makeGitRefs()
        owner_url = api_url(team)
        project_url = api_url(project)
        git_ref_url = api_url(git_ref)
        self.makeCharmRecipe(
            owner=team, project=project, name=name, git_ref=git_ref
        )
        response = self.webservice.named_post(
            "/+charm-recipes",
            "new",
            owner=owner_url,
            project=project_url,
            name=name,
            git_ref=git_ref_url,
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=400,
                body=(
                    b"There is already a charm recipe with the same project, "
                    b"owner, and name."
                ),
            ),
        )

    def test_not_owner(self):
        # If the registrant is not the owner or a member of the owner team,
        # charm recipe creation fails.
        other_person = self.factory.makePerson(displayname="Other Person")
        other_team = self.factory.makeTeam(
            owner=other_person, displayname="Other Team"
        )
        project = self.factory.makeProduct(owner=self.person)
        [git_ref] = self.factory.makeGitRefs()
        transaction.commit()
        other_person_url = api_url(other_person)
        other_team_url = api_url(other_team)
        project_url = api_url(project)
        git_ref_url = api_url(git_ref)
        logout()
        response = self.webservice.named_post(
            "/+charm-recipes",
            "new",
            owner=other_person_url,
            project=project_url,
            name="test-charm",
            git_ref=git_ref_url,
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=401,
                body=(
                    b"Test Person cannot create charm recipes owned by "
                    b"Other Person."
                ),
            ),
        )
        response = self.webservice.named_post(
            "/+charm-recipes",
            "new",
            owner=other_team_url,
            project=project_url,
            name="test-charm",
            git_ref=git_ref_url,
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=401, body=b"Test Person is not a member of Other Team."
            ),
        )

    def test_cannot_set_private_components_of_public_recipe(self):
        # If a charm recipe is public, then trying to change its owner or
        # git_ref components to be private fails.
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            owner=self.person,
            git_ref=self.factory.makeGitRefs()[0],
        )
        private_team = self.factory.makeTeam(
            owner=self.person, visibility=PersonVisibility.PRIVATE
        )
        [private_ref] = self.factory.makeGitRefs(
            owner=self.person, information_type=InformationType.PRIVATESECURITY
        )
        recipe_url = api_url(recipe)
        with person_logged_in(self.person):
            private_team_url = api_url(private_team)
            private_ref_url = api_url(private_ref)
        logout()
        private_webservice = webservice_for_person(
            self.person, permission=OAuthPermission.WRITE_PRIVATE
        )
        private_webservice.default_api_version = "devel"
        response = private_webservice.patch(
            recipe_url,
            "application/json",
            json.dumps({"owner_link": private_team_url}),
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=400,
                body=b"A public charm recipe cannot have a private owner.",
            ),
        )
        response = private_webservice.patch(
            recipe_url,
            "application/json",
            json.dumps({"git_ref_link": private_ref_url}),
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=400,
                body=(
                    b"A public charm recipe cannot have a private repository."
                ),
            ),
        )

    def test_is_stale(self):
        # is_stale is exported and is read-only.
        recipe = self.makeCharmRecipe()
        self.assertTrue(recipe["is_stale"])
        response = self.webservice.patch(
            recipe["self_link"],
            "application/json",
            json.dumps({"is_stale": False}),
        )
        self.assertEqual(400, response.status)

    def test_getByName(self):
        # lp.charm_recipes.getByName returns a matching CharmRecipe.
        project = self.factory.makeProduct(owner=self.person)
        name = self.factory.getUniqueUnicode()
        recipe = self.makeCharmRecipe(project=project, name=name)
        with person_logged_in(self.person):
            owner_url = api_url(self.person)
            project_url = api_url(project)
        response = self.webservice.named_get(
            "/+charm-recipes",
            "getByName",
            owner=owner_url,
            project=project_url,
            name=name,
        )
        self.assertEqual(200, response.status)
        self.assertEqual(recipe, response.jsonBody())

    def test_getByName_missing(self):
        # lp.charm_recipes.getByName returns 404 for a non-existent
        # CharmRecipe.
        project = self.factory.makeProduct(owner=self.person)
        logout()
        with person_logged_in(self.person):
            owner_url = api_url(self.person)
            project_url = api_url(project)
        response = self.webservice.named_get(
            "/+charm-recipes",
            "getByName",
            owner=owner_url,
            project=project_url,
            name="nonexistent",
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=404,
                body=(
                    b"No such charm recipe with this owner and project: "
                    b"'nonexistent'."
                ),
            ),
        )

    @responses.activate
    def assertBeginsAuthorization(self, recipe, **kwargs):
        recipe_url = api_url(recipe)
        root_macaroon = Macaroon(version=2)
        root_macaroon.add_third_party_caveat(
            "https://candid.test/", "", "identity"
        )
        root_macaroon_raw = root_macaroon.serialize(JsonSerializer())
        logout()
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens",
            json={"macaroon": root_macaroon_raw},
        )
        response = self.webservice.named_post(
            recipe_url, "beginAuthorization", **kwargs
        )
        [call] = responses.calls
        self.assertThat(
            call.request,
            MatchesStructure.byEquality(
                url="http://charmhub.example/v1/tokens", method="POST"
            ),
        )
        with person_logged_in(self.person):
            expected_body = {
                "description": (f"{recipe.store_name} for launchpad.test"),
                "packages": [{"type": "charm", "name": recipe.store_name}],
                "permissions": [
                    "package-manage-releases",
                    "package-manage-revisions",
                    "package-view-revisions",
                ],
            }
            self.assertEqual(expected_body, json.loads(call.request.body))
            self.assertEqual(
                {"root": root_macaroon_raw, "auth_method": "candid"},
                recipe.store_secrets,
            )
        return response, root_macaroon_raw

    def test_beginAuthorization(self):
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            store_upload=True,
            store_name=self.factory.getUniqueUnicode(),
        )
        response, root_macaroon_raw = self.assertBeginsAuthorization(recipe)
        self.assertEqual(root_macaroon_raw, response.jsonBody())

    def test_beginAuthorization_unauthorized(self):
        # A user without edit access cannot authorize charm recipe uploads.
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            store_upload=True,
            store_name=self.factory.getUniqueUnicode(),
        )
        recipe_url = api_url(recipe)
        other_person = self.factory.makePerson()
        other_webservice = webservice_for_person(
            other_person, permission=OAuthPermission.WRITE_PUBLIC
        )
        other_webservice.default_api_version = "devel"
        response = other_webservice.named_post(
            recipe_url, "beginAuthorization"
        )
        self.assertEqual(401, response.status)

    @responses.activate
    def test_completeAuthorization(self):
        private_key = PrivateKey.generate()
        self.pushConfig(
            "charms",
            charmhub_secrets_public_key=base64.b64encode(
                bytes(private_key.public_key)
            ).decode(),
        )
        root_macaroon = Macaroon(version=2)
        root_macaroon_raw = root_macaroon.serialize(JsonSerializer())
        unbound_discharge_macaroon = Macaroon(version=2)
        unbound_discharge_macaroon_raw = unbound_discharge_macaroon.serialize(
            JsonSerializer()
        )
        discharge_macaroon_raw = root_macaroon.prepare_for_request(
            unbound_discharge_macaroon
        ).serialize(JsonSerializer())
        exchanged_macaroon = Macaroon(version=2)
        exchanged_macaroon_raw = exchanged_macaroon.serialize(JsonSerializer())
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/exchange",
            json={"macaroon": exchanged_macaroon_raw},
        )
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            store_upload=True,
            store_name=self.factory.getUniqueUnicode(),
            store_secrets={"root": root_macaroon_raw},
        )
        recipe_url = api_url(recipe)
        logout()
        response = self.webservice.named_post(
            recipe_url,
            "completeAuthorization",
            discharge_macaroon=json.dumps(unbound_discharge_macaroon_raw),
        )
        self.assertEqual(200, response.status)
        self.pushConfig(
            "charms",
            charmhub_secrets_private_key=base64.b64encode(
                bytes(private_key)
            ).decode(),
        )
        container = getUtility(IEncryptedContainer, "charmhub-secrets")
        with person_logged_in(self.person):
            self.assertThat(
                recipe.store_secrets,
                MatchesDict(
                    {
                        "exchanged_encrypted": AfterPreprocessing(
                            lambda data: container.decrypt(data).decode(),
                            Equals(exchanged_macaroon_raw),
                        ),
                    }
                ),
            )
        exchange_matcher = MatchesStructure(
            url=Equals("http://charmhub.example/v1/tokens/exchange"),
            method=Equals("POST"),
            headers=ContainsDict(
                {
                    "Macaroons": AfterPreprocessing(
                        lambda v: json.loads(
                            base64.b64decode(v.encode()).decode()
                        ),
                        Equals(
                            [
                                json.loads(m)
                                for m in (
                                    root_macaroon_raw,
                                    discharge_macaroon_raw,
                                )
                            ]
                        ),
                    ),
                }
            ),
            body=AfterPreprocessing(json.loads, Equals({})),
        )
        self.assertThat(
            responses.calls,
            MatchesListwise([MatchesStructure(request=exchange_matcher)]),
        )

    def test_completeAuthorization_without_beginAuthorization(self):
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            store_upload=True,
            store_name=self.factory.getUniqueUnicode(),
        )
        recipe_url = api_url(recipe)
        logout()
        discharge_macaroon = Macaroon(version=2)
        response = self.webservice.named_post(
            recipe_url,
            "completeAuthorization",
            discharge_macaroon=json.dumps(
                discharge_macaroon.serialize(JsonSerializer())
            ),
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=400,
                body=(
                    b"beginAuthorization must be called before "
                    b"completeAuthorization."
                ),
            ),
        )

    def test_completeAuthorization_unauthorized(self):
        root_macaroon = Macaroon(version=2)
        discharge_macaroon = Macaroon(version=2)
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            store_upload=True,
            store_name=self.factory.getUniqueUnicode(),
            store_secrets={"root": root_macaroon.serialize(JsonSerializer())},
        )
        recipe_url = api_url(recipe)
        other_person = self.factory.makePerson()
        other_webservice = webservice_for_person(
            other_person, permission=OAuthPermission.WRITE_PUBLIC
        )
        other_webservice.default_api_version = "devel"
        response = other_webservice.named_post(
            recipe_url,
            "completeAuthorization",
            discharge_macaroon=json.dumps(
                discharge_macaroon.serialize(JsonSerializer())
            ),
        )
        self.assertEqual(401, response.status)

    def test_completeAuthorization_malformed_discharge_macaroon(self):
        root_macaroon = Macaroon(version=2)
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person,
            store_upload=True,
            store_name=self.factory.getUniqueUnicode(),
            store_secrets={"root": root_macaroon.serialize(JsonSerializer())},
        )
        recipe_url = api_url(recipe)
        logout()
        response = self.webservice.named_post(
            recipe_url, "completeAuthorization", discharge_macaroon="nonsense"
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=400, body=b"Discharge macaroon is invalid."
            ),
        )

    def makeBuildableDistroArchSeries(
        self,
        distroseries=None,
        architecturetag=None,
        processor=None,
        supports_virtualized=True,
        supports_nonvirtualized=True,
        **kwargs,
    ):
        if architecturetag is None:
            architecturetag = self.factory.getUniqueUnicode("arch")
        if processor is None:
            try:
                processor = getUtility(IProcessorSet).getByName(
                    architecturetag
                )
            except ProcessorNotFound:
                processor = self.factory.makeProcessor(
                    name=architecturetag,
                    supports_virtualized=supports_virtualized,
                    supports_nonvirtualized=supports_nonvirtualized,
                )
        das = self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag=architecturetag,
            processor=processor,
            **kwargs,
        )
        # Add both a chroot and a LXD image to test that
        # getAllowedArchitectures doesn't get confused by multiple
        # PocketChroot rows for a single DistroArchSeries.
        fake_chroot = self.factory.makeLibraryFileAlias(
            filename="fake_chroot.tar.gz", db_only=True
        )
        das.addOrUpdateChroot(fake_chroot)
        fake_lxd = self.factory.makeLibraryFileAlias(
            filename="fake_lxd.tar.gz", db_only=True
        )
        das.addOrUpdateChroot(fake_lxd, image_type=BuildBaseImageType.LXD)
        return das

    def test_requestBuilds(self):
        # Requests for builds for all relevant architectures can be
        # performed over the webservice, and the returned entry indicates
        # the status of the asynchronous job.
        distroseries = self.factory.makeDistroSeries(
            distribution=getUtility(ILaunchpadCelebrities).ubuntu,
            registrant=self.person,
        )
        processors = [
            self.factory.makeProcessor(supports_virtualized=True)
            for _ in range(3)
        ]
        for processor in processors:
            self.makeBuildableDistroArchSeries(
                distroseries=distroseries,
                architecturetag=processor.name,
                processor=processor,
                owner=self.person,
            )
        [git_ref] = self.factory.makeGitRefs()
        recipe = self.makeCharmRecipe(git_ref=git_ref)
        now = get_transaction_timestamp(IStore(distroseries))
        response = self.webservice.named_post(
            recipe["self_link"],
            "requestBuilds",
            channels={"charmcraft": "edge"},
        )
        self.assertEqual(201, response.status)
        build_request_url = response.getHeader("Location")
        build_request = self.webservice.get(build_request_url).jsonBody()
        self.assertThat(
            build_request,
            ContainsDict(
                {
                    "date_requested": AfterPreprocessing(
                        iso8601.parse_date, GreaterThan(now)
                    ),
                    "date_finished": Is(None),
                    "recipe_link": Equals(recipe["self_link"]),
                    "status": Equals("Pending"),
                    "error_message": Is(None),
                    "builds_collection_link": Equals(
                        build_request_url + "/builds"
                    ),
                }
            ),
        )
        self.assertEqual([], self.getCollectionLinks(build_request, "builds"))
        with person_logged_in(self.person):
            charmcraft_yaml = "bases:\n"
            for processor in processors:
                charmcraft_yaml += (
                    "  - build-on:\n"
                    "    - name: ubuntu\n"
                    '      channel: "%s"\n'
                    "      architectures: [%s]\n"
                    % (distroseries.version, processor.name)
                )
            self.useFixture(GitHostingFixture(blob=charmcraft_yaml))
            [job] = getUtility(ICharmRecipeRequestBuildsJobSource).iterReady()
            with dbuser(config.ICharmRecipeRequestBuildsJobSource.dbuser):
                JobRunner([job]).runAll()
        date_requested = iso8601.parse_date(build_request["date_requested"])
        now = get_transaction_timestamp(IStore(distroseries))
        build_request = self.webservice.get(
            build_request["self_link"]
        ).jsonBody()
        self.assertThat(
            build_request,
            ContainsDict(
                {
                    "date_requested": AfterPreprocessing(
                        iso8601.parse_date, Equals(date_requested)
                    ),
                    "date_finished": AfterPreprocessing(
                        iso8601.parse_date,
                        MatchesAll(GreaterThan(date_requested), LessThan(now)),
                    ),
                    "recipe_link": Equals(recipe["self_link"]),
                    "status": Equals("Completed"),
                    "error_message": Is(None),
                    "builds_collection_link": Equals(
                        build_request_url + "/builds"
                    ),
                }
            ),
        )
        builds = self.webservice.get(
            build_request["builds_collection_link"]
        ).jsonBody()["entries"]
        with person_logged_in(self.person):
            self.assertThat(
                builds,
                MatchesSetwise(
                    *(
                        ContainsDict(
                            {
                                "recipe_link": Equals(recipe["self_link"]),
                                "archive_link": Equals(
                                    self.getURL(distroseries.main_archive)
                                ),
                                "arch_tag": Equals(processor.name),
                                "channels": Equals({"charmcraft": "edge"}),
                            }
                        )
                        for processor in processors
                    )
                ),
            )

    def test_requestBuilds_failure(self):
        # If the asynchronous build request job fails, this is reflected in
        # the build request entry.
        [git_ref] = self.factory.makeGitRefs()
        recipe = self.makeCharmRecipe(git_ref=git_ref)
        now = get_transaction_timestamp(IStore(git_ref))
        response = self.webservice.named_post(
            recipe["self_link"], "requestBuilds"
        )
        self.assertEqual(201, response.status)
        build_request_url = response.getHeader("Location")
        build_request = self.webservice.get(build_request_url).jsonBody()
        self.assertThat(
            build_request,
            ContainsDict(
                {
                    "date_requested": AfterPreprocessing(
                        iso8601.parse_date, GreaterThan(now)
                    ),
                    "date_finished": Is(None),
                    "recipe_link": Equals(recipe["self_link"]),
                    "status": Equals("Pending"),
                    "error_message": Is(None),
                    "builds_collection_link": Equals(
                        build_request_url + "/builds"
                    ),
                }
            ),
        )
        self.assertEqual([], self.getCollectionLinks(build_request, "builds"))
        with person_logged_in(self.person):
            self.useFixture(GitHostingFixture()).getBlob.failure = Exception(
                "Something went wrong"
            )
            [job] = getUtility(ICharmRecipeRequestBuildsJobSource).iterReady()
            with dbuser(config.ICharmRecipeRequestBuildsJobSource.dbuser):
                JobRunner([job]).runAll()
        date_requested = iso8601.parse_date(build_request["date_requested"])
        now = get_transaction_timestamp(IStore(git_ref))
        build_request = self.webservice.get(
            build_request["self_link"]
        ).jsonBody()
        self.assertThat(
            build_request,
            ContainsDict(
                {
                    "date_requested": AfterPreprocessing(
                        iso8601.parse_date, Equals(date_requested)
                    ),
                    "date_finished": AfterPreprocessing(
                        iso8601.parse_date,
                        MatchesAll(GreaterThan(date_requested), LessThan(now)),
                    ),
                    "recipe_link": Equals(recipe["self_link"]),
                    "status": Equals("Failed"),
                    "error_message": Equals("Something went wrong"),
                    "builds_collection_link": Equals(
                        build_request_url + "/builds"
                    ),
                }
            ),
        )
        self.assertEqual([], self.getCollectionLinks(build_request, "builds"))

    def test_requestBuilds_not_owner(self):
        # If the requester is not the owner or a member of the owner team,
        # build requests are rejected.
        other_team = self.factory.makeTeam(
            displayname="Other Team",
            membership_policy=TeamMembershipPolicy.RESTRICTED,
        )
        other_webservice = webservice_for_person(
            other_team.teamowner, permission=OAuthPermission.WRITE_PUBLIC
        )
        other_webservice.default_api_version = "devel"
        login(ANONYMOUS)
        recipe = self.makeCharmRecipe(
            owner=other_team, webservice=other_webservice
        )
        response = self.webservice.named_post(
            recipe["self_link"], "requestBuilds"
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=401,
                body=(
                    b"Test Person cannot create charm recipe builds owned by "
                    b"Other Team."
                ),
            ),
        )

    def test_getBuilds(self):
        # The builds, completed_builds, and pending_builds properties are as
        # expected.
        project = self.factory.makeProduct(owner=self.person)
        distroseries = self.factory.makeDistroSeries(
            distribution=getUtility(ILaunchpadCelebrities).ubuntu,
            registrant=self.person,
        )
        processors = [
            self.factory.makeProcessor(supports_virtualized=True)
            for _ in range(4)
        ]
        for processor in processors:
            self.makeBuildableDistroArchSeries(
                distroseries=distroseries,
                architecturetag=processor.name,
                processor=processor,
                owner=self.person,
            )
        recipe = self.makeCharmRecipe(project=project)
        response = self.webservice.named_post(
            recipe["self_link"], "requestBuilds"
        )
        self.assertEqual(201, response.status)
        with person_logged_in(self.person):
            charmcraft_yaml = "bases:\n"
            for processor in processors:
                charmcraft_yaml += (
                    "  - build-on:\n"
                    "    - name: ubuntu\n"
                    '      channel: "%s"\n'
                    "      architectures: [%s]\n"
                    % (distroseries.version, processor.name)
                )
            self.useFixture(GitHostingFixture(blob=charmcraft_yaml))
            [job] = getUtility(ICharmRecipeRequestBuildsJobSource).iterReady()
            with dbuser(config.ICharmRecipeRequestBuildsJobSource.dbuser):
                JobRunner([job]).runAll()
        builds = self.getCollectionLinks(recipe, "builds")
        self.assertEqual(len(processors), len(builds))
        self.assertEqual(
            [], self.getCollectionLinks(recipe, "completed_builds")
        )
        self.assertEqual(
            builds, self.getCollectionLinks(recipe, "pending_builds")
        )

        with person_logged_in(self.person):
            db_recipe = getUtility(ICharmRecipeSet).getByName(
                self.person, project, recipe["name"]
            )
            db_builds = list(db_recipe.builds)
            db_builds[0].updateStatus(
                BuildStatus.BUILDING, date_started=db_recipe.date_created
            )
            db_builds[0].updateStatus(
                BuildStatus.FULLYBUILT,
                date_finished=db_recipe.date_created + timedelta(minutes=10),
            )
        # Builds that have not yet been started are listed last.  This does
        # mean that pending builds that have never been started are sorted
        # to the end, but means that builds that were cancelled before
        # starting don't pollute the start of the collection forever.
        self.assertEqual(builds, self.getCollectionLinks(recipe, "builds"))
        self.assertEqual(
            builds[:1], self.getCollectionLinks(recipe, "completed_builds")
        )
        self.assertEqual(
            builds[1:], self.getCollectionLinks(recipe, "pending_builds")
        )

        with person_logged_in(self.person):
            db_builds[1].updateStatus(
                BuildStatus.BUILDING, date_started=db_recipe.date_created
            )
            db_builds[1].updateStatus(
                BuildStatus.FULLYBUILT,
                date_finished=db_recipe.date_created + timedelta(minutes=20),
            )
        self.assertEqual(
            [builds[1], builds[0], builds[2], builds[3]],
            self.getCollectionLinks(recipe, "builds"),
        )
        self.assertEqual(
            [builds[1], builds[0]],
            self.getCollectionLinks(recipe, "completed_builds"),
        )
        self.assertEqual(
            builds[2:], self.getCollectionLinks(recipe, "pending_builds")
        )

    def test_query_count(self):
        # CharmRecipe has a reasonable query count.
        recipe = self.factory.makeCharmRecipe(
            registrant=self.person, owner=self.person
        )
        url = api_url(recipe)
        logout()
        store = Store.of(recipe)
        store.flush()
        store.invalidate()
        with StormStatementRecorder() as recorder:
            self.webservice.get(url)
        self.assertThat(recorder, HasQueryCount(Equals(19)))

    def test_builds_query_count(self):
        # The query count of CharmRecipe.builds is constant in the number of
        # builds, even if they have store upload jobs.
        self.pushConfig("charms", charmhub_url="http://charmhub.example/")
        distroseries = self.factory.makeDistroSeries(
            distribution=getUtility(ILaunchpadCelebrities).ubuntu,
            registrant=self.person,
        )
        processor = self.factory.makeProcessor(supports_virtualized=True)
        das = self.makeBuildableDistroArchSeries(
            distroseries=distroseries,
            architecturetag=processor.name,
            processor=processor,
            owner=self.person,
        )
        with person_logged_in(self.person):
            recipe = self.factory.makeCharmRecipe(
                registrant=self.person, owner=self.person
            )
            recipe.store_name = self.factory.getUniqueUnicode()
            recipe.store_upload = True
            # CharmRecipe.can_upload_to_store only checks whether
            # "exchanged_encrypted" is present, so don't bother setting up
            # encryption keys here.
            recipe.store_secrets = {
                "exchanged_encrypted": Macaroon().serialize()
            }
        builds_url = "%s/builds" % api_url(recipe)
        logout()

        def make_build():
            with person_logged_in(self.person):
                builder = self.factory.makeBuilder()
                build_request = self.factory.makeCharmRecipeBuildRequest(
                    recipe=recipe
                )
                build = recipe.requestBuild(build_request, das)
                with dbuser(config.builddmaster.dbuser):
                    build.updateStatus(
                        BuildStatus.BUILDING, date_started=recipe.date_created
                    )
                    build.updateStatus(
                        BuildStatus.FULLYBUILT,
                        builder=builder,
                        date_finished=(
                            recipe.date_created + timedelta(minutes=10)
                        ),
                    )
                return build

        def get_builds():
            response = self.webservice.get(builds_url)
            self.assertEqual(200, response.status)
            return response

        recorder1, recorder2 = record_two_runs(get_builds, make_build, 2)
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))
