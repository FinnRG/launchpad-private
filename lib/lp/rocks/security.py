# Copyright 2009-2024 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Security adapters for the rocks package."""

__all__ = []
from lp.app.security import (
    AnonymousAuthorization,
    AuthorizationBase,
    DelegatedAuthorization,
)
from lp.buildmaster.security import AdminBuildRecord
from lp.rocks.interfaces.rockbase import IRockBase, IRockBaseSet
from lp.rocks.interfaces.rockrecipe import IRockRecipe, IRockRecipeBuildRequest
from lp.rocks.interfaces.rockrecipebuild import IRockRecipeBuild
from lp.security import AdminByBuilddAdmin
from lp.services.webapp.security import EditByRegistryExpertsOrAdmins


class ViewRockRecipe(AuthorizationBase):
    """Private rock recipes are only visible to their owners and admins."""

    permission = "launchpad.View"
    usedfor = IRockRecipe

    def checkAuthenticated(self, user):
        return self.obj.visibleByUser(user.person)

    def checkUnauthenticated(self):
        return self.obj.visibleByUser(None)


class EditRockRecipe(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IRockRecipe

    def checkAuthenticated(self, user):
        return (
            user.isOwner(self.obj) or user.in_commercial_admin or user.in_admin
        )


class AdminRockRecipe(AuthorizationBase):
    """Restrict changing build settings on rock recipes.

    The security of the non-virtualised build farm depends on these
    settings, so they can only be changed by "PPA"/commercial admins, or by
    "PPA" self admins on rock recipes that they can already edit.
    """

    permission = "launchpad.Admin"
    usedfor = IRockRecipe

    def checkAuthenticated(self, user):
        if user.in_ppa_admin or user.in_commercial_admin or user.in_admin:
            return True
        return user.in_ppa_self_admins and EditRockRecipe(
            self.obj
        ).checkAuthenticated(user)


class ViewRockRecipeBuildRequest(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = IRockRecipeBuildRequest

    def __init__(self, obj):
        super().__init__(obj, obj.recipe, "launchpad.View")


class ViewRockRecipeBuild(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = IRockRecipeBuild

    def iter_objects(self):
        yield self.obj.recipe


class EditRockRecipeBuild(AdminByBuilddAdmin):
    permission = "launchpad.Edit"
    usedfor = IRockRecipeBuild

    def checkAuthenticated(self, user):
        """Check edit access for rock recipe builds.

        Allow admins, buildd admins, and the owner of the rock recipe.
        (Note that the requester of the build is required to be in the team
        that owns the rock recipe.)
        """
        auth_recipe = EditRockRecipe(self.obj.recipe)
        if auth_recipe.checkAuthenticated(user):
            return True
        return super().checkAuthenticated(user)


class AdminRockRecipeBuild(AdminBuildRecord):
    usedfor = IRockRecipeBuild


class ViewRockBase(AnonymousAuthorization):
    """Anyone can view an `IRockBase`."""

    usedfor = IRockBase


class EditRockBase(EditByRegistryExpertsOrAdmins):
    usedfor = IRockBase


class EditRockBaseSet(EditByRegistryExpertsOrAdmins):
    usedfor = IRockBaseSet
