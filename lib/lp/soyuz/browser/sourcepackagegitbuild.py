# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Source package Git build views."""

__all__ = [
    "SourcePackageGitBuildNavigation",
    "SourcePackageGitBuildView",
]

from lp.buildmaster.enums import BuildQueueStatus
from lp.services.librarian.browser import FileNavigationMixin
from lp.services.propertycache import cachedproperty
from lp.services.webapp import LaunchpadView, Navigation
from lp.soyuz.interfaces.sourcepackagegitbuild import ISourcePackageGitBuild


class SourcePackageGitBuildNavigation(Navigation, FileNavigationMixin):
    usedfor = ISourcePackageGitBuild


class SourcePackageGitBuildView(LaunchpadView):
    """Default view of a source package Git build."""

    @property
    def label(self):
        return self.context.title

    page_title = label

    @cachedproperty
    def eta(self):
        """The datetime when the build job is estimated to complete."""
        queue_record = self.context.buildqueue_record
        if queue_record is None:
            return None
        if queue_record.status == BuildQueueStatus.WAITING:
            start_time = queue_record.getEstimatedJobStartTime()
        else:
            start_time = queue_record.date_started
        if start_time is None:
            return None
        return start_time + queue_record.estimated_duration
