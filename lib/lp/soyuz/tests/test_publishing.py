# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test native publication workflow for Soyuz. """

import hashlib
import io
import json
import operator
import os
import shutil
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial
from unittest import mock

import transaction
from storm.store import Store
from testtools.matchers import Equals
from zope.component import getSiteManager, getUtility
from zope.interface import Interface
from zope.interface.verify import verifyObject
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.app.errors import NotFoundError
from lp.archivepublisher.artifactory import ArtifactoryPool
from lp.archivepublisher.config import getPubConfig
from lp.archivepublisher.diskpool import DiskPool
from lp.archivepublisher.indices import (
    build_binary_stanza_fields,
    build_source_stanza_fields,
)
from lp.archivepublisher.tests.artifactory_fixture import (
    FakeArtifactoryFixture,
)
from lp.buildmaster.enums import BuildStatus
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.code.autopkgtest import (
    AutopkgtestLookup,
    AutopkgtestLookupStatus,
    AutopkgtestReadiness,
    AutopkgtestResult,
    AutopkgtestStatus,
    AutopkgtestWatchHandler,
)
from lp.code.enums import BranchMergeProposalStatus, RevisionStatusResult
from lp.code.interfaces.autopkgtest import IRequestAutopkgtestBackend
from lp.code.interfaces.branchmergeproposal import (
    IBranchMergeProposalJobSource,
)
from lp.code.interfaces.revisionstatus import IRevisionStatusReportSet
from lp.code.interfaces.revisionstatuswatch import IRevisionStatusWatchSet
from lp.code.model.autopkgtest import AutopkgtestRequestBackend
from lp.code.model.branchmergeproposaljob import (
    AutopkgtestRequestNotReady,
    BranchMergeProposalJob,
    BranchMergeProposalJobSource,
    BranchMergeProposalJobType,
    RequestAutopkgtestJob,
)
from lp.code.model.revisionstatussummary import selectValidationChecks
from lp.code.scripts.revisionstatuswatch import RevisionStatusWatchCollector
from lp.code.tests.test_autopkgtest import FakeAutopkgtestAdapter
from lp.registry.interfaces.distribution import IDistributionSet
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.interfaces.sourcepackage import (
    SourcePackageType,
    SourcePackageUrgency,
)
from lp.registry.interfaces.sourcepackagename import ISourcePackageNameSet
from lp.services import features
from lp.services.channels import channel_string_to_list
from lp.services.config import config
from lp.services.database.constants import UTC_NOW
from lp.services.database.interfaces import IStore
from lp.services.features.testing import FeatureFixture
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.runner import JobRunner
from lp.services.librarian.interfaces import ILibraryFileAliasSet
from lp.services.log.logger import BufferLogger, DevNullLogger
from lp.soyuz.enums import (
    ArchivePublishingMethod,
    ArchivePurpose,
    ArchiveRepositoryFormat,
    BinaryPackageFormat,
    DistroArchSeriesFilterSense,
    PackageUploadStatus,
)
from lp.soyuz.event import BinaryPackagePublishedEvent
from lp.soyuz.interfaces.binarypackagename import IBinaryPackageNameSet
from lp.soyuz.interfaces.component import IComponentSet
from lp.soyuz.interfaces.event import IBinaryPackagePublishedEvent
from lp.soyuz.interfaces.publishing import (
    DeletionError,
    IBinaryPackagePublishingHistory,
    IPublishingSet,
    OverrideError,
    PackagePublishingPriority,
    PackagePublishingStatus,
    active_publishing_status,
)
from lp.soyuz.interfaces.section import ISectionSet
from lp.soyuz.model.distributionsourcepackagecache import (
    DistributionSourcePackageCache,
)
from lp.soyuz.model.distroseriesdifferencejob import find_waiting_jobs
from lp.soyuz.model.distroseriespackagecache import DistroSeriesPackageCache
from lp.soyuz.model.mergeproposalvalidation import MergeProposalValidation
from lp.soyuz.model.publishing import (
    BinaryPackagePublishingHistory,
    SourcePackagePublishingHistory,
)
from lp.soyuz.model.sourcepackagegitbuild import SourcePackageGitBuild
from lp.testing import (
    StormStatementRecorder,
    TestCaseWithFactory,
    login_admin,
    person_logged_in,
    record_two_runs,
)
from lp.testing.dbuser import dbuser, lp_dbuser, switch_dbuser
from lp.testing.factory import LaunchpadObjectFactory
from lp.testing.layers import (
    DatabaseFunctionalLayer,
    LaunchpadFunctionalLayer,
    LaunchpadZopelessLayer,
    ZopelessDatabaseLayer,
)
from lp.testing.matchers import HasQueryCount


class SoyuzTestPublisher:
    """Helper class able to publish coherent source and binaries in Soyuz."""

    def __init__(self):
        self.factory = LaunchpadObjectFactory()
        self.default_package_name = "foo"

    def setUpDefaultDistroSeries(self, distroseries=None):
        """Set up a distroseries that will be used by default.

        This distro series is used to publish packages in, if you don't
        specify any when using the publishing methods.

        It also sets up a person that can act as the default uploader,
        and makes sure that the default package name exists in the
        database.

        :param distroseries: The `IDistroSeries` to use as default. If
            it's None, one will be created.
        :return: The `IDistroSeries` that got set as default.
        """
        if distroseries is None:
            distroseries = self.factory.makeDistroSeries()
        self.distroseries = distroseries
        # Set up a person that has a GPG key.
        self.person = getUtility(IPersonSet).getByName("name16")
        # Make sure the name exists in the database, to make it easier
        # to get packages from distributions and distro series.
        name_set = getUtility(ISourcePackageNameSet)
        name_set.getOrCreateByName(self.default_package_name)
        return self.distroseries

    def prepareBreezyAutotest(self):
        """Prepare ubuntutest/breezy-autotest for publications.

        It's also called during the normal test-case setUp.
        """
        self.ubuntutest = getUtility(IDistributionSet)["ubuntutest"]
        self.breezy_autotest = self.ubuntutest["breezy-autotest"]
        self.setUpDefaultDistroSeries(self.breezy_autotest)
        # Only create the DistroArchSeries needed if they do not exist yet.
        # This makes it easier to experiment at the python command line
        # (using "make harness").
        try:
            self.breezy_autotest_i386 = self.breezy_autotest["i386"]
        except NotFoundError:
            self.breezy_autotest_i386 = self.breezy_autotest.newArch(
                "i386",
                getUtility(IProcessorSet).getByName("386"),
                False,
                self.person,
            )
        try:
            self.breezy_autotest_hppa = self.breezy_autotest["hppa"]
        except NotFoundError:
            self.breezy_autotest_hppa = self.breezy_autotest.newArch(
                "hppa",
                getUtility(IProcessorSet).getByName("hppa"),
                False,
                self.person,
            )
        self.breezy_autotest.nominatedarchindep = self.breezy_autotest_i386
        fake_chroot = self.addMockFile("fake_chroot.tar.gz")
        self.breezy_autotest_i386.addOrUpdateChroot(fake_chroot)
        self.breezy_autotest_hppa.addOrUpdateChroot(fake_chroot)

    def addFakeChroots(self, distroseries=None, db_only=False):
        """Add fake chroots for all the architectures in distroseries."""
        if distroseries is None:
            distroseries = self.distroseries
        if db_only:
            fake_chroot = self.factory.makeLibraryFileAlias(
                filename="fake_chroot.tar.gz", db_only=True
            )
        else:
            fake_chroot = self.addMockFile("fake_chroot.tar.gz")
        for arch in distroseries.architectures:
            arch.addOrUpdateChroot(fake_chroot)

    def regetBreezyAutotest(self):
        self.ubuntutest = getUtility(IDistributionSet)["ubuntutest"]
        self.breezy_autotest = self.ubuntutest["breezy-autotest"]
        self.person = getUtility(IPersonSet).getByName("name16")
        self.breezy_autotest_i386 = self.breezy_autotest["i386"]
        self.breezy_autotest_hppa = self.breezy_autotest["hppa"]

    def addMockFile(self, filename, filecontent=b"nothing", restricted=False):
        """Add a mock file in Librarian.

        Returns a ILibraryFileAlias corresponding to the file uploaded.
        """
        library_file = getUtility(ILibraryFileAliasSet).create(
            filename,
            len(filecontent),
            io.BytesIO(filecontent),
            "application/text",
            restricted=restricted,
        )
        return library_file

    def addPackageUpload(
        self,
        archive,
        distroseries,
        pocket=PackagePublishingPocket.RELEASE,
        changes_file_name="foo_666_source.changes",
        changes_file_content=b"fake changes file content",
        upload_status=PackageUploadStatus.DONE,
    ):
        signing_key = self.person.gpg_keys[0]
        package_upload = distroseries.createQueueEntry(
            pocket,
            archive,
            changes_file_name,
            changes_file_content,
            signing_key=signing_key,
        )

        status_to_method = {
            PackageUploadStatus.DONE: "setDone",
            PackageUploadStatus.ACCEPTED: "setAccepted",
        }
        naked_package_upload = removeSecurityProxy(package_upload)
        method = getattr(naked_package_upload, status_to_method[upload_status])
        method()

        return package_upload

    def getPubSource(
        self,
        sourcename=None,
        version=None,
        component="main",
        filename=None,
        section="base",
        filecontent=b"I do not care about sources.",
        changes_file_content=b"Fake: fake changes file content",
        status=PackagePublishingStatus.PENDING,
        pocket=PackagePublishingPocket.RELEASE,
        urgency=SourcePackageUrgency.LOW,
        scheduleddeletiondate=None,
        dateremoved=None,
        distroseries=None,
        archive=None,
        builddepends=None,
        builddependsindep=None,
        architecturehintlist="all",
        dsc_standards_version="3.6.2",
        dsc_format="1.0",
        dsc_binaries="foo-bin",
        build_conflicts=None,
        build_conflicts_indep=None,
        dsc_maintainer_rfc822="Foo Bar <foo@bar.com>",
        maintainer=None,
        creator=None,
        date_uploaded=UTC_NOW,
        spr_only=False,
        user_defined_fields=None,
        format=SourcePackageType.DPKG,
        channel=None,
    ):
        """Return a mock source publishing record.

        if spr_only is specified, the source is not published and the
        sourcepackagerelease object is returned instead.
        """
        if sourcename is None:
            sourcename = self.default_package_name
        if version is None:
            version = "666"
        spn = getUtility(ISourcePackageNameSet).getOrCreateByName(sourcename)

        component = getUtility(IComponentSet)[component]
        section = getUtility(ISectionSet)[section]

        if distroseries is None:
            distroseries = self.distroseries
        if archive is None:
            archive = distroseries.main_archive
        if maintainer is None:
            maintainer = self.person
        if creator is None:
            creator = self.person

        spr = distroseries.createUploadedSourcePackageRelease(
            sourcepackagename=spn,
            format=format,
            maintainer=maintainer,
            creator=creator,
            component=component,
            section=section,
            urgency=urgency,
            version=version,
            builddepends=builddepends,
            builddependsindep=builddependsindep,
            build_conflicts=build_conflicts,
            build_conflicts_indep=build_conflicts_indep,
            architecturehintlist=architecturehintlist,
            changelog=None,
            changelog_entry=None,
            dsc=None,
            copyright="placeholder ...",
            dscsigningkey=self.person.gpg_keys[0],
            dsc_maintainer_rfc822=dsc_maintainer_rfc822,
            dsc_standards_version=dsc_standards_version,
            dsc_format=dsc_format,
            dsc_binaries=dsc_binaries,
            archive=archive,
            dateuploaded=date_uploaded,
            user_defined_fields=user_defined_fields,
        )

        changes_file_name = "%s_%s_source.changes" % (sourcename, version)
        if spr_only:
            upload_status = PackageUploadStatus.ACCEPTED
        else:
            upload_status = PackageUploadStatus.DONE
        package_upload = self.addPackageUpload(
            archive,
            distroseries,
            pocket,
            changes_file_name=changes_file_name,
            changes_file_content=changes_file_content,
            upload_status=upload_status,
        )
        naked_package_upload = removeSecurityProxy(package_upload)
        naked_package_upload.addSource(spr)

        if filename is None:
            filename = "%s_%s.dsc" % (sourcename, version)
        alias = self.addMockFile(
            filename, filecontent, restricted=archive.private
        )
        spr.addFile(alias)

        if spr_only:
            return spr

        if status == PackagePublishingStatus.PUBLISHED:
            datepublished = UTC_NOW
        else:
            datepublished = None
        if channel is not None:
            channel = channel_string_to_list(channel)

        spph = SourcePackagePublishingHistory(
            distroseries=distroseries,
            sourcepackagerelease=spr,
            sourcepackagename=spr.sourcepackagename,
            format=spr.format,
            component=spr.component,
            section=spr.section,
            status=status,
            datecreated=date_uploaded,
            dateremoved=dateremoved,
            datepublished=datepublished,
            scheduleddeletiondate=scheduleddeletiondate,
            pocket=pocket,
            archive=archive,
            creator=creator,
            channel=channel,
        )

        return spph

    def getPubBinaries(
        self,
        binaryname="foo-bin",
        summary="Foo app is great",
        description="Well ...\nit does nothing, though",
        shlibdep=None,
        depends=None,
        recommends=None,
        suggests=None,
        conflicts=None,
        replaces=None,
        provides=None,
        pre_depends=None,
        enhances=None,
        breaks=None,
        built_using=None,
        filecontent=b"bbbiiinnnaaarrryyy",
        changes_file_content=b"Fake: fake changes file",
        status=PackagePublishingStatus.PENDING,
        pocket=PackagePublishingPocket.RELEASE,
        format=BinaryPackageFormat.DEB,
        scheduleddeletiondate=None,
        dateremoved=None,
        distroseries=None,
        archive=None,
        pub_source=None,
        version=None,
        architecturespecific=False,
        builder=None,
        component="main",
        phased_update_percentage=None,
        with_debug=False,
        user_defined_fields=None,
        channel=None,
    ):
        """Return a list of binary publishing records."""
        if distroseries is None:
            distroseries = self.distroseries

        if archive is None:
            archive = distroseries.main_archive

        if pub_source is None:
            sourcename = "%s" % binaryname.split("-")[0]
            if architecturespecific:
                architecturehintlist = "any"
            else:
                architecturehintlist = "all"

            pub_source = self.getPubSource(
                sourcename=sourcename,
                status=status,
                pocket=pocket,
                archive=archive,
                distroseries=distroseries,
                version=version,
                architecturehintlist=architecturehintlist,
                component=component,
            )
        else:
            archive = pub_source.archive

        builds = pub_source.createMissingBuilds()
        published_binaries = []
        for build in builds:
            build.updateStatus(BuildStatus.FULLYBUILT, builder=builder)
            pub_binaries = []
            if with_debug:
                binarypackagerelease_ddeb = self.uploadBinaryForBuild(
                    build,
                    binaryname + "-dbgsym",
                    filecontent,
                    summary,
                    description,
                    shlibdep,
                    depends,
                    recommends,
                    suggests,
                    conflicts,
                    replaces,
                    provides,
                    pre_depends,
                    enhances,
                    breaks,
                    built_using,
                    BinaryPackageFormat.DDEB,
                    version=version,
                )
                pub_binaries += self.publishBinaryInArchive(
                    binarypackagerelease_ddeb,
                    archive,
                    status,
                    pocket,
                    scheduleddeletiondate,
                    dateremoved,
                    phased_update_percentage,
                    channel=channel,
                )
            else:
                binarypackagerelease_ddeb = None

            binarypackagerelease = self.uploadBinaryForBuild(
                build,
                binaryname,
                filecontent,
                summary,
                description,
                shlibdep,
                depends,
                recommends,
                suggests,
                conflicts,
                replaces,
                provides,
                pre_depends,
                enhances,
                breaks,
                built_using,
                format,
                binarypackagerelease_ddeb,
                version=version,
                user_defined_fields=user_defined_fields,
            )
            pub_binaries += self.publishBinaryInArchive(
                binarypackagerelease,
                archive,
                status,
                pocket,
                scheduleddeletiondate,
                dateremoved,
                phased_update_percentage,
                channel=channel,
            )
            published_binaries.extend(pub_binaries)
            package_upload = self.addPackageUpload(
                archive,
                distroseries,
                pocket,
                changes_file_content=changes_file_content,
                changes_file_name="%s_%s_%s.changes"
                % (binaryname, binarypackagerelease.version, build.arch_tag),
            )
            package_upload.addBuild(build)

        return sorted(
            published_binaries, key=operator.attrgetter("id"), reverse=True
        )

    def uploadBinaryForBuild(
        self,
        build,
        binaryname,
        filecontent=b"anything",
        summary="summary",
        description="description",
        shlibdep=None,
        depends=None,
        recommends=None,
        suggests=None,
        conflicts=None,
        replaces=None,
        provides=None,
        pre_depends=None,
        enhances=None,
        breaks=None,
        built_using=None,
        format=BinaryPackageFormat.DEB,
        debug_package=None,
        user_defined_fields=None,
        homepage=None,
        version=None,
    ):
        """Return the corresponding `BinaryPackageRelease`."""
        sourcepackagerelease = build.source_package_release
        distroarchseries = build.distro_arch_series
        architecturespecific = (
            not sourcepackagerelease.architecturehintlist == "all"
        )

        binarypackagename = getUtility(
            IBinaryPackageNameSet
        ).getOrCreateByName(binaryname)

        if version is None:
            version = sourcepackagerelease.version

        binarypackagerelease = build.createBinaryPackageRelease(
            version=version,
            component=sourcepackagerelease.component,
            section=sourcepackagerelease.section,
            binarypackagename=binarypackagename,
            summary=summary,
            description=description,
            shlibdeps=shlibdep,
            depends=depends,
            recommends=recommends,
            suggests=suggests,
            conflicts=conflicts,
            replaces=replaces,
            provides=provides,
            pre_depends=pre_depends,
            enhances=enhances,
            breaks=breaks,
            built_using=built_using,
            essential=False,
            installedsize=100,
            architecturespecific=architecturespecific,
            binpackageformat=format,
            priority=PackagePublishingPriority.STANDARD,
            debug_package=debug_package,
            user_defined_fields=user_defined_fields,
            homepage=homepage,
        )

        # Create the corresponding binary file.
        if architecturespecific:
            filearchtag = distroarchseries.architecturetag
        else:
            filearchtag = "all"
        filename = "%s_%s_%s.%s" % (
            binaryname,
            sourcepackagerelease.version,
            filearchtag,
            format.name.lower(),
        )
        alias = self.addMockFile(
            filename, filecontent=filecontent, restricted=build.archive.private
        )
        binarypackagerelease.addFile(alias)

        # Adjust the build record in way it looks complete.
        date_finished = datetime(2008, 1, 1, 0, 5, 0, tzinfo=timezone.utc)
        date_started = date_finished - timedelta(minutes=5)
        build.updateStatus(
            BuildStatus.BUILDING,
            date_started=date_started,
            force_invalid_transition=True,
        )
        build.updateStatus(BuildStatus.FULLYBUILT, date_finished=date_finished)
        buildlog_filename = "buildlog_%s-%s-%s.%s_%s_%s.txt.gz" % (
            build.distribution.name,
            build.distro_series.name,
            build.distro_arch_series.architecturetag,
            build.source_package_release.name,
            build.source_package_release.version,
            build.status.name,
        )
        if not build.log:
            build.setLog(
                self.addMockFile(
                    buildlog_filename,
                    filecontent=b"Built!",
                    restricted=build.archive.private,
                )
            )

        return binarypackagerelease

    def publishBinaryInArchive(
        self,
        binarypackagerelease,
        archive,
        status=PackagePublishingStatus.PENDING,
        pocket=PackagePublishingPocket.RELEASE,
        scheduleddeletiondate=None,
        dateremoved=None,
        phased_update_percentage=None,
        channel=None,
    ):
        """Return the corresponding BinaryPackagePublishingHistory."""
        distroarchseries = binarypackagerelease.build.distro_arch_series

        # Publish the binary.
        if binarypackagerelease.architecturespecific:
            archs = [distroarchseries]
        else:
            archs = distroarchseries.distroseries.architectures
        if channel is not None:
            channel = channel_string_to_list(channel)

        pub_binaries = []
        for arch in archs:
            pub = BinaryPackagePublishingHistory(
                distroarchseries=arch,
                binarypackagerelease=binarypackagerelease,
                binarypackagename=binarypackagerelease.binarypackagename,
                binarypackageformat=binarypackagerelease.binpackageformat,
                component=binarypackagerelease.component,
                section=binarypackagerelease.section,
                priority=binarypackagerelease.priority,
                status=status,
                scheduleddeletiondate=scheduleddeletiondate,
                dateremoved=dateremoved,
                datecreated=UTC_NOW,
                pocket=pocket,
                archive=archive,
                phased_update_percentage=phased_update_percentage,
                channel=channel,
                sourcepackagename=(
                    binarypackagerelease.build.source_package_name
                ),
            )
            if status == PackagePublishingStatus.PUBLISHED:
                pub.datepublished = UTC_NOW
            pub_binaries.append(pub)

        return pub_binaries

    def _findChangesFile(self, top, name_fragment):
        """File with given name fragment in directory tree starting at top."""
        for root, _, files in os.walk(top, topdown=False):
            for name in files:
                if name.endswith(".changes") and name.find(name_fragment) > -1:
                    return os.path.join(root, name)
        return None

    def createSource(
        self, archive, sourcename, version, distroseries=None, new_version=None
    ):
        """Create source with meaningful '.changes' file."""
        top = "lib/lp/archiveuploader/tests/data/suite"
        name_fragment = "%s_%s" % (sourcename, version)
        changesfile_path = self._findChangesFile(top, name_fragment)

        source = None

        if changesfile_path is not None:
            if new_version is None:
                new_version = version
            changesfile_content = ""
            with open(changesfile_path, "rb") as handle:
                changesfile_content = handle.read()

            source = self.getPubSource(
                sourcename=sourcename,
                archive=archive,
                version=new_version,
                changes_file_content=changesfile_content,
                distroseries=distroseries,
            )

        return source

    def makeSourcePackageSummaryData(self, source_pub=None):
        """Make test data for SourcePackage.summary.

        The distroseries that is returned from this method needs to be
        passed into updatePackageCache() so that SourcePackage.summary can
        be populated.
        """
        if source_pub is None:
            distribution = self.factory.makeDistribution(
                name="youbuntu",
                displayname="Youbuntu",
                owner=self.factory.makePerson(email="owner@youbuntu.com"),
            )
            distroseries = self.factory.makeDistroSeries(
                name="busy", distribution=distribution
            )
            source_package_name = self.factory.makeSourcePackageName(
                name="bonkers"
            )
            self.factory.makeSourcePackage(
                sourcepackagename=source_package_name,
                distroseries=distroseries,
            )
            component = self.factory.makeComponent("multiverse")
            source_pub = self.factory.makeSourcePackagePublishingHistory(
                sourcepackagename=source_package_name,
                distroseries=distroseries,
                component=component,
            )

        das = self.factory.makeDistroArchSeries(
            distroseries=source_pub.distroseries
        )

        for name in ("flubber-bin", "flubber-lib"):
            binary_package_name = self.factory.makeBinaryPackageName(name)
            build = self.factory.makeBinaryPackageBuild(
                source_package_release=source_pub.sourcepackagerelease,
                archive=self.factory.makeArchive(),
                distroarchseries=das,
            )
            bpr = self.factory.makeBinaryPackageRelease(
                binarypackagename=binary_package_name,
                summary="summary for %s" % name,
                build=build,
                component=source_pub.component,
            )
            self.factory.makeBinaryPackagePublishingHistory(
                binarypackagerelease=bpr, distroarchseries=das
            )
        return dict(
            distroseries=source_pub.distroseries,
            source_package=source_pub.meta_sourcepackage,
        )

    def updatePackageCache(self, distroseries):
        with dbuser(config.statistician.dbuser):
            DistributionSourcePackageCache.updateAll(
                distroseries.distribution,
                archive=distroseries.main_archive,
                ztm=transaction,
                log=DevNullLogger(),
            )
            DistroSeriesPackageCache.updateAll(
                distroseries,
                archive=distroseries.main_archive,
                ztm=transaction,
                log=DevNullLogger(),
            )


class TestBinaryPackagePublicationEvents(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.events = []
        site_manager = getSiteManager()
        required = (
            Interface,
            IBinaryPackagePublishedEvent,
        )
        site_manager.registerHandler(self.recordPublication, required)
        self.addCleanup(
            site_manager.unregisterHandler, self.recordPublication, required
        )

    def recordPublication(self, publication, event):
        self.events.append(
            (publication, event, publication.status, publication.datepublished)
        )

    def makePublication(self):
        return removeSecurityProxy(
            self.factory.makeBinaryPackagePublishingHistory(
                status=PackagePublishingStatus.PENDING
            )
        )

    def test_event_interface(self):
        publication = self.makePublication()
        event = BinaryPackagePublishedEvent(publication)
        self.assertTrue(verifyObject(IBinaryPackagePublishedEvent, event))
        self.assertIs(publication, event.object)

    def test_first_publication_notifies_after_updating_record(self):
        publication = self.makePublication()
        publication.setPublished()
        self.assertEqual(1, len(self.events))
        observed, event, status, published_at = self.events[0]
        self.assertIs(publication, observed)
        self.assertIs(publication, event.object)
        self.assertEqual(PackagePublishingStatus.PUBLISHED, status)
        self.assertIsNotNone(published_at)
        self.assertEqual(publication.datepublished, published_at)

    def test_repeated_publication_does_not_notify_again(self):
        publication = self.makePublication()
        publication.setPublished()
        published_at = publication.datepublished
        publication.setPublished()
        self.assertEqual(1, len(self.events))
        self.assertEqual(published_at, publication.datepublished)

    def test_existing_publication_does_not_notify(self):
        publication = self.makePublication()
        publication.status = PackagePublishingStatus.PUBLISHED
        published_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
        publication.datepublished = published_at
        publication.setPublished()
        self.assertEqual([], self.events)
        self.assertEqual(published_at, publication.datepublished)

    def test_published_record_without_timestamp_notifies(self):
        publication = self.makePublication()
        publication.status = PackagePublishingStatus.PUBLISHED
        publication.setPublished()
        self.assertEqual(1, len(self.events))
        self.assertIsNotNone(publication.datepublished)

    def test_inactive_publications_do_not_notify(self):
        for status in (
            PackagePublishingStatus.SUPERSEDED,
            PackagePublishingStatus.DELETED,
            PackagePublishingStatus.OBSOLETE,
        ):
            publication = self.makePublication()
            publication.status = status
            publication.setPublished()
            self.assertEqual(status, publication.status)
            self.assertIsNone(publication.datepublished)
        self.assertEqual([], self.events)

    def test_source_publication_does_not_notify(self):
        publication = self.factory.makeSourcePackagePublishingHistory(
            status=PackagePublishingStatus.PENDING
        )
        publication.setPublished()
        self.assertEqual(PackagePublishingStatus.PUBLISHED, publication.status)
        self.assertIsNotNone(publication.datepublished)
        self.assertEqual([], self.events)

    def test_event_identifies_destination_archive(self):
        publication = self.makePublication()
        original_archive = publication.archive
        destination = self.factory.makeArchive(
            distribution=publication.distroseries.distribution,
            purpose=ArchivePurpose.PPA,
        )
        publication.archive = destination
        publication.copied_from_archive = original_archive
        publication.setPublished()
        event = self.events[0][1]
        self.assertEqual(destination.id, event.object.archive.id)
        self.assertEqual(
            original_archive.id, event.object.copied_from_archive.id
        )

    def test_rollback_allows_notification_on_retry(self):
        publication = self.makePublication()
        store = Store.of(publication)
        store.flush()
        publication_id = publication.id
        transaction.commit()
        publication.setPublished()
        self.assertEqual(1, len(self.events))
        transaction.abort()
        publication = store.get(BinaryPackagePublishingHistory, publication_id)
        self.assertEqual(PackagePublishingStatus.PENDING, publication.status)
        self.assertIsNone(publication.datepublished)
        publication.setPublished()
        self.assertEqual(2, len(self.events))


class TestAutopkgtestPublication(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({}))
        self.schedule = mock.Mock()
        self.patch(RequestAutopkgtestJob, "celeryRunOnCommit", self.schedule)
        self.proposal = self.factory.makeBranchMergeProposalForGit()
        removeSecurityProxy(self.proposal).source_git_commit_sha1 = "a" * 40
        self.series = self.factory.makeDistroSeries()
        self.arch = self.factory.makeDistroArchSeries(distroseries=self.series)
        removeSecurityProxy(self.series).nominatedarchindep = self.arch
        self.archive = self.factory.makeArchive(
            distribution=self.series.distribution, purpose=ArchivePurpose.PPA
        )
        self.git_build = SourcePackageGitBuild.new(
            self.proposal.source_git_repository,
            self.proposal.source_git_commit_sha1,
            self.archive,
            self.series,
            self.proposal.registrant,
            "~ppa1",
        )
        self.source = self.factory.makeSourcePackageRelease(version="1.0~ppa1")
        removeSecurityProxy(self.source).source_package_git_build = (
            self.git_build
        )
        self.validation = MergeProposalValidation(
            self.proposal,
            self.git_build.commit_sha1,
            self.archive,
            {
                "activate-workflow": "true",
                "autopkgtest-all-proposed": "false",
            },
        )
        IStore(MergeProposalValidation).add(self.validation)
        self.publication = self.makePublication()

    def makePublication(self, arch=None):
        publication = removeSecurityProxy(
            self.factory.makeBinaryPackagePublishingHistory(
                archive=self.archive,
                distroarchseries=self.arch if arch is None else arch,
                source_package_release=self.source,
                architecturespecific=True,
                status=PackagePublishingStatus.PENDING,
            )
        )
        removeSecurityProxy(publication.build).status = BuildStatus.FULLYBUILT
        return publication

    def getJobs(self):
        return [
            RequestAutopkgtestJob(job)
            for job in IStore(BranchMergeProposalJob).find(
                BranchMergeProposalJob,
                job_type=BranchMergeProposalJobType.REQUEST_AUTOPKGTEST,
            )
        ]

    def installBackend(self, backend=None):
        if backend is None:
            backend = mock.Mock()
        site_manager = getSiteManager()
        previous = site_manager.queryUtility(IRequestAutopkgtestBackend)
        site_manager.registerUtility(backend, IRequestAutopkgtestBackend)
        if previous is None:
            self.addCleanup(
                site_manager.unregisterUtility,
                backend,
                IRequestAutopkgtestBackend,
            )
        else:
            self.addCleanup(
                site_manager.registerUtility,
                previous,
                IRequestAutopkgtestBackend,
            )
        return backend

    def test_matching_publication_saves_request_identity(self):
        self.publication.setPublished()
        job = self.getJobs()[0]
        self.assertEqual(self.proposal.id, job.branch_merge_proposal.id)
        self.assertEqual(self.validation.id, job.metadata["validation_id"])
        self.assertEqual(
            self.git_build.commit_sha1, job.metadata["commit_sha1"]
        )
        self.assertEqual(self.archive.id, job.metadata["archive_id"])
        self.assertEqual(
            self.source.id, job.metadata["source_package_release_id"]
        )
        self.assertEqual(self.arch.id, job.metadata["distro_arch_series_id"])
        self.assertEqual("1.0~ppa1", job.metadata["source_package_version"])
        self.assertEqual(
            {"autopkgtest-all-proposed": "false"}, job.metadata["parameters"]
        )
        report = self.getRequestReport(job)
        self.assertEqual(RevisionStatusResult.WAITING, report.result)
        self.assertEqual(
            job.metadata["request_key"], report.properties["request_key"]
        )
        self.assertNotIn("autopkgtest", job.metadata)
        self.assertEqual([], self.getRequestWatches(job))
        self.schedule.assert_called_once_with()

    def test_publication_as_publish_distro_queues_request(self):
        with dbuser("publish_distro"):
            self.publication.setPublished()
            transaction.commit()

        self.assertEqual(
            PackagePublishingStatus.PUBLISHED, self.publication.status
        )
        [job] = self.getJobs()
        report = self.getRequestReport(job)
        self.assertEqual(RevisionStatusResult.WAITING, report.result)
        self.assertEqual(report.id, job.metadata["report_id"])
        self.assertEqual(
            report.properties["request_key"], job.metadata["request_key"]
        )
        self.schedule.assert_called_once_with()

    def test_only_autopkgtest_options_are_saved(self):
        self.validation.parameters = {
            "activate-workflow": "true",
            "autopkgtest-all-proposed": "true",
            "autopkgtest-trigger": "dependency/2.0~Test",
            "autopkgtest-future-option": "retained",
            "autopkgtest": "ignored",
            "unrelated-option": "ignored",
        }
        self.publication.setPublished()
        self.assertEqual(
            {
                "autopkgtest-all-proposed": "true",
                "autopkgtest-trigger": "dependency/2.0~Test",
                "autopkgtest-future-option": "retained",
            },
            self.getJobs()[0].metadata["parameters"],
        )

    def test_publication_without_feature_controller_queues(self):
        self.patch(features.per_thread, "features", None)
        self.publication.setPublished()
        self.assertEqual(1, len(self.getJobs()))

    def test_no_validation_does_not_queue(self):
        IStore(MergeProposalValidation).remove(self.validation)
        self.publication.setPublished()
        self.assertEqual([], self.getJobs())

    def test_cancelled_validation_does_not_queue(self):
        self.validation.status = RevisionStatusResult.CANCELLED
        self.publication.setPublished()
        self.assertEqual([], self.getJobs())

    def test_ambiguous_validations_do_not_queue(self):
        IStore(MergeProposalValidation).add(
            MergeProposalValidation(
                self.proposal, self.git_build.commit_sha1, self.archive, {}
            )
        )
        self.publication.setPublished()
        self.assertEqual([], self.getJobs())

    def test_wrong_commit_does_not_queue(self):
        self.validation.commit_sha1 = "f" * 40
        self.publication.setPublished()
        self.assertEqual([], self.getJobs())

    def test_finished_validation_does_not_queue(self):
        self.validation.date_finished = datetime.now(timezone.utc)
        self.publication.setPublished()
        self.assertEqual([], self.getJobs())

    def test_unrelated_source_repository_does_not_queue(self):
        self.git_build.git_repository = self.factory.makeGitRepository()
        self.publication.setPublished()
        self.assertEqual([], self.getJobs())

    def test_private_source_repository_does_not_queue(self):
        removeSecurityProxy(
            self.proposal.source_git_repository
        ).information_type = InformationType.USERDATA
        self.publication.setPublished()
        self.assertEqual([], self.getJobs())

    def test_private_target_repository_does_not_queue(self):
        removeSecurityProxy(
            self.proposal.target_git_repository
        ).information_type = InformationType.USERDATA
        self.publication.setPublished()
        self.assertEqual([], self.getJobs())

    def test_later_proposal_head_does_not_replace_built_commit(self):
        removeSecurityProxy(self.proposal).source_git_commit_sha1 = "f" * 40
        self.publication.setPublished()
        self.assertEqual(
            self.git_build.commit_sha1,
            self.getJobs()[0].metadata["commit_sha1"],
        )

    def test_private_archive_does_not_queue(self):
        removeSecurityProxy(self.archive).private = True
        self.publication.setPublished()
        self.assertEqual([], self.getJobs())

    def test_non_ppa_does_not_queue(self):
        self.publication.archive = self.series.distribution.main_archive
        self.publication.setPublished()
        self.assertEqual([], self.getJobs())

    def test_missing_git_build_does_not_queue(self):
        removeSecurityProxy(self.source).source_package_git_build = None
        self.publication.setPublished()
        self.assertEqual([], self.getJobs())

    def test_closed_proposal_does_not_queue(self):
        removeSecurityProxy(self.proposal).queue_status = (
            BranchMergeProposalStatus.MERGED
        )
        self.publication.setPublished()
        self.assertEqual([], self.getJobs())

    def test_multiple_binaries_for_same_architecture_share_job(self):
        self.publication.setPublished()
        report_id = self.getRequestReport(self.getJobs()[0]).id
        self.makePublication().setPublished()
        self.assertEqual(1, len(self.getJobs()))
        self.assertEqual(
            report_id, self.getRequestReport(self.getJobs()[0]).id
        )
        self.schedule.assert_called_once_with()

    def test_distinct_architectures_get_distinct_jobs(self):
        other_arch = self.factory.makeDistroArchSeries(
            distroseries=self.series
        )
        self.publication.setPublished()
        other_publication = self.makePublication(arch=other_arch)
        self.assertEqual(other_arch, other_publication.distroarchseries)
        other_publication.setPublished()
        self.assertEqual(
            {self.arch.id, other_arch.id},
            {job.metadata["distro_arch_series_id"] for job in self.getJobs()},
        )

    def test_matching_uses_destination_archive(self):
        destination = self.factory.makeArchive(
            distribution=self.series.distribution, purpose=ArchivePurpose.PPA
        )
        self.publication.archive = destination
        self.publication.copied_from_archive = self.archive
        self.validation.archive = destination
        self.publication.setPublished()
        self.assertEqual(
            destination.id, self.getJobs()[0].metadata["archive_id"]
        )

    def test_options_are_snapshotted(self):
        self.publication.setPublished()
        self.validation.parameters = {"autopkgtest-all-proposed": "true"}
        self.assertEqual(
            {"autopkgtest-all-proposed": "false"},
            self.getJobs()[0].metadata["parameters"],
        )

    def test_missing_backend_is_retryable(self):
        self.publication.setPublished()
        job = self.getJobs()[0]
        self.assertIn(AutopkgtestRequestNotReady, job.retry_error_types)
        self.assertRaises(AutopkgtestRequestNotReady, job.run)

    def test_job_delegates_to_separate_backend(self):
        backend = self.installBackend()
        self.publication.setPublished()
        job = self.getJobs()[0]
        job.run()
        backend.requestTests.assert_called_once_with(job)

    def test_cancelled_run_does_not_reach_backend(self):
        backend = self.installBackend()
        self.publication.setPublished()
        self.validation.status = RevisionStatusResult.CANCELLED
        self.getJobs()[0].run()
        backend.requestTests.assert_not_called()

    def test_job_source_preserves_interface_and_derivation(self):
        self.assertTrue(
            IBranchMergeProposalJobSource.providedBy(
                BranchMergeProposalJobSource
            )
        )
        self.publication.setPublished()
        job = self.getJobs()[0]
        source = removeSecurityProxy(getUtility(IBranchMergeProposalJobSource))
        derived = source.get(job.id)
        self.assertIsInstance(
            removeSecurityProxy(derived), RequestAutopkgtestJob
        )

    def test_changed_commit_does_not_reach_backend(self):
        backend = self.installBackend()
        self.publication.setPublished()
        self.validation.commit_sha1 = "f" * 40
        self.assertRaises(AutopkgtestRequestNotReady, self.getJobs()[0].run)
        backend.requestTests.assert_not_called()

    def test_changed_source_build_does_not_reach_backend(self):
        backend = self.installBackend()
        self.publication.setPublished()
        removeSecurityProxy(self.source).source_package_git_build = None
        self.assertRaises(AutopkgtestRequestNotReady, self.getJobs()[0].run)
        backend.requestTests.assert_not_called()

    def test_withdrawn_publication_does_not_reach_backend(self):
        backend = self.installBackend()
        self.publication.setPublished()
        self.publication.status = PackagePublishingStatus.DELETED
        self.assertRaises(AutopkgtestRequestNotReady, self.getJobs()[0].run)
        backend.requestTests.assert_not_called()

    def test_submission_without_feature_controller(self):
        job, adapter, backend = self.makeServiceJob()
        self.patch(features.per_thread, "features", None)
        job.run()
        self.assertEqual("accepted", job.metadata["autopkgtest"]["phase"])
        self.assertEqual(
            ["readiness", "submit"], [call[0] for call in adapter.calls]
        )

    def test_rollback_removes_saved_request_job(self):
        store = Store.of(self.publication)
        store.flush()
        publication_id = self.publication.id
        transaction.commit()
        self.publication.setPublished()
        self.assertEqual(1, len(self.getJobs()))
        report_id = self.getRequestReport(self.getJobs()[0]).id
        transaction.abort()
        self.assertEqual([], self.getJobs())
        self.assertIsNone(
            getUtility(IRevisionStatusReportSet).getByID(report_id)
        )
        publication = store.get(BinaryPackagePublishingHistory, publication_id)
        publication.setPublished()
        self.assertEqual(1, len(self.getJobs()))

    def makeServiceJob(self, adapter=None):
        adapter = FakeAutopkgtestAdapter() if adapter is None else adapter
        backend = self.installBackend(AutopkgtestRequestBackend(adapter))
        self.publication.setPublished()
        return self.getJobs()[0], adapter, backend

    def getRequestReport(self, job):
        metadata = job.metadata
        report_id = metadata.get("report_id")
        if report_id is None:
            report_id = metadata["autopkgtest"]["report_id"]
        return getUtility(IRevisionStatusReportSet).getByID(report_id)

    def getRequestWatches(self, job):
        watch_set = removeSecurityProxy(getUtility(IRevisionStatusWatchSet))
        watch = watch_set.findByReport(self.getRequestReport(job))
        return [] if watch is None else [watch]

    def collectRequest(self, adapter):
        collector = RevisionStatusWatchCollector(
            removeSecurityProxy(getUtility(IRevisionStatusWatchSet)),
            {"autopkgtest": AutopkgtestWatchHandler(adapter)},
        )
        collector.run()

    def test_worker_creates_report_for_legacy_unprepared_job(self):
        job, adapter, backend = self.makeServiceJob()
        metadata = job.metadata
        report = self.getRequestReport(job)
        del metadata["report_id"]
        del metadata["request_key"]
        job.context._json_data = json.dumps(metadata)
        IStore(report).remove(removeSecurityProxy(report))
        job.run()
        self.assertEqual("accepted", job.metadata["autopkgtest"]["phase"])
        self.assertEqual(
            job.metadata["report_id"],
            job.metadata["autopkgtest"]["report_id"],
        )

    def test_worker_preserves_legacy_submission_identity(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        metadata = job.metadata
        report_id = metadata.pop("report_id")
        metadata.pop("request_key")
        job.context._json_data = json.dumps(metadata)
        job.run()
        self.assertEqual(report_id, job.metadata["autopkgtest"]["report_id"])
        self.assertEqual(
            1, len([call for call in adapter.calls if call[0] == "submit"])
        )

    def test_cancellation_before_worker_cancels_waiting_report(self):
        job, adapter, backend = self.makeServiceJob()
        backend.cancelTests(job)
        self.assertEqual(
            RevisionStatusResult.CANCELLED, self.getRequestReport(job).result
        )
        job.run()
        self.assertEqual([], adapter.calls)

    def test_unconfigured_adapter_leaves_waiting_report_without_watch(self):
        self.publication.setPublished()
        job = self.getJobs()[0]
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        report = self.getRequestReport(job)
        self.assertEqual(RevisionStatusResult.WAITING, report.result)
        self.assertEqual("package-validation", report.group_name)
        self.assertEqual(self.git_build.id, report.source_package_git_build.id)
        self.assertIsNone(report.date_finished)
        self.assertEqual([], self.getRequestWatches(job))
        self.assertEqual("prepared", job.metadata["autopkgtest"]["phase"])

    def test_submission_creates_correlated_report_and_watch(self):
        job, adapter, backend = self.makeServiceJob()
        report = self.getRequestReport(job)
        request_key = job.metadata["request_key"]
        job.run()
        state = job.metadata["autopkgtest"]
        self.assertEqual(report.id, state["report_id"])
        self.assertEqual(request_key, state["request"]["key"])
        watch = self.getRequestWatches(job)[0]
        self.assertEqual("accepted", state["phase"])
        self.assertEqual(
            "package-validation", self.getRequestReport(job).group_name
        )
        self.assertEqual("autopkgtest", watch.remote_system)
        self.assertEqual(state["request"], watch.remote_ref["request"])
        self.assertEqual(state["receipt"], watch.remote_ref["receipt"])
        self.assertEqual(job.id, watch.remote_ref["job_id"])
        self.assertEqual(
            ["readiness", "submit"], [call[0] for call in adapter.calls]
        )

    def test_repeated_execution_does_not_resubmit_or_create_another_watch(
        self,
    ):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        job.run()
        self.assertEqual(1, len(self.getRequestWatches(job)))
        self.assertEqual(
            1, len([call for call in adapter.calls if call[0] == "submit"])
        )

    def test_not_ready_does_not_submit(self):
        job, adapter, backend = self.makeServiceJob()
        adapter.readiness = AutopkgtestReadiness(False)
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        self.assertEqual("prepared", job.metadata["autopkgtest"]["phase"])
        self.assertEqual([], self.getRequestWatches(job))
        self.assertEqual(["readiness"], [call[0] for call in adapter.calls])

    def test_unpublished_sibling_binary_prevents_submission(self):
        job, adapter, backend = self.makeServiceJob()
        self.makePublication()
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        self.assertEqual("prepared", job.metadata["autopkgtest"]["phase"])
        self.assertEqual([], adapter.calls)

    def test_unfinished_sibling_build_prevents_submission(self):
        job, adapter, backend = self.makeServiceJob()
        sibling = self.makePublication()
        removeSecurityProxy(sibling.build).status = BuildStatus.BUILDING
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        self.assertEqual([], adapter.calls)

    def test_runner_retries_until_all_binaries_are_published(self):
        job, adapter, backend = self.makeServiceJob()
        sibling = self.makePublication()
        request_key = job.metadata["request_key"]
        before = datetime.now(timezone.utc)
        JobRunner([job], BufferLogger()).runAll()
        self.assertEqual(JobStatus.WAITING, job.status)
        self.assertEqual(1, job.attempt_count)
        self.assertGreaterEqual(job.scheduled_start, before + job.retry_delay)
        self.assertLessEqual(
            job.scheduled_start,
            datetime.now(timezone.utc) + job.retry_delay,
        )
        self.assertIsNone(job.lease_expires)
        self.assertEqual([], list(BranchMergeProposalJobSource.iterReady()))
        self.assertEqual([], adapter.calls)
        sibling.setPublished()
        self.assertEqual(1, len(self.getJobs()))
        job.scheduled_start = datetime.now(timezone.utc) - timedelta(seconds=1)
        JobRunner.fromReady(
            BranchMergeProposalJobSource, BufferLogger()
        ).runAll()
        self.assertEqual(JobStatus.COMPLETED, job.status)
        self.assertEqual(2, job.attempt_count)
        self.assertEqual(request_key, job.metadata["request_key"])
        self.assertEqual(
            [("readiness", request_key), ("submit", request_key)],
            adapter.calls,
        )
        self.assertEqual(1, len(self.getRequestWatches(job)))

    def test_runner_retries_until_archive_is_ready(self):
        job, adapter, backend = self.makeServiceJob()
        adapter.readiness = AutopkgtestReadiness(False)
        JobRunner([job], BufferLogger()).runAll()
        self.assertEqual(JobStatus.WAITING, job.status)
        self.assertEqual(1, job.attempt_count)
        self.assertEqual([], self.getRequestWatches(job))
        adapter.readiness = AutopkgtestReadiness(True)
        job.scheduled_start = datetime.now(timezone.utc) - timedelta(seconds=1)
        JobRunner.fromReady(
            BranchMergeProposalJobSource, BufferLogger()
        ).runAll()
        self.assertEqual(JobStatus.COMPLETED, job.status)
        self.assertEqual(2, job.attempt_count)
        self.assertEqual(
            ["readiness", "readiness", "submit"],
            [call[0] for call in adapter.calls],
        )

    def test_runner_stops_after_retry_budget_is_exhausted(self):
        job, adapter, backend = self.makeServiceJob()
        self.assertEqual(25, job.max_retries)
        job.job.attempt_count = job.max_retries
        adapter.readiness = AutopkgtestReadiness(False)
        JobRunner([job], BufferLogger()).runAll()
        self.assertEqual(JobStatus.FAILED, job.status)
        self.assertEqual(26, job.attempt_count)
        self.assertEqual([], self.getRequestWatches(job))
        self.assertEqual(["readiness"], [call[0] for call in adapter.calls])

    def test_runner_reconciles_uncertain_submission_on_retry(self):
        job, adapter, backend = self.makeServiceJob()
        adapter.submission_error = TimeoutError()
        JobRunner([job], BufferLogger()).runAll()
        self.assertEqual(JobStatus.WAITING, job.status)
        job.scheduled_start = datetime.now(timezone.utc) - timedelta(seconds=1)
        JobRunner.fromReady(
            BranchMergeProposalJobSource, BufferLogger()
        ).runAll()
        self.assertEqual(JobStatus.WAITING, job.status)
        adapter.lookup = AutopkgtestLookup(
            AutopkgtestLookupStatus.ACCEPTED, adapter.receipt
        )
        job.scheduled_start = datetime.now(timezone.utc) - timedelta(seconds=1)
        JobRunner.fromReady(
            BranchMergeProposalJobSource, BufferLogger()
        ).runAll()
        self.assertEqual(JobStatus.COMPLETED, job.status)
        self.assertEqual(3, job.attempt_count)
        self.assertEqual(
            ["readiness", "submit", "reconcile", "reconcile"],
            [call[0] for call in adapter.calls],
        )
        self.assertEqual(1, len(self.getRequestWatches(job)))

    def test_failed_binary_build_returns_error_report(self):
        job, adapter, backend = self.makeServiceJob()
        removeSecurityProxy(self.publication.build).status = (
            BuildStatus.FAILEDTOBUILD
        )
        job.run()
        self.assertEqual(
            RevisionStatusResult.FAILED, self.getRequestReport(job).result
        )
        self.assertIn(
            "build failed", self.getRequestReport(job).result_summary
        )
        self.assertEqual("failed", job.metadata["autopkgtest"]["phase"])
        self.assertEqual([], adapter.calls)

    def test_failed_build_on_other_architecture_does_not_block_submission(
        self,
    ):
        job, adapter, backend = self.makeServiceJob()
        other_arch = self.factory.makeDistroArchSeries(
            distroseries=self.series
        )
        other = self.makePublication(arch=other_arch)
        removeSecurityProxy(other.build).status = BuildStatus.FAILEDTOBUILD
        job.run()
        self.assertEqual("accepted", job.metadata["autopkgtest"]["phase"])
        self.assertEqual(1, len(self.getRequestWatches(job)))
        self.assertEqual(
            ["readiness", "submit"], [call[0] for call in adapter.calls]
        )

    def test_unfinished_build_on_other_architecture_does_not_block_submission(
        self,
    ):
        job, adapter, backend = self.makeServiceJob()
        other_arch = self.factory.makeDistroArchSeries(
            distroseries=self.series
        )
        other = self.makePublication(arch=other_arch)
        removeSecurityProxy(other.build).status = BuildStatus.BUILDING
        job.run()
        self.assertEqual("accepted", job.metadata["autopkgtest"]["phase"])
        self.assertEqual(1, len(self.getRequestWatches(job)))
        self.assertEqual(
            ["readiness", "submit"], [call[0] for call in adapter.calls]
        )

    def test_request_records_expected_binaries_and_attempt_date(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        request = job.metadata["autopkgtest"]["request"]
        self.assertIn(
            self.publication.binarypackagerelease.binarypackagename.name,
            request["expected_binary_packages"],
        )
        self.assertRegex(request["date_requested"], r"^[0-9]{8}_[0-9]{6}$")

    def test_ineligible_source_gets_error_report(self):
        job, adapter, backend = self.makeServiceJob()
        adapter.readiness = AutopkgtestReadiness(False, False, "No tests")
        job.run()
        report = self.getRequestReport(job)
        self.assertEqual(RevisionStatusResult.FAILED, report.result)
        self.assertEqual("No tests", report.result_summary)
        self.assertEqual([], self.getRequestWatches(job))

    def test_timeout_reconciles_accepted_submission_without_resubmitting(self):
        job, adapter, backend = self.makeServiceJob()
        adapter.submission_error = TimeoutError()
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        self.assertEqual("submitting", job.metadata["autopkgtest"]["phase"])
        adapter.lookup = AutopkgtestLookup(
            AutopkgtestLookupStatus.ACCEPTED, adapter.receipt
        )
        job.run()
        self.assertEqual("accepted", job.metadata["autopkgtest"]["phase"])
        self.assertEqual(1, len(self.getRequestWatches(job)))
        self.assertEqual(
            1, len([call for call in adapter.calls if call[0] == "submit"])
        )

    def test_connection_error_requires_reconciliation_before_resubmitting(
        self,
    ):
        job, adapter, backend = self.makeServiceJob()
        adapter.submission_error = ConnectionError()
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        self.assertEqual("submitting", job.metadata["autopkgtest"]["phase"])
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        self.assertEqual([], self.getRequestWatches(job))
        self.assertEqual(
            ["readiness", "submit", "reconcile"],
            [call[0] for call in adapter.calls],
        )

    def test_adapter_type_error_is_not_treated_as_temporary_unavailability(
        self,
    ):
        job, adapter, backend = self.makeServiceJob()
        adapter.submission_error = TypeError("Adapter programming error")
        error = self.assertRaises(TypeError, job.run)
        self.assertIs(adapter.submission_error, error)
        self.assertEqual("submitting", job.metadata["autopkgtest"]["phase"])
        self.assertEqual([], self.getRequestWatches(job))

    def test_adapter_value_error_is_not_treated_as_temporary_unavailability(
        self,
    ):
        job, adapter, backend = self.makeServiceJob()
        adapter.submission_error = ValueError("Adapter programming error")
        error = self.assertRaises(ValueError, job.run)
        self.assertIs(adapter.submission_error, error)
        self.assertEqual("submitting", job.metadata["autopkgtest"]["phase"])
        self.assertEqual([], self.getRequestWatches(job))

    def test_uncertain_submission_is_not_repeated(self):
        job, adapter, backend = self.makeServiceJob()
        adapter.submission_error = TimeoutError()
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        self.assertEqual([], self.getRequestWatches(job))
        self.assertEqual(
            ["readiness", "submit", "reconcile"],
            [call[0] for call in adapter.calls],
        )

    def test_authoritative_absence_retries_with_same_request_key(self):
        job, adapter, backend = self.makeServiceJob()
        adapter.submission_error = TimeoutError()
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        adapter.submission_error = None
        adapter.lookup = AutopkgtestLookup(AutopkgtestLookupStatus.ABSENT)
        job.run()
        keys = [
            key for operation, key in adapter.calls if operation == "submit"
        ]
        self.assertEqual(2, len(keys))
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(1, len(self.getRequestWatches(job)))

    def test_uncertain_submission_retains_reconciliation_progress(self):
        job, adapter, backend = self.makeServiceJob()
        adapter.submission_error = TimeoutError()
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        progress = {"next_run": "next-directory", "scan_in_progress": True}
        adapter.lookup = AutopkgtestLookup(
            AutopkgtestLookupStatus.UNKNOWN, service_metadata=progress
        )
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        self.assertEqual(
            progress, job.metadata["autopkgtest"]["reconciliation"]
        )
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        self.assertEqual([None, progress], adapter.reconciliation_metadata)
        self.assertEqual("submitting", job.metadata["autopkgtest"]["phase"])
        self.assertEqual([], self.getRequestWatches(job))
        self.assertEqual(
            1, len([call for call in adapter.calls if call[0] == "submit"])
        )

    def test_verified_result_updates_report_and_removes_watch(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        adapter.result_metadata = {
            "duration_seconds": 123,
            "log_url": "https://example.test/log",
        }
        self.collectRequest(adapter)
        report = self.getRequestReport(job)
        self.assertEqual(RevisionStatusResult.SUCCEEDED, report.result)
        self.assertIsNotNone(report.date_finished)
        self.assertEqual([], self.getRequestWatches(job))
        self.assertEqual(
            self.validation.id, report.properties["validation_id"]
        )
        self.assertEqual(
            dict(adapter.receipt.service_metadata, **adapter.result_metadata),
            report.properties["autopkgtest"]["service_metadata"],
        )

    def test_infrastructure_error_retries_without_failing_the_package(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        adapter.result_status = AutopkgtestStatus.INFRASTRUCTURE_ERROR
        self.collectRequest(adapter)
        watch = self.getRequestWatches(job)[0]
        self.assertEqual(1, watch.failure_count)
        self.assertEqual(RevisionStatusResult.WAITING, watch.report.result)

    def test_wrong_result_identity_is_not_applied(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        adapter.result_override = AutopkgtestResult(
            "wrong-request",
            adapter.receipt.request_key,
            adapter.receipt.request_fingerprint,
            AutopkgtestStatus.PASSED,
        )
        self.collectRequest(adapter)
        watch = self.getRequestWatches(job)[0]
        self.assertEqual(RevisionStatusResult.WAITING, watch.report.result)
        self.assertEqual(1, watch.failure_count)

    def test_polling_preserves_updated_service_cursor(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        adapter.result_status = AutopkgtestStatus.PENDING
        adapter.result_metadata = {"cursor": "next", "result_locator": "path"}
        self.collectRequest(adapter)
        metadata = self.getRequestWatches(job)[0].remote_ref["receipt"][
            "service_metadata"
        ]
        self.assertEqual("next", metadata["cursor"])
        self.assertEqual("path", metadata["result_locator"])

    def test_cancelled_during_readiness_does_not_submit(self):
        job, adapter, backend = self.makeServiceJob()

        def cancel_validation(request):
            self.validation.status = RevisionStatusResult.CANCELLED
            transaction.commit()

        adapter.on_readiness = cancel_validation
        job.run()
        self.assertEqual(
            RevisionStatusResult.CANCELLED, self.getRequestReport(job).result
        )
        self.assertEqual([], self.getRequestWatches(job))
        self.assertEqual(["readiness"], [call[0] for call in adapter.calls])

    def test_cancelled_during_submission_retains_receipt_without_watch(self):
        job, adapter, backend = self.makeServiceJob()

        def close_proposal(request):
            removeSecurityProxy(self.proposal).queue_status = (
                BranchMergeProposalStatus.MERGED
            )
            transaction.commit()

        adapter.on_submit = close_proposal
        job.run()
        state = job.metadata["autopkgtest"]
        self.assertEqual("cancelled", state["phase"])
        self.assertEqual(
            adapter.receipt.request_id, state["receipt"]["request_id"]
        )
        self.assertEqual([], self.getRequestWatches(job))
        self.assertEqual(
            RevisionStatusResult.CANCELLED, self.getRequestReport(job).result
        )

    def test_competing_submission_generation_does_not_submit_twice(self):
        job, adapter, backend = self.makeServiceJob()

        def competing_submission(request):
            state = job.metadata["autopkgtest"]
            state["phase"] = "submitting"
            state["generation"] = 1
            job.updateAutopkgtestState(state)
            transaction.commit()

        adapter.on_readiness = competing_submission
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        self.assertEqual(["readiness"], [call[0] for call in adapter.calls])
        self.assertEqual("submitting", job.metadata["autopkgtest"]["phase"])

    def test_invalid_receipt_never_creates_a_watch(self):
        job, adapter, backend = self.makeServiceJob()

        def wrong_receipt(request):
            adapter.receipt_override = replace(
                adapter.receipt, request_key="wrong-key"
            )

        adapter.on_submit = wrong_receipt
        self.assertRaises(AutopkgtestRequestNotReady, job.run)
        self.assertEqual("submitting", job.metadata["autopkgtest"]["phase"])
        self.assertEqual([], self.getRequestWatches(job))

    def test_closed_proposal_cancels_watch_without_polling(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        removeSecurityProxy(self.proposal).queue_status = (
            BranchMergeProposalStatus.REJECTED
        )
        self.collectRequest(adapter)
        self.assertEqual([], self.getRequestWatches(job))
        self.assertEqual(
            RevisionStatusResult.CANCELLED, self.getRequestReport(job).result
        )
        self.assertEqual(
            ["readiness", "submit"], [call[0] for call in adapter.calls]
        )

    def test_closed_during_poll_does_not_apply_the_remote_result(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()

        def close_proposal(request):
            removeSecurityProxy(self.proposal).queue_status = (
                BranchMergeProposalStatus.REJECTED
            )
            transaction.commit()

        adapter.on_poll = close_proposal
        self.collectRequest(adapter)
        self.assertEqual([], self.getRequestWatches(job))
        self.assertEqual(
            RevisionStatusResult.CANCELLED, self.getRequestReport(job).result
        )

    def test_explicit_retry_gets_new_attempt_key_and_saved_options(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        self.collectRequest(adapter)
        self.validation.parameters = {"changed": True}
        retry = RequestAutopkgtestJob.retry(job)
        self.assertEqual(1, retry.metadata["attempt"])
        self.assertEqual(
            RevisionStatusResult.WAITING, self.getRequestReport(retry).result
        )
        self.assertNotIn("autopkgtest", retry.metadata)
        waiting_report_id = self.getRequestReport(retry).id
        self.assertEqual(
            job.metadata["parameters"], retry.metadata["parameters"]
        )
        retry.run()
        self.assertEqual(
            waiting_report_id, retry.metadata["autopkgtest"]["report_id"]
        )
        self.assertNotEqual(
            job.metadata["autopkgtest"]["request"]["key"],
            retry.metadata["autopkgtest"]["request"]["key"],
        )
        self.assertNotEqual(
            job.metadata["autopkgtest"]["report_id"],
            retry.metadata["autopkgtest"]["report_id"],
        )
        self.makePublication().setPublished()
        self.assertEqual(2, len(self.getJobs()))

    def test_unfinished_request_cannot_be_explicitly_retried(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        self.assertRaises(ValueError, RequestAutopkgtestJob.retry, job)
        self.assertEqual(1, len(self.getJobs()))

    def test_explicit_retry_waits_for_publication(self):
        job, adapter, backend = self.makeServiceJob()
        JobRunner([job], BufferLogger()).runAll()
        self.collectRequest(adapter)
        retry = RequestAutopkgtestJob.retry(job)
        sibling = self.makePublication()
        JobRunner([retry], BufferLogger()).runAll()
        self.assertEqual(JobStatus.WAITING, retry.status)
        self.assertEqual(1, retry.attempt_count)
        sibling.setPublished()
        self.assertEqual(2, len(self.getJobs()))
        retry.scheduled_start = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )
        JobRunner.fromReady(RequestAutopkgtestJob, BufferLogger()).runAll()
        self.assertEqual(JobStatus.COMPLETED, retry.status)
        self.assertEqual(2, retry.attempt_count)
        self.assertEqual(1, retry.metadata["attempt"])
        self.assertEqual(
            2, len([call for call in adapter.calls if call[0] == "submit"])
        )

    def makeValidationBuildReports(self):
        self.publication.pocket = self.git_build.pocket
        binary_build = removeSecurityProxy(self.publication.build)
        binary_build.pocket = self.git_build.pocket
        getUtility(IRevisionStatusReportSet).new(
            creator=self.validation.requester,
            title="source-build",
            git_repository=self.git_build.git_repository,
            commit_sha1=self.git_build.commit_sha1,
            source_package_git_build=self.git_build,
            group_name="package-validation",
            properties={"validation_id": self.validation.id},
            result=RevisionStatusResult.SUCCEEDED,
        )
        MergeProposalValidation.updateBinaryBuildReport(self.publication.build)

    def test_current_checks_select_retry_before_worker_and_keep_history(self):
        job, adapter, backend = self.makeServiceJob()
        self.makeValidationBuildReports()
        job.run()
        self.collectRequest(adapter)
        selection = selectValidationChecks(self.validation)
        self.assertEqual([], selection.issues)
        self.assertEqual(RevisionStatusResult.SUCCEEDED, selection.result)
        old_report = self.getRequestReport(job)
        retry = RequestAutopkgtestJob.retry(job)
        selection = selectValidationChecks(self.validation)
        self.assertEqual(RevisionStatusResult.WAITING, selection.result)
        self.assertIn(self.getRequestReport(retry), selection.current)
        self.assertIn(old_report, selection.history)
        self.assertNotIn(old_report, selection.current)
        removeSecurityProxy(old_report).update(
            result=RevisionStatusResult.FAILED
        )
        self.assertEqual(
            RevisionStatusResult.WAITING,
            selectValidationChecks(self.validation).result,
        )
        self.assertRaises(ValueError, RequestAutopkgtestJob.retry, job)
        retry.run()
        self.collectRequest(adapter)
        self.assertEqual(
            RevisionStatusResult.SUCCEEDED,
            selectValidationChecks(self.validation).result,
        )

    def test_current_checks_missing_attempt_report_is_incomplete(self):
        self.publication.setPublished()
        report = removeSecurityProxy(self.getRequestReport(self.getJobs()[0]))
        IStore(report).remove(report)
        self.makeValidationBuildReports()
        selection = selectValidationChecks(self.validation)
        self.assertTrue(selection.issues)
        self.assertEqual(RevisionStatusResult.WAITING, selection.result)

    def test_current_checks_include_tests_of_arch_indep_publications(self):
        other_arch = self.factory.makeDistroArchSeries(
            distroseries=self.series
        )
        binary = removeSecurityProxy(self.publication.binarypackagerelease)
        binary.architecturespecific = False
        self.makeValidationBuildReports()
        self.factory.makeBinaryPackagePublishingHistory(
            binarypackagerelease=binary,
            archive=self.archive,
            distroarchseries=other_arch,
            pocket=self.git_build.pocket,
            status=PackagePublishingStatus.PENDING,
        )
        other_publication = (
            IStore(BinaryPackagePublishingHistory)
            .find(
                BinaryPackagePublishingHistory,
                binarypackagerelease=binary,
                distroarchseries=other_arch,
            )
            .one()
        )
        job, adapter, backend = self.makeServiceJob()
        job.run()
        self.collectRequest(adapter)
        selection = selectValidationChecks(self.validation)
        self.assertEqual(
            [
                "Autopkgtest report for %s is not available yet."
                % other_arch.architecturetag
            ],
            selection.issues,
        )
        self.assertEqual(RevisionStatusResult.WAITING, selection.result)
        other_publication.setPublished()
        [other_job] = [
            candidate
            for candidate in self.getJobs()
            if candidate.metadata["distro_arch_series_id"] == other_arch.id
        ]
        adapter.result_status = AutopkgtestStatus.FAILED
        other_job.run()
        self.collectRequest(adapter)
        selection = selectValidationChecks(self.validation)
        self.assertEqual([], selection.issues)
        self.assertIn(self.getRequestReport(other_job), selection.current)
        self.assertEqual(RevisionStatusResult.FAILED, selection.result)

    def test_retry_report_is_rolled_back_with_job(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        self.collectRequest(adapter)
        retry = RequestAutopkgtestJob.retry(job)
        report_id = self.getRequestReport(retry).id
        transaction.abort()
        self.assertEqual(1, len(self.getJobs()))
        self.assertIsNone(
            getUtility(IRevisionStatusReportSet).getByID(report_id)
        )

    def test_retry_of_cancelled_unsubmitted_attempt(self):
        job, adapter, backend = self.makeServiceJob()
        backend.cancelTests(job)
        retry = RequestAutopkgtestJob.retry(job)
        self.assertEqual(
            RevisionStatusResult.WAITING, self.getRequestReport(retry).result
        )
        self.assertNotEqual(
            job.metadata["request_key"], retry.metadata["request_key"]
        )
        retry.run()
        self.assertEqual("accepted", retry.metadata["autopkgtest"]["phase"])

    def test_retry_refuses_inactive_validation(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        self.collectRequest(adapter)
        self.validation.status = RevisionStatusResult.CANCELLED
        self.assertRaises(ValueError, RequestAutopkgtestJob.retry, job)
        self.assertEqual(1, len(self.getJobs()))

    def test_retry_refuses_unpublished_package(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        self.collectRequest(adapter)
        self.publication.status = PackagePublishingStatus.SUPERSEDED
        error = self.assertRaises(ValueError, RequestAutopkgtestJob.retry, job)
        self.assertIn("not published", str(error))
        self.assertEqual(1, len(self.getJobs()))

    def test_local_cancellation_cleans_watches_without_claiming_remote_stop(
        self,
    ):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        backend.cancelTests(job)
        self.assertEqual([], self.getRequestWatches(job))
        self.assertEqual(
            RevisionStatusResult.CANCELLED, self.getRequestReport(job).result
        )
        self.assertNotIn("remote_cancellation", job.metadata["autopkgtest"])
        self.assertEqual(
            ["readiness", "submit"], [call[0] for call in adapter.calls]
        )

    def test_explicit_remote_cancellation_is_recorded_separately(self):
        job, adapter, backend = self.makeServiceJob()
        job.run()
        backend.cancelTests(job)
        response = backend.cancelRemoteTests(job)
        self.assertTrue(response.acknowledged)
        self.assertTrue(
            job.metadata["autopkgtest"]["remote_cancellation"]["acknowledged"]
        )
        self.assertEqual("cancel", adapter.calls[-1][0])

    def test_validation_cleanup_cancels_all_architecture_watches(self):
        job, adapter, backend = self.makeServiceJob()
        other_arch = self.factory.makeDistroArchSeries(
            distroseries=self.series
        )
        self.makePublication(arch=other_arch).setPublished()
        jobs = self.getJobs()
        for request_job in jobs:
            request_job.run()
        self.validation.status = RevisionStatusResult.CANCELLED
        backend.cancelForValidation(self.validation)
        self.assertEqual(2, len(jobs))
        for request_job in jobs:
            self.assertEqual([], self.getRequestWatches(request_job))
            self.assertEqual(
                RevisionStatusResult.CANCELLED,
                self.getRequestReport(request_job).result,
            )


class TestNativePublishingBase(TestCaseWithFactory, SoyuzTestPublisher):
    layer = LaunchpadZopelessLayer
    dbuser = config.archivepublisher.dbuser

    def __init__(self, methodName="runTest"):
        super().__init__(methodName=methodName)
        SoyuzTestPublisher.__init__(self)

    def setUp(self):
        """Setup a pool dir, the librarian, and instantiate the DiskPool."""
        super().setUp()
        switch_dbuser(config.archivepublisher.dbuser)
        self.prepareBreezyAutotest()
        self.config = getPubConfig(self.ubuntutest.main_archive)
        self.config.setupArchiveDirs()
        self.pool_dir = self.config.poolroot
        self.temp_dir = self.config.temproot
        self.logger = DevNullLogger()
        self.disk_pool = self.config.getDiskPool(self.logger)
        self.disk_pool.logger = self.logger

    def tearDown(self):
        """Tear down blows the pool dirs away."""
        super().tearDown()
        for root in (
            self.config.distroroot,
            config.personalpackagearchive.root,
            config.personalpackagearchive.private_root,
        ):
            if root is not None and os.path.exists(root):
                shutil.rmtree(root)

    def getPubSource(self, *args, **kwargs):
        """Overrides `SoyuzTestPublisher.getPubSource`.

        Commits the transaction before returning, this way the rest of
        the test will immediately notice the just-created records.
        """
        source = SoyuzTestPublisher.getPubSource(self, *args, **kwargs)
        self.layer.commit()
        return source

    def getPubBinaries(self, *args, **kwargs):
        """Overrides `SoyuzTestPublisher.getPubBinaries`.

        Commits the transaction before returning, this way the rest of
        the test will immediately notice the just-created records.
        """
        binaries = SoyuzTestPublisher.getPubBinaries(self, *args, **kwargs)
        self.layer.commit()
        return binaries

    def checkPublication(self, pub, status):
        """Assert the publication has the given status."""
        self.assertEqual(
            status,
            pub.status,
            "%s is not %s (%s)"
            % (pub.displayname, status.name, pub.status.name),
        )

    def checkPublications(self, pubs, status):
        """Assert the given publications have the given status.

        See `checkPublication`.
        """
        for pub in pubs:
            self.checkPublication(pub, status)

    def checkPastDate(self, date, lag=None):
        """Assert given date is older than 'now'.

        Optionally the user can pass a 'lag' which will be added to 'now'
        before comparing.
        """
        limit = datetime.now(timezone.utc)
        if lag is not None:
            limit = limit + lag
        self.assertTrue(date < limit, "%s >= %s" % (date, limit))

    def checkSuperseded(self, pubs, supersededby=None):
        self.checkPublications(pubs, PackagePublishingStatus.SUPERSEDED)
        for pub in pubs:
            self.checkPastDate(pub.datesuperseded)
            if supersededby is not None:
                if isinstance(pub, BinaryPackagePublishingHistory):
                    dominant = supersededby.binarypackagerelease.build
                else:
                    dominant = supersededby.sourcepackagerelease
                self.assertEqual(dominant, pub.supersededby)
            else:
                self.assertIsNone(pub.supersededby)


class TestNativePublishing(TestNativePublishingBase):
    def test_publish_source(self):
        # Source publications result in a PUBLISHED publishing record and
        # the corresponding files are dumped in the disk pool/.
        pub_source = self.getPubSource(filecontent=b"Hello world")
        pub_source.publish(self.disk_pool, self.logger)
        self.assertEqual(PackagePublishingStatus.PUBLISHED, pub_source.status)
        pool_path = "%s/main/f/foo/foo_666.dsc" % self.pool_dir
        with open(pool_path) as pool_file:
            self.assertEqual(pool_file.read().strip(), "Hello world")

    @mock.patch.object(ArtifactoryPool, "addFile")
    def test_publisher_skips_conda_source_packages(self, mock):
        root_url = "https://foo.example.com/artifactory/repository"
        archive = self.factory.makeArchive(
            purpose=ArchivePurpose.PPA,
            repository_format=ArchiveRepositoryFormat.CONDA,
        )
        pool = ArtifactoryPool(archive, root_url, BufferLogger())
        # getPubSource returns a SourcePackagePublishingHistory object
        pub_source = self.getPubSource(
            filecontent=b"Hello world",
            archive=archive,
            format=SourcePackageType.CI_BUILD,
            user_defined_fields=[("bogus_field", "instead_of_subdir")],
        )
        pub_source.publish(pool, self.logger)
        self.assertFalse(mock.called)

    def test_publish_binaries(self):
        # Binary publications result in a PUBLISHED publishing record and
        # the corresponding files are dumped in the disk pool/.
        pub_binary = self.getPubBinaries(filecontent=b"Hello world")[0]
        pub_binary.publish(self.disk_pool, self.logger)
        self.assertEqual(PackagePublishingStatus.PUBLISHED, pub_binary.status)
        pool_path = "%s/main/f/foo/foo-bin_666_all.deb" % self.pool_dir
        with open(pool_path) as pool_file:
            self.assertEqual(pool_file.read().strip(), "Hello world")

    def test_publish_isolated_binaries(self):
        # Some binary publications have no associated source publication
        # (e.g. Python wheels in an archive published using Artifactory).
        # In these cases, the binary package name/version is passed to the
        # pool.
        base_url = "https://foo.example.com/artifactory"
        self.pushConfig("artifactory", base_url=base_url)
        archive = self.factory.makeArchive(
            distribution=self.distroseries.distribution,
            publishing_method=ArchivePublishingMethod.ARTIFACTORY,
            repository_format=ArchiveRepositoryFormat.PYTHON,
        )
        config = getPubConfig(archive)
        disk_pool = config.getDiskPool(self.logger)
        disk_pool.logger = self.logger
        self.useFixture(FakeArtifactoryFixture(base_url, archive.name))
        with lp_dbuser():
            ci_build = self.factory.makeCIBuild(
                distro_arch_series=self.distroseries.architectures[0]
            )
            bpn = self.factory.makeBinaryPackageName(name="foo")
            bpr = self.factory.makeBinaryPackageRelease(
                binarypackagename=bpn,
                version="0.1",
                ci_build=ci_build,
                binpackageformat=BinaryPackageFormat.WHL,
            )
            lfa = self.addMockFile("foo-0.1.whl", filecontent=b"Hello world")
            bpr.addFile(lfa)
            bpph = self.factory.makeBinaryPackagePublishingHistory(
                binarypackagerelease=bpr,
                archive=archive,
                distroarchseries=self.distroseries.architectures[0],
                pocket=PackagePublishingPocket.RELEASE,
                channel="stable",
            )
        bpph.publish(disk_pool, self.logger)
        self.assertEqual(PackagePublishingStatus.PUBLISHED, bpph.status)
        pool_path = disk_pool.rootpath / "foo" / "0.1" / "foo-0.1.whl"
        with pool_path.open() as pool_file:
            self.assertEqual(b"Hello world", pool_file.read())

    def test_publish_ddeb_when_disabled_is_noop(self):
        # Publishing a DDEB publication when
        # Archive.publish_debug_symbols is false just sets PUBLISHED,
        # without a file in the pool.
        pubs = self.getPubBinaries(
            binaryname="dbg", filecontent=b"Hello world", with_debug=True
        )

        def publish_everything():
            existence_map = {}
            for pub in pubs:
                pub.publish(self.disk_pool, self.logger)
                self.assertEqual(PackagePublishingStatus.PUBLISHED, pub.status)
                filename = pub.files[0].libraryfile.filename
                path = "%s/main/d/dbg/%s" % (self.pool_dir, filename)
                existence_map[filename] = os.path.exists(path)
            return existence_map

        self.assertEqual(
            {"dbg_666_all.deb": True, "dbg-dbgsym_666_all.ddeb": False},
            publish_everything(),
        )

        pubs[0].archive.publish_debug_symbols = True

        self.assertEqual(
            {"dbg_666_all.deb": True, "dbg-dbgsym_666_all.ddeb": True},
            publish_everything(),
        )

    def testPublishingOverwriteFileInPool(self):
        """Test if publishOne refuses to overwrite a file in pool.

        Check if it also keeps the original file content.
        It's done by publishing 'foo' by-hand and ensuring it
        has a special content, then publish 'foo' again, via publisher,
        and finally check one of the 'foo' files content.
        """
        foo_path = os.path.join(self.pool_dir, "main", "f", "foo")
        os.makedirs(foo_path)
        foo_dsc_path = os.path.join(foo_path, "foo_666.dsc")
        with open(foo_dsc_path, "w") as foo_dsc:
            foo_dsc.write("Hello world")

        pub_source = self.getPubSource(filecontent=b"Something")
        pub_source.publish(self.disk_pool, self.logger)

        # An oops should be filed for the error, but we don't include the
        # SQL timeline; it may be very large and tells us nothing that we
        # can't get from the error message.
        self.assertEqual("PoolFileOverwriteError", self.oopses[0]["type"])
        self.assertEqual([], self.oopses[0]["timeline"])

        self.layer.commit()
        self.assertEqual(pub_source.status, PackagePublishingStatus.PENDING)
        with open(foo_dsc_path) as foo_dsc:
            self.assertEqual(foo_dsc.read().strip(), "Hello world")

    def testPublishingDifferentContents(self):
        """Test if publishOne refuses to overwrite its own publication."""
        pub_source = self.getPubSource(filecontent=b"foo is happy")
        pub_source.publish(self.disk_pool, self.logger)
        self.layer.commit()

        foo_name = "%s/main/f/foo/foo_666.dsc" % self.pool_dir
        IStore(pub_source).flush()
        self.assertEqual(pub_source.status, PackagePublishingStatus.PUBLISHED)
        with open(foo_name) as foo:
            self.assertEqual(foo.read().strip(), "foo is happy")

        # try to publish 'foo' again with a different content, it
        # raises internally and keeps the files with the original
        # content.
        pub_source2 = self.getPubSource(filecontent=b"foo is depressing")
        pub_source2.publish(self.disk_pool, self.logger)
        self.layer.commit()

        IStore(pub_source2).flush()
        self.assertEqual(pub_source2.status, PackagePublishingStatus.PENDING)
        with open(foo_name) as foo:
            self.assertEqual(foo.read().strip(), "foo is happy")

    def testPublishingAlreadyInPool(self):
        """Test if publishOne works if file is already in Pool.

        It should identify that the file has the same content and
        mark it as PUBLISHED.
        """
        pub_source = self.getPubSource(
            sourcename="bar", filecontent=b"bar is good"
        )
        pub_source.publish(self.disk_pool, self.logger)
        self.layer.commit()
        bar_name = "%s/main/b/bar/bar_666.dsc" % self.pool_dir
        with open(bar_name) as bar:
            self.assertEqual(bar.read().strip(), "bar is good")
        IStore(pub_source).flush()
        self.assertEqual(pub_source.status, PackagePublishingStatus.PUBLISHED)

        pub_source2 = self.getPubSource(
            sourcename="bar", filecontent=b"bar is good"
        )
        pub_source2.publish(self.disk_pool, self.logger)
        self.layer.commit()
        IStore(pub_source2).flush()
        self.assertEqual(pub_source2.status, PackagePublishingStatus.PUBLISHED)

    def testPublishingSymlink(self):
        """Test if publishOne moving publication between components.

        After check if the pool file contents as the same, it should
        create a symlink in the new pointing to the original file.
        """
        content = b"am I a file or a symbolic link ?"
        # publish sim.dsc in main and re-publish in universe
        pub_source = self.getPubSource(sourcename="sim", filecontent=content)
        pub_source2 = self.getPubSource(
            sourcename="sim", component="universe", filecontent=content
        )
        pub_source.publish(self.disk_pool, self.logger)
        pub_source2.publish(self.disk_pool, self.logger)
        self.layer.commit()

        IStore(pub_source).flush()
        IStore(pub_source2).flush()
        self.assertEqual(pub_source.status, PackagePublishingStatus.PUBLISHED)
        self.assertEqual(pub_source2.status, PackagePublishingStatus.PUBLISHED)

        # check the resulted symbolic link
        sim_universe = "%s/universe/s/sim/sim_666.dsc" % self.pool_dir
        self.assertEqual(
            os.readlink(sim_universe), "../../../main/s/sim/sim_666.dsc"
        )

        # if the contexts don't match it raises, so the publication
        # remains pending.
        pub_source3 = self.getPubSource(
            sourcename="sim",
            component="restricted",
            filecontent=b"It is all my fault",
        )
        pub_source3.publish(self.disk_pool, self.logger)
        self.layer.commit()

        IStore(pub_source3).flush()
        self.assertEqual(pub_source3.status, PackagePublishingStatus.PENDING)

    def testPublishInAnotherArchive(self):
        """Publication in another archive

        Basically test if publishing records target to other archive
        than Distribution.main_archive work as expected
        """
        cprov = getUtility(IPersonSet).getByName("cprov")
        test_pool_dir = tempfile.mkdtemp()
        test_temp_dir = tempfile.mkdtemp()
        test_disk_pool = DiskPool(
            cprov.archive, test_pool_dir, test_temp_dir, self.logger
        )

        pub_source = self.getPubSource(
            sourcename="foo",
            filecontent=b"Am I a PPA Record ?",
            archive=cprov.archive,
        )
        pub_source.publish(test_disk_pool, self.logger)
        self.layer.commit()

        IStore(pub_source).flush()
        self.assertEqual(pub_source.status, PackagePublishingStatus.PUBLISHED)
        self.assertEqual(
            pub_source.sourcepackagerelease.upload_archive, cprov.archive
        )
        foo_name = "%s/main/f/foo/foo_666.dsc" % test_pool_dir
        with open(foo_name) as foo:
            self.assertEqual(foo.read().strip(), "Am I a PPA Record ?")

        # Remove locally created dir.
        shutil.rmtree(test_pool_dir)
        shutil.rmtree(test_temp_dir)


class PublishingSetTests(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.distroseries = self.factory.makeDistroSeries()
        self.archive = self.factory.makeArchive(
            distribution=self.distroseries.distribution
        )
        self.publishing = self.factory.makeSourcePackagePublishingHistory(
            distroseries=self.distroseries, archive=self.archive
        )
        self.publishing_set = getUtility(IPublishingSet)

    def test_getByIdAndArchive_finds_record(self):
        record = self.publishing_set.getByIdAndArchive(
            self.publishing.id, self.archive
        )
        self.assertEqual(self.publishing, record)

    def test_getByIdAndArchive_finds_record_explicit_source(self):
        record = self.publishing_set.getByIdAndArchive(
            self.publishing.id, self.archive, source=True
        )
        self.assertEqual(self.publishing, record)

    def test_getByIdAndArchive_wrong_archive(self):
        wrong_archive = self.factory.makeArchive()
        record = self.publishing_set.getByIdAndArchive(
            self.publishing.id, wrong_archive
        )
        self.assertEqual(None, record)

    def makeBinaryPublishing(self):
        distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries
        )
        binary_publishing = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive, distroarchseries=distroarchseries
        )
        return binary_publishing

    def test_getByIdAndArchive_wrong_type(self):
        self.makeBinaryPublishing()
        record = self.publishing_set.getByIdAndArchive(
            self.publishing.id, self.archive, source=False
        )
        if record is not None:
            self.assertTrue(IBinaryPackagePublishingHistory.providedBy(record))

    def test_getByIdAndArchive_finds_binary(self):
        binary_publishing = self.makeBinaryPublishing()
        record = self.publishing_set.getByIdAndArchive(
            binary_publishing.id, self.archive, source=False
        )
        self.assertEqual(binary_publishing, record)

    def test_getByIdAndArchive_binary_wrong_archive(self):
        binary_publishing = self.makeBinaryPublishing()
        wrong_archive = self.factory.makeArchive()
        record = self.publishing_set.getByIdAndArchive(
            binary_publishing.id, wrong_archive, source=False
        )
        self.assertEqual(None, record)


class TestPublishingSourceEnrichments(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        """Create an archive and architecture for direct publishing queries."""
        super().setUp()
        login_admin()
        self.archive = self.factory.makeArchive()
        self.series = self.factory.makeDistroSeries(
            distribution=self.archive.distribution
        )
        self.das = self.factory.makeDistroArchSeries(distroseries=self.series)
        self.publishing_set = getUtility(IPublishingSet)

    def test_binaries_count_all_names_with_default_or_unlimited_limit(self):
        """Object/id inputs count all names in one query with either limit."""
        # Exceed the default 20-name cap by one. Zero-padded suffixes make
        # alphabetical ordering agree with the expected list below.
        names = ["binary-%02d" % index for index in range(21)]
        first = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive,
            distroarchseries=self.das,
            binarypackagename=names[0],
            status=PackagePublishingStatus.PUBLISHED,
        )
        source = self.factory.makeSPPHForBPPH(first)
        # Keep all names in the same source release and publication scope.
        # Pending binaries are active too, including the name beyond the cap.
        for name in names[1:]:
            self.factory.makeBinaryPackagePublishingHistory(
                archive=self.archive,
                distroarchseries=self.das,
                pocket=source.pocket,
                source_package_release=source.sourcepackagerelease,
                binarypackagename=name,
                status=PackagePublishingStatus.PENDING,
            )
        source_id = source.id
        # Flush fixture writes before recording so the query count measures
        # the helper alone, not inserts triggered by Storm's autoflush.
        Store.of(removeSecurityProxy(source)).flush()
        # Both accepted input forms must produce the same source-id mapping.
        for sources in ([source], [source_id]):
            # Omitted limit uses the default cap; None returns all names.
            for options, expected_names in (
                ({}, names[:20]),
                ({"limit": None}, names),
            ):
                with StormStatementRecorder() as recorder:
                    result = self.publishing_set.getActiveBinariesForSources(
                        sources, **options
                    )
                # Fetching the list and its total must not need separate
                # queries, even when the list is capped.
                self.assertThat(recorder, HasQueryCount(Equals(1)))
                # The total is computed before the cap, so it remains 21
                # whether 20 names or all 21 names are returned.
                self.assertEqual(21, result[source_id]["total"])
                # Exact equality checks both the selected names and order.
                self.assertEqual(
                    expected_names,
                    [
                        binary["binary_package_name"]
                        for binary in result[source_id]["binaries"]
                    ],
                )

    def test_binaries_match_source_archive_series_and_pocket(self):
        """Exclude binaries published in another archive, series or pocket."""
        matching = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive,
            distroarchseries=self.das,
            binarypackagename="matching",
            status=PackagePublishingStatus.PUBLISHED,
        )
        source = self.factory.makeSPPHForBPPH(matching)
        other_archive = self.factory.makeArchive(
            distribution=self.archive.distribution
        )
        other_series = self.factory.makeDistroSeries(
            distribution=self.archive.distribution
        )
        other_das = self.factory.makeDistroArchSeries(
            distroseries=other_series
        )
        # Share the source release and vary one publication field at a time
        # so each exclusion depends on archive, series or pocket filtering.
        for archive, das, pocket, name in (
            (other_archive, self.das, source.pocket, "other-archive"),
            (self.archive, other_das, source.pocket, "other-series"),
            (
                self.archive,
                self.das,
                PackagePublishingPocket.UPDATES,
                "other-pocket",
            ),
        ):
            self.factory.makeBinaryPackagePublishingHistory(
                archive=archive,
                distroarchseries=das,
                pocket=pocket,
                source_package_release=source.sourcepackagerelease,
                binarypackagename=name,
                status=PackagePublishingStatus.PUBLISHED,
            )
        result = self.publishing_set.getActiveBinariesForSources([source.id])
        self.assertEqual(
            [
                {
                    "binary_package_name": "matching",
                    "binary_publication_id": matching.id,
                }
            ],
            result[source.id]["binaries"],
        )
        self.assertEqual(1, result[source.id]["total"])

    def test_binaries_include_copies_from_other_build_contexts(self):
        """Select copied publications despite their original build context."""
        original = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive,
            distroarchseries=self.das,
            binarypackagename="copied",
            architecturespecific=False,
            status=PackagePublishingStatus.PUBLISHED,
        )
        source = self.factory.makeSPPHForBPPH(original)
        target_archive = self.factory.makeArchive(
            distribution=self.archive.distribution
        )
        target_series = self.factory.makeDistroSeries(
            distribution=self.archive.distribution
        )
        target_das = self.factory.makeDistroArchSeries(
            distroseries=target_series
        )
        copied_source = self.factory.makeSourcePackagePublishingHistory(
            archive=target_archive,
            distroseries=target_series,
            sourcepackagerelease=source.sourcepackagerelease,
            pocket=source.pocket,
            status=PackagePublishingStatus.PUBLISHED,
        )
        # A copy reuses the binary release and its original build; only the
        # publication belongs to the target archive and series.
        copied_binary = self.factory.makeBinaryPackagePublishingHistory(
            archive=target_archive,
            distroarchseries=target_das,
            binarypackagerelease=original.binarypackagerelease,
            pocket=source.pocket,
            status=PackagePublishingStatus.PUBLISHED,
        )
        result = self.publishing_set.getActiveBinariesForSources(
            [copied_source.id]
        )
        # Return the target publication id, not the original publication id.
        self.assertEqual(
            [
                {
                    "binary_package_name": "copied",
                    "binary_publication_id": copied_binary.id,
                }
            ],
            result[copied_source.id]["binaries"],
        )
        self.assertEqual(1, result[copied_source.id]["total"])

    def test_empty_inputs_do_not_query(self):
        """Both batch helpers return empty mappings without database work."""
        # Empty batches must skip the joins used for non-empty inputs.
        with StormStatementRecorder() as recorder:
            self.assertEqual(
                {}, self.publishing_set.getActiveBinariesForSources([])
            )
            self.assertEqual(
                {}, self.publishing_set.getBuildArchitecturesForSources([])
            )
        self.assertThat(recorder, HasQueryCount(Equals(0)))

    def test_architectures_select_newest_active_same_named_build(self):
        """Ignore later inactive or differently named builds when selecting."""
        name = self.factory.makeSourcePackageName(name="same-name")
        first = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive,
            distroarchseries=self.das,
            sourcepackagename=name,
            binarypackagename="same-name",
            status=PackagePublishingStatus.PUBLISHED,
        )
        source = self.factory.makeSPPHForBPPH(first)
        # A distinct binary version creates another publication for a new
        # build of the same source release and architecture.
        newest = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive,
            distroarchseries=self.das,
            pocket=source.pocket,
            source_package_release=source.sourcepackagerelease,
            binarypackagename="same-name",
            version="2.0",
            status=PackagePublishingStatus.PUBLISHED,
        )
        removeSecurityProxy(newest.build).status = BuildStatus.FULLYBUILT
        # These builds have higher ids but fail the name or status filter;
        # neither may displace the newest eligible build.
        for binary_name, version, status in (
            ("other-name", "3.0", PackagePublishingStatus.PUBLISHED),
            ("same-name", "4.0", PackagePublishingStatus.SUPERSEDED),
        ):
            self.factory.makeBinaryPackagePublishingHistory(
                archive=self.archive,
                distroarchseries=self.das,
                pocket=source.pocket,
                source_package_release=source.sourcepackagerelease,
                binarypackagename=binary_name,
                version=version,
                status=status,
            )
        for sources in ([source], [source.id]):
            result = self.publishing_set.getBuildArchitecturesForSources(
                sources
            )
            self.assertEqual(1, len(result[source.id]))
            architecture = result[source.id][0]
            self.assertEqual(
                self.das.architecturetag, architecture["architecture_tag"]
            )
            self.assertEqual(
                "Successfully built", architecture["build_status"]
            )
            # The URL identifies the selected build, not just its status.
            self.assertTrue(
                architecture["build_web_link"].endswith(
                    "/+build/%d" % newest.build.id
                )
            )


class TestPublishingSetLite(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()

    def test_requestDeletion_marks_SPPHs_deleted(self):
        spph = self.factory.makeSourcePackagePublishingHistory()
        getUtility(IPublishingSet).requestDeletion([spph], self.person)
        self.assertEqual(PackagePublishingStatus.DELETED, spph.status)

    def test_requestDeletion_leaves_other_SPPHs_alone(self):
        spph = self.factory.makeSourcePackagePublishingHistory()
        other_spph = self.factory.makeSourcePackagePublishingHistory()
        getUtility(IPublishingSet).requestDeletion([other_spph], self.person)
        self.assertEqual(PackagePublishingStatus.PENDING, spph.status)

    def test_requestDeletion_marks_BPPHs_deleted(self):
        bpph = self.factory.makeBinaryPackagePublishingHistory()
        getUtility(IPublishingSet).requestDeletion([bpph], self.person)
        self.assertEqual(PackagePublishingStatus.DELETED, bpph.status)

    def test_requestDeletion_marks_attached_BPPHs_deleted(self):
        bpph = self.factory.makeBinaryPackagePublishingHistory()
        spph = self.factory.makeSPPHForBPPH(bpph)
        getUtility(IPublishingSet).requestDeletion([spph], self.person)
        self.assertEqual(PackagePublishingStatus.DELETED, spph.status)

    def test_requestDeletion_leaves_other_BPPHs_alone(self):
        bpph = self.factory.makeBinaryPackagePublishingHistory()
        unrelated_spph = self.factory.makeSourcePackagePublishingHistory()
        getUtility(IPublishingSet).requestDeletion(
            [unrelated_spph], self.person
        )
        self.assertEqual(PackagePublishingStatus.PENDING, bpph.status)

    def test_requestDeletion_accepts_empty_sources_list(self):
        getUtility(IPublishingSet).requestDeletion([], self.person)
        # The test is that this does not fail.
        Store.of(self.person).flush()

    def test_requestDeletion_creates_DistroSeriesDifferenceJobs(self):
        dsp = self.factory.makeDistroSeriesParent()
        spph = self.factory.makeSourcePackagePublishingHistory(
            dsp.derived_series, pocket=PackagePublishingPocket.RELEASE
        )
        spn = spph.sourcepackagerelease.sourcepackagename
        getUtility(IPublishingSet).requestDeletion([spph], self.person)
        self.assertEqual(
            1,
            len(find_waiting_jobs(dsp.derived_series, spn, dsp.parent_series)),
        )

    def test_requestDeletion_disallows_unmodifiable_suites(self):
        bpph = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE
        )
        spph = self.factory.makeSourcePackagePublishingHistory(
            distroseries=bpph.distroseries,
            pocket=PackagePublishingPocket.RELEASE,
        )
        spph.distroseries.status = SeriesStatus.CURRENT
        message = "Cannot delete publications from suite '%s'" % (
            spph.distroseries.getSuite(spph.pocket)
        )
        for pub in spph, bpph:
            self.assertRaisesWithContent(
                DeletionError, message, pub.requestDeletion, self.person
            )
            self.assertRaisesWithContent(
                DeletionError, message, pub.api_requestDeletion, self.person
            )

    def test_requestDeletion_marks_debug_as_deleted(self):
        (
            matching_bpph,
            debug_matching_bpph,
        ) = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE, with_debug=True
        )
        non_match_bpph = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE
        )
        non_match_bpr = removeSecurityProxy(
            non_match_bpph.binarypackagerelease
        )
        debug_non_match_bpph = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE,
            binpackageformat=BinaryPackageFormat.DDEB,
        )
        debug_non_match_bpr = debug_non_match_bpph.binarypackagerelease
        non_match_bpr.debug_package = debug_non_match_bpr
        getUtility(IPublishingSet).requestDeletion(
            [matching_bpph, non_match_bpph], self.person
        )
        for pub in (matching_bpph, debug_matching_bpph, non_match_bpph):
            self.assertEqual(pub.status, PackagePublishingStatus.DELETED)
        self.assertEqual(
            debug_non_match_bpph.status, PackagePublishingStatus.PENDING
        )

    def test_changeOverride_also_overrides_debug_package(self):
        user = self.factory.makePerson()
        bpph, debug_bpph = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE, with_debug=True
        )
        new_section = self.factory.makeSection()
        new_bpph = bpph.changeOverride(new_section=new_section, creator=user)
        publishing_set = getUtility(IPublishingSet)
        [new_debug_bpph] = publishing_set.findCorrespondingDDEBPublications(
            [new_bpph]
        )
        self.assertEqual(new_debug_bpph.creator, user)
        self.assertEqual(new_debug_bpph.section, new_section)

    def test_requestDeletion_forbids_debug_package(self):
        bpph, debug_bpph = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE, with_debug=True
        )
        self.assertRaisesWithContent(
            DeletionError,
            "Cannot delete ddeb publications directly; delete "
            "the corresponding deb instead.",
            debug_bpph.requestDeletion,
            self.factory.makePerson(),
        )

    def test_changeOverride_forbids_debug_package(self):
        bpph, debug_bpph = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE, with_debug=True
        )
        self.assertRaisesWithContent(
            OverrideError,
            "Cannot override ddeb publications directly; "
            "override the corresponding deb instead.",
            debug_bpph.changeOverride,
            new_phased_update_percentage=20,
        )

    def makePublishedSourcePackage(self, series, pocket=None, status=None):
        # Make a published source package.
        name = self.factory.getUniqueUnicode()
        sourcepackagename = self.factory.makeSourcePackageName(name)
        component = getUtility(IComponentSet)["universe"]
        spph = self.factory.makeSourcePackagePublishingHistory(
            sourcepackagename=sourcepackagename,
            distroseries=series,
            component=component,
            pocket=pocket,
            status=status,
        )
        source_package = self.factory.makeSourcePackage(
            sourcepackagename=spph.sourcepackagename, distroseries=series
        )
        spr = spph.sourcepackagerelease
        for extension in ("dsc", "tar.gz"):
            filename = "%s_%s.%s" % (spr.name, spr.version, extension)
            spr.addFile(
                self.factory.makeLibraryFileAlias(
                    filename=filename, db_only=True
                )
            )
        return source_package

    def test_getSourcesForPublishing(self):
        # PublisherSet.getSourcesForPublishing returns all the ISPPH records
        # in a given publishing context.  It is used as part of publishing
        # some types of archives.
        # XXX cjwatson 2022-03-28: Detach test from sampledata.
        ubuntu = getUtility(IDistributionSet)["ubuntu"]
        hoary = ubuntu["hoary"]
        component_main = getUtility(IComponentSet)["main"]
        component_multiverse = getUtility(IComponentSet)["multiverse"]
        debian_archive = getUtility(IDistributionSet)["debian"].main_archive
        publishing_set = getUtility(IPublishingSet)

        spphs = publishing_set.getSourcesForPublishing(
            archive=hoary.main_archive,
            distroseries=hoary,
            pocket=PackagePublishingPocket.RELEASE,
            component=component_main,
        )
        self.assertEqual(6, spphs.count())
        self.assertContentEqual(
            [
                "alsa-utils",
                "evolution",
                "libstdc++",
                "linux-source-2.6.15",
                "netapplet",
                "pmount",
            ],
            {spph.sourcepackagerelease.name for spph in spphs},
        )
        self.assertEqual(
            0,
            publishing_set.getSourcesForPublishing(
                archive=hoary.main_archive,
                distroseries=hoary,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_multiverse,
            ).count(),
        )
        self.assertEqual(
            0,
            publishing_set.getSourcesForPublishing(
                archive=hoary.main_archive,
                distroseries=hoary,
                pocket=PackagePublishingPocket.BACKPORTS,
                component=component_main,
            ).count(),
        )
        self.assertEqual(
            0,
            publishing_set.getSourcesForPublishing(
                archive=debian_archive,
                distroseries=hoary,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_main,
            ).count(),
        )
        self.assertEqual(
            14,
            publishing_set.getSourcesForPublishing(
                archive=hoary.main_archive
            ).count(),
        )

    def test_getSourcesForPublishing_query_count(self):
        # Check that the number of queries required to publish source
        # packages is constant in the number of source packages.
        series = self.factory.makeDistroSeries()
        archive = series.main_archive
        component_universe = getUtility(IComponentSet)["universe"]

        def get_index_stanzas():
            for spp in getUtility(IPublishingSet).getSourcesForPublishing(
                archive=archive,
                distroseries=series,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_universe,
            ):
                build_source_stanza_fields(
                    spp.sourcepackagerelease, spp.component, spp.section
                )

        recorder1, recorder2 = record_two_runs(
            get_index_stanzas,
            partial(
                self.makePublishedSourcePackage,
                series=series,
                pocket=PackagePublishingPocket.RELEASE,
                status=PackagePublishingStatus.PUBLISHED,
            ),
            5,
            5,
        )
        self.assertThat(recorder1, HasQueryCount(Equals(8)))
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    def makePublishedBinaryPackage(self, das, pocket=None, status=None):
        # Make a published binary package.
        source = self.makePublishedSourcePackage(
            das.distroseries, pocket=pocket, status=status
        )
        spr = source.distinctreleases[0]
        binarypackagename = self.factory.makeBinaryPackageName(source.name)
        bpph = self.factory.makeBinaryPackagePublishingHistory(
            binarypackagename=binarypackagename,
            distroarchseries=das,
            component=spr.component,
            section_name=spr.section.name,
            status=status,
            pocket=pocket,
            source_package_release=spr,
        )
        bpr = bpph.binarypackagerelease
        filename = "%s_%s_%s.deb" % (
            bpr.name,
            bpr.version,
            das.architecturetag,
        )
        bpr.addFile(
            self.factory.makeLibraryFileAlias(filename=filename, db_only=True)
        )
        return bpph

    def test_getBinariesForPublishing(self):
        # PublisherSet.getBinariesForPublishing returns all the IBPPH
        # records in a given publishing context.  It is used as part of
        # publishing some types of archives.
        # XXX cjwatson 2022-03-28: Detach test from sampledata.
        ubuntu = getUtility(IDistributionSet)["ubuntu"]
        warty = ubuntu["warty"]
        warty_i386 = warty["i386"]
        warty_another = self.factory.makeDistroArchSeries(distroseries=warty)
        component_main = getUtility(IComponentSet)["main"]
        component_multiverse = getUtility(IComponentSet)["multiverse"]
        debian_archive = getUtility(IDistributionSet)["debian"].main_archive
        publishing_set = getUtility(IPublishingSet)

        bpphs = publishing_set.getBinariesForPublishing(
            archive=warty.main_archive,
            distroarchseries=warty_i386,
            pocket=PackagePublishingPocket.RELEASE,
            component=component_main,
        )
        self.assertEqual(8, bpphs.count())
        self.assertIn(
            "mozilla-firefox",
            {bpph.binarypackagerelease.name for bpph in bpphs},
        )
        self.assertEqual(
            0,
            publishing_set.getBinariesForPublishing(
                archive=warty.main_archive,
                distroarchseries=warty_another,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_main,
            ).count(),
        )
        self.assertEqual(
            0,
            publishing_set.getBinariesForPublishing(
                archive=warty.main_archive,
                distroarchseries=warty_i386,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_multiverse,
            ).count(),
        )
        self.assertEqual(
            0,
            publishing_set.getBinariesForPublishing(
                archive=warty.main_archive,
                distroarchseries=warty_i386,
                pocket=PackagePublishingPocket.BACKPORTS,
                component=component_main,
            ).count(),
        )
        self.assertEqual(
            0,
            publishing_set.getBinariesForPublishing(
                archive=debian_archive,
                distroarchseries=warty_i386,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_main,
            ).count(),
        )
        self.assertEqual(
            12,
            publishing_set.getBinariesForPublishing(
                archive=warty.main_archive
            ).count(),
        )

    def test_getBinariesForPublishing_query_count(self):
        # Check that the number of queries required to publish binary
        # packages is constant in the number of binary packages.
        das = self.factory.makeDistroArchSeries()
        archive = das.main_archive
        component_universe = getUtility(IComponentSet)["universe"]

        def get_index_stanzas():
            for bpp in getUtility(IPublishingSet).getBinariesForPublishing(
                archive=archive,
                distroarchseries=das,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_universe,
            ):
                build_binary_stanza_fields(
                    bpp.binarypackagerelease,
                    bpp.component,
                    bpp.section,
                    bpp.priority,
                    bpp.phased_update_percentage,
                    False,
                )

        recorder1, recorder2 = record_two_runs(
            get_index_stanzas,
            partial(
                self.makePublishedBinaryPackage,
                das=das,
                pocket=PackagePublishingPocket.RELEASE,
                status=PackagePublishingStatus.PUBLISHED,
            ),
            5,
            5,
        )
        self.assertThat(recorder1, HasQueryCount(Equals(11)))
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))


class TestSourceDomination(TestNativePublishingBase):
    """Test SourcePackagePublishingHistory.supersede() operates correctly."""

    def testSupersede(self):
        """Check that supersede() without arguments works."""
        source = self.getPubSource()
        source.supersede()
        self.checkSuperseded([source])

    def testSupersedeWithDominant(self):
        """Check that supersede() with a dominant publication works."""
        source = self.getPubSource()
        super_source = self.getPubSource()
        source.supersede(super_source)
        self.checkSuperseded([source], super_source)

    def testSupersedingSupersededSourceFails(self):
        """Check that supersede() fails with a superseded source.

        Sources should not be superseded twice. If a second attempt is made,
        the Dominator's lookups are buggy.
        """
        source = self.getPubSource()
        super_source = self.getPubSource()
        source.supersede(super_source)
        self.checkSuperseded([source], super_source)

        # Manually set a date in the past, so we can confirm that
        # the second supersede() fails properly.
        source.datesuperseded = datetime(2006, 12, 25, tzinfo=timezone.utc)
        super_date = source.datesuperseded

        self.assertRaises(AssertionError, source.supersede, super_source)
        self.checkSuperseded([source], super_source)
        self.assertEqual(super_date, source.datesuperseded)


class TestBinaryDomination(TestNativePublishingBase):
    """Test BinaryPackagePublishingHistory.supersede() operates correctly."""

    def testSupersede(self):
        """Check that supersede() without arguments works."""
        bins = self.getPubBinaries(architecturespecific=True)
        bins[0].supersede()
        self.checkSuperseded([bins[0]])
        self.checkPublication(bins[1], PackagePublishingStatus.PENDING)

    def testSupersedeWithDominant(self):
        """Check that supersede() with a dominant publication works."""
        bins = self.getPubBinaries(architecturespecific=True)
        super_bins = self.getPubBinaries(architecturespecific=True)
        bins[0].supersede(super_bins[0])
        self.checkSuperseded([bins[0]], super_bins[0])
        self.checkPublication(bins[1], PackagePublishingStatus.PENDING)

    def testSupersedingSupersededArchSpecificBinaryFails(self):
        """Check that supersede() fails with a superseded arch-dep binary.

        Architecture-specific binaries should not normally be superseded
        twice. If a second attempt is made, the Dominator's lookups are buggy.
        """
        bin = self.getPubBinaries(architecturespecific=True)[0]
        super_bin = self.getPubBinaries(architecturespecific=True)[0]
        bin.supersede(super_bin)

        # Manually set a date in the past, so we can confirm that
        # the second supersede() fails properly.
        bin.datesuperseded = datetime(2006, 12, 25, tzinfo=timezone.utc)
        super_date = bin.datesuperseded

        self.assertRaises(AssertionError, bin.supersede, super_bin)
        self.checkSuperseded([bin], super_bin)
        self.assertEqual(super_date, bin.datesuperseded)

    def testSkipsSupersededArchIndependentBinary(self):
        """Check that supersede() skips a superseded arch-indep binary.

        Since all publications of an architecture-independent binary are
        superseded atomically, they may be superseded again later. In that
        case, we skip the domination, leaving the old date unchanged.
        """
        bin = self.getPubBinaries(architecturespecific=False)[0]
        super_bin = self.getPubBinaries(architecturespecific=False)[0]
        bin.supersede(super_bin)
        self.checkSuperseded([bin], super_bin)

        # Manually set a date in the past, so we can confirm that
        # the second supersede() skips properly.
        bin.datesuperseded = datetime(2006, 12, 25, tzinfo=timezone.utc)
        super_date = bin.datesuperseded

        bin.supersede(super_bin)
        self.checkSuperseded([bin], super_bin)
        self.assertEqual(super_date, bin.datesuperseded)

    def testSupersedesCorrespondingDDEB(self):
        """Check that supersede() takes with it any corresponding DDEB.

        DDEB publications should be superseded when their corresponding DEB
        is.
        """
        # Each of these will return (i386 deb, i386 ddeb, hppa deb,
        # hppa ddeb).
        bins = self.getPubBinaries(architecturespecific=True, with_debug=True)
        super_bins = self.getPubBinaries(
            architecturespecific=True, with_debug=True
        )

        bins[0].supersede(super_bins[0])
        self.checkSuperseded(bins[:2], super_bins[0])
        self.checkPublications(bins[2:], PackagePublishingStatus.PENDING)
        self.checkPublications(super_bins, PackagePublishingStatus.PENDING)

        bins[2].supersede(super_bins[2])
        self.checkSuperseded(bins[:2], super_bins[0])
        self.checkSuperseded(bins[2:], super_bins[2])
        self.checkPublications(super_bins, PackagePublishingStatus.PENDING)

    def testDDEBsCannotSupersede(self):
        """Check that DDEBs cannot supersede other publications.

        Since DDEBs are superseded when their DEBs are, there's no need to
        for them supersede anything themselves. Any such attempt is an error.
        """
        # This will return (i386 deb, i386 ddeb, hppa deb, hppa ddeb).
        bins = self.getPubBinaries(architecturespecific=True, with_debug=True)
        self.assertRaises(AssertionError, bins[0].supersede, bins[1])


class TestBinaryGetOtherPublications(TestNativePublishingBase):
    """Test BinaryPackagePublishingHistory.getOtherPublications() works."""

    def checkOtherPublications(self, this, others):
        self.assertContentEqual(
            removeSecurityProxy(this).getOtherPublications(), others
        )

    def testFindsOtherArchIndepPublications(self):
        """Arch-indep publications with the same overrides should be found."""
        bins = self.getPubBinaries(architecturespecific=False)
        self.checkOtherPublications(bins[0], bins)

    def testDoesntFindArchSpecificPublications(self):
        """Arch-dep publications shouldn't be found."""
        bins = self.getPubBinaries(architecturespecific=True)
        self.checkOtherPublications(bins[0], [bins[0]])

    def testDoesntFindPublicationsInOtherArchives(self):
        """Publications in other archives shouldn't be found."""
        bins = self.getPubBinaries(architecturespecific=False)
        foreign_bins = bins[0].copyTo(
            bins[0].distroarchseries.distroseries,
            bins[0].pocket,
            self.factory.makeArchive(
                distribution=(
                    bins[0].distroarchseries.distroseries.distribution
                )
            ),
        )
        self.checkOtherPublications(bins[0], bins)
        self.checkOtherPublications(foreign_bins[0], foreign_bins)

    def testDoesntFindPublicationsWithDifferentOverrides(self):
        """Publications with different overrides shouldn't be found."""
        bins = self.getPubBinaries(architecturespecific=False)
        universe = getUtility(IComponentSet)["universe"]
        foreign_bin = bins[0].changeOverride(new_component=universe)
        self.checkOtherPublications(bins[0], bins)
        self.checkOtherPublications(foreign_bin, [foreign_bin])

    def testDoesntFindSupersededPublications(self):
        """Superseded publications shouldn't be found."""
        bins = self.getPubBinaries(architecturespecific=False)
        self.checkOtherPublications(bins[0], bins)
        for bpph in bins:
            bpph.supersede()
        self.checkOtherPublications(bins[0], [])

    def testDoesntFindPublicationsInOtherSeries(self):
        """Publications in other series shouldn't be found."""
        bins = self.getPubBinaries(architecturespecific=False)
        series = self.factory.makeDistroSeries(
            distribution=bins[0].archive.distribution
        )
        self.factory.makeDistroArchSeries(
            distroseries=series, architecturetag="i386"
        )
        foreign_bins = bins[0].copyTo(series, bins[0].pocket, bins[0].archive)
        self.checkOtherPublications(bins[0], bins)
        self.checkOtherPublications(foreign_bins[0], foreign_bins)


class TestGetOtherPublicationsForSameSource(TestNativePublishingBase):
    """Test parts of the BinaryPackagePublishingHistory model.

    See also lib/lp/soyuz/doc/publishing.rst
    """

    layer = LaunchpadZopelessLayer

    def _makeMixedSingleBuildPackage(self, version="1.0"):
        # Set up a source with a build that generated four binaries,
        # two of them an arch-all.
        foo_src_pub = self.getPubSource(
            sourcename="foo",
            version=version,
            architecturehintlist="i386",
            status=PackagePublishingStatus.PUBLISHED,
        )
        [foo_bin_pub] = self.getPubBinaries(
            binaryname="foo-bin",
            status=PackagePublishingStatus.PUBLISHED,
            architecturespecific=True,
            version=version,
            pub_source=foo_src_pub,
        )
        # Now need to grab the build for the source so we can add
        # more binaries to it.
        [build] = foo_src_pub.getBuilds()
        foo_one_common = self.factory.makeBinaryPackageRelease(
            binarypackagename="foo-one-common",
            version=version,
            build=build,
            architecturespecific=False,
        )
        foo_one_common_pubs = self.publishBinaryInArchive(
            foo_one_common,
            self.ubuntutest.main_archive,
            pocket=foo_src_pub.pocket,
            status=PackagePublishingStatus.PUBLISHED,
        )
        foo_two_common = self.factory.makeBinaryPackageRelease(
            binarypackagename="foo-two-common",
            version=version,
            build=build,
            architecturespecific=False,
        )
        foo_two_common_pubs = self.publishBinaryInArchive(
            foo_two_common,
            self.ubuntutest.main_archive,
            pocket=foo_src_pub.pocket,
            status=PackagePublishingStatus.PUBLISHED,
        )
        foo_three = self.factory.makeBinaryPackageRelease(
            binarypackagename="foo-three",
            version=version,
            build=build,
            architecturespecific=True,
        )
        [foo_three_pub] = self.publishBinaryInArchive(
            foo_three,
            self.ubuntutest.main_archive,
            pocket=foo_src_pub.pocket,
            status=PackagePublishingStatus.PUBLISHED,
        )
        # So now we have source foo, which has arch specific binaries
        # foo-bin and foo-three, and arch:all binaries foo-one-common and
        # foo-two-common. The latter two will have multiple publications,
        # one for each DAS in the series.
        return (
            foo_src_pub,
            foo_bin_pub,
            foo_one_common_pubs,
            foo_two_common_pubs,
            foo_three_pub,
        )


class TestGetBuiltBinaries(TestNativePublishingBase):
    """Test SourcePackagePublishingHistory.getBuiltBinaries() works."""

    def test_flat_query_count(self):
        spph = self.getPubSource(architecturehintlist="any")
        store = Store.of(spph)
        store.flush()
        store.invalidate()

        # An initial invocation issues one query for each of the SPPH, BPPHs
        # and BPRs, as well as one query to check whether the SPR came from
        # a CI build.
        with StormStatementRecorder() as recorder:
            bins = spph.getBuiltBinaries()
        self.assertEqual(0, len(bins))
        self.assertThat(recorder, HasQueryCount(Equals(4)))

        self.getPubBinaries(pub_source=spph)
        store.flush()
        store.invalidate()

        # A subsequent invocation with files preloaded queries the SPPH,
        # BPPHs, BPRs, BPFs and LFAs, as well as one query to check whether
        # the SPR came from a CI build.  Checking the filenames of each BPF
        # has no query penalty.
        with StormStatementRecorder() as recorder:
            bins = spph.getBuiltBinaries(want_files=True)
            self.assertEqual(2, len(bins))
            for bpph in bins:
                files = bpph.binarypackagerelease.files
                self.assertEqual(1, len(files))
                for bpf in files:
                    bpf.libraryfile.filename
        self.assertThat(recorder, HasQueryCount(Equals(6)))

    def test_ci_build(self):
        with lp_dbuser():
            distroseries = self.factory.makeDistroSeries()
            dases = [
                self.factory.makeDistroArchSeries(distroseries=distroseries)
                for _ in range(2)
            ]
            archive = self.factory.makeArchive(
                distribution=distroseries.distribution
            )
            repository = self.factory.makeGitRepository()
            commit_sha1 = hashlib.sha1(
                self.factory.getUniqueBytes()
            ).hexdigest()
            builds = [
                self.factory.makeCIBuild(
                    git_repository=repository,
                    commit_sha1=commit_sha1,
                    distro_arch_series=das,
                )
                for das in dases
            ]
            spn = self.factory.makeSourcePackageName()
            spr = builds[0].createSourcePackageRelease(
                distroseries,
                spn,
                "1.0",
                creator=repository.owner,
                archive=archive,
            )
            spph = self.factory.makeSourcePackagePublishingHistory(
                archive=archive,
                sourcepackagerelease=spr,
                format=SourcePackageType.CI_BUILD,
            )
            bpn = self.factory.makeBinaryPackageName()
            bpphs = []
            for build in builds:
                bpr = build.createBinaryPackageRelease(
                    bpn,
                    "1.0",
                    "test summary",
                    "test description",
                    BinaryPackageFormat.WHL,
                    True,
                )
                bpphs.extend(
                    getUtility(IPublishingSet).publishBinaries(
                        archive,
                        distroseries,
                        spph.pocket,
                        {bpr: (None, None, None, None)},
                    )
                )

        self.assertContentEqual(bpphs, spph.getBuiltBinaries())


class TestGetActiveArchSpecificPublications(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def makeSPR(self):
        """Create a `SourcePackageRelease`."""
        # Return an un-proxied SPR.  This test is for script code; it
        # won't get proxied objects in real life.
        return removeSecurityProxy(self.factory.makeSourcePackageRelease())

    def makeBPPHs(self, spr, number=1):
        """Create `BinaryPackagePublishingHistory` object(s).

        Each of the publications will be active and architecture-specific.
        Each will be for the same archive, distroseries, and pocket.

        Since the tests need to create a pocket mismatch, it is guaranteed
        that the BPPHs are for the UPDATES pocket.
        """
        das = self.factory.makeDistroArchSeries()
        distroseries = das.distroseries
        archive = distroseries.main_archive
        pocket = PackagePublishingPocket.UPDATES

        bpbs = [
            self.factory.makeBinaryPackageBuild(
                source_package_release=spr, distroarchseries=das
            )
            for counter in range(number)
        ]
        bprs = [
            self.factory.makeBinaryPackageRelease(
                build=bpb, architecturespecific=True
            )
            for bpb in bpbs
        ]

        return [
            removeSecurityProxy(
                self.factory.makeBinaryPackagePublishingHistory(
                    archive=archive,
                    distroarchseries=das,
                    pocket=pocket,
                    binarypackagerelease=bpr,
                    status=PackagePublishingStatus.PUBLISHED,
                )
            )
            for bpr in bprs
        ]

    def test_getActiveArchSpecificPublications_finds_only_matches(self):
        spr = self.makeSPR()
        bpphs = self.makeBPPHs(spr, 5)

        # This BPPH will match our search.
        match = bpphs[0]

        distroseries = match.distroseries
        distro = distroseries.distribution

        # These BPPHs will not match our search, each because they fail
        # one search parameter.
        bpphs[1].archive = self.factory.makeArchive(
            distribution=distro, purpose=ArchivePurpose.PARTNER
        )
        bpphs[2].distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=self.factory.makeDistroSeries(distribution=distro)
        )
        bpphs[3].pocket = PackagePublishingPocket.SECURITY
        bpphs[4].binarypackagerelease.architecturespecific = False

        self.assertContentEqual(
            [match],
            getUtility(IPublishingSet).getActiveArchSpecificPublications(
                spr, match.archive, match.distroseries, match.pocket
            ),
        )

    def test_getActiveArchSpecificPublications_detects_absence(self):
        spr = self.makeSPR()
        distroseries = spr.upload_distroseries
        result = getUtility(IPublishingSet).getActiveArchSpecificPublications(
            spr,
            distroseries.main_archive,
            distroseries,
            self.factory.getAnyPocket(),
        )
        self.assertFalse(result.any())

    def test_getActiveArchSpecificPublications_filters_status(self):
        spr = self.makeSPR()
        bpphs = self.makeBPPHs(spr, len(PackagePublishingStatus.items))
        for bpph, status in zip(bpphs, PackagePublishingStatus.items):
            bpph.status = status
        by_status = {bpph.status: bpph for bpph in bpphs}
        self.assertContentEqual(
            [by_status[status] for status in active_publishing_status],
            getUtility(IPublishingSet).getActiveArchSpecificPublications(
                spr, bpphs[0].archive, bpphs[0].distroseries, bpphs[0].pocket
            ),
        )


class TestPublishBinaries(TestCaseWithFactory):
    """Test PublishingSet.publishBinaries() works."""

    layer = LaunchpadZopelessLayer

    def makeArgs(self, bprs, distroseries, archive=None, channel=None):
        """Create a dict of arguments for publishBinaries."""
        if archive is None:
            archive = distroseries.main_archive
        args = {
            "archive": archive,
            "distroseries": distroseries,
            "pocket": (
                PackagePublishingPocket.BACKPORTS
                if channel is None
                else PackagePublishingPocket.RELEASE
            ),
            "binaries": {
                bpr: (
                    self.factory.makeComponent(),
                    self.factory.makeSection(),
                    PackagePublishingPriority.REQUIRED,
                    50,
                )
                for bpr in bprs
            },
        }
        if channel is not None:
            args["channel"] = channel
        return args

    def test_architecture_dependent(self):
        # Architecture-dependent binaries get created as PENDING in the
        # corresponding architecture of the destination series and pocket,
        # with the given overrides.
        arch_tag = self.factory.getUniqueString("arch-")
        orig_das = self.factory.makeDistroArchSeries(architecturetag=arch_tag)
        target_das = self.factory.makeDistroArchSeries(
            architecturetag=arch_tag
        )
        build = self.factory.makeBinaryPackageBuild(distroarchseries=orig_das)
        bpr = self.factory.makeBinaryPackageRelease(
            build=build, architecturespecific=True
        )
        args = self.makeArgs([bpr], target_das.distroseries)
        [bpph] = getUtility(IPublishingSet).publishBinaries(**args)
        overrides = args["binaries"][bpr]
        self.assertEqual(bpr, bpph.binarypackagerelease)
        self.assertEqual(
            (args["archive"], target_das, args["pocket"], None),
            (bpph.archive, bpph.distroarchseries, bpph.pocket, bpph.channel),
        )
        self.assertEqual(
            overrides,
            (
                bpph.component,
                bpph.section,
                bpph.priority,
                bpph.phased_update_percentage,
            ),
        )
        self.assertEqual(PackagePublishingStatus.PENDING, bpph.status)

    def test_architecture_variant(self):
        # When a package is not built for a variant, the binaries for
        # the underlying architecture are published to the variant DAS.
        arch_tag = self.factory.getUniqueString("arch-")
        orig_das = self.factory.makeDistroArchSeries(architecturetag=arch_tag)
        target_das = self.factory.makeDistroArchSeries(
            architecturetag=arch_tag
        )
        target_variant_das = self.factory.makeDistroArchSeries(
            distroseries=target_das.distroseries,
            architecturetag=arch_tag + "v2",
            underlying_architecturetag=arch_tag,
        )
        dasf = self.factory.makeDistroArchSeriesFilter(
            distroarchseries=target_variant_das,
            sense=DistroArchSeriesFilterSense.EXCLUDE,
        )
        build = self.factory.makeBinaryPackageBuild(distroarchseries=orig_das)
        dasf.packageset.add([build.source_package_release.sourcepackagename])
        bpr = self.factory.makeBinaryPackageRelease(
            build=build, architecturespecific=True
        )
        args = self.makeArgs([bpr], target_das.distroseries)
        bpphes = list(getUtility(IPublishingSet).publishBinaries(**args))
        self.assertEqual(len(bpphes), 2)
        actual_target_dases = {bpph.distroarchseries for bpph in bpphes}
        actual_bprs = {bpph.binarypackagerelease for bpph in bpphes}
        self.assertEqual(actual_target_dases, {target_das, target_variant_das})
        self.assertEqual(actual_bprs, {bpr})

    def test_architecture_independent(self):
        # Architecture-independent binaries get published to all enabled
        # DASes in the series.
        bpr = self.factory.makeBinaryPackageRelease(architecturespecific=False)
        # Create 3 architectures. The binary will not be published in
        # the disabled one.
        target_das_a = self.factory.makeDistroArchSeries()
        target_das_b = self.factory.makeDistroArchSeries(
            distroseries=target_das_a.distroseries
        )
        # We don't reference target_das_c so it doesn't get a name.
        self.factory.makeDistroArchSeries(
            distroseries=target_das_a.distroseries, enabled=False
        )
        args = self.makeArgs([bpr], target_das_a.distroseries)
        bpphs = getUtility(IPublishingSet).publishBinaries(**args)
        self.assertEqual(2, len(bpphs))
        self.assertContentEqual(
            (target_das_a, target_das_b),
            [bpph.distroarchseries for bpph in bpphs],
        )

    def test_architecture_disabled(self):
        # An empty list is return if the DistroArchSeries was disabled.
        arch_tag = self.factory.getUniqueString("arch-")
        orig_das = self.factory.makeDistroArchSeries(architecturetag=arch_tag)
        target_das = self.factory.makeDistroArchSeries(
            architecturetag=arch_tag
        )
        build = self.factory.makeBinaryPackageBuild(distroarchseries=orig_das)
        bpr = self.factory.makeBinaryPackageRelease(
            build=build, architecturespecific=True
        )
        target_das.enabled = False
        args = self.makeArgs([bpr], target_das.distroseries)
        results = getUtility(IPublishingSet).publishBinaries(**args)
        self.assertEqual([], results)

    def test_does_not_duplicate(self):
        # An attempt to copy something for a second time is ignored.
        bpr = self.factory.makeBinaryPackageRelease()
        target_das = self.factory.makeDistroArchSeries()
        args = self.makeArgs([bpr], target_das.distroseries)
        [new_bpph] = getUtility(IPublishingSet).publishBinaries(**args)
        self.assertContentEqual(
            [], getUtility(IPublishingSet).publishBinaries(**args)
        )

        # But changing the target (eg. to RELEASE instead of BACKPORTS)
        # causes a new publication to be created.
        args["pocket"] = PackagePublishingPocket.RELEASE
        [another_bpph] = getUtility(IPublishingSet).publishBinaries(**args)

    def test_channel(self):
        bpr = self.factory.makeBinaryPackageRelease(
            binpackageformat=BinaryPackageFormat.WHL
        )
        target_das = self.factory.makeDistroArchSeries()
        args = self.makeArgs([bpr], target_das.distroseries, channel="stable")
        [bpph] = getUtility(IPublishingSet).publishBinaries(**args)
        self.assertEqual(bpr, bpph.binarypackagerelease)
        self.assertEqual(
            (args["archive"], target_das, args["pocket"], args["channel"]),
            (bpph.archive, bpph.distroarchseries, bpph.pocket, bpph.channel),
        )
        self.assertEqual(PackagePublishingStatus.PENDING, bpph.status)

    def test_does_not_duplicate_by_channel(self):
        bpr = self.factory.makeBinaryPackageRelease(
            binpackageformat=BinaryPackageFormat.WHL
        )
        target_das = self.factory.makeDistroArchSeries()
        args = self.makeArgs([bpr], target_das.distroseries, channel="stable")
        [bpph] = getUtility(IPublishingSet).publishBinaries(**args)
        self.assertContentEqual(
            [], getUtility(IPublishingSet).publishBinaries(**args)
        )
        args["channel"] = "edge"
        [another_bpph] = getUtility(IPublishingSet).publishBinaries(**args)


class TestChangeOverride(TestNativePublishingBase):
    """Test that changing overrides works."""

    def setUpOverride(
        self,
        status=SeriesStatus.DEVELOPMENT,
        pocket=PackagePublishingPocket.RELEASE,
        channel=None,
        binary=False,
        format=None,
        ddeb=False,
        **kwargs,
    ):
        self.distroseries.status = status
        get_pub_kwargs = {"pocket": pocket, "channel": channel}
        if format is not None:
            get_pub_kwargs["format"] = format
        if ddeb:
            pub = self.getPubBinaries(with_debug=True, **get_pub_kwargs)[2]
            self.assertEqual(
                BinaryPackageFormat.DDEB,
                pub.binarypackagerelease.binpackageformat,
            )
        elif binary:
            pub = self.getPubBinaries(**get_pub_kwargs)[0]
        else:
            pub = self.getPubSource(**get_pub_kwargs)
        return pub.changeOverride(**kwargs)

    def assertCanOverride(
        self,
        status=SeriesStatus.DEVELOPMENT,
        pocket=PackagePublishingPocket.RELEASE,
        channel=None,
        **kwargs,
    ):
        new_pub = self.setUpOverride(
            status=status, pocket=pocket, channel=channel, **kwargs
        )
        self.assertEqual(new_pub.status, PackagePublishingStatus.PENDING)
        self.assertEqual(new_pub.pocket, pocket)
        self.assertEqual(new_pub.channel, channel)
        if "new_component" in kwargs:
            self.assertEqual(kwargs["new_component"], new_pub.component.name)
        if "new_section" in kwargs:
            self.assertEqual(kwargs["new_section"], new_pub.section.name)
        if "new_priority" in kwargs:
            self.assertEqual(
                kwargs["new_priority"], new_pub.priority.name.lower()
            )
        if "new_phased_update_percentage" in kwargs:
            self.assertEqual(
                kwargs["new_phased_update_percentage"],
                new_pub.phased_update_percentage,
            )
        return new_pub

    def assertCannotOverride(self, **kwargs):
        self.assertRaises(OverrideError, self.setUpOverride, **kwargs)

    def test_changes_source(self):
        # SPPH.changeOverride changes the properties of source publications.
        self.assertCanOverride(new_component="universe", new_section="misc")

    def test_changes_binary(self):
        # BPPH.changeOverride changes the properties of binary publications.
        self.assertCanOverride(
            binary=True,
            new_component="universe",
            new_section="misc",
            new_priority="extra",
            new_phased_update_percentage=90,
        )

    def test_change_binary_logged_in_user(self):
        person = self.factory.makePerson()
        new_pub = self.assertCanOverride(
            binary=True,
            new_component="universe",
            new_section="misc",
            new_priority="extra",
            new_phased_update_percentage=90,
            creator=person,
        )
        self.assertEqual(person, new_pub.creator)

    def test_change_source_logged_in_user(self):
        person = self.factory.makePerson()
        new_pub = self.assertCanOverride(
            binary=False,
            new_component="universe",
            new_section="misc",
            creator=person,
        )
        self.assertEqual(person, new_pub.creator)

    def test_set_and_clear_phased_update_percentage(self):
        # new_phased_update_percentage=<integer> sets a phased update
        # percentage; new_phased_update_percentage=100 clears it.
        pub = self.assertCanOverride(
            binary=True, new_phased_update_percentage=50
        )
        new_pub = pub.changeOverride(new_phased_update_percentage=100)
        self.assertIsNone(new_pub.phased_update_percentage)

    def test_no_change(self):
        # changeOverride does not create a new publication if the existing
        # publication is already in the desired state.
        self.assertIsNone(
            self.setUpOverride(new_component="main", new_section="base")
        )
        self.assertIsNone(
            self.setUpOverride(
                binary=True,
                new_component="main",
                new_section="base",
                new_priority="standard",
            )
        )

    def test_forbids_stable_RELEASE(self):
        # changeOverride is not allowed in the RELEASE pocket of a stable
        # distroseries.
        self.assertCannotOverride(
            status=SeriesStatus.CURRENT, new_component="universe"
        )
        self.assertCannotOverride(
            status=SeriesStatus.CURRENT, binary=True, new_component="universe"
        )

    def test_allows_development_RELEASE(self):
        # changeOverride is allowed in the RELEASE pocket of a development
        # distroseries.
        self.assertCanOverride(new_component="universe")
        self.assertCanOverride(binary=True, new_component="universe")

    def test_allows_stable_PROPOSED(self):
        # changeOverride is allowed in the PROPOSED pocket of a stable
        # distroseries.
        self.assertCanOverride(
            status=SeriesStatus.CURRENT,
            pocket=PackagePublishingPocket.PROPOSED,
            new_component="universe",
        )
        self.assertCanOverride(
            status=SeriesStatus.CURRENT,
            pocket=PackagePublishingPocket.PROPOSED,
            binary=True,
            new_component="universe",
        )

    def test_forbids_changing_archive(self):
        # changeOverride refuses to make changes that would require changing
        # archive.
        self.assertCannotOverride(new_component="partner")
        self.assertCannotOverride(binary=True, new_component="partner")

    def test_preserves_channel(self):
        self.assertCanOverride(
            binary=True,
            format=BinaryPackageFormat.WHL,
            channel="stable",
            new_component="universe",
            new_section="misc",
            new_priority="extra",
            new_phased_update_percentage=90,
        )


class TestPublishingHistoryView(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def test_constant_query_counts_on_publishing_history_change_override(self):
        admin = self.factory.makeAdministrator()
        normal_user = self.factory.makePerson()

        with person_logged_in(admin):
            test_publisher = SoyuzTestPublisher()
            test_publisher.prepareBreezyAutotest()

            source_pub = test_publisher.getPubSource(
                "test-history", status=PackagePublishingStatus.PUBLISHED
            )
        url = (
            "http://launchpad.test/ubuntutest/+source/test-history"
            "/+publishinghistory"
        )

        def insert_more_publish_history():
            person1 = self.factory.makePerson()
            new_component = (
                "universe" if source_pub.component.name == "main" else "main"
            )
            source_pub.changeOverride(
                new_component=new_component, creator=person1
            )

            person2 = self.factory.makePerson()
            new_section = (
                "web" if source_pub.section.name == "base" else "base"
            )
            source_pub.changeOverride(new_section=new_section, creator=person2)

        def show_page():
            self.getUserBrowser(url, normal_user)

        # Make sure to have all the history fitting in one page.
        self.pushConfig("launchpad", default_batch_size=50)

        recorder1, recorder2 = record_two_runs(
            show_page,
            insert_more_publish_history,
            1,
            10,
            login_method=login_admin,
        )

        self.assertThat(recorder1, HasQueryCount(Equals(26)))
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))


class TestGetRecentSourceUploads(TestCaseWithFactory):
    """Tests for IPublishingSet.getRecentSourceUploads."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.distroseries = self.factory.makeDistroSeries()
        self.publishing_set = getUtility(IPublishingSet)

    def _makeSpph(self, **kwargs):
        """Create an SPPH in the test distroseries' main archive."""
        kwargs.setdefault("distroseries", self.distroseries)
        kwargs.setdefault("pocket", PackagePublishingPocket.RELEASE)
        if "archive" not in kwargs:
            kwargs["archive"] = self.distroseries.main_archive
        return self.factory.makeSourcePackagePublishingHistory(**kwargs)

    def test_getRecentSourceUploads_empty(self):
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual([], result)

    def test_getRecentSourceUploads_active_only(self):
        self._makeSpph(status=PackagePublishingStatus.PENDING)
        self._makeSpph(status=PackagePublishingStatus.PUBLISHED)
        self._makeSpph(status=PackagePublishingStatus.SUPERSEDED)
        self._makeSpph(status=PackagePublishingStatus.DELETED)
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(2, len(result))

    def test_getRecentSourceUploads_distro_archive_only(self):
        self._makeSpph()
        ppa = self.factory.makeArchive(
            distribution=self.distroseries.distribution,
            purpose=ArchivePurpose.PPA,
        )
        self._makeSpph(archive=ppa)
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(1, len(result))

    def test_getRecentSourceUploads_limit(self):
        for _ in range(25):
            self._makeSpph()
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(20, len(result))

    def test_getRecentSourceUploads_ordering(self):
        now = datetime.now(timezone.utc)
        spph_old = self._makeSpph(
            date_uploaded=now - timedelta(days=2),
        )
        spph_new = self._makeSpph(
            date_uploaded=now,
        )
        spph_mid = self._makeSpph(
            date_uploaded=now - timedelta(days=1),
        )
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(3, len(result))
        self.assertEqual(
            spph_new.sourcepackagerelease.version, result[0]["version"]
        )
        self.assertEqual(
            spph_mid.sourcepackagerelease.version, result[1]["version"]
        )
        self.assertEqual(
            spph_old.sourcepackagerelease.version, result[2]["version"]
        )

    def test_getRecentSourceUploads_dict_shape(self):
        self._makeSpph()
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(1, len(result))
        row = result[0]
        self.assertIn("name", row)
        self.assertIn("version", row)
        self.assertIn("pocket_title", row)
        self.assertIn("builds", row)
        self.assertIsInstance(row["builds"], list)

    def test_getRecentSourceUploads_pocket_title(self):
        self._makeSpph(pocket=PackagePublishingPocket.PROPOSED)
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual("Proposed", result[0]["pocket_title"])

    def test_getRecentSourceUploads_no_builds(self):
        self._makeSpph()
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual([], result[0]["builds"])

    def test_getRecentSourceUploads_with_builds(self):
        spph = self._makeSpph()
        das1 = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries, architecturetag="amd64"
        )
        das2 = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries, architecturetag="arm64"
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das1,
            archive=spph.archive,
            status=BuildStatus.FULLYBUILT,
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das2,
            archive=spph.archive,
            status=BuildStatus.FAILEDTOBUILD,
        )
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(1, len(result))
        builds = result[0]["builds"]
        self.assertEqual(2, len(builds))
        # Ordered by arch_tag ascending.
        self.assertEqual("amd64", builds[0]["arch_tag"])
        self.assertEqual(BuildStatus.FULLYBUILT, builds[0]["build_status"])
        self.assertEqual("arm64", builds[1]["arch_tag"])
        self.assertEqual(BuildStatus.FAILEDTOBUILD, builds[1]["build_status"])

    def test_getRecentSourceUploads_with_in_progress_builds(self):
        spph = self._makeSpph()
        das_amd64 = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries, architecturetag="amd64"
        )
        das_arm64 = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries, architecturetag="arm64"
        )
        das_riscv = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries, architecturetag="riscv64"
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das_amd64,
            archive=spph.archive,
            status=BuildStatus.FULLYBUILT,
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das_arm64,
            archive=spph.archive,
            status=BuildStatus.BUILDING,
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das_riscv,
            archive=spph.archive,
            status=BuildStatus.NEEDSBUILD,
        )
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(1, len(result))
        builds = result[0]["builds"]
        self.assertEqual(3, len(builds))
        # Ordered by arch_tag ascending.
        self.assertEqual("amd64", builds[0]["arch_tag"])
        self.assertEqual(BuildStatus.FULLYBUILT, builds[0]["build_status"])
        self.assertEqual("arm64", builds[1]["arch_tag"])
        self.assertEqual(BuildStatus.BUILDING, builds[1]["build_status"])
        self.assertEqual("riscv64", builds[2]["arch_tag"])
        self.assertEqual(BuildStatus.NEEDSBUILD, builds[2]["build_status"])

    def test_getRecentSourceUploads_person_filter(self):
        person = self.factory.makePerson()
        other = self.factory.makePerson()
        self._makeSpph(creator=person)
        self._makeSpph(creator=other)
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, creator=person
        )
        self.assertEqual(1, len(result))

    def test_getRecentSourceUploads_person_no_uploads(self):
        person = self.factory.makePerson()
        self._makeSpph()
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, creator=person
        )
        self.assertEqual([], result)

    def test_getRecentSourceUploads_person_none_returns_all(self):
        self._makeSpph(creator=self.factory.makePerson())
        self._makeSpph(creator=self.factory.makePerson())
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, creator=None
        )
        self.assertEqual(2, len(result))

    def test_getRecentSourceUploads_offset_skips_records(self):
        now = datetime.now(timezone.utc)
        for i in range(5):
            self._makeSpph(date_uploaded=now - timedelta(days=i))
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, offset=2
        )
        self.assertEqual(3, len(result))

    def test_getRecentSourceUploads_offset_preserves_order(self):
        now = datetime.now(timezone.utc)
        spphs = [
            self._makeSpph(date_uploaded=now - timedelta(days=i))
            for i in range(4)
        ]
        # Full result ordered newest-first: [0, 1, 2, 3]
        # offset=1 should return [1, 2, 3]
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, offset=1
        )
        self.assertEqual(3, len(result))
        self.assertEqual(
            spphs[1].sourcepackagerelease.version, result[0]["version"]
        )
        self.assertEqual(
            spphs[3].sourcepackagerelease.version, result[2]["version"]
        )

    def test_getRecentSourceUploads_offset_beyond_end_returns_empty(self):
        for _ in range(3):
            self._makeSpph()
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, offset=10
        )
        self.assertEqual([], result)

    def test_getRecentSourceUploads_offset_and_limit(self):
        now = datetime.now(timezone.utc)
        spphs = [
            self._makeSpph(date_uploaded=now - timedelta(days=i))
            for i in range(6)
        ]
        # offset=2, limit=2 should return records [2, 3]
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, offset=2, limit=2
        )
        self.assertEqual(2, len(result))
        self.assertEqual(
            spphs[2].sourcepackagerelease.version, result[0]["version"]
        )
        self.assertEqual(
            spphs[3].sourcepackagerelease.version, result[1]["version"]
        )


class TestGetPocketCountsForDistro(TestCaseWithFactory):
    """Tests for IPublishingSet.getPocketCountsForDistro."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.distribution = self.factory.makeDistribution()
        self.distroseries = self.factory.makeDistroSeries(
            distribution=self.distribution
        )
        self.other_distroseries = self.factory.makeDistroSeries(
            distribution=self.distribution
        )
        self.publishing_set = getUtility(IPublishingSet)

    def _makeSpph(self, **kwargs):
        """Create an SPPH in the appropriate distro archive by default."""
        distroseries = kwargs.setdefault("distroseries", self.distroseries)
        kwargs.setdefault("pocket", PackagePublishingPocket.RELEASE)
        kwargs.setdefault("status", PackagePublishingStatus.PUBLISHED)
        if "archive" not in kwargs:
            kwargs["archive"] = distroseries.main_archive
        return self.factory.makeSourcePackagePublishingHistory(**kwargs)

    def test_getPocketCountsForDistro_distribution(self):
        self._makeSpph(pocket=PackagePublishingPocket.RELEASE)
        self._makeSpph(pocket=PackagePublishingPocket.PROPOSED)
        self._makeSpph(
            distroseries=self.other_distroseries,
            pocket=PackagePublishingPocket.PROPOSED,
        )
        counts = self.publishing_set.getPocketCountsForDistro(
            self.distribution
        )
        self.assertEqual(1, counts.get(PackagePublishingPocket.RELEASE, 0))
        self.assertEqual(2, counts.get(PackagePublishingPocket.PROPOSED, 0))

    def test_getPocketCountsForDistro_distroseries(self):
        self._makeSpph(pocket=PackagePublishingPocket.RELEASE)
        self._makeSpph(pocket=PackagePublishingPocket.UPDATES)
        self._makeSpph(
            distroseries=self.other_distroseries,
            pocket=PackagePublishingPocket.SECURITY,
        )
        counts = self.publishing_set.getPocketCountsForDistro(
            self.distroseries
        )
        self.assertEqual(1, counts.get(PackagePublishingPocket.RELEASE, 0))
        self.assertEqual(1, counts.get(PackagePublishingPocket.UPDATES, 0))
        self.assertEqual(0, counts.get(PackagePublishingPocket.SECURITY))

    def test_getPocketCountsForDistro_counts_all_pockets_across_statuses(self):
        self._makeSpph(
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PENDING,
        )
        self._makeSpph(
            pocket=PackagePublishingPocket.PROPOSED,
            status=PackagePublishingStatus.PUBLISHED,
        )
        self._makeSpph(
            pocket=PackagePublishingPocket.UPDATES,
            status=PackagePublishingStatus.SUPERSEDED,
        )
        self._makeSpph(
            pocket=PackagePublishingPocket.SECURITY,
            status=PackagePublishingStatus.DELETED,
        )
        self._makeSpph(
            pocket=PackagePublishingPocket.BACKPORTS,
            status=PackagePublishingStatus.OBSOLETE,
        )
        self._makeSpph(
            pocket=PackagePublishingPocket.PROPOSED,
            status=PackagePublishingStatus.PENDING,
        )
        counts = self.publishing_set.getPocketCountsForDistro(
            self.distroseries
        )
        self.assertEqual(1, counts.get(PackagePublishingPocket.RELEASE, 0))
        self.assertEqual(2, counts.get(PackagePublishingPocket.PROPOSED, 0))
        self.assertEqual(1, counts.get(PackagePublishingPocket.UPDATES, 0))
        self.assertEqual(1, counts.get(PackagePublishingPocket.SECURITY, 0))
        self.assertEqual(1, counts.get(PackagePublishingPocket.BACKPORTS, 0))

    def test_getPocketCountsForDistro_distro_archive_only(self):
        self._makeSpph(pocket=PackagePublishingPocket.RELEASE)
        ppa = self.factory.makeArchive(
            distribution=self.distribution,
            purpose=ArchivePurpose.PPA,
        )
        self._makeSpph(
            archive=ppa,
            pocket=PackagePublishingPocket.PROPOSED,
        )
        counts = self.publishing_set.getPocketCountsForDistro(
            self.distroseries
        )
        self.assertEqual(1, counts.get(PackagePublishingPocket.RELEASE, 0))
        self.assertEqual(0, counts.get(PackagePublishingPocket.PROPOSED))

    def test_getPocketCountsForDistro_empty(self):
        counts = self.publishing_set.getPocketCountsForDistro(
            self.distroseries
        )
        self.assertEqual(
            {pocket: 0 for pocket in PackagePublishingPocket.items},
            counts,
        )

    def test_getPocketCountsForDistro_includes_zero_pockets(self):
        self._makeSpph(pocket=PackagePublishingPocket.RELEASE)
        counts = self.publishing_set.getPocketCountsForDistro(
            self.distroseries
        )
        self.assertEqual(0, counts.get(PackagePublishingPocket.PROPOSED))
        self.assertEqual(0, counts.get(PackagePublishingPocket.UPDATES))
        self.assertEqual(
            {pocket for pocket in PackagePublishingPocket.items},
            set(counts.keys()),
        )

    def test_getPocketCountsForDistro_filters_by_statuses(self):
        self._makeSpph(
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PUBLISHED,
        )
        self._makeSpph(
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PENDING,
        )
        self._makeSpph(
            pocket=PackagePublishingPocket.PROPOSED,
            status=PackagePublishingStatus.SUPERSEDED,
        )
        self._makeSpph(
            pocket=PackagePublishingPocket.UPDATES,
            status=PackagePublishingStatus.DELETED,
        )
        counts = self.publishing_set.getPocketCountsForDistro(
            self.distroseries,
            statuses=[
                PackagePublishingStatus.PUBLISHED,
                PackagePublishingStatus.PENDING,
            ],
        )
        self.assertEqual(2, counts.get(PackagePublishingPocket.RELEASE, 0))
        self.assertEqual(0, counts.get(PackagePublishingPocket.PROPOSED, 0))
        self.assertEqual(0, counts.get(PackagePublishingPocket.UPDATES, 0))
        self.assertEqual(
            {pocket for pocket in PackagePublishingPocket.items},
            set(counts.keys()),
        )
