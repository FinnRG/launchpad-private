# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Security adapters for the charms package."""

__all__ = []

from lp.app.security import (
    AnonymousAuthorization,
    AuthorizationBase,
    DelegatedAuthorization,
)
from lp.buildmaster.security import AdminBuildRecord
from lp.charms.interfaces.charmbase import ICharmBase, ICharmBaseSet
from lp.charms.interfaces.charmrecipe import (
    ICharmRecipe,
    ICharmRecipeBuildRequest,
)
from lp.charms.interfaces.charmrecipebuild import ICharmRecipeBuild
from lp.security import AdminByBuilddAdmin
from lp.services.webapp.security import EditByRegistryExpertsOrAdmins


class ViewCharmRecipe(AuthorizationBase):
    """Private charm recipes are only visible to their owners and admins."""

    permission = "launchpad.View"
    usedfor = ICharmRecipe

    def checkAuthenticated(self, user):
        return self.obj.visibleByUser(user.person)

    def checkUnauthenticated(self):
        return self.obj.visibleByUser(None)


class EditCharmRecipe(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = ICharmRecipe

    def checkAuthenticated(self, user):
        return (
            user.isOwner(self.obj) or user.in_commercial_admin or user.in_admin
        )


class AdminCharmRecipe(AuthorizationBase):
    """Restrict changing build settings on charm recipes.

    The security of the non-virtualised build farm depends on these
    settings, so they can only be changed by "PPA"/commercial admins, or by
    "PPA" self admins on charm recipes that they can already edit.
    """

    permission = "launchpad.Admin"
    usedfor = ICharmRecipe

    def checkAuthenticated(self, user):
        if user.in_ppa_admin or user.in_commercial_admin or user.in_admin:
            return True
        return user.in_ppa_self_admins and EditCharmRecipe(
            self.obj
        ).checkAuthenticated(user)


class ViewCharmRecipeBuildRequest(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ICharmRecipeBuildRequest

    def __init__(self, obj):
        super().__init__(obj, obj.recipe, "launchpad.View")


class ViewCharmRecipeBuild(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ICharmRecipeBuild

    def iter_objects(self):
        yield self.obj.recipe


class EditCharmRecipeBuild(AdminByBuilddAdmin):
    permission = "launchpad.Edit"
    usedfor = ICharmRecipeBuild

    def checkAuthenticated(self, user):
        """Check edit access for snap package builds.

        Allow admins, buildd admins, and the owner of the charm recipe.
        (Note that the requester of the build is required to be in the team
        that owns the charm recipe.)
        """
        auth_recipe = EditCharmRecipe(self.obj.recipe)
        if auth_recipe.checkAuthenticated(user):
            return True
        return super().checkAuthenticated(user)


class AdminCharmRecipeBuild(AdminBuildRecord):
    usedfor = ICharmRecipeBuild


class ViewCharmBase(AnonymousAuthorization):
    """Anyone can view an `ICharmBase`."""

    usedfor = ICharmBase


class EditCharmBase(EditByRegistryExpertsOrAdmins):
    usedfor = ICharmBase


class EditCharmBaseSet(EditByRegistryExpertsOrAdmins):
    usedfor = ICharmBaseSet
