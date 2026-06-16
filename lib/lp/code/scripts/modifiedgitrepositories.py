# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Implementation of the Launchpad script to list modified Git repositories."""

__all__ = ["ModifiedGitRepositoriesScript"]


import os
from datetime import datetime, timedelta, timezone
from time import strptime

from zope.component import getUtility

from lp.code.interfaces.gitcollection import IAllGitRepositories
from lp.services.scripts.base import LaunchpadScript, LaunchpadScriptFailure


class ModifiedGitRepositoriesScript(LaunchpadScript):
    """List Git repositories modified since the specified time.

    Only repositories that have been modified since the specified time will
    be returned.  It is possible that the repository will have been modified
    only in the web UI and not actually received any new commits, and will
    be a false positive.
    """

    description = (
        "List repository paths for Git repositories modified since the "
        "specified time."
    )

    def __init__(self, name, dbuser=None, test_args=None):
        LaunchpadScript.__init__(self, name, dbuser, test_args)
        # Cache this on object creation so it can be used in tests.
        self.now_timestamp = datetime.utcnow()
        self.locations = set()

    def add_my_options(self):
        self.parser.add_option(
            "-s",
            "--since",
            metavar="DATE",
            help="A date in the format YYYY-MM-DD.  Repositories that "
            "have been modified since this date will be returned.",
        )
        self.parser.add_option(
            "-l",
            "--last-hours",
            metavar="HOURS",
            type="int",
            help="Return the repositories that have been modified in "
            "the last HOURS number of hours.",
        )
        self.parser.add_option(
            "-r",
            "--repositories-root",
            metavar="ROOT",
            help="The root directory where Git repositories are stored on "
            "disk.  The repository path is formed by joining this root "
            "with the repository's internal numeric ID.",
        )
        self.parser.add_option(
            "--strip-prefix",
            metavar="PREFIX",
            help="The prefix to remove from the repository locations.",
        )
        self.parser.add_option(
            "--append-suffix",
            metavar="SUFFIX",
            help="A suffix to append to the end of the repository locations.",
        )

    def get_last_modified_epoch(self):
        """Return the timezone aware datetime for the last modified epoch."""
        if (
            self.options.last_hours is not None
            and self.options.since is not None
        ):
            raise LaunchpadScriptFailure(
                "Only one of --since or --last-hours can be specified."
            )
        last_modified = None
        if self.options.last_hours is not None:
            last_modified = self.now_timestamp - timedelta(
                hours=self.options.last_hours
            )
        elif self.options.since is not None:
            try:
                parsed_time = strptime(self.options.since, "%Y-%m-%d")
                last_modified = datetime(*(parsed_time[:3]))
            except ValueError as e:
                raise LaunchpadScriptFailure(str(e))
        else:
            raise LaunchpadScriptFailure(
                "One of --since or --last-hours needs to be specified."
            )

        # Make the datetime timezone aware.
        return last_modified.replace(tzinfo=timezone.utc)

    def repository_location(self, repository):
        """Return the repository path on disk for the given repository."""
        if self.options.repositories_root is None:
            raise LaunchpadScriptFailure(
                "--repositories-root must be specified."
            )
        return os.path.join(
            self.options.repositories_root, repository.getInternalPath()
        )

    def process_location(self, location):
        """Strip the defined prefix, and append the suffix as configured."""
        if self.options.strip_prefix is not None and location.startswith(
            self.options.strip_prefix
        ):
            location = location[len(self.options.strip_prefix) :]
        if self.options.append_suffix is not None:
            location = location + self.options.append_suffix
        return location

    def update_locations(self, location):
        """Add the location, and all the possible parent directories."""
        paths = location.split("/")
        curr = []
        for segment in paths:
            curr.append(segment)
            # Don't add an empty string.
            if curr != [""]:
                self.locations.add("/".join(curr))

    def main(self):
        if self.options.repositories_root is None:
            raise LaunchpadScriptFailure(
                "--repositories-root must be specified."
            )
        last_modified = self.get_last_modified_epoch()
        self.logger.info(
            "Looking for Git repositories modified since %s", last_modified
        )
        collection = getUtility(IAllGitRepositories)
        collection = collection.modifiedSince(last_modified)
        for repo_id in collection.getRepositoryIds():
            self.logger.info("Repository %d", repo_id)
            location = os.path.join(
                self.options.repositories_root, str(repo_id)
            )
            self.update_locations(self.process_location(location))

        for location in sorted(self.locations):
            print(location)

        self.logger.info("Done.")
