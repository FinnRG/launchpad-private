# Copyright 2024 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Security adapters for the craft package."""

__all__ = []

from lp.app.security import AuthorizationBase, DelegatedAuthorization
from lp.buildmaster.security import AdminBuildRecord
from lp.crafts.interfaces.craftrecipe import (
    ICraftRecipe,
    ICraftRecipeBuildRequest,
)
from lp.crafts.interfaces.craftrecipebuild import ICraftRecipeBuild
from lp.security import AdminByBuilddAdmin


class ViewCraftRecipe(AuthorizationBase):
    """Private craft recipes are only visible to their owners and admins."""

    permission = "launchpad.View"
    usedfor = ICraftRecipe

    def checkAuthenticated(self, user):
        return self.obj.visibleByUser(user.person)

    def checkUnauthenticated(self):
        return self.obj.visibleByUser(None)


class EditCraftRecipe(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = ICraftRecipe

    def checkAuthenticated(self, user):
        return (
            user.isOwner(self.obj) or user.in_commercial_admin or user.in_admin
        )


class AdminCraftRecipe(AuthorizationBase):
    """Restrict changing build settings on craft recipes.

    The security of the non-virtualised build farm depends on these
    settings, so they can only be changed by "PPA"/commercial admins, or by
    "PPA" self admins on craft recipes that they can already edit.
    """

    permission = "launchpad.Admin"
    usedfor = ICraftRecipe

    def checkAuthenticated(self, user):
        if user.in_ppa_admin or user.in_commercial_admin or user.in_admin:
            return True
        return user.in_ppa_self_admins and EditCraftRecipe(
            self.obj
        ).checkAuthenticated(user)


class ViewCraftRecipeBuildRequest(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ICraftRecipeBuildRequest

    def __init__(self, obj):
        super().__init__(obj, obj.recipe, "launchpad.View")


class ViewCraftRecipeBuild(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ICraftRecipeBuild

    def iter_objects(self):
        yield self.obj.recipe


class EditCraftRecipeBuild(AdminByBuilddAdmin):
    permission = "launchpad.Edit"
    usedfor = ICraftRecipeBuild

    def checkAuthenticated(self, user):
        """Check edit access for craft recipe builds.

        Allow admins, buildd admins, and the owner of the craft recipe.
        (Note that the requester of the build is required to be in the team
        that owns the craft recipe.)
        """
        auth_recipe = EditCraftRecipe(self.obj.recipe)
        if auth_recipe.checkAuthenticated(user):
            return True
        return super().checkAuthenticated(user)


class AdminCraftRecipeBuild(AdminBuildRecord):
    usedfor = ICraftRecipeBuild
