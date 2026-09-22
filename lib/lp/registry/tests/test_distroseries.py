# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for distroseries."""

__all__ = [
    "CurrentSourceReleasesMixin",
]

import json
from datetime import date, datetime, timedelta, timezone

import transaction
from testtools.matchers import Equals
from zope.component import getUtility
from zope.schema.interfaces import IInt, TooSmall
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.bugs.interfaces.bugtargetparent import IBugTargetParent
from lp.bugs.interfaces.bugtask import BugTaskImportance, BugTaskStatus
from lp.buildmaster.enums import BuildStatus
from lp.registry.errors import NoSuchDistroSeries
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.distroseries import (
    IDistroSeriesPublic,
    IDistroSeriesSet,
)
from lp.registry.interfaces.externalpackage import ExternalPackageType
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.database.interfaces import IStore
from lp.services.webapp.interfaces import OAuthPermission
from lp.services.webapp.publisher import canonical_url
from lp.soyuz.enums import (
    ArchivePurpose,
    IndexCompressionType,
    PackagePublishingStatus,
)
from lp.soyuz.interfaces.archive import IArchiveSet
from lp.soyuz.interfaces.buildrecords import IHasBuildRecords
from lp.soyuz.interfaces.component import IComponentSet
from lp.soyuz.interfaces.distributionjob import (
    IInitializeDistroSeriesJobSource,
)
from lp.soyuz.interfaces.distributionsourcepackagerelease import (
    IDistributionSourcePackageRelease,
)
from lp.soyuz.interfaces.publishing import active_publishing_status
from lp.soyuz.tests.test_publishing import SoyuzTestPublisher
from lp.testing import (
    ANONYMOUS,
    StormStatementRecorder,
    TestCase,
    TestCaseWithFactory,
    admin_logged_in,
    api_url,
    login,
    person_logged_in,
)
from lp.testing.layers import (
    BaseLayer,
    DatabaseFunctionalLayer,
    LaunchpadFunctionalLayer,
)
from lp.testing.matchers import HasQueryCount
from lp.testing.pages import webservice_for_person
from lp.translations.interfaces.distroserieslanguage import (
    IDistroSeriesLanguageEntry,
)
from lp.translations.interfaces.translations import (
    TranslationsBranchImportMode,
)


class CurrentSourceReleasesMixin:
    """Mixin class for current source release tests.

    Used by tests of DistroSeries and Distribution.  The mixin must not extend
    TestCase or it will be run by other modules when imported.
    """

    def setUp(self):
        # Log in as an admin, so that we can create distributions.
        super().setUp()
        login("foo.bar@canonical.com")
        self.publisher = SoyuzTestPublisher()
        self.factory = self.publisher.factory
        self.development_series = self.publisher.setUpDefaultDistroSeries()
        self.distribution = self.development_series.distribution
        self.published_package = self.target.getSourcePackage(
            self.publisher.default_package_name
        )
        login(ANONYMOUS)

    def assertCurrentVersion(self, expected_version, package_name=None):
        """Assert the current version of a package is the expected one.

        It uses getCurrentSourceReleases() to get the version.

        If package_name isn't specified, the test publisher's default
        name is used.
        """
        if package_name is None:
            package_name = self.publisher.default_package_name
        package = self.target.getSourcePackage(package_name)
        releases = self.target.getCurrentSourceReleases(
            [package.sourcepackagename]
        )
        self.assertEqual(releases[package].version, expected_version)

    def test_one_release(self):
        # If there is one published version, that one will be returned.
        self.publisher.getPubSource(version="0.9")
        self.assertCurrentVersion("0.9")

    def test_return_value(self):
        # getCurrentSourceReleases() returns a dict. The corresponding
        # source package is used as the key, with
        # a DistributionSourcePackageRelease as the values.
        self.publisher.getPubSource(version="0.9")
        releases = self.target.getCurrentSourceReleases(
            [self.published_package.sourcepackagename]
        )
        self.assertTrue(self.published_package in releases)
        self.assertTrue(
            self.release_interface.providedBy(releases[self.published_package])
        )

    def test_latest_version(self):
        # If more than one version is published, the latest one is
        # returned.
        self.publisher.getPubSource(version="0.9")
        self.publisher.getPubSource(version="1.0")
        self.assertCurrentVersion("1.0")

    def test_active_publishing_status(self):
        # Every status defined in active_publishing_status is considered
        # when checking for the current release.
        self.publisher.getPubSource(version="0.9")
        for minor_version, status in enumerate(active_publishing_status):
            latest_version = "1.%s" % minor_version
            self.publisher.getPubSource(version=latest_version, status=status)
            self.assertCurrentVersion(latest_version)

    def test_not_active_publishing_status(self):
        # Every status not defined in active_publishing_status is
        # ignored when checking for the current release.
        self.publisher.getPubSource(version="0.9")
        for minor_version, status in enumerate(PackagePublishingStatus.items):
            if status in active_publishing_status:
                continue
            self.publisher.getPubSource(
                version="1.%s" % minor_version, status=status
            )
            self.assertCurrentVersion("0.9")

    def test_ignore_other_package_names(self):
        # Packages with different names don't affect the returned
        # version.
        self.publisher.getPubSource(version="0.9", sourcename="foo")
        self.publisher.getPubSource(version="1.0", sourcename="bar")
        self.assertCurrentVersion("0.9", package_name="foo")

    def ignore_other_distributions(self):
        # Packages with the same name in other distributions don't
        # affect the returned version.
        series_in_other_distribution = self.factory.makeDistroSeries()
        self.publisher.getPubSource(version="0.9")
        self.publisher.getPubSource(
            version="1.0", distroseries=series_in_other_distribution
        )
        self.assertCurrentVersion("0.9")

    def test_ignore_ppa(self):
        # PPA packages having the same name don't affect the returned
        # version.
        ppa_uploader = self.factory.makePerson()
        ppa_archive = getUtility(IArchiveSet).new(
            purpose=ArchivePurpose.PPA,
            owner=ppa_uploader,
            distribution=self.distribution,
        )
        self.publisher.getPubSource(version="0.9")
        self.publisher.getPubSource(version="1.0", archive=ppa_archive)
        self.assertCurrentVersion("0.9")

    def test_get_multiple(self):
        # getCurrentSourceReleases() allows you to get information about
        # the current release for multiple packages at the same time.
        # This is done using a single DB query, making it more efficient
        # than using IDistributionSource.currentrelease.
        self.publisher.getPubSource(version="0.9", sourcename="foo")
        self.publisher.getPubSource(version="1.0", sourcename="bar")
        foo_package = self.distribution.getSourcePackage("foo")
        bar_package = self.distribution.getSourcePackage("bar")
        releases = self.distribution.getCurrentSourceReleases(
            [foo_package.sourcepackagename, bar_package.sourcepackagename]
        )
        self.assertEqual(releases[foo_package].version, "0.9")
        self.assertEqual(releases[bar_package].version, "1.0")


class TestDistroSeriesCurrentSourceReleases(
    CurrentSourceReleasesMixin, TestCase
):
    """Test for DistroSeries.getCurrentSourceReleases()."""

    layer = LaunchpadFunctionalLayer
    release_interface = IDistributionSourcePackageRelease

    @property
    def target(self):
        return self.development_series


class TestDistroSeries(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def _makeBugTask(
        self, distroseries, owner, status, importance, milestone=None
    ):
        task = self.factory.makeBugTask(owner=owner, target=distroseries)
        with person_logged_in(owner):
            if milestone is not None:
                task.transitionToMilestone(milestone, milestone.target.owner)
            if task.status != status:
                task.transitionToStatus(status)
            if task.importance != importance:
                task.transitionToImportance(importance)
        return task

    def test_getSuite_release_pocket(self):
        # The suite of a distro series and the release pocket is the name of
        # the distroseries.
        distroseries = self.factory.makeDistroSeries()
        self.assertEqual(
            distroseries.name,
            distroseries.getSuite(PackagePublishingPocket.RELEASE),
        )

    def test_getSuite_non_release_pocket(self):
        # The suite of a distro series and a non-release pocket is the name of
        # the distroseries followed by a hyphen and the name of the pocket in
        # lower case.
        distroseries = self.factory.makeDistroSeries()
        pocket = PackagePublishingPocket.PROPOSED
        suite = "%s-%s" % (distroseries.name, pocket.name.lower())
        self.assertEqual(suite, distroseries.getSuite(pocket))

    def test_getDistroArchSeriesByProcessor(self):
        # A IDistroArchSeries can be retrieved by processor.
        distroseries = self.factory.makeDistroSeries()
        processor = self.factory.makeProcessor()
        distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag="i386",
            processor=processor,
        )
        self.assertEqual(
            distroarchseries,
            distroseries.getDistroArchSeriesByProcessor(processor),
        )

    def test_getDistroArchSeriesByProcessor_none(self):
        # getDistroArchSeriesByProcessor returns None when no distroarchseries
        # is found
        distroseries = self.factory.makeDistroSeries()
        processor = self.factory.makeProcessor()
        self.assertIs(
            None, distroseries.getDistroArchSeriesByProcessor(processor)
        )

    def test_getDerivedSeries(self):
        dsp = self.factory.makeDistroSeriesParent()
        self.assertEqual(
            [dsp.derived_series], dsp.parent_series.getDerivedSeries()
        )

    def test_registrant_owner_differ(self):
        # The registrant is the creator whereas the owner is the
        # distribution's owner.
        registrant = self.factory.makePerson()
        distroseries = self.factory.makeDistroSeries(registrant=registrant)
        self.assertEqual(distroseries.distribution.owner, distroseries.owner)
        self.assertEqual(registrant, distroseries.registrant)
        self.assertNotEqual(distroseries.registrant, distroseries.owner)

    def test_isDerivedSeries(self):
        # The series method isInitializing() returns True only if the series
        # has one or more parent series.
        distroseries = self.factory.makeDistroSeries()
        self.assertFalse(distroseries.isDerivedSeries())
        self.factory.makeDistroSeriesParent(derived_series=distroseries)
        self.assertTrue(distroseries.isDerivedSeries())

    def test_inherit_overrides_from_parents(self):
        # inherit_overrides_from_parents is an accessor which maps to
        # all of the series' DistroSeriesParent.inherit_overrides, for
        # UI simplicity for now.
        distroseries = self.factory.makeDistroSeries()
        dsp1 = self.factory.makeDistroSeriesParent(
            derived_series=distroseries, inherit_overrides=True
        )
        dsp2 = self.factory.makeDistroSeriesParent(
            derived_series=distroseries, inherit_overrides=False
        )
        self.assertEqual(True, distroseries.inherit_overrides_from_parents)
        with person_logged_in(distroseries.distribution.owner):
            distroseries.inherit_overrides_from_parents = False
        self.assertEqual(False, dsp1.inherit_overrides)
        self.assertEqual(False, dsp2.inherit_overrides)
        self.assertEqual(False, distroseries.inherit_overrides_from_parents)
        with person_logged_in(distroseries.distribution.owner):
            distroseries.inherit_overrides_from_parents = True
        self.assertEqual(True, dsp1.inherit_overrides)
        self.assertEqual(True, dsp2.inherit_overrides)
        self.assertEqual(True, distroseries.inherit_overrides_from_parents)

    def test_isInitializing(self):
        # The series method isInitializing() returns True only if there is an
        # initialization job with a pending status attached to this series.
        distroseries = self.factory.makeDistroSeries()
        parent_distroseries = self.factory.makeDistroSeries()
        self.assertFalse(distroseries.isInitializing())
        job_source = getUtility(IInitializeDistroSeriesJobSource)
        job = job_source.create(distroseries, [parent_distroseries.id])
        self.assertTrue(distroseries.isInitializing())
        job.start()
        self.assertTrue(distroseries.isInitializing())
        job.queue()
        self.assertTrue(distroseries.isInitializing())
        job.start()
        job.complete()
        self.assertFalse(distroseries.isInitializing())

    def test_isInitialized(self):
        # The series method isInitialized() returns True once the series has
        # been initialized.
        distroseries = self.factory.makeDistroSeries()
        self.assertFalse(distroseries.isInitialized())
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries, archive=distroseries.main_archive
        )
        self.assertTrue(distroseries.isInitialized())

    def test_getInitializationJob(self):
        # getInitializationJob() returns the most recent
        # `IInitializeDistroSeriesJob` for the given series.
        distroseries = self.factory.makeDistroSeries()
        parent_distroseries = self.factory.makeDistroSeries()
        self.assertIs(None, distroseries.getInitializationJob())
        job_source = getUtility(IInitializeDistroSeriesJobSource)
        job = job_source.create(distroseries, [parent_distroseries.id])
        self.assertEqual(job, distroseries.getInitializationJob())

    def test_getDifferenceComments_gets_DistroSeriesDifferenceComments(self):
        distroseries = self.factory.makeDistroSeries()
        dsd = self.factory.makeDistroSeriesDifference(
            derived_series=distroseries
        )
        comment = self.factory.makeDistroSeriesDifferenceComment(dsd)
        self.assertContentEqual(
            [comment], distroseries.getDifferenceComments()
        )

    def test_valid_specifications_query_count(self):
        distroseries = self.factory.makeDistroSeries()
        distribution = distroseries.distribution
        spec1 = self.factory.makeSpecification(
            distribution=distribution, goal=distroseries
        )
        spec2 = self.factory.makeSpecification(
            distribution=distribution, goal=distroseries
        )
        for _ in range(5):
            self.factory.makeSpecificationWorkItem(specification=spec1)
            self.factory.makeSpecificationWorkItem(specification=spec2)
        IStore(spec1.__class__).flush()
        IStore(spec1.__class__).invalidate()
        with StormStatementRecorder() as recorder:
            for spec in distroseries.api_valid_specifications:
                spec.workitems_text
        self.assertThat(recorder, HasQueryCount(Equals(3)))

    def test_valid_specifications_preloading_excludes_deleted_workitems(self):
        distroseries = self.factory.makeDistroSeries()
        spec = self.factory.makeSpecification(
            distribution=distroseries.distribution, goal=distroseries
        )
        self.factory.makeSpecificationWorkItem(
            specification=spec, deleted=True
        )
        self.factory.makeSpecificationWorkItem(specification=spec)
        workitems = [
            s.workitems_text for s in distroseries.api_valid_specifications
        ]
        self.assertContentEqual([spec.workitems_text], workitems)

    def test_backports_not_automatic(self):
        distroseries = self.factory.makeDistroSeries()
        self.assertFalse(distroseries.backports_not_automatic)
        with admin_logged_in():
            distroseries.backports_not_automatic = True
        self.assertTrue(distroseries.backports_not_automatic)
        naked_distroseries = removeSecurityProxy(distroseries)
        self.assertTrue(
            naked_distroseries.publishing_options["backports_not_automatic"]
        )

    def test_proposed_not_automatic(self):
        distroseries = self.factory.makeDistroSeries()
        self.assertFalse(distroseries.proposed_not_automatic)
        with admin_logged_in():
            distroseries.proposed_not_automatic = True
        self.assertTrue(distroseries.proposed_not_automatic)
        naked_distroseries = removeSecurityProxy(distroseries)
        self.assertTrue(
            naked_distroseries.publishing_options["proposed_not_automatic"]
        )

    def test_include_long_descriptions(self):
        distroseries = self.factory.makeDistroSeries()
        self.assertTrue(distroseries.include_long_descriptions)
        with admin_logged_in():
            distroseries.include_long_descriptions = False
        self.assertFalse(distroseries.include_long_descriptions)
        naked_distroseries = removeSecurityProxy(distroseries)
        self.assertFalse(
            naked_distroseries.publishing_options["include_long_descriptions"]
        )

    def test_index_compressors(self):
        distroseries = self.factory.makeDistroSeries()
        self.assertEqual(
            [IndexCompressionType.GZIP, IndexCompressionType.BZIP2],
            distroseries.index_compressors,
        )
        with admin_logged_in():
            distroseries.index_compressors = [IndexCompressionType.XZ]
        self.assertEqual(
            [IndexCompressionType.XZ], distroseries.index_compressors
        )
        naked_distroseries = removeSecurityProxy(distroseries)
        self.assertEqual(
            ["xz"], naked_distroseries.publishing_options["index_compressors"]
        )

    def test_publish_by_hash(self):
        distroseries = self.factory.makeDistroSeries()
        self.assertFalse(distroseries.publish_by_hash)
        with admin_logged_in():
            distroseries.publish_by_hash = True
        self.assertTrue(distroseries.publish_by_hash)
        naked_distroseries = removeSecurityProxy(distroseries)
        self.assertTrue(
            naked_distroseries.publishing_options["publish_by_hash"]
        )

    def test_advertise_by_hash(self):
        distroseries = self.factory.makeDistroSeries()
        self.assertFalse(distroseries.advertise_by_hash)
        with admin_logged_in():
            distroseries.advertise_by_hash = True
        self.assertTrue(distroseries.advertise_by_hash)
        naked_distroseries = removeSecurityProxy(distroseries)
        self.assertTrue(
            naked_distroseries.publishing_options["advertise_by_hash"]
        )

    def test_strict_supported_component_dependencies(self):
        distroseries = self.factory.makeDistroSeries()
        self.assertTrue(distroseries.strict_supported_component_dependencies)
        with admin_logged_in():
            distroseries.strict_supported_component_dependencies = False
        self.assertFalse(distroseries.strict_supported_component_dependencies)
        naked_distroseries = removeSecurityProxy(distroseries)
        self.assertFalse(
            naked_distroseries.publishing_options[
                "strict_supported_component_dependencies"
            ]
        )

    def test_publish_i18n_index(self):
        distroseries = self.factory.makeDistroSeries()
        self.assertTrue(distroseries.publish_i18n_index)
        with admin_logged_in():
            distroseries.publish_i18n_index = False
        self.assertFalse(distroseries.publish_i18n_index)
        naked_distroseries = removeSecurityProxy(distroseries)
        self.assertFalse(
            naked_distroseries.publishing_options["publish_i18n_index"]
        )

    def test_valid_until_config(self):
        distroseries = self.factory.makeDistroSeries()
        self.assertFalse(distroseries.valid_until_config)
        with admin_logged_in():
            distroseries.valid_until_config = {
                PackagePublishingPocket.BACKPORTS: {
                    "refresh_threshold": 5,
                    "validity_period": 10,
                }
            }
        naked_distroseries = removeSecurityProxy(distroseries)
        self.assertEqual(
            {"BACKPORTS": {"refresh_threshold": 5, "validity_period": 10}},
            naked_distroseries.publishing_options["valid_until_config"],
        )

    def test_valid_until_config_empty(self):
        distroseries = self.factory.makeDistroSeries()
        self.assertFalse(distroseries.valid_until_config)
        with admin_logged_in():
            distroseries.valid_until_config = {}
        naked_distroseries = removeSecurityProxy(distroseries)
        self.assertEqual(
            {},
            naked_distroseries.publishing_options["valid_until_config"],
        )

    def test_valid_until_raises_assert(self):
        """
        Test that setting valid_until_config to non-dict values raises
        AssertionError
        """
        distroseries = self.factory.makeDistroSeries()
        values = [None, [], (), 12]
        for value in values:
            with admin_logged_in():
                self.assertRaises(
                    AssertionError,
                    setattr,
                    distroseries,
                    "valid_until_config",
                    value,
                )
        self.assertFalse(distroseries.valid_until_config)

    def test_anonymous_users_cant_modify_valid_until_config(self):
        """Test that anonymous users cannot edit valid_until_config."""
        distroseries = self.factory.makeDistroSeries()

        self.assertRaises(
            Unauthorized, setattr, distroseries, "valid_until_config", {}
        )

    def test_regular_users_cant_modify_valid_until_config(self):
        """
        Test that regular authenticated users cannot edit
        valid_until_config.
        """
        distroseries = self.factory.makeDistroSeries()
        regular_user = self.factory.makePerson()
        with person_logged_in(regular_user):
            self.assertRaises(
                Unauthorized, setattr, distroseries, "valid_until_config", {}
            )

    def test_distribution_owner_can_modify_valid_until_config(self):
        """Test that distribution owners can edit valid_until_config."""
        distroseries = self.factory.makeDistroSeries()
        distribution_owner = distroseries.distribution.owner

        # Distribution owner can modify valid_until_config
        with person_logged_in(distribution_owner):
            distroseries.valid_until_config = {
                PackagePublishingPocket.BACKPORTS: {
                    "refresh_threshold": 5,
                    "validity_period": 10,
                }
            }
        self.assertTrue(distroseries.valid_until_config)

    def test_getExternalPackageSeries(self):
        # Test that we get the ExternalPackageSeries that belongs to the
        # distribution with the proper attributes
        distroseries = self.factory.makeDistroSeries()
        sourcepackagename = self.factory.getOrMakeSourcePackageName(
            "my-package"
        )
        channel = ("22.04", "candidate", "staging")
        externalpackageseries = distroseries.getExternalPackageSeries(
            name=sourcepackagename,
            packagetype=ExternalPackageType.ROCK,
            channel=channel,
        )
        self.assertEqual(externalpackageseries.distroseries, distroseries)
        self.assertEqual(externalpackageseries.name, "my-package")
        self.assertEqual(
            externalpackageseries.packagetype, ExternalPackageType.ROCK
        )
        self.assertEqual(externalpackageseries.channel, channel)

        # We can have external package series without channel
        externalpackageseries = distroseries.getExternalPackageSeries(
            name=sourcepackagename,
            packagetype=ExternalPackageType.SNAP,
            channel=None,
        )
        self.assertEqual(externalpackageseries.distroseries, distroseries)
        self.assertEqual(externalpackageseries.name, "my-package")
        self.assertEqual(
            externalpackageseries.packagetype, ExternalPackageType.SNAP
        )
        self.assertEqual(externalpackageseries.channel, None)

    def test_bug_target_parent(self):
        distroseries = self.factory.makeDistroSeries()
        parent = distroseries.bug_target_parent

        # The parent should not be None.
        self.assertIsNotNone(parent)

        # The parent should provide the IDistribution interface.
        self.assertTrue(IDistribution.providedBy(parent))

        # The parent should provide the IBugTargetParent interface.
        self.assertTrue(IBugTargetParent.providedBy(parent))

        # The parent should be the distribution of the series.
        self.assertEqual(distroseries.distribution, parent)

    def test_getArchiveSourcePackageSeries(self):
        # Test that we get the ArchiveSourcePackageSeries that belongs to the
        # distroseries with the proper attributes
        distroseries = self.factory.makeDistroSeries()
        archive = self.factory.makeArchive(
            distribution=distroseries.distribution
        )
        sourcepackagename = self.factory.getOrMakeSourcePackageName(
            "my-package"
        )
        archivesourcepackageseries = (
            distroseries.getArchiveSourcePackageSeries(
                archive=archive, name=sourcepackagename
            )
        )
        self.assertEqual(
            removeSecurityProxy(archivesourcepackageseries).distroseries,
            distroseries,
        )
        self.assertEqual(archivesourcepackageseries.name, "my-package")
        self.assertEqual(
            removeSecurityProxy(archivesourcepackageseries).archive, archive
        )

        # We can pass a string name as well
        archivesourcepackageseries = (
            distroseries.getArchiveSourcePackageSeries(
                archive=archive, name="my-package"
            )
        )
        self.assertEqual(
            removeSecurityProxy(archivesourcepackageseries).distroseries,
            distroseries,
        )
        self.assertEqual(archivesourcepackageseries.name, "my-package")
        self.assertEqual(
            removeSecurityProxy(archivesourcepackageseries).archive, archive
        )

        # Returns None if archive is from a different distribution
        other_archive = self.factory.makeArchive()
        result = distroseries.getArchiveSourcePackageSeries(
            archive=other_archive, name="my-package"
        )
        self.assertIsNone(result)

        # Returns None if source package name doesn't exist
        result = distroseries.getArchiveSourcePackageSeries(
            archive=archive, name="nonexistent-package"
        )
        self.assertIsNone(result)

    def test_getBugSummaryStatusImportanceCounts_returns_grouped_counts(self):
        distroseries = self.factory.makeDistroSeries()
        owner = self.factory.makePerson()

        # Unresolved tasks should be counted.
        self._makeBugTask(
            distroseries,
            owner=owner,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.CRITICAL,
        )
        self._makeBugTask(
            distroseries,
            owner=owner,
            status=BugTaskStatus.INPROGRESS,
            importance=BugTaskImportance.HIGH,
        )
        # Resolved tasks should be excluded by countBugs.
        self._makeBugTask(
            distroseries,
            owner=owner,
            status=BugTaskStatus.FIXRELEASED,
            importance=BugTaskImportance.CRITICAL,
        )

        counts = distroseries.getBugSummaryStatusImportanceCounts(owner)

        self.assertIsInstance(counts, dict)
        self.assertEqual(
            counts[(BugTaskStatus.NEW, BugTaskImportance.CRITICAL)], 1
        )
        self.assertEqual(
            counts[(BugTaskStatus.INPROGRESS, BugTaskImportance.HIGH)], 1
        )
        self.assertNotIn(
            (BugTaskStatus.FIXRELEASED, BugTaskImportance.CRITICAL), counts
        )

    def test_getBugSummaryStatusImportanceCounts_filters_by_milestone(self):
        distroseries = self.factory.makeDistroSeries()
        owner = self.factory.makePerson()
        milestone = self.factory.makeMilestone(distroseries=distroseries)

        # Included: task on the selected milestone.
        self._makeBugTask(
            distroseries,
            owner=owner,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.HIGH,
            milestone=milestone,
        )
        # Excluded: task not on that milestone.
        self._makeBugTask(
            distroseries,
            owner=owner,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.CRITICAL,
        )

        counts = distroseries.getBugSummaryStatusImportanceCounts(
            owner, milestone=milestone
        )

        self.assertEqual(
            counts[(BugTaskStatus.NEW, BugTaskImportance.HIGH)], 1
        )
        self.assertNotIn(
            (BugTaskStatus.NEW, BugTaskImportance.CRITICAL), counts
        )

    def test_get_milestone_returns_matching_milestone(self):
        distroseries = self.factory.makeDistroSeries()
        milestone = self.factory.makeMilestone(distroseries=distroseries)
        IStore(distroseries).flush()
        self.assertEqual(milestone, distroseries.get_milestone(milestone.id))

    def test_get_milestone_unknown_id_returns_None(self):
        distroseries = self.factory.makeDistroSeries()
        self.assertIsNone(distroseries.get_milestone(-1))

    def test_get_milestone_other_series_returns_None(self):
        distroseries = self.factory.makeDistroSeries()
        other_series = self.factory.makeDistroSeries()
        other_milestone = self.factory.makeMilestone(distroseries=other_series)
        IStore(distroseries).flush()
        self.assertIsNone(distroseries.get_milestone(other_milestone.id))

    def test_get_milestone_product_milestone_returns_None(self):
        distroseries = self.factory.makeDistroSeries()
        product_milestone = self.factory.makeMilestone(
            product=self.factory.makeProduct()
        )
        IStore(distroseries).flush()
        self.assertIsNone(distroseries.get_milestone(product_milestone.id))

    def test_get_milestone_returns_inactive_milestone(self):
        distroseries = self.factory.makeDistroSeries()
        milestone = self.factory.makeMilestone(
            distroseries=distroseries, active=False
        )
        IStore(distroseries).flush()
        self.assertEqual(milestone, distroseries.get_milestone(milestone.id))

    def test_get_upcoming_milestone_no_milestones(self):
        distroseries = self.factory.makeDistroSeries()
        self.assertIsNone(distroseries.get_upcoming_milestone())

    def test_get_upcoming_milestone_no_dateexpected(self):
        distroseries = self.factory.makeDistroSeries()
        self.factory.makeMilestone(distroseries=distroseries)
        self.assertIsNone(distroseries.get_upcoming_milestone())

    def test_get_upcoming_milestone_past_excluded(self):
        distroseries = self.factory.makeDistroSeries()
        self.factory.makeMilestone(
            distroseries=distroseries,
            dateexpected=date.today() - timedelta(days=1),
        )
        self.assertIsNone(distroseries.get_upcoming_milestone())

    def test_get_upcoming_milestone_inactive_excluded(self):
        distroseries = self.factory.makeDistroSeries()
        self.factory.makeMilestone(
            distroseries=distroseries,
            dateexpected=date.today() + timedelta(days=1),
            active=False,
        )
        self.assertIsNone(distroseries.get_upcoming_milestone())

    def test_get_upcoming_milestone_other_series_excluded(self):
        distroseries = self.factory.makeDistroSeries()
        other_series = self.factory.makeDistroSeries()
        self.factory.makeMilestone(
            distroseries=other_series,
            dateexpected=date.today() + timedelta(days=1),
        )
        self.assertIsNone(distroseries.get_upcoming_milestone())

    def test_get_upcoming_milestone_returns_earliest(self):
        distroseries = self.factory.makeDistroSeries()
        later = self.factory.makeMilestone(
            distroseries=distroseries,
            dateexpected=date.today() + timedelta(days=10),
        )
        earlier = self.factory.makeMilestone(
            distroseries=distroseries,
            dateexpected=date.today() + timedelta(days=2),
        )
        self.assertEqual(earlier, distroseries.get_upcoming_milestone())
        self.assertNotEqual(later, distroseries.get_upcoming_milestone())

    def test_get_upcoming_milestone_ties_broken_by_name(self):
        distroseries = self.factory.makeDistroSeries()
        same_date = date.today() + timedelta(days=5)
        milestone_b = self.factory.makeMilestone(
            distroseries=distroseries, name="b", dateexpected=same_date
        )
        milestone_a = self.factory.makeMilestone(
            distroseries=distroseries, name="a", dateexpected=same_date
        )
        self.assertEqual(milestone_a, distroseries.get_upcoming_milestone())
        self.assertNotEqual(milestone_b, distroseries.get_upcoming_milestone())

    def test_get_upcoming_milestone_includes_today(self):
        distroseries = self.factory.makeDistroSeries()
        milestone = self.factory.makeMilestone(
            distroseries=distroseries, dateexpected=date.today()
        )
        self.assertEqual(milestone, distroseries.get_upcoming_milestone())

    def test_getArchiveSeries(self):
        # Test that we get the ArchiveSeries that belongs to the
        # distroseries with the proper attributes
        distroseries = self.factory.makeDistroSeries()
        archive = self.factory.makeArchive(
            distribution=distroseries.distribution
        )
        archiveseries = distroseries.getArchiveSeries(archive=archive)
        self.assertEqual(
            removeSecurityProxy(archiveseries).distroseries, distroseries
        )
        self.assertEqual(removeSecurityProxy(archiveseries).archive, archive)

        # Returns None if archive is from a different distribution
        other_archive = self.factory.makeArchive()
        result = distroseries.getArchiveSeries(archive=other_archive)
        self.assertIsNone(result)


class TestDistroSeriesPackaging(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.series = self.factory.makeDistroSeries()
        self.user = self.series.distribution.owner
        login("admin@canonical.com")
        component_set = getUtility(IComponentSet)
        self.packages = {}
        self.main_component = component_set["main"]
        self.universe_component = component_set["universe"]
        self.makeSeriesPackage("normal")
        self.makeSeriesPackage("translatable", messages=800)
        hot_package = self.makeSeriesPackage("hot", bugs=50)
        # Create a second SPPH for 'hot', to verify that duplicates are
        # eliminated in the queries.
        self.factory.makeSourcePackagePublishingHistory(
            sourcepackagename=hot_package.sourcepackagename,
            distroseries=self.series,
            component=self.universe_component,
            section_name="web",
        )
        self.makeSeriesPackage("hot-translatable", bugs=25, messages=1000)
        self.makeSeriesPackage("main", is_main=True)
        self.makeSeriesPackage("linked")
        self.linkPackage("linked")
        transaction.commit()
        login(ANONYMOUS)

    def makeSeriesPackage(
        self,
        name=None,
        is_main=False,
        bugs=None,
        messages=None,
        is_translations=False,
        pocket=None,
        status=None,
    ):
        # Make a published source package.
        if name is None:
            name = self.factory.getUniqueString()
        if is_main:
            component = self.main_component
        else:
            component = self.universe_component
        if is_translations:
            section = "translations"
        else:
            section = "web"
        sourcepackagename = self.factory.makeSourcePackageName(name)
        spph = self.factory.makeSourcePackagePublishingHistory(
            sourcepackagename=sourcepackagename,
            distroseries=self.series,
            component=component,
            section_name=section,
            pocket=pocket,
            status=status,
        )
        source_package = self.factory.makeSourcePackage(
            sourcepackagename=sourcepackagename, distroseries=self.series
        )
        if bugs is not None:
            dsp = removeSecurityProxy(
                source_package.distribution_sourcepackage
            )
            dsp.bug_count = bugs
        if messages is not None:
            template = self.factory.makePOTemplate(
                distroseries=self.series,
                sourcepackagename=sourcepackagename,
                owner=self.user,
            )
            removeSecurityProxy(template).messagecount = messages
        spr = spph.sourcepackagerelease
        for extension in ("dsc", "tar.gz"):
            filename = "%s_%s.%s" % (spr.name, spr.version, extension)
            spr.addFile(
                self.factory.makeLibraryFileAlias(
                    filename=filename, db_only=True
                )
            )
        self.packages[name] = source_package
        return source_package

    def makeSeriesBinaryPackage(
        self,
        name=None,
        is_main=False,
        is_translations=False,
        das=None,
        pocket=None,
        status=None,
    ):
        # Make a published binary package.
        if name is None:
            name = self.factory.getUniqueString()
        source = self.makeSeriesPackage(
            name=name,
            is_main=is_main,
            is_translations=is_translations,
            pocket=pocket,
            status=status,
        )
        spr = source.distinctreleases[0]
        binarypackagename = self.factory.makeBinaryPackageName(name)
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

    def linkPackage(self, name):
        product_series = self.factory.makeProductSeries()
        removeSecurityProxy(product_series).setPackaging(
            self.series, self.packages[name].sourcepackagename, self.user
        )
        return product_series

    def test_getPrioritizedUnlinkedSourcePackages(self):
        # Verify the ordering of source packages that need linking.
        package_summaries = self.series.getPrioritizedUnlinkedSourcePackages()
        names = [summary["package"].name for summary in package_summaries]
        expected = [
            "main",
            "hot-translatable",
            "hot",
            "translatable",
            "normal",
        ]
        self.assertEqual(expected, names)

    def test_getPrioritizedUnlinkedSourcePackages_no_language_packs(self):
        # Verify that translations packages are not listed.
        self.makeSeriesPackage("language-pack-vi", is_translations=True)
        package_summaries = self.series.getPrioritizedUnlinkedSourcePackages()
        names = [summary["package"].name for summary in package_summaries]
        expected = [
            "main",
            "hot-translatable",
            "hot",
            "translatable",
            "normal",
        ]
        self.assertEqual(expected, names)

    def test_getPrioritizedPackagings(self):
        # Verify the ordering of packagings that need more upstream info.
        for name in ["main", "hot-translatable", "hot", "translatable"]:
            self.linkPackage(name)
        packagings = self.series.getPrioritizedPackagings()
        names = [packaging.sourcepackagename.name for packaging in packagings]
        expected = [
            "main",
            "hot-translatable",
            "hot",
            "translatable",
            "linked",
        ]
        self.assertEqual(expected, names)

    def test_getPrioritizedPackagings_bug_tracker(self):
        # Verify the ordering of packagings with and without a bug tracker.
        self.linkPackage("hot")
        self.makeSeriesPackage("cold")
        product_series = self.linkPackage("cold")
        with person_logged_in(product_series.product.owner):
            product_series.product.bugtracker = self.factory.makeBugTracker()
        packagings = self.series.getPrioritizedPackagings()
        names = [packaging.sourcepackagename.name for packaging in packagings]
        expected = ["hot", "linked", "cold"]
        self.assertEqual(expected, names)

    def test_getPrioritizedPackagings_branch(self):
        # Verify the ordering of packagings with and without a branch.
        self.linkPackage("translatable")
        self.makeSeriesPackage("withbranch")
        product_series = self.linkPackage("withbranch")
        with person_logged_in(product_series.product.owner):
            product_series.branch = self.factory.makeBranch()
        packagings = self.series.getPrioritizedPackagings()
        names = [packaging.sourcepackagename.name for packaging in packagings]
        expected = ["translatable", "linked", "withbranch"]
        self.assertEqual(expected, names)

    def test_getPrioritizedPackagings_translation(self):
        # Verify the ordering of translatable packagings that are and are not
        # configured to import.
        self.linkPackage("translatable")
        self.makeSeriesPackage("importabletranslatable")
        product_series = self.linkPackage("importabletranslatable")
        with person_logged_in(product_series.product.owner):
            product_series.branch = self.factory.makeBranch()
            product_series.translations_autoimport_mode = (
                TranslationsBranchImportMode.IMPORT_TEMPLATES
            )
        packagings = self.series.getPrioritizedPackagings()
        names = [packaging.sourcepackagename.name for packaging in packagings]
        expected = ["translatable", "linked", "importabletranslatable"]
        self.assertEqual(expected, names)


class TestDistroSeriesBuildRecordsCountsWebservice(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.distroseries = self.factory.makeDistroSeries()
        self.distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries
        )
        self.distroseries_url = api_url(self.distroseries)
        self.webservice = webservice_for_person(
            None, default_api_version="devel"
        )
        self.cutoff = datetime(2026, 9, 2, 14, 30, tzinfo=timezone.utc)

    def makeBuild(
        self,
        status,
        pocket=PackagePublishingPocket.RELEASE,
        name=None,
        date_finished=None,
        archive=None,
    ):
        with admin_logged_in():
            sourcepackagename = (
                self.factory.makeSourcePackageName(name=name) if name else None
            )
            build = self.factory.makeBinaryPackageBuild(
                distroarchseries=self.distroarchseries,
                archive=archive,
                status=status,
                pocket=pocket,
                sourcepackagename=sourcepackagename,
            )
        if date_finished is not None:
            removeSecurityProxy(build).date_finished = date_finished
        return build

    def getCounts(self, **parameters):
        response = self.webservice.named_get(
            self.distroseries_url,
            "getBuildRecordsCounts",
            **parameters,
        )
        self.assertEqual(200, response.status, response.body)
        return response.jsonBody()

    def test_empty_response_contract_and_anonymous_access(self):
        response = self.getCounts()

        self.assertEqual(
            {"total", "by_build_state", "by_pocket"}, set(response)
        )
        self.assertEqual(0, response["total"])
        self.assertEqual(
            {status.name for status in BuildStatus.items},
            set(response["by_build_state"]),
        )
        self.assertEqual(
            {pocket.name for pocket in PackagePublishingPocket.items},
            set(response["by_pocket"]),
        )
        self.assertTrue(
            all(count == 0 for count in response["by_build_state"].values())
        )
        self.assertTrue(
            all(count == 0 for count in response["by_pocket"].values())
        )

    def test_counts_complete_state_and_pocket_marginals(self):
        builds = [
            (
                BuildStatus.FULLYBUILT,
                PackagePublishingPocket.RELEASE,
            ),
            (
                BuildStatus.FAILEDTOBUILD,
                PackagePublishingPocket.UPDATES,
            ),
            (
                BuildStatus.FAILEDTOUPLOAD,
                PackagePublishingPocket.PROPOSED,
            ),
        ]
        for status, pocket in builds:
            self.makeBuild(
                status,
                pocket=pocket,
                date_finished=self.cutoff,
            )

        response = self.getCounts()

        self.assertEqual(3, response["total"])
        for status, pocket in builds:
            self.assertEqual(1, response["by_build_state"][status.name])
            self.assertEqual(1, response["by_pocket"][pocket.name])
        self.assertEqual(0, response["by_build_state"]["NEEDSBUILD"])
        self.assertEqual(0, response["by_pocket"]["BACKPORTS"])
        self.assertEqual(
            response["total"], sum(response["by_build_state"].values())
        )
        self.assertEqual(
            response["total"], sum(response["by_pocket"].values())
        )

        for status, pocket in builds:
            filtered = self.getCounts(build_status=status.title)
            self.assertEqual(1, filtered["total"])
            self.assertEqual(1, filtered["by_build_state"][status.name])
            self.assertEqual(1, filtered["by_pocket"][pocket.name])

    def test_finish_date_bounds_and_imported_builds(self):
        self.makeBuild(
            BuildStatus.FULLYBUILT,
            name="at-boundary",
            date_finished=self.cutoff,
        )
        self.makeBuild(
            BuildStatus.FAILEDTOBUILD,
            name="older",
            date_finished=self.cutoff - timedelta(seconds=1),
        )
        self.makeBuild(
            BuildStatus.FAILEDTOUPLOAD,
            name="newer",
            date_finished=self.cutoff + timedelta(seconds=1),
        )
        self.makeBuild(
            BuildStatus.NEEDSBUILD,
            name="unfinished",
        )
        self.makeBuild(
            BuildStatus.FULLYBUILT,
            name="imported-success",
        )

        filtered = self.getCounts(finished_since=self.cutoff.isoformat())
        self.assertEqual(2, filtered["total"])
        self.assertEqual(1, filtered["by_build_state"]["FULLYBUILT"])
        self.assertEqual(1, filtered["by_build_state"]["FAILEDTOUPLOAD"])

        filtered = self.getCounts(finished_before=self.cutoff.isoformat())
        self.assertEqual(1, filtered["total"])
        self.assertEqual(0, filtered["by_build_state"]["FULLYBUILT"])
        self.assertEqual(1, filtered["by_build_state"]["FAILEDTOBUILD"])

        filtered = self.getCounts(
            finished_since=self.cutoff.isoformat(),
            finished_before=self.cutoff.isoformat(),
        )
        self.assertEqual(0, filtered["total"])
        self.assertEqual(0, filtered["by_build_state"]["FULLYBUILT"])

        unfiltered = self.getCounts()
        self.assertEqual(4, unfiltered["total"])
        self.assertEqual(1, unfiltered["by_build_state"]["FULLYBUILT"])
        self.assertEqual(1, unfiltered["by_build_state"]["FAILEDTOBUILD"])
        self.assertEqual(1, unfiltered["by_build_state"]["FAILEDTOUPLOAD"])
        self.assertEqual(1, unfiltered["by_build_state"]["NEEDSBUILD"])

    def test_rejects_reversed_finish_date_bounds(self):
        response = self.webservice.named_get(
            self.distroseries_url,
            "getBuildRecordsCounts",
            finished_since=(self.cutoff + timedelta(seconds=1)).isoformat(),
            finished_before=self.cutoff.isoformat(),
        )

        self.assertEqual(400, response.status)
        self.assertIn(
            b"finished_since must not be later than finished_before.",
            response.body,
        )

    def test_source_state_and_pocket_filters(self):
        self.makeBuild(
            BuildStatus.FULLYBUILT,
            name="matching-built",
            date_finished=self.cutoff,
        )
        self.makeBuild(
            BuildStatus.FAILEDTOBUILD,
            pocket=PackagePublishingPocket.UPDATES,
            name="matching-failed",
            date_finished=self.cutoff,
        )
        self.makeBuild(
            BuildStatus.FAILEDTOUPLOAD,
            name="unrelated",
            date_finished=self.cutoff,
        )

        by_source = self.getCounts(source_name="matching")
        self.assertEqual(2, by_source["total"])
        self.assertEqual(0, by_source["by_build_state"]["FAILEDTOUPLOAD"])
        self.assertEqual(1, by_source["by_pocket"]["RELEASE"])
        self.assertEqual(1, by_source["by_pocket"]["UPDATES"])

        by_state = self.getCounts(build_status=BuildStatus.FULLYBUILT.title)
        self.assertEqual(1, by_state["total"])
        self.assertEqual(1, by_state["by_pocket"]["RELEASE"])

        by_pocket = self.getCounts(
            pocket=PackagePublishingPocket.RELEASE.title
        )
        self.assertEqual(2, by_pocket["total"])
        self.assertEqual(1, by_pocket["by_build_state"]["FULLYBUILT"])
        self.assertEqual(1, by_pocket["by_build_state"]["FAILEDTOUPLOAD"])

        combined = self.getCounts(
            source_name="matching",
            build_status=BuildStatus.FAILEDTOBUILD.title,
            pocket=PackagePublishingPocket.UPDATES.title,
        )
        self.assertEqual(1, combined["total"])
        self.assertEqual(1, combined["by_build_state"]["FAILEDTOBUILD"])
        self.assertEqual(0, combined["by_build_state"]["FULLYBUILT"])
        self.assertEqual(1, combined["by_pocket"]["UPDATES"])
        self.assertEqual(0, combined["by_pocket"]["RELEASE"])

    def test_excludes_ppa_and_other_series_builds(self):
        self.makeBuild(
            BuildStatus.FULLYBUILT,
            date_finished=self.cutoff,
        )
        with admin_logged_in():
            ppa = self.factory.makeArchive(
                distribution=self.distroseries.distribution,
                purpose=ArchivePurpose.PPA,
                private=True,
            )
        self.makeBuild(
            BuildStatus.FAILEDTOBUILD,
            date_finished=self.cutoff,
            archive=ppa,
        )
        with admin_logged_in():
            self.factory.makeBinaryPackageBuild(
                status=BuildStatus.FAILEDTOUPLOAD
            )
        response = self.getCounts()

        self.assertEqual(1, response["total"])
        self.assertEqual(1, response["by_build_state"]["FULLYBUILT"])

    def test_operation_is_devel_only(self):
        for version in ("beta", "1.0"):
            webservice = webservice_for_person(
                None, default_api_version=version
            )
            response = webservice.named_get(
                self.distroseries_url, "getBuildRecordsCounts"
            )
            self.assertEqual(400, response.status, version)

    def test_getBuildRecords_schema_is_unchanged(self):
        exported = IHasBuildRecords["getBuildRecords"].queryTaggedValue(
            "lazr.restful.exported"
        )

        self.assertEqual(
            {"name", "build_state", "pocket"},
            set(exported["params"]),
        )


class TestDistroSeriesPackageCounts(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.distroseries = self.factory.makeDistroSeries()
        self.distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries
        )
        self.partner_archive = self.factory.makeArchive(
            distribution=self.distroseries.distribution,
            purpose=ArchivePurpose.PARTNER,
        )
        self.ppa = self.factory.makeArchive(
            distribution=self.distroseries.distribution,
            purpose=ArchivePurpose.PPA,
        )
        self.copy_archive = self.factory.makeArchive(
            distribution=self.distroseries.distribution,
            purpose=ArchivePurpose.COPY,
        )
        other_distribution = self.factory.makeDistribution()
        self.foreign_archive = other_distribution.main_archive
        self.other_distroseries = self.factory.makeDistroSeries(
            distribution=self.distroseries.distribution
        )
        self.other_distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=self.other_distroseries
        )

    def updateAndAssertSourceCount(self, expected):
        self.distroseries.updatePackageCount()
        self.assertEqual(expected, self.distroseries.sourcecount)

    def updateAndAssertBinaryCount(self, expected):
        self.distroseries.updatePackageCount()
        self.assertEqual(expected, self.distroseries.binarycount)

    def makeSourcePublication(
        self,
        sourcepackagename=None,
        status=PackagePublishingStatus.PENDING,
        pocket=PackagePublishingPocket.RELEASE,
        archive=None,
        distroseries=None,
    ):
        if sourcepackagename is None:
            sourcepackagename = self.factory.makeSourcePackageName()
        if archive is None:
            archive = self.distroseries.main_archive
        if distroseries is None:
            distroseries = self.distroseries
        return self.factory.makeSourcePackagePublishingHistory(
            sourcepackagename=sourcepackagename,
            distroseries=distroseries,
            archive=archive,
            status=status,
            pocket=pocket,
        )

    def makeBinaryPublication(
        self,
        binarypackagename=None,
        status=PackagePublishingStatus.PENDING,
        pocket=PackagePublishingPocket.RELEASE,
        archive=None,
        distroarchseries=None,
    ):
        if binarypackagename is None:
            binarypackagename = self.factory.makeBinaryPackageName()
        if archive is None:
            archive = self.distroseries.main_archive
        if distroarchseries is None:
            distroarchseries = self.distroarchseries
        return self.factory.makeBinaryPackagePublishingHistory(
            binarypackagename=binarypackagename,
            distroarchseries=distroarchseries,
            archive=archive,
            status=status,
            pocket=pocket,
        )

    def test_source_package_count_membership(self):
        self.updateAndAssertSourceCount(0)

        self.makeSourcePublication(status=PackagePublishingStatus.PENDING)
        self.updateAndAssertSourceCount(1)
        self.makeSourcePublication(status=PackagePublishingStatus.PUBLISHED)
        self.updateAndAssertSourceCount(2)

        duplicate_name = self.factory.makeSourcePackageName()
        self.makeSourcePublication(sourcepackagename=duplicate_name)
        self.updateAndAssertSourceCount(3)
        self.makeSourcePublication(
            sourcepackagename=duplicate_name,
            status=PackagePublishingStatus.PUBLISHED,
        )
        self.updateAndAssertSourceCount(3)

        for status in (
            PackagePublishingStatus.SUPERSEDED,
            PackagePublishingStatus.DELETED,
            PackagePublishingStatus.OBSOLETE,
        ):
            self.makeSourcePublication(status=status)
        self.updateAndAssertSourceCount(3)

        self.makeSourcePublication(pocket=PackagePublishingPocket.UPDATES)
        self.makeSourcePublication(archive=self.ppa)
        self.makeSourcePublication(archive=self.copy_archive)
        # Publication creation rejects cross-distribution archive pairs, so
        # set the archive ID directly to exercise this defensive filter.
        foreign_publication = self.makeSourcePublication()
        removeSecurityProxy(foreign_publication).archive_id = (
            self.foreign_archive.id
        )
        self.makeSourcePublication(distroseries=self.other_distroseries)
        self.updateAndAssertSourceCount(3)

        self.makeSourcePublication(archive=self.partner_archive)
        self.updateAndAssertSourceCount(4)

    def test_binary_package_count_membership(self):
        self.updateAndAssertBinaryCount(0)

        self.makeBinaryPublication(status=PackagePublishingStatus.PENDING)
        self.updateAndAssertBinaryCount(1)
        self.makeBinaryPublication(status=PackagePublishingStatus.PUBLISHED)
        self.updateAndAssertBinaryCount(2)

        duplicate_name = self.factory.makeBinaryPackageName()
        self.makeBinaryPublication(binarypackagename=duplicate_name)
        self.updateAndAssertBinaryCount(3)
        self.makeBinaryPublication(
            binarypackagename=duplicate_name,
            status=PackagePublishingStatus.PUBLISHED,
        )
        self.updateAndAssertBinaryCount(3)

        for status in (
            PackagePublishingStatus.SUPERSEDED,
            PackagePublishingStatus.DELETED,
            PackagePublishingStatus.OBSOLETE,
        ):
            self.makeBinaryPublication(status=status)
        self.updateAndAssertBinaryCount(3)

        self.makeBinaryPublication(pocket=PackagePublishingPocket.UPDATES)
        self.makeBinaryPublication(archive=self.ppa)
        self.makeBinaryPublication(archive=self.copy_archive)
        # Publication creation rejects cross-distribution archive pairs, so
        # set the archive ID directly to exercise this defensive filter.
        foreign_publication = self.makeBinaryPublication()
        removeSecurityProxy(foreign_publication).archive_id = (
            self.foreign_archive.id
        )
        self.makeBinaryPublication(
            distroarchseries=self.other_distroarchseries
        )
        self.updateAndAssertBinaryCount(3)

        self.makeBinaryPublication(archive=self.partner_archive)
        self.updateAndAssertBinaryCount(4)


class TestDistroSeriesPackageCountSchema(TestCase):
    layer = BaseLayer

    def test_fields_require_non_negative_integers(self):
        for name in ("sourcecount", "binarycount"):
            field = IDistroSeriesPublic[name]
            self.assertTrue(field.required)
            self.assertTrue(IInt.providedBy(field))
            self.assertTrue(field.readonly)
            self.assertEqual(0, field.min)
            self.assertRaises(TooSmall, field.validate, -1)
            field.validate(0)


class TestDistroSeriesPackageCountsWebservice(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def getEntry(self, distroseries, version="devel"):
        with person_logged_in(None):
            distroseries_url = api_url(distroseries)
        webservice = webservice_for_person(None, default_api_version=version)
        response = webservice.get(distroseries_url)
        self.assertEqual(200, response.status, response.body)
        return response.jsonBody()

    def test_empty_devel_representation_is_anonymously_accessible(self):
        entry = self.getEntry(self.factory.makeDistroSeries())

        self.assertEqual(
            {
                "source_package_count": 0,
                "binary_package_count": 0,
            },
            {
                name: entry[name]
                for name in (
                    "source_package_count",
                    "binary_package_count",
                )
            },
        )
        self.assertIs(int, type(entry["source_package_count"]))
        self.assertIs(int, type(entry["binary_package_count"]))

    def test_private_distribution_visibility(self):
        owner = self.factory.makePerson()
        unrelated_person = self.factory.makePerson()
        with person_logged_in(owner):
            distribution = self.factory.makeDistribution(
                owner=owner,
                information_type=InformationType.PROPRIETARY,
            )
            distroseries = self.factory.makeDistroSeries(
                distribution=distribution
            )
            distroarchseries = self.factory.makeDistroArchSeries(
                distroseries=distroseries
            )
            self.factory.makeSourcePackagePublishingHistory(
                distroseries=distroseries,
                archive=distroseries.main_archive,
                pocket=PackagePublishingPocket.RELEASE,
                status=PackagePublishingStatus.PUBLISHED,
            )
            self.factory.makeBinaryPackagePublishingHistory(
                distroarchseries=distroarchseries,
                archive=distroseries.main_archive,
                pocket=PackagePublishingPocket.RELEASE,
                status=PackagePublishingStatus.PENDING,
            )
            distroseries.updatePackageCount()
            distroseries_url = api_url(distroseries)

        owner_webservice = webservice_for_person(
            owner,
            default_api_version="devel",
            permission=OAuthPermission.READ_PRIVATE,
        )
        response = owner_webservice.get(distroseries_url)
        self.assertEqual(200, response.status, response.body)
        owner_entry = response.jsonBody()
        expected_counts = {
            "source_package_count": 2,
            "binary_package_count": 1,
        }
        self.assertEqual(
            expected_counts,
            {name: owner_entry[name] for name in expected_counts},
        )

        unrelated_webservice = webservice_for_person(
            unrelated_person,
            default_api_version="devel",
            permission=OAuthPermission.READ_PRIVATE,
        )
        response = unrelated_webservice.get(distroseries_url)
        self.assertEqual(404, response.status)

        anonymous_webservice = webservice_for_person(
            None, default_api_version="devel"
        )
        response = anonymous_webservice.get(distroseries_url)
        self.assertEqual(404, response.status)

    def test_fields_are_absent_from_older_versions(self):
        distroseries = self.factory.makeDistroSeries()
        for version in ("beta", "1.0"):
            entry = self.getEntry(distroseries, version=version)
            self.assertNotIn("source_package_count", entry, version)
            self.assertNotIn("binary_package_count", entry, version)

    def test_api_values_match_updated_model_counts(self):
        distroseries = self.factory.makeDistroSeries()
        distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=distroseries
        )
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PUBLISHED,
        )
        self.factory.makeBinaryPackagePublishingHistory(
            distroarchseries=distroarchseries,
            archive=distroseries.main_archive,
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PENDING,
        )
        distroseries.updatePackageCount()

        entry = self.getEntry(distroseries)

        self.assertEqual(2, distroseries.sourcecount)
        self.assertEqual(1, distroseries.binarycount)
        self.assertEqual(2, entry["source_package_count"])
        self.assertEqual(1, entry["binary_package_count"])


class TestDistroSeriesWebservice(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def getTranslationCoverage(self, distroseries):
        distroseries_url = api_url(distroseries)
        webservice = webservice_for_person(None, default_api_version="devel")
        distroseries_entry = webservice.get(distroseries_url).jsonBody()
        collection_link = distroseries_entry[
            "translation_coverage_collection_link"
        ]
        return webservice, collection_link

    def test_language_pack_full_export_requested_not_translations_admin(self):
        # Somebody with only launchpad.TranslationsAdmin cannot request full
        # language pack exports.
        distroseries = self.factory.makeDistroSeries()
        distroseries_url = api_url(distroseries)
        self.assertFalse(distroseries.language_pack_full_export_requested)
        group = self.factory.makeTranslationGroup()
        with admin_logged_in():
            distroseries.distribution.translationgroup = group
        webservice = webservice_for_person(
            group.owner, permission=OAuthPermission.WRITE_PRIVATE
        )
        response = webservice.patch(
            distroseries_url,
            "application/json",
            json.dumps({"language_pack_full_export_requested": True}),
        )
        self.assertEqual(401, response.status)
        self.assertFalse(distroseries.language_pack_full_export_requested)

    def test_language_pack_full_export_requested_langpacks_admin(self):
        # Somebody with launchpad.LanguagePacksAdmin can request full
        # language pack exports.
        distroseries = self.factory.makeDistroSeries()
        distroseries_url = api_url(distroseries)
        self.assertFalse(distroseries.language_pack_full_export_requested)
        person = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).rosetta_experts]
        )
        webservice = webservice_for_person(
            person, permission=OAuthPermission.WRITE_PRIVATE
        )
        response = webservice.patch(
            distroseries_url,
            "application/json",
            json.dumps({"language_pack_full_export_requested": True}),
        )
        self.assertEqual(209, response.status)
        self.assertTrue(distroseries.language_pack_full_export_requested)

    def test_translation_template_statistics(self):
        distroseries = self.factory.makeDistroSeries()
        templates = [
            self.factory.makePOTemplate(distroseries=distroseries)
            for _ in range(3)
        ]
        removeSecurityProxy(templates[0]).messagecount = 100
        removeSecurityProxy(templates[0]).priority = 10
        removeSecurityProxy(templates[1]).iscurrent = False
        removeSecurityProxy(templates[2]).languagepack = True
        self.factory.makePOTemplate()
        distroseries_url = api_url(distroseries)
        webservice = webservice_for_person(None, default_api_version="devel")
        response = webservice.named_get(
            distroseries_url, "getTranslationTemplateStatistics"
        )
        self.assertEqual(200, response.status)
        self.assertEqual(
            [
                {
                    "sourcepackage": template.sourcepackage.name,
                    "translation_domain": template.translation_domain,
                    "template_name": template.name,
                    "total": template.messagecount,
                    "enabled": template.iscurrent,
                    "languagepack": template.languagepack,
                    "priority": template.priority,
                    "date_last_updated": (
                        template.date_last_updated.isoformat()
                    ),
                }
                for template in templates
            ],
            response.jsonBody(),
        )

    def test_translation_coverage_contract(self):
        distroseries = self.factory.makeDistroSeries()
        naked_distroseries = removeSecurityProxy(distroseries)
        naked_distroseries.hide_all_translations = False
        naked_distroseries.messagecount = 3
        date_updated = datetime(2026, 8, 16, 11, 45, tzinfo=timezone.utc)
        coverage = self.factory.makeDistroSeriesLanguage(
            distroseries,
            language_name="French",
            current_count=1,
            rosetta_count=1,
            contributor_count=143,
            date_updated=date_updated,
        )
        distroseries_url = api_url(distroseries)
        coverage_url = api_url(coverage)
        language_url = api_url(coverage.language)
        coverage_web_link = canonical_url(coverage)
        webservice, collection_link = self.getTranslationCoverage(distroseries)

        self.assertEqual(
            webservice.getAbsoluteUrl(
                distroseries_url + "/translation_coverage"
            ),
            collection_link,
        )
        entry_response = webservice.get(
            webservice.getAbsoluteUrl(coverage_url)
        )
        self.assertEqual(200, entry_response.status)
        response = webservice.get(collection_link)
        self.assertEqual(200, response.status)
        collection = response.jsonBody()
        self.assertEqual(0, collection["start"])
        self.assertEqual(1, collection["total_size"])
        self.assertEqual(1, len(collection["entries"]))
        entry = collection["entries"][0]
        self.assertEqual(
            {
                "contributor_count",
                "date_updated",
                "distroseries_link",
                "http_etag",
                "language_code",
                "language_link",
                "language_name",
                "resource_type_link",
                "self_link",
                "translated_percentage",
                "web_link",
            },
            set(entry),
        )
        self.assertEqual(
            webservice.getAbsoluteUrl(coverage_url), entry["self_link"]
        )
        self.assertEqual(coverage_web_link, entry["web_link"])
        self.assertEqual(
            "http://api.launchpad.test/devel/#distro_series_language",
            entry["resource_type_link"],
        )
        self.assertEqual(
            webservice.getAbsoluteUrl(language_url),
            entry["language_link"],
        )
        self.assertEqual(
            webservice.getAbsoluteUrl(distroseries_url),
            entry["distroseries_link"],
        )
        self.assertEqual(coverage.language.code, entry["language_code"])
        self.assertEqual("French", entry["language_name"])
        self.assertEqual(date_updated.isoformat(), entry["date_updated"])
        self.assertEqual(66, entry["translated_percentage"])
        self.assertEqual(143, entry["contributor_count"])
        self.assertEqual(entry, webservice.get(entry["self_link"]).jsonBody())

    def test_translation_coverage_percentage_is_exact_and_clamped(self):
        distroseries = self.factory.makeDistroSeries()
        naked_distroseries = removeSecurityProxy(distroseries)
        naked_distroseries.hide_all_translations = False
        naked_distroseries.messagecount = 100
        self.factory.makeDistroSeriesLanguage(
            distroseries,
            language_name="Exact",
            language_code="exact",
            current_count=29,
        )
        self.factory.makeDistroSeriesLanguage(
            distroseries,
            language_name="Clamped",
            language_code="clamped",
            current_count=101,
        )
        self.factory.makeDistroSeriesLanguage(
            distroseries,
            language_name="Negative",
            language_code="negative",
            current_count=-1,
        )
        distroseries_url = api_url(distroseries)
        webservice = webservice_for_person(None, default_api_version="devel")

        response = webservice.named_get(
            distroseries_url,
            "getTranslationCoverage",
            order_by=["language_code"],
        )

        self.assertEqual(200, response.status)
        percentages = {
            entry["language_code"]: entry["translated_percentage"]
            for entry in response.jsonBody()["entries"]
        }
        self.assertEqual(
            {"clamped": 100, "exact": 29, "negative": 0}, percentages
        )

    def test_translation_coverage_order_visibility_and_pagination(self):
        distroseries = self.factory.makeDistroSeries()
        naked_distroseries = removeSecurityProxy(distroseries)
        naked_distroseries.hide_all_translations = False
        naked_distroseries.messagecount = 1
        coverages = [
            self.factory.makeDistroSeriesLanguage(
                distroseries, language_name=name
            )
            for name in ("Zulu", "alpha", "Beta")
        ]
        hidden = self.factory.makeDistroSeriesLanguage(
            distroseries, language_name="aardvark"
        )
        removeSecurityProxy(hidden.language).visible = False
        webservice, collection_link = self.getTranslationCoverage(distroseries)

        collection = webservice.get(collection_link).jsonBody()
        self.assertEqual(0, collection["start"])
        self.assertEqual(3, collection["total_size"])
        self.assertEqual(
            [
                coverages[1].language.code,
                coverages[2].language.code,
                coverages[0].language.code,
            ],
            [entry["language_code"] for entry in collection["entries"]],
        )
        self.assertNotIn(
            hidden.language.code,
            [entry["language_code"] for entry in collection["entries"]],
        )

        page = webservice.get(
            collection_link + "?ws.start=1&ws.size=1"
        ).jsonBody()
        self.assertEqual(1, page["start"])
        self.assertEqual(3, page["total_size"])
        self.assertEqual(
            [coverages[2].language.code],
            [entry["language_code"] for entry in page["entries"]],
        )

    def test_distroserieslanguages_preloads_languages(self):
        distroseries = self.factory.makeDistroSeries()
        for language_name in ("Zulu", "alpha", "Beta"):
            self.factory.makeDistroSeriesLanguage(
                distroseries, language_name=language_name
            )
        IStore(distroseries).flush()
        IStore(distroseries).invalidate()

        with StormStatementRecorder() as recorder:
            coverages = list(distroseries.distroserieslanguages)
            language_names = [coverage.language_name for coverage in coverages]

        self.assertEqual(["Beta", "Zulu", "alpha"], language_names)
        self.assertThat(recorder, HasQueryCount(Equals(2)))

    def test_get_translation_coverage_accepts_scalar_order_by(self):
        distroseries = self.factory.makeDistroSeries()
        naked_distroseries = removeSecurityProxy(distroseries)
        naked_distroseries.hide_all_translations = False
        for language_name in ("Alpha", "Beta"):
            self.factory.makeDistroSeriesLanguage(
                distroseries, language_name=language_name
            )

        coverages = naked_distroseries.getTranslationCoverage("-language_name")

        self.assertEqual(
            ["Beta", "Alpha"],
            [coverage.language_name for coverage in coverages],
        )

    def test_get_translation_coverage_sort_options(self):
        distroseries = self.factory.makeDistroSeries()
        naked_distroseries = removeSecurityProxy(distroseries)
        naked_distroseries.hide_all_translations = False
        # Alpha and Zulu both export 1%. This checks that later sort fields
        # resolve equal rounded percentages.
        naked_distroseries.messagecount = 1000
        coverages = {
            "alpha": self.factory.makeDistroSeriesLanguage(
                distroseries,
                language_name="alpha",
                language_code="alpha",
                current_count=10,
                contributor_count=20,
                date_updated=datetime(2026, 1, 2, tzinfo=timezone.utc),
            ),
            "Beta": self.factory.makeDistroSeriesLanguage(
                distroseries,
                language_name="Beta",
                language_code="beta",
                current_count=1,
                rosetta_count=29,
                contributor_count=10,
                date_updated=datetime(2026, 1, 3, tzinfo=timezone.utc),
            ),
            "Zulu": self.factory.makeDistroSeriesLanguage(
                distroseries,
                language_name="Zulu",
                language_code="zulu",
                current_count=19,
                contributor_count=30,
                date_updated=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
        }
        distroseries_url = api_url(distroseries)
        webservice = webservice_for_person(None, default_api_version="devel")

        response = webservice.named_get(
            distroseries_url, "getTranslationCoverage"
        )
        self.assertEqual(200, response.status)
        self.assertEqual(
            ["alpha", "Beta", "Zulu"],
            [
                entry["language_name"]
                for entry in response.jsonBody()["entries"]
            ],
        )

        sort_cases = [
            ([], ["alpha", "Beta", "Zulu"]),
            (["language_name"], ["alpha", "Beta", "Zulu"]),
            (["-language_name"], ["Zulu", "Beta", "alpha"]),
            (["language_code"], ["alpha", "Beta", "Zulu"]),
            (["-language_code"], ["Zulu", "Beta", "alpha"]),
            (["-translated_percentage"], ["Beta", "alpha", "Zulu"]),
            (["translated_percentage"], ["alpha", "Zulu", "Beta"]),
            (["-contributor_count"], ["Zulu", "alpha", "Beta"]),
            (["contributor_count"], ["Beta", "alpha", "Zulu"]),
            (["-date_updated"], ["Beta", "alpha", "Zulu"]),
            (["date_updated"], ["Zulu", "alpha", "Beta"]),
            (
                ["translated_percentage", "-contributor_count"],
                ["Zulu", "alpha", "Beta"],
            ),
        ]
        for order_by, expected in sort_cases:
            message = "order_by=%r" % (order_by,)
            response = webservice.named_get(
                distroseries_url,
                "getTranslationCoverage",
                order_by=order_by,
            )
            self.assertEqual(200, response.status, message)
            body = response.jsonBody()
            self.assertEqual(3, body["total_size"], message)
            self.assertEqual(
                expected,
                [entry["language_name"] for entry in body["entries"]],
                message,
            )

        page = webservice.named_get(
            distroseries_url,
            "getTranslationCoverage",
            order_by=["-translated_percentage"],
            **{"ws.start": 1, "ws.size": 1},
        ).jsonBody()
        self.assertEqual(1, page["start"])
        self.assertEqual(
            [coverages["alpha"].language.code],
            [entry["language_code"] for entry in page["entries"]],
        )

    def test_get_translation_coverage_sort_with_no_messages(self):
        distroseries = self.factory.makeDistroSeries()
        naked_distroseries = removeSecurityProxy(distroseries)
        naked_distroseries.hide_all_translations = False
        naked_distroseries.messagecount = 0
        self.factory.makeDistroSeriesLanguage(
            distroseries,
            language_name="Alpha",
            language_code="alpha",
            contributor_count=1,
        )
        self.factory.makeDistroSeriesLanguage(
            distroseries,
            language_name="Beta",
            language_code="beta",
            contributor_count=2,
        )
        distroseries_url = api_url(distroseries)
        webservice = webservice_for_person(None, default_api_version="devel")

        response = webservice.named_get(
            distroseries_url,
            "getTranslationCoverage",
            order_by=["-translated_percentage", "-contributor_count"],
        )

        self.assertEqual(200, response.status)
        entries = response.jsonBody()["entries"]
        self.assertEqual(
            ["Beta", "Alpha"], [entry["language_name"] for entry in entries]
        )
        self.assertEqual(
            [0, 0], [entry["translated_percentage"] for entry in entries]
        )

    def test_get_translation_coverage_rejects_unknown_sort(self):
        distroseries = self.factory.makeDistroSeries()
        removeSecurityProxy(distroseries).hide_all_translations = False
        distroseries_url = api_url(distroseries)
        webservice = webservice_for_person(None, default_api_version="devel")

        response = webservice.named_get(
            distroseries_url,
            "getTranslationCoverage",
            order_by=["unknown"],
        )

        self.assertEqual(400, response.status)
        self.assertIn(b"Unrecognized order_by: 'unknown'", response.body)

    def test_translation_coverage_empty_excludes_placeholder(self):
        distroseries = self.factory.makeDistroSeries()
        removeSecurityProxy(distroseries).hide_all_translations = False
        language = self.factory.makeLanguage()
        placeholder = distroseries.getDistroSeriesLanguageOrEmpty(language)
        self.assertIsNone(placeholder.id)
        self.assertFalse(IDistroSeriesLanguageEntry.providedBy(placeholder))
        webservice, collection_link = self.getTranslationCoverage(distroseries)

        collection = webservice.get(collection_link).jsonBody()
        self.assertEqual(0, collection["start"])
        self.assertEqual(0, collection["total_size"])
        self.assertEqual([], collection["entries"])

    def test_translation_coverage_is_devel_only(self):
        distroseries = self.factory.makeDistroSeries()
        distroseries_url = api_url(distroseries)

        for version in ("beta", "1.0"):
            webservice = webservice_for_person(
                None, default_api_version=version
            )
            entry = webservice.get(distroseries_url).jsonBody()
            message = "API version %s" % version
            self.assertNotIn(
                "translation_coverage_collection_link", entry, message
            )
            response = webservice.named_get(
                distroseries_url, "getTranslationCoverage"
            )
            self.assertEqual(400, response.status, message)

    def test_translation_coverage_respects_hidden_series(self):
        distroseries = self.factory.makeDistroSeries()
        self.assertTrue(distroseries.hide_all_translations)
        translations_admin = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).rosetta_experts]
        )
        self.factory.makeDistroSeriesLanguage(distroseries)
        distroseries_url = api_url(distroseries)
        webservice, collection_link = self.getTranslationCoverage(distroseries)

        response = webservice.get(collection_link)
        self.assertEqual(503, response.status)
        response = webservice.named_get(
            distroseries_url, "getTranslationCoverage"
        )
        self.assertEqual(503, response.status)
        admin_webservice = webservice_for_person(
            translations_admin,
            default_api_version="devel",
            permission=OAuthPermission.READ_PUBLIC,
        )
        response = admin_webservice.get(collection_link)
        self.assertEqual(200, response.status)
        self.assertEqual(1, response.jsonBody()["total_size"])
        response = admin_webservice.named_get(
            distroseries_url, "getTranslationCoverage"
        )
        self.assertEqual(200, response.status)
        self.assertEqual(1, response.jsonBody()["total_size"])


class TestDistroSeriesSet(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def _get_translatables(self):
        distro_series_set = getUtility(IDistroSeriesSet)
        # Get translatables as a sequence of names of the series.
        return sorted(
            series.name for series in distro_series_set.translatables()
        )

    def _ref_translatables(self, expected=None):
        # Return the reference value, merged with expected data.
        if expected is None:
            return self.ref_translatables
        if isinstance(expected, list):
            return sorted(self.ref_translatables + expected)
        return sorted(self.ref_translatables + [expected])

    def test_translatables(self):
        # translatables() returns all distroseries that have potemplates
        # and are not set to "hide all translations".
        # See whatever distroseries sample data already has.
        self.ref_translatables = self._get_translatables()

        new_distroseries = self.factory.makeDistroSeries(name="sampleseries")
        with person_logged_in(new_distroseries.distribution.owner):
            new_distroseries.hide_all_translations = False
        transaction.commit()
        translatables = self._get_translatables()
        self.assertEqual(
            translatables,
            self._ref_translatables(),
            "A newly created distroseries should not be translatable but "
            "translatables() returns %r instead of %r."
            % (translatables, self._ref_translatables()),
        )

        new_sourcepackagename = self.factory.makeSourcePackageName()
        self.factory.makePOTemplate(
            distroseries=new_distroseries,
            sourcepackagename=new_sourcepackagename,
        )
        transaction.commit()
        translatables = self._get_translatables()
        self.assertEqual(
            translatables,
            self._ref_translatables("sampleseries"),
            "After assigning a PO template, a distroseries should be "
            "translatable but translatables() returns %r instead of %r."
            % (translatables, self._ref_translatables("sampleseries")),
        )

        with person_logged_in(new_distroseries.distribution.owner):
            new_distroseries.hide_all_translations = True
        transaction.commit()
        translatables = self._get_translatables()
        self.assertEqual(
            translatables,
            self._ref_translatables(),
            "After hiding all translation, a distroseries should not be "
            "translatable but translatables() returns %r instead of %r."
            % (translatables, self._ref_translatables()),
        )

    def test_fromSuite_release_pocket(self):
        series = self.factory.makeDistroSeries()
        result = getUtility(IDistroSeriesSet).fromSuite(
            series.distribution, series.name
        )
        self.assertEqual((series, PackagePublishingPocket.RELEASE), result)

    def test_fromSuite_non_release_pocket(self):
        series = self.factory.makeDistroSeries()
        suite = "%s-backports" % series.name
        result = getUtility(IDistroSeriesSet).fromSuite(
            series.distribution, suite
        )
        self.assertEqual((series, PackagePublishingPocket.BACKPORTS), result)

    def test_fromSuite_no_such_series(self):
        distribution = self.factory.makeDistribution()
        self.assertRaises(
            NoSuchDistroSeries,
            getUtility(IDistroSeriesSet).fromSuite,
            distribution,
            "doesntexist",
        )
