# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test the modified archives script."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from zope.component import getUtility

from lp.archivepublisher.interfaces.archivepublisherrun import (
    ArchivePublisherRunStatus,
)
from lp.archivepublisher.interfaces.archivepublishinghistory import (
    IArchivePublishingHistorySet,
)
from lp.archivepublisher.scripts.modifiedarchives import ModifiedArchivesScript
from lp.services.scripts.base import LaunchpadScriptFailure
from lp.soyuz.enums import ArchivePublishingMethod
from lp.testing import TestCase, TestCaseWithFactory
from lp.testing.layers import ZopelessDatabaseLayer


class TestModifiedArchivesLastModifiedEpoch(TestCase):
    """Test the calculation of the last modified date."""

    def test_no_args(self):
        """The script needs one of --since or --last-hours to be specified."""
        script = ModifiedArchivesScript("modified-archives", test_args=[])
        self.assertRaises(
            LaunchpadScriptFailure, script.get_last_modified_epoch
        )

    def test_both_args(self):
        """The script cannot have both --since and --last-hours specified."""
        script = ModifiedArchivesScript(
            "modified-archives",
            test_args=["--since=2026-06-16", "--last-hours=12"],
        )
        self.assertRaises(
            LaunchpadScriptFailure, script.get_last_modified_epoch
        )

    def test_modified_since(self):
        """The --since parameter is parsed into a datetime using YYYY-MM-DD
        format."""
        script = ModifiedArchivesScript(
            "modified-archives", test_args=["--since=2026-06-16"]
        )
        self.assertEqual(
            datetime(2026, 6, 16, tzinfo=timezone.utc),
            script.get_last_modified_epoch(),
        )

    def test_modified_since_bad_format(self):
        """Passing in a bad format string for --since errors."""
        script = ModifiedArchivesScript(
            "modified-archives", test_args=["--since=2026-06"]
        )
        self.assertRaises(
            LaunchpadScriptFailure, script.get_last_modified_epoch
        )

    def test_modified_last_hours(self):
        """The --last-hours parameter is subtracted from now_timestamp."""
        script = ModifiedArchivesScript(
            "modified-archives", test_args=["--last-hours=12"]
        )
        # Override the script's now_timestamp to have a definitive test.
        # 3pm on the first of January.
        script.now_timestamp = datetime(2026, 1, 1, 15, tzinfo=timezone.utc)
        # The last modified should be 3am on the same day.
        self.assertEqual(
            datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
            script.get_last_modified_epoch(),
        )

    def test_modified_last_hours_zero_raises(self):
        """Passing in 0 for --last-hours raises an error."""
        script = ModifiedArchivesScript(
            "modified-archives", test_args=["--last-hours=0"]
        )
        self.assertRaises(
            LaunchpadScriptFailure, script.get_last_modified_epoch
        )

    def test_modified_last_hours_negative_raises(self):
        """Passing in a negative value for --last-hours raises an error."""
        script = ModifiedArchivesScript(
            "modified-archives", test_args=["--last-hours=-1"]
        )
        self.assertRaises(
            LaunchpadScriptFailure, script.get_last_modified_epoch
        )


class TestModifiedArchivesStripPrefix(TestCase):
    """Test the prefix stripping."""

    def test_no_args(self):
        """Without --strip-prefix the option is None."""
        script = ModifiedArchivesScript(
            "modified-archives", test_args=["--last-hours=12"]
        )
        self.assertIsNone(script.options.strip_prefix)

    def test_prefix_is_stripped(self):
        """If the location starts with the prefix, it is stripped."""
        script = ModifiedArchivesScript(
            "modified-archives",
            test_args=["--last-hours=12", "--strip-prefix=/srv/"],
        )
        script.options.append_suffix = None
        location = script.process_location("/srv/testing")
        self.assertEqual("testing", location)

    def test_prefix_boundary_is_respected(self):
        """A path that starts with a string matching the prefix but at a
        different path boundary is left unchanged."""
        script = ModifiedArchivesScript(
            "modified-archives",
            test_args=["--last-hours=12", "--strip-prefix=/srv/ppa"],
        )
        script.options.append_suffix = None
        location = script.process_location("/srv/ppadev/testing")
        self.assertEqual("/srv/ppadev/testing", location)

    def test_non_matching_location_unchanged(self):
        """If the location doesn't start with the prefix, it is left
        unchanged."""
        script = ModifiedArchivesScript(
            "modified-archives",
            test_args=["--last-hours=12", "--strip-prefix=/srv/"],
        )
        script.options.append_suffix = None
        location = script.process_location("/var/testing")
        self.assertEqual("/var/testing", location)


class TestModifiedArchivesAppendSuffix(TestCase):
    """Test the suffix appending."""

    def test_no_args(self):
        """Without --append-suffix the option is None."""
        script = ModifiedArchivesScript(
            "modified-archives", test_args=["--last-hours=12"]
        )
        self.assertIsNone(script.options.append_suffix)

    def test_suffix_appended(self):
        """The suffix is appended to all archive locations."""
        script = ModifiedArchivesScript(
            "modified-archives",
            test_args=["--last-hours=12", "--append-suffix=/**"],
        )
        location = script.process_location("/var/testing")
        self.assertEqual("/var/testing/**", location)


class TestModifiedArchivesUpdateLocations(TestCase):
    """Test the path splitting and reassembly adding to locations."""

    def setUp(self):
        super().setUp()
        self.script = ModifiedArchivesScript(
            "modified-archives", test_args=["--last-hours=12"]
        )

    def test_locations_initially_empty(self):
        self.assertEqual(set(), self.script.locations)

    def test_single_path_element(self):
        self.script.update_locations("foo")
        self.assertEqual({"foo"}, self.script.locations)

    def test_single_root_element(self):
        self.script.update_locations("/foo")
        self.assertEqual({"/foo"}, self.script.locations)

    def test_multi_path_element(self):
        self.script.update_locations("foo/bar/baz")
        expected = {"foo", "foo/bar", "foo/bar/baz"}
        self.assertEqual(expected, self.script.locations)

    def test_duplicates(self):
        self.script.update_locations("foo/bar/baz")
        self.script.update_locations("foo/bar/who")
        expected = {"foo", "foo/bar", "foo/bar/baz", "foo/bar/who"}
        self.assertEqual(expected, self.script.locations)


class TestModifiedArchivesArchiveLocation(TestCase):
    """Test the archive_location method."""

    def test_archive_location_uses_pubconfig(self):
        """archive_location returns the archiveroot from getPubConfig."""
        script = ModifiedArchivesScript(
            "modified-archives", test_args=["--last-hours=12"]
        )
        fake_archive = MagicMock()
        fake_pubconf = MagicMock()
        fake_pubconf.archiveroot = "/srv/ppa/~owner/ppa/ubuntu"
        with patch(
            "lp.archivepublisher.scripts.modifiedarchives.getPubConfig",
            return_value=fake_pubconf,
        ):
            location = script.archive_location(fake_archive)
        self.assertEqual("/srv/ppa/~owner/ppa/ubuntu", location)

    def test_archive_location_returns_none_when_no_pubconfig(self):
        """archive_location returns None when getPubConfig returns None."""
        script = ModifiedArchivesScript(
            "modified-archives", test_args=["--last-hours=12"]
        )
        fake_archive = MagicMock()
        with patch(
            "lp.archivepublisher.scripts.modifiedarchives.getPubConfig",
            return_value=None,
        ):
            location = script.archive_location(fake_archive)
        self.assertIsNone(location)


class TestGetArchivesPublishedSince(TestCaseWithFactory):
    """Test ArchivePublishingHistorySet.getArchivesPublishedSince."""

    layer = ZopelessDatabaseLayer

    def test_returns_archive_for_succeeded_run(self):
        """An archive published in a succeeded run after the cutoff is
        returned."""
        archive = self.factory.makeArchive()
        publisher_run = self.factory.makeArchivePublisherRun()
        publisher_run.mark_succeeded()
        self.factory.makeArchivePublishingHistory(
            archive=archive, publisher_run=publisher_run
        )
        cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc)
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        archives = list(
            publishing_history_set.getArchivesPublishedSince(cutoff)
        )
        self.assertIn(archive, archives)

    def test_returns_archive_for_incomplete_run_without_status_filter(self):
        """Without a status filter, archives from incomplete runs are also
        returned."""
        archive = self.factory.makeArchive()
        publisher_run = self.factory.makeArchivePublisherRun()
        # Leave publisher_run in INCOMPLETE state — do not call mark_succeeded.
        self.factory.makeArchivePublishingHistory(
            archive=archive, publisher_run=publisher_run
        )
        cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc)
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        archives = list(
            publishing_history_set.getArchivesPublishedSince(cutoff)
        )
        self.assertIn(archive, archives)

    def test_does_not_return_incomplete_archive_when_filtering_by_succeeded(
        self,
    ):
        """An archive in an incomplete run is excluded when filtering by
        SUCCEEDED."""
        archive = self.factory.makeArchive()
        publisher_run = self.factory.makeArchivePublisherRun()
        # Leave publisher_run in INCOMPLETE state.
        self.factory.makeArchivePublishingHistory(
            archive=archive, publisher_run=publisher_run
        )
        cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc)
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        archives = list(
            publishing_history_set.getArchivesPublishedSince(
                cutoff, status=ArchivePublisherRunStatus.SUCCEEDED
            )
        )
        self.assertNotIn(archive, archives)

    def test_returns_archive_for_failed_run_when_filtering_by_failed(self):
        """An archive in a failed run is returned when filtering by FAILED."""
        archive = self.factory.makeArchive()
        publisher_run = self.factory.makeArchivePublisherRun()
        publisher_run.mark_failed()
        self.factory.makeArchivePublishingHistory(
            archive=archive, publisher_run=publisher_run
        )
        cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc)
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        archives = list(
            publishing_history_set.getArchivesPublishedSince(
                cutoff, status=ArchivePublisherRunStatus.FAILED
            )
        )
        self.assertIn(archive, archives)

    def test_does_not_return_archive_before_cutoff(self):
        """An archive published before the cutoff is not returned."""
        archive = self.factory.makeArchive()
        publisher_run = self.factory.makeArchivePublisherRun()
        publisher_run.mark_succeeded()
        self.factory.makeArchivePublishingHistory(
            archive=archive, publisher_run=publisher_run
        )
        # Set the cutoff to the future so the run's date_started is before it.
        cutoff = datetime(9999, 1, 1, tzinfo=timezone.utc)
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        archives = list(
            publishing_history_set.getArchivesPublishedSince(cutoff)
        )
        self.assertNotIn(archive, archives)

    def test_returns_distinct_archives(self):
        """An archive published in multiple runs is only returned once."""
        archive = self.factory.makeArchive()
        for _ in range(3):
            publisher_run = self.factory.makeArchivePublisherRun()
            publisher_run.mark_succeeded()
            self.factory.makeArchivePublishingHistory(
                archive=archive, publisher_run=publisher_run
            )
        cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc)
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        archives = list(
            publishing_history_set.getArchivesPublishedSince(cutoff)
        )
        self.assertEqual(1, archives.count(archive))

    def test_filters_by_publishing_method(self):
        """Only archives matching the given publishing_method are returned."""
        local_archive = self.factory.makeArchive(
            publishing_method=ArchivePublishingMethod.LOCAL
        )
        artifactory_archive = self.factory.makeArchive(
            publishing_method=ArchivePublishingMethod.ARTIFACTORY
        )
        cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc)
        for archive in (local_archive, artifactory_archive):
            publisher_run = self.factory.makeArchivePublisherRun()
            publisher_run.mark_succeeded()
            self.factory.makeArchivePublishingHistory(
                archive=archive, publisher_run=publisher_run
            )
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        archives = list(
            publishing_history_set.getArchivesPublishedSince(
                cutoff,
                publishing_method=ArchivePublishingMethod.LOCAL,
            )
        )
        self.assertIn(local_archive, archives)
        self.assertNotIn(artifactory_archive, archives)
