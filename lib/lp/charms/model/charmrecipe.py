# Copyright 2021-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Charm recipes."""

__all__ = [
    "CharmRecipe",
    "get_charm_recipe_privacy_filter",
    "is_unified_format",
]

import base64
from datetime import datetime, timedelta, timezone
from operator import attrgetter, itemgetter
from urllib.parse import urlsplit

import yaml
from lazr.lifecycle.event import ObjectCreatedEvent
from pymacaroons import Macaroon
from pymacaroons.serializers import JsonSerializer
from storm.databases.postgres import JSON
from storm.expr import Cast, Coalesce, Except, Is
from storm.locals import (
    And,
    Bool,
    DateTime,
    Desc,
    Int,
    Join,
    Not,
    Or,
    Reference,
    Select,
    Store,
    Unicode,
)
from zope.component import getUtility
from zope.event import notify
from zope.interface import implementer
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import (
    FREE_INFORMATION_TYPES,
    PUBLIC_INFORMATION_TYPES,
    InformationType,
)
from lp.app.errors import IncompatibleArguments
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.buildmaster.builderproxy import FetchServicePolicy
from lp.buildmaster.enums import BuildStatus
from lp.buildmaster.interfaces.buildqueue import IBuildQueueSet
from lp.buildmaster.model.builder import Builder
from lp.buildmaster.model.buildfarmjob import BuildFarmJob
from lp.buildmaster.model.buildqueue import BuildQueue
from lp.charms.adapters.buildarch import determine_instances_to_build
from lp.charms.interfaces.charmbase import ICharmBaseSet, NoSuchCharmBase
from lp.charms.interfaces.charmhubclient import ICharmhubClient
from lp.charms.interfaces.charmrecipe import (
    CHARM_RECIPE_ALLOW_CREATE,
    CHARM_RECIPE_BUILD_DISTRIBUTION,
    CHARM_RECIPE_PRIVATE_FEATURE_FLAG,
    CHARM_RECIPE_USSO_FEATURE_FLAG,
    BadCharmRecipeSearchContext,
    CannotAuthorizeCharmhubUploads,
    CannotFetchCharmcraftYaml,
    CannotParseCharmcraftYaml,
    CharmRecipeBuildAlreadyPending,
    CharmRecipeBuildDisallowedArchitecture,
    CharmRecipeBuildRequestStatus,
    CharmRecipeFeatureDisabled,
    CharmRecipeNotOwner,
    CharmRecipePrivacyMismatch,
    CharmRecipePrivateFeatureDisabled,
    DuplicateCharmRecipeName,
    ICharmRecipe,
    ICharmRecipeBuildRequest,
    ICharmRecipeSet,
    MissingCharmcraftYaml,
    NoSourceForCharmRecipe,
    NoSuchCharmRecipe,
)
from lp.charms.interfaces.charmrecipebuild import ICharmRecipeBuildSet
from lp.charms.interfaces.charmrecipejob import (
    ICharmRecipeRequestBuildsJobSource,
)
from lp.charms.model.charmbase import CharmBase
from lp.charms.model.charmrecipebuild import CharmRecipeBuild
from lp.charms.model.charmrecipejob import CharmRecipeJob, CharmRecipeJobType
from lp.code.errors import GitRepositoryBlobNotFound, GitRepositoryScanFault
from lp.code.interfaces.gitcollection import (
    IAllGitRepositories,
    IGitCollection,
)
from lp.code.interfaces.gitref import IGitRef, IGitRefRemoteSet
from lp.code.interfaces.gitrepository import IGitRepository
from lp.code.model.gitcollection import GenericGitCollection
from lp.code.model.gitref import GitRef
from lp.code.model.gitrepository import GitRepository
from lp.code.model.reciperegistry import recipe_registry
from lp.registry.errors import PrivatePersonLinkageError
from lp.registry.interfaces.distribution import IDistributionSet
from lp.registry.interfaces.person import (
    IPerson,
    IPersonSet,
    validate_public_person,
)
from lp.registry.interfaces.product import IProduct
from lp.registry.model.distribution import Distribution
from lp.registry.model.distroseries import DistroSeries
from lp.registry.model.product import Product
from lp.registry.model.series import ACTIVE_STATUSES
from lp.services.config import config
from lp.services.crypto.interfaces import IEncryptedContainer
from lp.services.crypto.model import NaClEncryptedContainerBase
from lp.services.database.bulk import load_related
from lp.services.database.constants import DEFAULT, UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.stormbase import StormBase
from lp.services.database.stormexpr import Greatest, JSONExtract, NullsLast
from lp.services.features import getFeatureFlag
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.model.job import Job
from lp.services.librarian.model import LibraryFileAlias
from lp.services.openid.adapters.openid import CurrentOpenIDEndPoint
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.services.webapp.candid import extract_candid_caveat
from lp.services.webhooks.interfaces import IWebhookSet
from lp.services.webhooks.model import WebhookTargetMixin
from lp.soyuz.model.distroarchseries import DistroArchSeries, PocketChroot


def is_unified_format(configuration_as_dict):
    """CharmCraft's configuration file exits in two versions.

    The new version, referred to as `unified`, was introduced to unify the
    syntax across the various craft tools, see the corresponding spec:
    https://docs.google.com/document/d/1HrGw_MpfJoMpoGRw74Qk3eP7cl7viwcmoPe9nICag2U/edit
    (access restricted to Canonical employees)

    The unified format is not backwards compatible.

    Relevant for the detection are the following differences compared to the
    old version:
    - `bases` was renamed to `base`
    - a `build-base` key was introduced
    - a `platforms` key was introduced
    """
    new_keys = ["base", "build-base", "platforms"]
    for key in new_keys:
        if key in configuration_as_dict.keys():
            return True
    return False


def charm_recipe_modified(recipe, event):
    """Update the date_last_modified property when a charm recipe is modified.

    This method is registered as a subscriber to `IObjectModifiedEvent`
    events on charm recipes.
    """
    removeSecurityProxy(recipe).date_last_modified = UTC_NOW


@implementer(ICharmRecipeBuildRequest)
class CharmRecipeBuildRequest:
    """See `ICharmRecipeBuildRequest`.

    This is not directly backed by a database table; instead, it is a
    webservice-friendly view of an asynchronous build request.
    """

    def __init__(self, recipe, id):
        self.recipe = recipe
        self.id = id

    @classmethod
    def fromJob(cls, job):
        """See `ICharmRecipeBuildRequest`."""
        request = cls(job.recipe, job.job_id)
        get_property_cache(request)._job = job
        return request

    @cachedproperty
    def _job(self):
        job_source = getUtility(ICharmRecipeRequestBuildsJobSource)
        return job_source.getByRecipeAndID(self.recipe, self.id)

    @property
    def date_requested(self):
        """See `ICharmRecipeBuildRequest`."""
        return self._job.date_created

    @property
    def date_finished(self):
        """See `ICharmRecipeBuildRequest`."""
        return self._job.date_finished

    @property
    def status(self):
        """See `ICharmRecipeBuildRequest`."""
        status_map = {
            JobStatus.WAITING: CharmRecipeBuildRequestStatus.PENDING,
            JobStatus.RUNNING: CharmRecipeBuildRequestStatus.PENDING,
            JobStatus.COMPLETED: CharmRecipeBuildRequestStatus.COMPLETED,
            JobStatus.FAILED: CharmRecipeBuildRequestStatus.FAILED,
            JobStatus.SUSPENDED: CharmRecipeBuildRequestStatus.PENDING,
        }
        return status_map[self._job.job.status]

    @property
    def error_message(self):
        """See `ICharmRecipeBuildRequest`."""
        return self._job.error_message

    @property
    def builds(self):
        """See `ICharmRecipeBuildRequest`."""
        return self._job.builds

    @property
    def requester(self):
        """See `ICharmRecipeBuildRequest`."""
        return self._job.requester

    @property
    def channels(self):
        """See `ICharmRecipeBuildRequest`."""
        return self._job.channels

    @property
    def architectures(self):
        """See `ICharmRecipeBuildRequest`."""
        return self._job.architectures


@implementer(ICharmRecipe)
class CharmRecipe(StormBase, WebhookTargetMixin):
    """See `ICharmRecipe`."""

    __storm_table__ = "CharmRecipe"

    id = Int(primary=True)

    date_created = DateTime(
        name="date_created", tzinfo=timezone.utc, allow_none=False
    )
    date_last_modified = DateTime(
        name="date_last_modified", tzinfo=timezone.utc, allow_none=False
    )

    registrant_id = Int(name="registrant", allow_none=False)
    registrant = Reference(registrant_id, "Person.id")

    def _validate_owner(self, attr, value):
        if not self.private:
            try:
                validate_public_person(self, attr, value)
            except PrivatePersonLinkageError:
                raise CharmRecipePrivacyMismatch(
                    "A public charm recipe cannot have a private owner."
                )
        return value

    owner_id = Int(name="owner", allow_none=False, validator=_validate_owner)
    owner = Reference(owner_id, "Person.id")

    project_id = Int(name="project", allow_none=False)
    project = Reference(project_id, "Product.id")

    name = Unicode(name="name", allow_none=False)

    description = Unicode(name="description", allow_none=True)

    def _validate_git_repository(self, attr, value):
        if not self.private and value is not None:
            if IStore(GitRepository).get(GitRepository, value).private:
                raise CharmRecipePrivacyMismatch(
                    "A public charm recipe cannot have a private repository."
                )
        return value

    git_repository_id = Int(
        name="git_repository",
        allow_none=True,
        validator=_validate_git_repository,
    )
    git_repository = Reference(git_repository_id, "GitRepository.id")

    git_repository_url = Unicode(name="git_repository_url", allow_none=True)

    git_path = Unicode(name="git_path", allow_none=True)

    build_path = Unicode(name="build_path", allow_none=True)

    require_virtualized = Bool(name="require_virtualized")

    def _valid_information_type(self, attr, value):
        if not getUtility(ICharmRecipeSet).isValidInformationType(
            value, self.owner, self.git_ref
        ):
            raise CharmRecipePrivacyMismatch
        return value

    information_type = DBEnum(
        enum=InformationType,
        default=InformationType.PUBLIC,
        name="information_type",
        validator=_valid_information_type,
        allow_none=False,
    )

    auto_build = Bool(name="auto_build", allow_none=False)

    auto_build_channels = JSON("auto_build_channels", allow_none=True)

    is_stale = Bool(name="is_stale", allow_none=False)

    store_upload = Bool(name="store_upload", allow_none=False)

    store_name = Unicode(name="store_name", allow_none=True)

    store_secrets = JSON("store_secrets", allow_none=True)

    _store_channels = JSON("store_channels", allow_none=True)

    use_fetch_service = Bool(name="use_fetch_service", allow_none=False)

    fetch_service_policy = DBEnum(
        enum=FetchServicePolicy,
        default=FetchServicePolicy.STRICT,
        name="fetch_service_policy",
        allow_none=True,
    )

    def __init__(
        self,
        registrant,
        owner,
        project,
        name,
        description=None,
        git_ref=None,
        build_path=None,
        require_virtualized=True,
        information_type=InformationType.PUBLIC,
        auto_build=False,
        auto_build_channels=None,
        store_upload=False,
        store_name=None,
        store_secrets=None,
        store_channels=None,
        date_created=DEFAULT,
        use_fetch_service=False,
        fetch_service_policy=FetchServicePolicy.STRICT,
    ):
        """Construct a `CharmRecipe`."""
        if not getFeatureFlag(CHARM_RECIPE_ALLOW_CREATE):
            raise CharmRecipeFeatureDisabled()
        super().__init__()

        # Set this first for use by other validators.
        self.information_type = information_type

        self.date_created = date_created
        self.date_last_modified = date_created
        self.registrant = registrant
        self.owner = owner
        self.project = project
        self.name = name
        self.description = description
        self.git_ref = git_ref
        self.build_path = build_path
        self.require_virtualized = require_virtualized
        self.auto_build = auto_build
        self.auto_build_channels = auto_build_channels
        self.store_upload = store_upload
        self.store_name = store_name
        self.store_secrets = store_secrets
        self.store_channels = store_channels
        self.use_fetch_service = use_fetch_service
        self.fetch_service_policy = fetch_service_policy

    def __repr__(self):
        return "<CharmRecipe ~%s/%s/+charm/%s>" % (
            self.owner.name,
            self.project.name,
            self.name,
        )

    @property
    def private(self):
        """See `ICharmRecipe`."""
        return self.information_type not in PUBLIC_INFORMATION_TYPES

    @cachedproperty
    def _git_ref(self):
        if self.git_repository is not None:
            return self.git_repository.getRefByPath(self.git_path)
        elif self.git_repository_url is not None:
            return getUtility(IGitRefRemoteSet).new(
                self.git_repository_url, self.git_path
            )
        else:
            return None

    @property
    def git_ref(self):
        """See `ICharmRecipe`."""
        return self._git_ref

    @git_ref.setter
    def git_ref(self, value):
        """See `ICharmRecipe`."""
        if value is not None:
            self.git_repository = value.repository
            self.git_repository_url = value.repository_url
            self.git_path = value.path
        else:
            self.git_repository = None
            self.git_repository_url = None
            self.git_path = None
        get_property_cache(self)._git_ref = value

    @property
    def source(self):
        """See `ICharmRecipe`."""
        return self.git_ref

    @property
    def store_channels(self):
        """See `ICharmRecipe`."""
        return self._store_channels or []

    @store_channels.setter
    def store_channels(self, value):
        """See `ICharmRecipe`."""
        self._store_channels = value or None

    @cachedproperty
    def _default_distribution(self):
        """See `ICharmRecipe`."""
        # Use the default distribution set by this feature rule, or Ubuntu
        # if none is set.
        distro_name = getFeatureFlag(CHARM_RECIPE_BUILD_DISTRIBUTION)
        if not distro_name:
            return getUtility(ILaunchpadCelebrities).ubuntu
        distro = getUtility(IDistributionSet).getByName(distro_name)
        if not distro:
            raise ValueError(
                "'%s' is not a valid value for feature rule '%s'"
                % (distro_name, CHARM_RECIPE_BUILD_DISTRIBUTION)
            )
        return distro

    @cachedproperty
    def _default_distro_series(self):
        """See `ICharmRecipe`."""
        # Use the series set by this feature rule, or the current series of
        # the default distribution if the feature rule is not set.
        series_name = getFeatureFlag(
            "charm.default_build_series.%s" % self._default_distribution.name
        )
        if series_name:
            return self._default_distribution.getSeries(series_name)
        else:
            return self._default_distribution.currentseries

    def getAllowedInformationTypes(self, user):
        """See `ICharmRecipe`."""
        # XXX cjwatson 2021-05-26: Only allow free information types until
        # we have more privacy infrastructure in place.
        return FREE_INFORMATION_TYPES

    def visibleByUser(self, user):
        """See `ICharmRecipe`."""
        if self.information_type in PUBLIC_INFORMATION_TYPES:
            return True
        if user is None:
            return False
        return (
            not IStore(CharmRecipe)
            .find(
                CharmRecipe,
                CharmRecipe.id == self.id,
                get_charm_recipe_privacy_filter(user),
            )
            .is_empty()
        )

    def _isBuildableArchitectureAllowed(self, das, charm_base=None):
        """Check whether we may build for a buildable `DistroArchSeries`.

        The caller is assumed to have already checked that a suitable chroot
        is available (either directly or via
        `DistroSeries.buildable_architectures`).
        """
        return (
            das.enabled
            and (
                das.processor.supports_virtualized
                or not self.require_virtualized
            )
            and (charm_base is None or das.processor in charm_base.processors)
        )

    def _isArchitectureAllowed(self, das, charm_base=None):
        """Check whether we may build for a `DistroArchSeries`."""
        return (
            das.getChroot() is not None
            and self._isBuildableArchitectureAllowed(
                das, charm_base=charm_base
            )
        )

    def getAllowedArchitectures(self):
        """See `ICharmRecipe`."""
        store = Store.of(self)
        origin = [
            DistroArchSeries,
            Join(
                DistroSeries, DistroArchSeries.distroseries == DistroSeries.id
            ),
            Join(Distribution, DistroSeries.distribution == Distribution.id),
            Join(
                PocketChroot,
                PocketChroot.distroarchseries == DistroArchSeries.id,
            ),
            Join(LibraryFileAlias, PocketChroot.chroot == LibraryFileAlias.id),
        ]
        # Preload DistroSeries and Distribution, since we'll need those in
        # determine_architectures_to_build.
        results = (
            store.using(*origin)
            .find(
                (DistroArchSeries, DistroSeries, Distribution),
                DistroSeries.status.is_in(ACTIVE_STATUSES),
            )
            .config(distinct=True)
        )
        all_buildable_dases = DecoratedResultSet(results, itemgetter(0))
        charm_bases = {
            charm_base.distro_series_id: charm_base
            for charm_base in store.find(
                CharmBase,
                CharmBase.distro_series_id.is_in(
                    {das.distroseries_id for das in all_buildable_dases}
                ),
            )
        }
        return [
            das
            for das in all_buildable_dases
            if self._isBuildableArchitectureAllowed(
                das, charm_base=charm_bases.get(das.distroseries_id)
            )
        ]

    def _checkRequestBuild(self, requester):
        """May `requester` request builds of this charm recipe?"""
        if not requester.inTeam(self.owner):
            raise CharmRecipeNotOwner(
                "%s cannot create charm recipe builds owned by %s."
                % (requester.display_name, self.owner.display_name)
            )

    def requestBuild(
        self,
        build_request,
        distro_arch_series,
        charm_base=None,
        channels=None,
        craft_platform=None,
    ):
        """Request a single build of this charm recipe.

        This method is for internal use; external callers should use
        `requestBuilds` instead.

        :param build_request: The `ICharmRecipeBuildRequest` job being
            processed.
        :param distro_arch_series: The architecture to build for.
        :param channels: A dictionary mapping snap names to channels to use
            for this build.
        :param craft_platform: The platform to build for.
        :return: `ICharmRecipeBuild`.
        """
        self._checkRequestBuild(build_request.requester)
        if not self._isArchitectureAllowed(
            distro_arch_series, charm_base=charm_base
        ):
            raise CharmRecipeBuildDisallowedArchitecture(distro_arch_series)

        if not channels:
            channels_clause = Or(
                CharmRecipeBuild.channels == None,
                CharmRecipeBuild.channels == {},
            )
        else:
            channels_clause = CharmRecipeBuild.channels == channels
        pending = IStore(self).find(
            CharmRecipeBuild,
            CharmRecipeBuild.recipe == self,
            CharmRecipeBuild.distro_arch_series == distro_arch_series,
            channels_clause,
            CharmRecipeBuild.craft_platform == craft_platform,
            CharmRecipeBuild.status == BuildStatus.NEEDSBUILD,
        )
        if pending.any() is not None:
            raise CharmRecipeBuildAlreadyPending

        build = getUtility(ICharmRecipeBuildSet).new(
            build_request,
            self,
            distro_arch_series,
            channels=channels,
            craft_platform=craft_platform,
        )
        build.queueBuild()
        notify(ObjectCreatedEvent(build, user=build_request.requester))
        return build

    def requestBuilds(self, requester, channels=None, architectures=None):
        """See `ICharmRecipe`."""
        self._checkRequestBuild(requester)
        job = getUtility(ICharmRecipeRequestBuildsJobSource).create(
            self, requester, channels=channels, architectures=architectures
        )
        return self.getBuildRequest(job.job_id)

    def requestBuildsFromJob(
        self,
        build_request,
        channels=None,
        architectures=None,
        allow_failures=False,
        logger=None,
    ):
        """See `ICharmRecipe`."""
        try:
            try:
                charmcraft_data = removeSecurityProxy(
                    getUtility(ICharmRecipeSet).getCharmcraftYaml(self)
                )
            except MissingCharmcraftYaml:
                # charmcraft doesn't currently require charmcraft.yaml, and
                # we have reasonable defaults without it.
                charmcraft_data = {}

            # Sort by (Distribution.id, DistroSeries.id, Processor.id) for
            # determinism.  This is chosen to be a similar order as in
            # BinaryPackageBuildSet.createForSource, to minimize confusion.
            supported_arches = [
                das
                for das in sorted(
                    self.getAllowedArchitectures(),
                    key=attrgetter(
                        "distroseries.distribution.id",
                        "distroseries.id",
                        "processor.id",
                    ),
                )
                if (
                    architectures is None
                    or das.architecturetag in architectures
                )
            ]
            instances_to_build = determine_instances_to_build(
                charmcraft_data, supported_arches, self._default_distro_series
            )
        except Exception as e:
            if not allow_failures:
                raise
            elif logger is not None:
                logger.exception(
                    " - %s/%s/%s: %s",
                    self.owner.name,
                    self.project.name,
                    self.name,
                    e,
                )

        builds = []
        for info, das in instances_to_build:
            try:
                charm_base = getUtility(ICharmBaseSet).getByDistroSeries(
                    das.distroseries
                )
            except NoSuchCharmBase:
                charm_base = None
            if charm_base is not None:
                arch_channels = dict(charm_base.build_snap_channels)
                channels_by_arch = arch_channels.pop("_byarch", {})
                if das.architecturetag in channels_by_arch:
                    arch_channels.update(channels_by_arch[das.architecturetag])
                if channels is not None:
                    arch_channels.update(channels)
            else:
                arch_channels = channels
            try:
                platform_name = info.platform if info is not None else None
                build = self.requestBuild(
                    build_request,
                    das,
                    channels=arch_channels,
                    craft_platform=platform_name,
                )
                if logger is not None:
                    logger.debug(
                        " - %s/%s/%s %s/%s/%s/%s: Build requested.",
                        self.owner.name,
                        self.project.name,
                        self.name,
                        das.distroseries.distribution.name,
                        das.distroseries.name,
                        das.architecturetag,
                        platform_name,
                    )
                builds.append(build)
            except CharmRecipeBuildAlreadyPending:
                pass
            except Exception as e:
                if not allow_failures:
                    raise
                elif logger is not None:
                    logger.exception(
                        " - %s/%s/%s %s/%s/%s/%s: %s",
                        self.owner.name,
                        self.project.name,
                        self.name,
                        das.distroseries.distribution.name,
                        das.distroseries.name,
                        das.architecturetag,
                        platform_name,
                        e,
                    )
        return builds

    def requestAutoBuilds(self, logger=None):
        """See `ICharmRecipe`."""
        self.is_stale = False
        if logger is not None:
            logger.debug(
                "Scheduling builds of charm recipe %s/%s/%s",
                self.owner.name,
                self.project.name,
                self.name,
            )
        return self.requestBuilds(
            requester=self.owner, channels=self.auto_build_channels
        )

    def getBuildRequest(self, job_id):
        """See `ICharmRecipe`."""
        return CharmRecipeBuildRequest(self, job_id)

    @property
    def pending_build_requests(self):
        """See `ICharmRecipe`."""
        job_source = getUtility(ICharmRecipeRequestBuildsJobSource)
        # The returned jobs are ordered by descending ID.
        jobs = job_source.findByRecipe(
            self, statuses=(JobStatus.WAITING, JobStatus.RUNNING)
        )
        return DecoratedResultSet(
            jobs, result_decorator=CharmRecipeBuildRequest.fromJob
        )

    @property
    def failed_build_requests(self):
        """See `ICharmRecipe`."""
        job_source = getUtility(ICharmRecipeRequestBuildsJobSource)
        # The returned jobs are ordered by descending ID.
        jobs = job_source.findByRecipe(self, statuses=(JobStatus.FAILED,))
        return DecoratedResultSet(
            jobs, result_decorator=CharmRecipeBuildRequest.fromJob
        )

    def _getBuilds(self, filter_term, order_by):
        """The actual query to get the builds."""
        query_args = [
            CharmRecipeBuild.recipe == self,
        ]
        if filter_term is not None:
            query_args.append(filter_term)
        result = Store.of(self).find(CharmRecipeBuild, *query_args)
        result.order_by(order_by)

        def eager_load(rows):
            getUtility(ICharmRecipeBuildSet).preloadBuildsData(rows)
            getUtility(IBuildQueueSet).preloadForBuildFarmJobs(rows)
            load_related(Builder, rows, ["builder_id"])

        return DecoratedResultSet(result, pre_iter_hook=eager_load)

    @property
    def builds(self):
        """See `ICharmRecipe`."""
        order_by = (
            NullsLast(
                Desc(
                    Greatest(
                        CharmRecipeBuild.date_started,
                        CharmRecipeBuild.date_finished,
                    )
                )
            ),
            Desc(CharmRecipeBuild.date_created),
            Desc(CharmRecipeBuild.id),
        )
        return self._getBuilds(None, order_by)

    @property
    def _pending_states(self):
        """All the build states we consider pending (non-final)."""
        return [
            BuildStatus.NEEDSBUILD,
            BuildStatus.BUILDING,
            BuildStatus.GATHERING,
            BuildStatus.UPLOADING,
            BuildStatus.CANCELLING,
        ]

    @property
    def completed_builds(self):
        """See `ICharmRecipe`."""
        filter_term = Not(CharmRecipeBuild.status.is_in(self._pending_states))
        order_by = (
            NullsLast(
                Desc(
                    Greatest(
                        CharmRecipeBuild.date_started,
                        CharmRecipeBuild.date_finished,
                    )
                )
            ),
            Desc(CharmRecipeBuild.id),
        )
        return self._getBuilds(filter_term, order_by)

    @property
    def pending_builds(self):
        """See `ICharmRecipe`."""
        filter_term = CharmRecipeBuild.status.is_in(self._pending_states)
        # We want to order by date_created but this is the same as ordering
        # by id (since id increases monotonically) and is less expensive.
        order_by = Desc(CharmRecipeBuild.id)
        return self._getBuilds(filter_term, order_by)

    @staticmethod
    def extractSSOCaveats(macaroon):
        locations = [
            urlsplit(root).netloc
            for root in CurrentOpenIDEndPoint.getAllRootURLs()
        ]
        return [
            caveat
            for caveat in macaroon.third_party_caveats()
            if caveat.location in locations
        ]

    def beginAuthorization(self):
        """See `ICharmRecipe`."""
        if self.store_name is None:
            raise CannotAuthorizeCharmhubUploads(
                "Cannot authorize uploads of a charm recipe with no store "
                "name."
            )
        use_usso = bool(getFeatureFlag(CHARM_RECIPE_USSO_FEATURE_FLAG))
        charmhub_client = getUtility(ICharmhubClient)
        root_macaroon_raw = charmhub_client.requestPackageUploadPermission(
            self.store_name, use_usso=use_usso
        )
        if use_usso:
            # The Ubuntu SSO branch mirrors how snap recipes authorize
            # uploads to the snap store, which also discharge an Ubuntu SSO
            # caveat on a root macaroon from the store.
            sso_caveats = self.extractSSOCaveats(
                Macaroon.deserialize(root_macaroon_raw)
            )
            # We must have exactly one SSO caveat; more than one should never
            # be required and could be an attempt to substitute weaker
            # caveats.
            if not sso_caveats:
                raise CannotAuthorizeCharmhubUploads(
                    "Macaroon has no SSO caveats."
                )
            elif len(sso_caveats) > 1:
                raise CannotAuthorizeCharmhubUploads(
                    "Macaroon has multiple SSO caveats."
                )
            self.store_secrets = {
                "root": root_macaroon_raw,
                "auth_method": "usso",
            }
            return sso_caveats[0].caveat_id
        else:
            # Check that the macaroon has exactly one Candid caveat.
            extract_candid_caveat(
                Macaroon.deserialize(root_macaroon_raw, JsonSerializer())
            )
            self.store_secrets = {
                "root": root_macaroon_raw,
                "auth_method": "candid",
            }
            return root_macaroon_raw

    def completeAuthorization(self, unbound_discharge_macaroon_raw):
        """See `ICharmRecipe`."""
        if self.store_secrets is None or "root" not in self.store_secrets:
            raise CannotAuthorizeCharmhubUploads(
                "beginAuthorization must be called before "
                "completeAuthorization."
            )
        # Use the method recorded when the authorization began, so that a
        # change to the feature flag partway through does not break an
        # authorization that is already in progress.
        use_usso = self.store_secrets.get("auth_method") == "usso"
        try:
            if use_usso:
                Macaroon.deserialize(unbound_discharge_macaroon_raw)
            else:
                Macaroon.deserialize(
                    unbound_discharge_macaroon_raw, JsonSerializer()
                )
        except Exception:
            raise CannotAuthorizeCharmhubUploads(
                "Discharge macaroon is invalid."
            )
        charmhub_client = getUtility(ICharmhubClient)
        description = "{} for {}".format(
            self.store_name, config.vhost.mainsite.hostname
        )
        exchanged_macaroon_raw = charmhub_client.exchangeMacaroons(
            self.store_secrets["root"],
            unbound_discharge_macaroon_raw,
            use_usso=use_usso,
            description=description,
        )
        container = getUtility(IEncryptedContainer, "charmhub-secrets")
        assert container.can_encrypt
        self.store_secrets["exchanged_encrypted"] = removeSecurityProxy(
            container.encrypt(exchanged_macaroon_raw.encode())
        )
        self.store_secrets.pop("root", None)
        self.store_secrets.pop("auth_method", None)

    @property
    def can_upload_to_store(self):
        return (
            config.charms.charmhub_url is not None
            and self.store_name is not None
            and self.store_secrets is not None
            and "exchanged_encrypted" in self.store_secrets
        )

    @property
    def valid_webhook_event_types(self):
        return ["charm-recipe:build:0.1"]

    def destroySelf(self):
        """See `ICharmRecipe`."""
        store = IStore(self)
        # Remove build jobs.  There won't be many queued builds, so we can
        # afford to do this the safe but slow way via BuildQueue.destroySelf
        # rather than in bulk.
        buildqueue_records = store.find(
            BuildQueue,
            BuildQueue._build_farm_job_id
            == CharmRecipeBuild.build_farm_job_id,
            CharmRecipeBuild.recipe == self,
        )
        for buildqueue_record in buildqueue_records:
            buildqueue_record.destroySelf()
        build_farm_job_ids = list(
            store.find(
                CharmRecipeBuild.build_farm_job_id,
                CharmRecipeBuild.recipe == self,
            )
        )
        store.execute(
            """
            DELETE FROM CharmFile
            USING CharmRecipeBuild
            WHERE
                CharmFile.build = CharmRecipeBuild.id AND
                CharmRecipeBuild.recipe = ?
            """,
            (self.id,),
        )
        store.execute(
            """
            DELETE FROM CharmRecipeBuildJob
            USING CharmRecipeBuild
            WHERE
                CharmRecipeBuildJob.build = CharmRecipeBuild.id AND
                CharmRecipeBuild.recipe = ?
            """,
            (self.id,),
        )
        store.find(CharmRecipeBuild, CharmRecipeBuild.recipe == self).remove()
        affected_jobs = Select(
            [CharmRecipeJob.job_id],
            And(CharmRecipeJob.job == Job.id, CharmRecipeJob.recipe == self),
        )
        store.find(Job, Job.id.is_in(affected_jobs)).remove()
        getUtility(IWebhookSet).delete(self.webhooks)
        store.remove(self)
        store.find(
            BuildFarmJob, BuildFarmJob.id.is_in(build_farm_job_ids)
        ).remove()


@recipe_registry.register_recipe_type(
    ICharmRecipeSet, "Some charm recipes build from this repository."
)
@implementer(ICharmRecipeSet)
class CharmRecipeSet:
    """See `ICharmRecipeSet`."""

    def new(
        self,
        registrant,
        owner,
        project,
        name,
        description=None,
        git_repository=None,
        git_repository_url=None,
        git_path=None,
        git_ref=None,
        build_path=None,
        require_virtualized=True,
        information_type=InformationType.PUBLIC,
        auto_build=False,
        auto_build_channels=None,
        store_upload=False,
        store_name=None,
        store_secrets=None,
        store_channels=None,
        date_created=DEFAULT,
        use_fetch_service=False,
        fetch_service_policy=FetchServicePolicy.STRICT,
    ):
        """See `ICharmRecipeSet`."""
        if not registrant.inTeam(owner):
            if owner.is_team:
                raise CharmRecipeNotOwner(
                    "%s is not a member of %s."
                    % (registrant.displayname, owner.displayname)
                )
            else:
                raise CharmRecipeNotOwner(
                    "%s cannot create charm recipes owned by %s."
                    % (registrant.displayname, owner.displayname)
                )
        if (
            sum(
                [
                    git_repository is not None,
                    git_repository_url is not None,
                    git_ref is not None,
                ]
            )
            > 1
        ):
            raise IncompatibleArguments(
                "You cannot specify more than one of 'git_repository', "
                "'git_repository_url', and 'git_ref'."
            )
        if (git_repository is None and git_repository_url is None) != (
            git_path is None
        ):
            raise IncompatibleArguments(
                "You must specify both or neither of "
                "'git_repository'/'git_repository_url' and 'git_path'."
            )
        if git_repository is not None:
            git_ref = git_repository.getRefByPath(git_path)
        elif git_repository_url is not None:
            git_ref = getUtility(IGitRefRemoteSet).new(
                git_repository_url, git_path
            )

        if git_ref is None:
            raise NoSourceForCharmRecipe
        if self.exists(owner, project, name):
            raise DuplicateCharmRecipeName

        # The relevant validators will do their own checks as well, but we
        # do a single up-front check here in order to avoid an
        # IntegrityError due to exceptions being raised during object
        # creation and to ensure that everything relevant is in the Storm
        # cache.
        if not self.isValidInformationType(information_type, owner, git_ref):
            raise CharmRecipePrivacyMismatch

        store = IPrimaryStore(CharmRecipe)
        recipe = CharmRecipe(
            registrant,
            owner,
            project,
            name,
            description=description,
            git_ref=git_ref,
            build_path=build_path,
            require_virtualized=require_virtualized,
            information_type=information_type,
            auto_build=auto_build,
            auto_build_channels=auto_build_channels,
            store_upload=store_upload,
            store_name=store_name,
            store_secrets=store_secrets,
            store_channels=store_channels,
            date_created=date_created,
            use_fetch_service=use_fetch_service,
            fetch_service_policy=fetch_service_policy,
        )
        store.add(recipe)

        return recipe

    def _getByName(self, owner, project, name):
        return (
            IStore(CharmRecipe)
            .find(CharmRecipe, owner=owner, project=project, name=name)
            .one()
        )

    def exists(self, owner, project, name):
        """See `ICharmRecipeSet`."""
        return self._getByName(owner, project, name) is not None

    def getByName(self, owner, project, name):
        """See `ICharmRecipeSet`."""
        recipe = self._getByName(owner, project, name)
        if recipe is None:
            raise NoSuchCharmRecipe(name)
        return recipe

    def _getRecipesFromCollection(
        self, collection, owner=None, visible_by_user=None
    ):
        id_column = CharmRecipe.git_repository_id
        ids = collection.getRepositoryIds()
        expressions = [id_column.is_in(ids._get_select())]
        if owner is not None:
            expressions.append(CharmRecipe.owner == owner)
        expressions.append(get_charm_recipe_privacy_filter(visible_by_user))
        return IStore(CharmRecipe).find(CharmRecipe, *expressions)

    def findByOwner(self, owner):
        """See `ICharmRecipeSet`."""
        return IStore(CharmRecipe).find(CharmRecipe, owner=owner)

    def findByPerson(self, person, visible_by_user=None):
        """See `ICharmRecipeSet`."""

        def _getRecipes(collection):
            collection = collection.visibleByUser(visible_by_user)
            owned = self._getRecipesFromCollection(
                collection.ownedBy(person), visible_by_user=visible_by_user
            )
            packaged = self._getRecipesFromCollection(
                collection, owner=person, visible_by_user=visible_by_user
            )
            return owned.union(packaged)

        git_collection = removeSecurityProxy(getUtility(IAllGitRepositories))
        git_recipes = _getRecipes(git_collection)
        git_url_recipes = IStore(CharmRecipe).find(
            CharmRecipe,
            CharmRecipe.owner == person,
            CharmRecipe.git_repository_url != None,
        )
        return git_recipes.union(git_url_recipes)

    def findByProject(self, project, visible_by_user=None):
        """See `ICharmRecipeSet`."""

        def _getRecipes(collection):
            return self._getRecipesFromCollection(
                collection.visibleByUser(visible_by_user),
                visible_by_user=visible_by_user,
            )

        recipes_for_project = IStore(CharmRecipe).find(
            CharmRecipe,
            CharmRecipe.project == project,
            get_charm_recipe_privacy_filter(visible_by_user),
        )
        git_collection = removeSecurityProxy(IGitCollection(project))
        return recipes_for_project.union(_getRecipes(git_collection))

    def findByGitRepository(
        self,
        repository,
        paths=None,
        visible_by_user=None,
        check_permissions=True,
    ):
        """See `ICharmRecipeSet`."""
        clauses = [CharmRecipe.git_repository == repository]
        if paths is not None:
            clauses.append(CharmRecipe.git_path.is_in(paths))
        if check_permissions:
            clauses.append(get_charm_recipe_privacy_filter(visible_by_user))
        return IStore(CharmRecipe).find(CharmRecipe, *clauses)

    def findByGitRef(self, ref, visible_by_user=None):
        """See `ICharmRecipeSet`."""
        return IStore(CharmRecipe).find(
            CharmRecipe,
            CharmRecipe.git_repository == ref.repository,
            CharmRecipe.git_path == ref.path,
            get_charm_recipe_privacy_filter(visible_by_user),
        )

    def findByContext(self, context, visible_by_user=None, order_by_date=True):
        """See `ICharmRecipeSet`."""
        if IPerson.providedBy(context):
            recipes = self.findByPerson(
                context, visible_by_user=visible_by_user
            )
        elif IProduct.providedBy(context):
            recipes = self.findByProject(
                context, visible_by_user=visible_by_user
            )
        elif IGitRepository.providedBy(context):
            recipes = self.findByGitRepository(
                context, visible_by_user=visible_by_user
            )
        elif IGitRef.providedBy(context):
            recipes = self.findByGitRef(
                context, visible_by_user=visible_by_user
            )
        else:
            raise BadCharmRecipeSearchContext(context)
        if order_by_date:
            recipes = recipes.order_by(Desc(CharmRecipe.date_last_modified))
        return recipes

    def isValidInformationType(self, information_type, owner, git_ref=None):
        """See `ICharmRecipeSet`."""
        private = information_type not in PUBLIC_INFORMATION_TYPES
        if private:
            # If appropriately enabled via feature flag.
            if not getFeatureFlag(CHARM_RECIPE_PRIVATE_FEATURE_FLAG):
                raise CharmRecipePrivateFeatureDisabled
            return True

        # Public charm recipes with private sources are not allowed.
        if git_ref is not None and git_ref.private:
            return False

        # Public charm recipes owned by private teams are not allowed.
        if owner is not None and owner.private:
            return False

        return True

    def preloadDataForRecipes(self, recipes, user=None):
        """See `ICharmRecipeSet`."""
        recipes = [removeSecurityProxy(recipe) for recipe in recipes]

        load_related(Product, recipes, ["project_id"])

        person_ids = set()
        for recipe in recipes:
            person_ids.add(recipe.registrant_id)
            person_ids.add(recipe.owner_id)

        repositories = load_related(
            GitRepository, recipes, ["git_repository_id"]
        )
        if repositories:
            GenericGitCollection.preloadDataForRepositories(repositories)

        git_refs = GitRef.findByReposAndPaths(
            [
                (recipe.git_repository, recipe.git_path)
                for recipe in recipes
                if recipe.git_repository
            ]
        )
        for recipe in recipes:
            git_ref = git_refs.get((recipe.git_repository, recipe.git_path))
            if git_ref is not None:
                get_property_cache(recipe)._git_ref = git_ref

        # Add repository owners to the list of pre-loaded persons.  We need
        # the target repository owner as well, since repository unique names
        # aren't trigger-maintained.
        person_ids.update(repository.owner_id for repository in repositories)

        list(
            getUtility(IPersonSet).getPrecachedPersonsFromIDs(
                person_ids, need_validity=True
            )
        )

    def getCharmcraftYaml(self, context, logger=None):
        """See `ICharmRecipeSet`."""
        if ICharmRecipe.providedBy(context):
            recipe = context
            source = context.git_ref
        else:
            recipe = None
            source = context
        if source is None:
            raise CannotFetchCharmcraftYaml("Charm source is not defined")
        try:
            path = "charmcraft.yaml"
            if recipe is not None and recipe.build_path is not None:
                path = "/".join((recipe.build_path, path))
            try:
                blob = source.getBlob(path)
            except GitRepositoryBlobNotFound:
                if logger is not None:
                    logger.exception(
                        "Cannot find charmcraft.yaml in %s", source.unique_name
                    )
                raise MissingCharmcraftYaml(source.unique_name)
        except GitRepositoryScanFault as e:
            msg = "Failed to get charmcraft.yaml from %s"
            if logger is not None:
                logger.exception(msg, source.unique_name)
            raise CannotFetchCharmcraftYaml(
                "%s: %s" % (msg % source.unique_name, e)
            )

        try:
            charmcraft_data = yaml.safe_load(blob)
        except Exception as e:
            # Don't bother logging parsing errors from user-supplied YAML.
            raise CannotParseCharmcraftYaml(
                "Cannot parse charmcraft.yaml from %s: %s"
                % (source.unique_name, e)
            )

        if not isinstance(charmcraft_data, dict):
            raise CannotParseCharmcraftYaml(
                "The top level of charmcraft.yaml from %s is not a mapping"
                % source.unique_name
            )

        return charmcraft_data

    @staticmethod
    def _findStaleRecipes():
        """Find recipes that need to be rebuilt."""
        threshold_date = datetime.now(timezone.utc) - timedelta(
            minutes=config.charms.auto_build_frequency
        )
        stale_clauses = [
            Is(CharmRecipe.is_stale, True),
            Is(CharmRecipe.auto_build, True),
        ]
        recent_clauses = [
            CharmRecipeJob.recipe_id == CharmRecipe.id,
            CharmRecipeJob.job_type == CharmRecipeJobType.REQUEST_BUILDS,
            JSONExtract(CharmRecipeJob.metadata, "channels")
            == Coalesce(
                CharmRecipe.auto_build_channels, Cast("null", "jsonb")
            ),
            CharmRecipeJob.job_id == Job.id,
            # We only want recipes that haven't had an automatic build
            # requested for them recently.
            Job.date_created >= threshold_date,
        ]
        return IStore(CharmRecipe).find(
            CharmRecipe,
            CharmRecipe.id.is_in(
                Except(
                    Select(CharmRecipe.id, where=And(*stale_clauses)),
                    Select(
                        CharmRecipe.id,
                        where=And(*(stale_clauses + recent_clauses)),
                    ),
                )
            ),
        )

    @classmethod
    def makeAutoBuilds(cls, logger=None):
        """See `ICharmRecipeSet`."""
        recipes = cls._findStaleRecipes()
        build_requests = []
        for recipe in recipes:
            try:
                build_request = recipe.requestAutoBuilds(logger=logger)
            except Exception as e:
                if logger is not None:
                    logger.exception(e)
                continue
            build_requests.append(build_request)
        return build_requests

    def detachFromGitRepository(self, repository):
        """See `ICharmRecipeSet`."""
        recipes = self.findByGitRepository(repository)
        for recipe in recipes:
            get_property_cache(recipe)._git_ref = None
        recipes.set(
            git_repository_id=None, git_path=None, date_last_modified=UTC_NOW
        )

    def empty_list(self):
        """See `ICharmRecipeSet`."""
        return []


@implementer(IEncryptedContainer)
class CharmhubSecretsEncryptedContainer(NaClEncryptedContainerBase):
    @property
    def public_key_bytes(self):
        if config.charms.charmhub_secrets_public_key is not None:
            return base64.b64decode(
                config.charms.charmhub_secrets_public_key.encode()
            )
        else:
            return None

    @property
    def private_key_bytes(self):
        if config.charms.charmhub_secrets_private_key is not None:
            return base64.b64decode(
                config.charms.charmhub_secrets_private_key.encode()
            )
        else:
            return None


def get_charm_recipe_privacy_filter(user):
    """Return a Storm query filter to find charm recipes visible to `user`."""
    public_filter = CharmRecipe.information_type.is_in(
        PUBLIC_INFORMATION_TYPES
    )

    # XXX cjwatson 2021-06-07: Flesh this out once we have more privacy
    # infrastructure.
    return [public_filter]
