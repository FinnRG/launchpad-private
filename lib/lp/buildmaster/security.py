# Copyright 2011 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Security adapters for the buildmaster package."""

__all__ = [
    "AdminBuildRecord",
    "EditPackageBuild",
    "ViewBuilder",
    "ViewProcessor",
]

from lp.app.security import AnonymousAuthorization
from lp.buildmaster.interfaces.builder import IBuilder, IBuilderSet
from lp.buildmaster.interfaces.buildfarmjob import IBuildFarmJob
from lp.buildmaster.interfaces.packagebuild import IPackageBuild
from lp.buildmaster.interfaces.processor import IProcessor
from lp.security import AdminByBuilddAdmin


class ViewBuilder(AnonymousAuthorization):
    """Anyone can view a `IBuilder`."""

    usedfor = IBuilder


class ViewProcessor(AnonymousAuthorization):
    """Anyone can view an `IProcessor`."""

    usedfor = IProcessor


class AdminBuilderSet(AdminByBuilddAdmin):
    usedfor = IBuilderSet


class AdminBuilder(AdminByBuilddAdmin):
    usedfor = IBuilder


class EditBuilder(AdminByBuilddAdmin):
    permission = "launchpad.Edit"
    usedfor = IBuilder


class ModerateBuilder(EditBuilder):
    permission = "launchpad.Moderate"
    usedfor = IBuilder

    def checkAuthenticated(self, user):
        return user.in_registry_experts or super().checkAuthenticated(user)


class AdminBuildRecord(AdminByBuilddAdmin):
    usedfor = IBuildFarmJob

    def checkAuthenticated(self, user):
        return user.in_launchpad_developers or super().checkAuthenticated(user)


class EditBuildFarmJob(AdminByBuilddAdmin):
    permission = "launchpad.Edit"
    usedfor = IBuildFarmJob


class EditPackageBuild(EditBuildFarmJob):
    usedfor = IPackageBuild

    def checkAuthenticated(self, user):
        """Check if the user has access to edit the archive."""
        if EditBuildFarmJob.checkAuthenticated(self, user):
            return True

        # If the user is in the owning team for the archive,
        # then they have access to edit the builds.
        # If it's a PPA or a copy archive only allow its owner.
        return self.obj.archive.owner and user.inTeam(self.obj.archive.owner)
