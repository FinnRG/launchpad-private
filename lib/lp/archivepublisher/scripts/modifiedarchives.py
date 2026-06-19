# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Implementation of the Launchpad script to list modified archives."""

__all__ = ["ModifiedArchivesScript"]


from datetime import datetime, timedelta, timezone
from time import strptime

from zope.component import getUtility

from lp.archivepublisher.config import getPubConfig
from lp.archivepublisher.interfaces.archivepublishinghistory import (
    IArchivePublishingHistorySet,
)
from lp.services.scripts.base import LaunchpadScript, LaunchpadScriptFailure
from lp.soyuz.enums import ArchivePublishingMethod


class ModifiedArchivesScript(LaunchpadScript):
    """List archives modified since the specified time.

    Archives from publisher runs whose `date_started` is within the
    requested window are returned, regardless of the run status.
    """

    description = (
        "List archive root paths for archives published since the "
        "specified time."
    )

    def __init__(self, name, dbuser=None, test_args=None):
        LaunchpadScript.__init__(self, name, dbuser, test_args)
        # Cache this on object creation so it can be used in tests.
        self.now_timestamp = datetime.now(timezone.utc)
        self.locations = set()

    def add_my_options(self):
        self.parser.add_option(
            "-s",
            "--since",
            metavar="DATE",
            help="A date in the format YYYY-MM-DD.  Archives that "
            "have been published since this date will be returned.",
        )
        self.parser.add_option(
            "-l",
            "--last-hours",
            metavar="HOURS",
            type="int",
            help="Return the archives that have been published in "
            "the last HOURS number of hours.",
        )
        self.parser.add_option(
            "--strip-prefix",
            metavar="PREFIX",
            help="The prefix to remove from the archive locations.",
        )
        self.parser.add_option(
            "--append-suffix",
            metavar="SUFFIX",
            help="A suffix to append to the end of the archive locations.",
        )

    def get_last_modified_epoch(self):
        """Return the timezone-aware datetime for the last modified epoch."""
        if (
            self.options.last_hours is not None
            and self.options.since is not None
        ):
            raise LaunchpadScriptFailure(
                "Only one of --since or --last-hours can be specified."
            )
        if (
            self.options.last_hours is not None
            and self.options.last_hours <= 0
        ):
            raise LaunchpadScriptFailure(
                "--last-hours must be a positive integer."
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

    def archive_location(self, archive):
        """Return the archive root path on disk for the given archive."""
        pubconf = getPubConfig(archive)
        if pubconf is None:
            return None
        return pubconf.archiveroot

    def process_location(self, location):
        """Strip the defined prefix, and append the suffix as configured."""
        if self.options.strip_prefix is not None:
            prefix = self.options.strip_prefix.rstrip("/") + "/"
            if location.startswith(prefix):
                location = location[len(prefix) :]
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
        last_modified = self.get_last_modified_epoch()
        self.logger.info(
            "Looking for archives published since %s", last_modified
        )
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        archives = publishing_history_set.getArchivesPublishedSince(
            last_modified,
            publishing_method=ArchivePublishingMethod.LOCAL,
        )
        for archive in archives:
            self.logger.info(archive.displayname)
            location = self.archive_location(archive)
            if location is None:
                self.logger.warning(
                    "Skipping %s: no publisher config found.",
                    archive.displayname,
                )
                continue
            self.update_locations(self.process_location(location))

        for location in sorted(self.locations):
            print(location)

        self.logger.info("Done.")
