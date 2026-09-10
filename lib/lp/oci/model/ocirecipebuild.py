# Copyright 2019-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""A build record for OCI Recipes."""

__all__ = [
    "OCIFile",
    "OCIRecipeBuild",
    "OCIRecipeBuildSet",
]

from datetime import timedelta, timezone

from storm.expr import LeftJoin
from storm.locals import (
    Bool,
    DateTime,
    Desc,
    Int,
    Or,
    Reference,
    Store,
    Unicode,
)
from storm.store import EmptyResultSet
from zope.component import getUtility
from zope.interface import implementer
from zope.security.proxy import removeSecurityProxy

from lp.app.errors import NotFoundError
from lp.buildmaster.enums import (
    BuildFarmJobType,
    BuildQueueStatus,
    BuildStatus,
)
from lp.buildmaster.interfaces.buildfarmjob import IBuildFarmJobSource
from lp.buildmaster.model.buildfarmjob import SpecificBuildFarmJobSourceMixin
from lp.buildmaster.model.packagebuild import PackageBuildMixin
from lp.code.interfaces.gitrepository import IGitRepository
from lp.oci.interfaces.ocirecipe import IOCIRecipeSet
from lp.oci.interfaces.ocirecipebuild import (
    CannotScheduleRegistryUpload,
    IOCIFile,
    IOCIFileSet,
    IOCIRecipeBuild,
    IOCIRecipeBuildSet,
    OCIRecipeBuildRegistryUploadStatus,
)
from lp.oci.interfaces.ocirecipebuildjob import IOCIRegistryUploadJobSource
from lp.oci.model.ocirecipebuildjob import (
    OCIRecipeBuildJob,
    OCIRecipeBuildJobType,
)
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.model.person import Person
from lp.services.config import config
from lp.services.database.bulk import load_related
from lp.services.database.constants import DEFAULT
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.stormbase import StormBase
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.model.job import Job
from lp.services.librarian.browser import ProxiedLibraryFileAlias
from lp.services.librarian.model import LibraryFileAlias, LibraryFileContent
from lp.services.macaroons.interfaces import (
    NO_USER,
    BadMacaroonContext,
    IMacaroonIssuer,
)
from lp.services.macaroons.model import MacaroonIssuerBase
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.services.webapp.snapshot import notify_modified


@implementer(IOCIFile)
class OCIFile(StormBase):
    __storm_table__ = "OCIFile"

    id = Int(name="id", primary=True)

    build_id = Int(name="build", allow_none=False)
    build = Reference(build_id, "OCIRecipeBuild.id")

    library_file_id = Int(name="library_file", allow_none=False)
    library_file = Reference(library_file_id, "LibraryFileAlias.id")

    layer_file_digest = Unicode(name="layer_file_digest", allow_none=True)

    date_last_used = DateTime(
        name="date_last_used", tzinfo=timezone.utc, allow_none=False
    )

    def __init__(self, build, library_file, layer_file_digest=None):
        """Construct a `OCIFile`."""
        super().__init__()
        self.build = build
        self.library_file = library_file
        self.layer_file_digest = layer_file_digest


@implementer(IOCIFileSet)
class OCIFileSet:
    def getByLayerDigest(self, layer_file_digest):
        return (
            IStore(OCIFile)
            .find(OCIFile, OCIFile.layer_file_digest == layer_file_digest)
            .order_by(OCIFile.id)
            .first()
        )


@implementer(IOCIRecipeBuild)
class OCIRecipeBuild(PackageBuildMixin, StormBase):
    __storm_table__ = "OCIRecipeBuild"

    job_type = BuildFarmJobType.OCIRECIPEBUILD

    id = Int(name="id", primary=True)

    build_request_id = Int(name="build_request", allow_none=True)

    requester_id = Int(name="requester", allow_none=False)
    requester = Reference(requester_id, "Person.id")

    recipe_id = Int(name="recipe", allow_none=False)
    recipe = Reference(recipe_id, "OCIRecipe.id")

    processor_id = Int(name="processor", allow_none=False)
    processor = Reference(processor_id, "Processor.id")

    virtualized = Bool(name="virtualized")

    date_created = DateTime(
        name="date_created", tzinfo=timezone.utc, allow_none=False
    )
    date_started = DateTime(name="date_started", tzinfo=timezone.utc)
    date_finished = DateTime(name="date_finished", tzinfo=timezone.utc)
    date_first_dispatched = DateTime(
        name="date_first_dispatched", tzinfo=timezone.utc
    )

    builder_id = Int(name="builder")
    builder = Reference(builder_id, "Builder.id")

    status = DBEnum(name="status", enum=BuildStatus, allow_none=False)

    log_id = Int(name="log")
    log = Reference(log_id, "LibraryFileAlias.id")

    upload_log_id = Int(name="upload_log")
    upload_log = Reference(upload_log_id, "LibraryFileAlias.id")

    dependencies = Unicode(name="dependencies")

    failure_count = Int(name="failure_count", allow_none=False)

    build_farm_job_id = Int(name="build_farm_job", allow_none=False)
    build_farm_job = Reference(build_farm_job_id, "BuildFarmJob.id")

    # We only care about the pocket from a building environment POV,
    # it is not a target, nor referenced in the final build.
    pocket = PackagePublishingPocket.UPDATES

    def __init__(
        self,
        build_farm_job,
        requester,
        recipe,
        processor,
        virtualized,
        date_created,
        build_request=None,
    ):
        """Construct an `OCIRecipeBuild`."""
        self.requester = requester
        self.recipe = recipe
        self.processor = processor
        self.virtualized = virtualized
        self.date_created = date_created
        self.status = BuildStatus.NEEDSBUILD
        self.build_farm_job = build_farm_job
        if build_request is not None:
            self.build_request_id = build_request.id

    @property
    def build_request(self):
        """See `IOCIRecipeBuild`."""
        if self.build_request_id is not None:
            return self.recipe.getBuildRequest(self.build_request_id)

    def __repr__(self):
        return "<OCIRecipeBuild ~%s/%s/+oci/%s/+recipe/%s/+build/%d>" % (
            self.recipe.owner.name,
            self.recipe.oci_project.pillar.name,
            self.recipe.oci_project.name,
            self.recipe.name,
            self.id,
        )

    @property
    def title(self):
        # XXX cjwatson 2020-02-19: This should use a DAS architecture tag
        # rather than a processor name once we can do that.
        return "%s build of /~%s/%s/+oci/%s/+recipe/%s" % (
            self.processor.name,
            self.recipe.owner.name,
            self.recipe.oci_project.pillar.name,
            self.recipe.oci_project.name,
            self.recipe.name,
        )

    @property
    def score(self):
        """See `IOCIRecipeBuild`."""
        if self.buildqueue_record is None:
            return None
        else:
            return self.buildqueue_record.lastscore

    @property
    def can_be_retried(self):
        """See `IBuildFarmJob`."""
        # First check that the behaviour would accept the build if it
        # succeeded.
        if self.distro_series.status == SeriesStatus.OBSOLETE:
            return False
        return super().can_be_retried

    @property
    def is_private(self):
        """See `IBuildFarmJob`."""
        # XXX pappacena 2021-01-28: We need to keep track of git
        # repository's history in the build itself, in order to know if an
        # OCIRecipeBuild was created while the repo was private, and even
        # which repository it was using at that time (since the OCIRecipe's
        # repo can be changed or deleted).
        # There was some discussions about it for Snaps here:
        # https://code.launchpad.net/
        # ~cjwatson/launchpad/snap-build-record-code/+merge/365356
        return (
            self.recipe.private
            or self.recipe.owner.private
            or self.recipe.git_repository is None
            or self.recipe.git_repository.private
        )

    private = is_private

    def calculateScore(self):
        # XXX twom 2020-02-11 - This might need an addition?
        # XXX ikoruk 2026-09-03: We should introduce relative_build_score
        # everywhere. But for now, we will put this at the same build score as
        # PROPOSED + main + MEDIUM urgency.
        return 4010

    def estimateDuration(self):
        """See `IBuildFarmJob`."""
        median = self.getMedianBuildDuration()
        if median is not None:
            return median
        return timedelta(minutes=30)

    def getMedianBuildDuration(self):
        """Return the median duration of our successful builds."""
        store = IStore(self)
        result = store.find(
            (OCIRecipeBuild.date_started, OCIRecipeBuild.date_finished),
            OCIRecipeBuild.recipe == self.recipe_id,
            OCIRecipeBuild.processor == self.processor_id,
            OCIRecipeBuild.status == BuildStatus.FULLYBUILT,
        )
        result.order_by(Desc(OCIRecipeBuild.date_finished))
        durations = [row[1] - row[0] for row in result[:9]]
        if len(durations) == 0:
            return None
        durations.sort()
        return durations[len(durations) // 2]

    def getFiles(self):
        """See `IOCIRecipeBuild`."""
        result = Store.of(self).find(
            (OCIFile, LibraryFileAlias, LibraryFileContent),
            OCIFile.build == self.id,
            LibraryFileAlias.id == OCIFile.library_file_id,
            LibraryFileContent.id == LibraryFileAlias.content_id,
        )
        return result.order_by([LibraryFileAlias.filename, OCIFile.id])

    def getFileByName(self, filename):
        """See `IOCIRecipeBuild`."""
        origin = [
            LibraryFileAlias,
            LeftJoin(OCIFile, LibraryFileAlias.id == OCIFile.library_file_id),
        ]
        file_object = (
            Store.of(self)
            .using(*origin)
            .find(
                LibraryFileAlias,
                Or(
                    LibraryFileAlias.id.is_in(
                        (self.log_id, self.upload_log_id)
                    ),
                    OCIFile.build == self.id,
                ),
                LibraryFileAlias.filename == filename,
            )
            .one()
        )

        if file_object is not None and file_object.filename == filename:
            return file_object

        raise NotFoundError(filename)

    def lfaUrl(self, lfa):
        """Return the URL for a LibraryFileAlias in this context."""
        if lfa is None:
            return None
        return ProxiedLibraryFileAlias(lfa, self).http_url

    def getFileUrls(self):
        return [self.lfaUrl(lfa) for _, lfa, _ in self.getFiles()]

    @cachedproperty
    def eta(self):
        """The datetime when the build job is estimated to complete.

        This is the BuildQueue.estimated_duration plus the
        Job.date_started or BuildQueue.getEstimatedJobStartTime.
        """
        if self.buildqueue_record is None:
            return None
        queue_record = self.buildqueue_record
        if queue_record.status == BuildQueueStatus.WAITING:
            start_time = queue_record.getEstimatedJobStartTime()
        else:
            start_time = queue_record.date_started
        if start_time is None:
            return None
        duration = queue_record.estimated_duration
        return start_time + duration

    @property
    def estimate(self):
        """If true, the date value is an estimate."""
        if self.date_finished is not None:
            return False
        return self.eta is not None

    @property
    def date(self):
        """The date when the build completed or is estimated to complete."""
        if self.estimate:
            return self.eta
        return self.date_finished

    @property
    def archive(self):
        # XXX twom 2019-12-05 This may need to change when an OCIProject
        # pillar isn't just a distribution
        return self.recipe.distribution.main_archive

    @property
    def distribution(self):
        return self.recipe.distribution

    @property
    def distro_series(self):
        return self.recipe.distro_series

    @property
    def distro_arch_series(self):
        return self.recipe.distro_series.getDistroArchSeriesByProcessor(
            self.processor
        )

    @property
    def arch_tag(self):
        """See `IOCIRecipeBuild`."""
        return self.distro_arch_series.architecturetag

    def updateStatus(
        self,
        status,
        builder=None,
        worker_status=None,
        date_started=None,
        date_finished=None,
        force_invalid_transition=False,
    ):
        """See `IBuildFarmJob`."""
        edited_fields = set()
        with notify_modified(self, edited_fields) as previous_obj:
            super().updateStatus(
                status,
                builder=builder,
                worker_status=worker_status,
                date_started=date_started,
                date_finished=date_finished,
                force_invalid_transition=force_invalid_transition,
            )
            if self.status != previous_obj.status:
                edited_fields.add("status")
        # notify_modified evaluates all attributes mentioned in the
        # interface, but we may then make changes that affect self.eta.
        del get_property_cache(self).eta

    def notify(self, extra_info=None):
        """See `IPackageBuild`."""
        if not config.builddmaster.send_build_notification:
            return
        if self.status == BuildStatus.FULLYBUILT:
            return
        # XXX twom 2019-12-11 This should send mail

    def getLayerFileByDigest(self, layer_file_digest):
        file_object = (
            Store.of(self)
            .find(
                (OCIFile, LibraryFileAlias, LibraryFileContent),
                OCIFile.build == self.id,
                LibraryFileAlias.id == OCIFile.library_file_id,
                LibraryFileContent.id == LibraryFileAlias.content_id,
                OCIFile.layer_file_digest == layer_file_digest,
            )
            .one()
        )
        if file_object is not None:
            return file_object
        raise NotFoundError(layer_file_digest)

    def addFile(self, lfa, layer_file_digest=None):
        oci_file = OCIFile(
            build=self, library_file=lfa, layer_file_digest=layer_file_digest
        )
        IPrimaryStore(OCIFile).add(oci_file)
        return oci_file

    @cachedproperty
    def manifest(self):
        try:
            return self.getFileByName("manifest.json")
        except NotFoundError:
            return None

    @cachedproperty
    def digests(self):
        try:
            return self.getFileByName("digests.json")
        except NotFoundError:
            return None

    def verifySuccessfulUpload(self) -> bool:
        """See `IPackageBuild`."""
        layer_files = Store.of(self).find(
            OCIFile,
            OCIFile.build == self.id,
            OCIFile.layer_file_digest is not None,
        )
        layer_files_present = not layer_files.is_empty()
        metadata_present = (
            self.manifest is not None and self.digests is not None
        )
        return layer_files_present and metadata_present

    @property
    def registry_upload_jobs(self):
        jobs = Store.of(self).find(
            OCIRecipeBuildJob,
            OCIRecipeBuildJob.build == self,
            OCIRecipeBuildJob.job_type
            == OCIRecipeBuildJobType.REGISTRY_UPLOAD,
        )
        jobs.order_by(Desc(OCIRecipeBuildJob.job_id))

        def preload_jobs(rows):
            load_related(Job, rows, ["job_id"])

        return DecoratedResultSet(
            jobs, lambda job: job.makeDerived(), pre_iter_hook=preload_jobs
        )

    @cachedproperty
    def last_registry_upload_job(self):
        return self.registry_upload_jobs.first()

    @property
    def registry_upload_status(self):
        if self.status == BuildStatus.SUPERSEDED:
            return OCIRecipeBuildRegistryUploadStatus.SUPERSEDED
        job = self.last_registry_upload_job
        if job is None or job.job.status == JobStatus.SUSPENDED:
            return OCIRecipeBuildRegistryUploadStatus.UNSCHEDULED
        elif job.job.status in (JobStatus.WAITING, JobStatus.RUNNING):
            return OCIRecipeBuildRegistryUploadStatus.PENDING
        elif job.job.status == JobStatus.COMPLETED:
            return OCIRecipeBuildRegistryUploadStatus.UPLOADED
        else:
            return OCIRecipeBuildRegistryUploadStatus.FAILEDTOUPLOAD

    @property
    def registry_upload_error_summary(self):
        job = self.last_registry_upload_job
        return job and job.error_summary

    @property
    def registry_upload_errors(self):
        job = self.last_registry_upload_job
        return (job and job.errors) or []

    def scheduleRegistryUpload(self):
        """See `IOCIRecipeBuild`."""
        if not self.recipe.can_upload_to_registry:
            raise CannotScheduleRegistryUpload(
                "Cannot upload this build to registries because the recipe is "
                "not properly configured."
            )
        if not self.was_built or self.getFiles().is_empty():
            raise CannotScheduleRegistryUpload(
                "Cannot upload this build because it has no files."
            )
        if (
            self.registry_upload_status
            == OCIRecipeBuildRegistryUploadStatus.PENDING
        ):
            raise CannotScheduleRegistryUpload(
                "An upload of this build is already in progress."
            )
        elif (
            self.registry_upload_status
            == OCIRecipeBuildRegistryUploadStatus.UPLOADED
        ):
            # XXX cjwatson 2020-04-22: This won't be quite right in the case
            # where a recipe has multiple push rules.
            raise CannotScheduleRegistryUpload(
                "Cannot upload this build because it has already been "
                "uploaded."
            )
        getUtility(IOCIRegistryUploadJobSource).create(self)

    def hasMoreRecentBuild(self):
        """See `IOCIRecipeBuild`."""
        recent_builds = IStore(self).find(
            OCIRecipeBuild,
            OCIRecipeBuild.recipe == self.recipe,
            OCIRecipeBuild.processor == self.processor,
            OCIRecipeBuild.status == BuildStatus.FULLYBUILT,
            OCIRecipeBuild.date_created > self.date_created,
        )
        return not recent_builds.is_empty()


@implementer(IOCIRecipeBuildSet)
class OCIRecipeBuildSet(SpecificBuildFarmJobSourceMixin):
    """See `IOCIRecipeBuildSet`."""

    def new(
        self,
        requester,
        recipe,
        distro_arch_series,
        date_created=DEFAULT,
        build_request=None,
    ):
        """See `IOCIRecipeBuildSet`."""
        virtualized = (
            not distro_arch_series.processor.supports_nonvirtualized
            or recipe.require_virtualized
        )

        store = IPrimaryStore(OCIRecipeBuild)
        build_farm_job = getUtility(IBuildFarmJobSource).new(
            OCIRecipeBuild.job_type, BuildStatus.NEEDSBUILD, date_created
        )
        ocirecipebuild = OCIRecipeBuild(
            build_farm_job,
            requester,
            recipe,
            distro_arch_series.processor,
            virtualized,
            date_created,
            build_request=build_request,
        )
        store.add(ocirecipebuild)
        store.flush()
        return ocirecipebuild

    def preloadBuildsData(self, builds):
        """See `IOCIRecipeBuildSet`."""
        # Circular import.
        from lp.oci.model.ocirecipe import OCIRecipe

        load_related(Person, builds, ["requester_id"])
        lfas = load_related(LibraryFileAlias, builds, ["log_id"])
        load_related(LibraryFileContent, lfas, ["content_id"])
        recipes = load_related(OCIRecipe, builds, ["recipe_id"])
        getUtility(IOCIRecipeSet).preloadDataForOCIRecipes(recipes)
        # XXX twom 2019-12-05 This needs to be extended to include
        # OCIRecipeBuildJob when that exists.
        return

    def getByID(self, build_id):
        """See `ISpecificBuildFarmJobSource`."""
        store = IPrimaryStore(OCIRecipeBuild)
        return store.get(OCIRecipeBuild, build_id)

    def getByBuildFarmJob(self, build_farm_job):
        """See `ISpecificBuildFarmJobSource`."""
        return (
            Store.of(build_farm_job)
            .find(OCIRecipeBuild, build_farm_job_id=build_farm_job.id)
            .one()
        )

    def getByBuildFarmJobs(self, build_farm_jobs):
        """See `ISpecificBuildFarmJobSource`."""
        if len(build_farm_jobs) == 0:
            return EmptyResultSet()
        rows = Store.of(build_farm_jobs[0]).find(
            OCIRecipeBuild,
            OCIRecipeBuild.build_farm_job_id.is_in(
                bfj.id for bfj in build_farm_jobs
            ),
        )
        return DecoratedResultSet(rows, pre_iter_hook=self.preloadBuildsData)


@implementer(IMacaroonIssuer)
class OCIRecipeBuildMacaroonIssuer(MacaroonIssuerBase):
    identifier = "oci-recipe-build"
    issuable_via_authserver = True

    def checkIssuingContext(self, context, **kwargs):
        """See `MacaroonIssuerBase`.

        For issuing, the context is an `IOCIRecipeBuild`.
        """
        if not IOCIRecipeBuild.providedBy(context):
            raise BadMacaroonContext(context)
        if not removeSecurityProxy(context).is_private:
            raise BadMacaroonContext(
                context, "Refusing to issue macaroon for public build."
            )
        return removeSecurityProxy(context).id

    def checkVerificationContext(self, context, **kwargs):
        """See `MacaroonIssuerBase`."""
        if not IGitRepository.providedBy(context):
            raise BadMacaroonContext(context)
        return context

    def verifyPrimaryCaveat(
        self, verified, caveat_value, context, user=None, **kwargs
    ):
        """See `MacaroonIssuerBase`.

        For verification, the context is an `IGitRepository`.  We check that
        the repository is needed to build the `IOCIRecipeBuild` that is the
        context of the macaroon, and that the context build is currently
        building.
        """
        # Circular import.
        from lp.oci.model.ocirecipe import OCIRecipe

        # OCIRecipeBuild builds only support free-floating macaroons for Git
        # authentication, not ones bound to a user.
        if user:
            return False
        verified.user = NO_USER

        if context is None:
            # We're only verifying that the macaroon could be valid for some
            # context.
            return True

        try:
            build_id = int(caveat_value)
        except ValueError:
            return False
        return (
            not IStore(OCIRecipeBuild)
            .find(
                OCIRecipeBuild,
                OCIRecipeBuild.id == build_id,
                OCIRecipeBuild.recipe_id == OCIRecipe.id,
                OCIRecipe.git_repository == context,
                OCIRecipeBuild.status == BuildStatus.BUILDING,
            )
            .is_empty()
        )
