# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test the modified Git repositories script."""

import os
from datetime import datetime, timezone

from lp.code.scripts.modifiedgitrepositories import (
    ModifiedGitRepositoriesScript,
)
from lp.services.scripts.base import LaunchpadScriptFailure
from lp.testing import TestCase, TestCaseWithFactory
from lp.testing.layers import DatabaseFunctionalLayer


class TestModifiedGitRepositoriesLocations(TestCaseWithFactory):
    """Test the ModifiedGitRepositoriesScript.repository_location method."""

    layer = DatabaseFunctionalLayer

    def test_repository(self):
        """A repository location is the physical disk directory formed by
        joining the configured root with the repository's internal path."""
        repository = self.factory.makeGitRepository()
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories",
            test_args=["--last-hours=12", "--repositories-root=/srv/git"],
        )
        location = script.repository_location(repository)
        self.assertEqual(
            os.path.join("/srv/git", repository.getInternalPath()),
            location,
        )

    def test_repository_no_root_raises(self):
        """Calling repository_location without --repositories-root raises."""
        repository = self.factory.makeGitRepository()
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories",
            test_args=["--last-hours=12"],
        )
        self.assertRaises(
            LaunchpadScriptFailure,
            script.repository_location,
            repository,
        )


class TestModifiedGitRepositoriesLastModifiedEpoch(TestCase):
    """Test the calculation of the last modified date."""

    def test_no_args(self):
        """The script needs one of --since or --last-hours to be specified."""
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories", test_args=[]
        )
        self.assertRaises(
            LaunchpadScriptFailure, script.get_last_modified_epoch
        )

    def test_both_args(self):
        """The script cannot have both --since and --last-hours specified."""
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories",
            test_args=["--since=2026-06-16", "--last-hours=12"],
        )
        self.assertRaises(
            LaunchpadScriptFailure, script.get_last_modified_epoch
        )

    def test_modified_since(self):
        """The --since parameter is parsed into a datetime using the YYYY-MM-DD
        format."""
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories", test_args=["--since=2026-06-16"]
        )
        self.assertEqual(
            datetime(2026, 6, 16, tzinfo=timezone.utc),
            script.get_last_modified_epoch(),
        )

    def test_modified_since_bad_format(self):
        """Passing in a bad format string for the --since parameter errors."""
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories", test_args=["--since=2026-06"]
        )
        self.assertRaises(
            LaunchpadScriptFailure, script.get_last_modified_epoch
        )

    def test_modified_last_hours(self):
        """The --last-hours parameter is subtracted from the current
        timestamp."""
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories", test_args=["--last-hours=12"]
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
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories", test_args=["--last-hours=0"]
        )
        self.assertRaises(
            LaunchpadScriptFailure, script.get_last_modified_epoch
        )

    def test_modified_last_hours_negative_raises(self):
        """Passing in a negative value for --last-hours raises an error."""
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories", test_args=["--last-hours=-1"]
        )
        self.assertRaises(
            LaunchpadScriptFailure, script.get_last_modified_epoch
        )


class TestModifiedGitRepositoriesStripPrefix(TestCase):
    """Test the prefix stripping."""

    def test_no_args(self):
        """Without --strip-prefix the option is None."""
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories", test_args=["--last-hours=12"]
        )
        self.assertIsNone(script.options.strip_prefix)

    def test_prefix_is_stripped(self):
        """If the location starts with the prefix, it is stripped."""
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories",
            test_args=["--last-hours=12", "--strip-prefix=/srv/"],
        )
        # Override the append_suffix as we aren't testing that here.
        script.options.append_suffix = None
        location = script.process_location("/srv/testing")
        self.assertEqual("testing", location)

    def test_prefix_boundary_is_respected(self):
        """A path that starts with a string matching the prefix but at a
        different path boundary is left unchanged."""
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories",
            test_args=["--last-hours=12", "--strip-prefix=/srv/ppa"],
        )
        script.options.append_suffix = None
        location = script.process_location("/srv/ppadev/testing")
        self.assertEqual("/srv/ppadev/testing", location)

    def test_non_matching_location_unchanged(self):
        """If the location doesn't start with the prefix, it is left
        unchanged."""
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories",
            test_args=["--last-hours=12", "--strip-prefix=/srv/"],
        )
        # Override the append_suffix as we aren't testing that here.
        script.options.append_suffix = None
        location = script.process_location("/var/testing")
        self.assertEqual("/var/testing", location)


class TestModifiedGitRepositoriesAppendSuffix(TestCase):
    """Test the suffix appending."""

    def test_no_args(self):
        """Without --append-suffix the option is None."""
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories", test_args=["--last-hours=12"]
        )
        self.assertIsNone(script.options.append_suffix)

    def test_suffix_appended(self):
        """The suffix is appended to all repository locations."""
        script = ModifiedGitRepositoriesScript(
            "modified-git-repositories",
            test_args=["--last-hours=12", "--append-suffix=/**"],
        )
        location = script.process_location("/var/testing")
        self.assertEqual("/var/testing/**", location)


class TestModifiedGitRepositoriesUpdateLocations(TestCase):
    """Test the path splitting and reassembly adding to locations."""

    def setUp(self):
        super().setUp()
        self.script = ModifiedGitRepositoriesScript(
            "modified-git-repositories", test_args=["--last-hours=12"]
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
