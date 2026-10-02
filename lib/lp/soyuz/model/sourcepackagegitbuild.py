# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Source package builds from Git revisions."""

__all__ = ["SourcePackageGitBuild"]

from datetime import timedelta, timezone

from storm.locals import Bool, DateTime, Int, Reference, Unicode
from storm.store import EmptyResultSet, Store
from zope.component import getUtility
from zope.interface import implementer, provider

from lp.app.errors import NotFoundError
from lp.buildmaster.enums import BuildFarmJobType, BuildStatus
from lp.buildmaster.interfaces.buildfarmjob import IBuildFarmJobSource
from lp.buildmaster.model.buildfarmjob import (
    BuildFarmJob,
    SpecificBuildFarmJobSourceMixin,
)
from lp.buildmaster.model.packagebuild import PackageBuildMixin
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.model.person import Person
from lp.services.database.bulk import load_related
from lp.services.database.constants import UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IPrimaryStore
from lp.services.database.stormbase import StormBase
from lp.services.librarian.browser import ProxiedLibraryFileAlias
from lp.services.privacy.interfaces import IObjectPrivacy
from lp.soyuz.interfaces.sourcepackagegitbuild import (
    ISourcePackageGitBuild,
    ISourcePackageGitBuildSource,
)
from lp.soyuz.model.archive import Archive
from lp.soyuz.model.sourcepackagerelease import SourcePackageRelease


@implementer(ISourcePackageGitBuild, IObjectPrivacy)
@provider(ISourcePackageGitBuildSource)
class SourcePackageGitBuild(
    SpecificBuildFarmJobSourceMixin, PackageBuildMixin, StormBase
):
    """See `ISourcePackageGitBuild`."""

    __storm_table__ = "SourcePackageGitBuild"
    job_type = BuildFarmJobType.SOURCEPACKAGEGITBUILD

    id = Int(primary=True)
    build_farm_job_id = Int(name="build_farm_job", allow_none=False)
    build_farm_job = Reference(build_farm_job_id, BuildFarmJob.id)
    git_repository_id = Int(name="git_repository", allow_none=False)
    git_repository = Reference(git_repository_id, "GitRepository.id")
    commit_sha1 = Unicode(name="commit_sha1", allow_none=False)
    archive_id = Int(name="archive", allow_none=False)
    archive = Reference(archive_id, "Archive.id")
    distroseries_id = Int(name="distroseries", allow_none=False)
    distroseries = Reference(distroseries_id, "DistroSeries.id")
    distro_series = distroseries
    pocket = DBEnum(
        name="pocket", enum=PackagePublishingPocket, allow_none=False
    )
    requester_id = Int(name="requester", allow_none=False)
    requester = Reference(requester_id, "Person.id")
    version_suffix = Unicode(name="version_suffix", allow_none=False)
    processor_id = Int(name="processor", allow_none=False)
    processor = Reference(processor_id, "Processor.id")
    virtualized = Bool(name="virtualized", allow_none=False)
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

    def __init__(
        self,
        build_farm_job,
        git_repository,
        commit_sha1,
        archive,
        distroseries,
        requester,
        version_suffix,
        pocket,
        date_created,
    ):
        super().__init__()
        processor = distroseries.nominatedarchindep.processor
        self.build_farm_job = build_farm_job
        self.git_repository = git_repository
        self.commit_sha1 = commit_sha1
        self.archive = archive
        self.distroseries = distroseries
        self.requester = requester
        self.version_suffix = version_suffix
        self.pocket = pocket
        self.processor = processor
        self.virtualized = True
        self.date_created = date_created
        self.status = BuildStatus.NEEDSBUILD
        self.failure_count = 0

    @classmethod
    def new(
        cls,
        git_repository,
        commit_sha1,
        archive,
        distroseries,
        requester,
        version_suffix,
        pocket=None,
        date_created=None,
    ):
        """See `ISourcePackageGitBuildSource`."""
        if pocket is None:
            pocket = PackagePublishingPocket.RELEASE
        if date_created is None:
            date_created = UTC_NOW
        store = IPrimaryStore(cls)
        build_farm_job = getUtility(IBuildFarmJobSource).new(
            cls.job_type, BuildStatus.NEEDSBUILD, date_created, None, archive
        )
        build = cls(
            build_farm_job,
            git_repository,
            commit_sha1,
            archive,
            distroseries,
            requester,
            version_suffix,
            pocket,
            date_created,
        )
        store.add(build)
        store.flush()
        return build

    @property
    def current_component(self):
        component = self.archive.default_component
        assert component is not None
        return component

    @property
    def distribution(self):
        return self.distroseries.distribution

    @property
    def title(self):
        return "%s build of %s in %s %s" % (
            self.commit_sha1[:7],
            self.git_repository.unique_name,
            self.distribution.name,
            self.distroseries.name,
        )

    @property
    def is_private(self):
        return self.git_repository.private or self.archive.private

    can_be_retried = False

    def calculateScore(self):
        return 2510 + self.archive.relative_build_score

    def estimateDuration(self):
        return timedelta(minutes=10)

    def verifySuccessfulUpload(self):
        return self.source_package_release is not None

    def notify(self, extra_info=None):
        pass

    @property
    def source_package_release(self):
        """See `ISourcePackageGitBuild`."""
        return (
            Store.of(self)
            .find(SourcePackageRelease, source_package_git_build=self)
            .one()
        )

    def destroySelf(self):
        if self.buildqueue_record is not None:
            self.buildqueue_record.destroySelf()
        store = Store.of(self)
        store.find(SourcePackageRelease, source_package_git_build=self).set(
            source_package_git_build_id=None
        )
        store.remove(self)
        store.remove(self.build_farm_job)

    def getUploader(self, changes):
        return self.requester

    def lfaUrl(self, lfa):
        if lfa is None:
            return None
        return ProxiedLibraryFileAlias(lfa, self).http_url

    @property
    def log_url(self):
        return self.lfaUrl(self.log)

    @property
    def upload_log_url(self):
        return self.lfaUrl(self.upload_log)

    def getFileByName(self, filename):
        files = {
            lfa.filename: lfa
            for lfa in (self.log, self.upload_log)
            if lfa is not None
        }
        try:
            return files[filename]
        except KeyError:
            raise NotFoundError(filename)

    @classmethod
    def getByID(cls, build_id):
        return IPrimaryStore(cls).find(cls, cls.id == build_id).one()

    @classmethod
    def getByBuildFarmJob(cls, build_farm_job):
        return (
            Store.of(build_farm_job)
            .find(cls, build_farm_job_id=build_farm_job.id)
            .one()
        )

    @classmethod
    def preloadBuildsData(cls, builds):
        # Circular imports.
        from lp.registry.model.distribution import Distribution
        from lp.registry.model.distroseries import DistroSeries
        from lp.services.librarian.model import LibraryFileAlias

        load_related(LibraryFileAlias, builds, ["log_id"])
        archives = load_related(Archive, builds, ["archive_id"])
        load_related(Person, archives, ["owner_id"])
        distroseries = load_related(DistroSeries, builds, ["distroseries_id"])
        load_related(Distribution, distroseries, ["distribution_id"])

    @classmethod
    def getByBuildFarmJobs(cls, build_farm_jobs):
        if not build_farm_jobs:
            return EmptyResultSet()
        rows = Store.of(build_farm_jobs[0]).find(
            cls,
            cls.build_farm_job_id.is_in(job.id for job in build_farm_jobs),
        )
        return DecoratedResultSet(rows, pre_iter_hook=cls.preloadBuildsData)
