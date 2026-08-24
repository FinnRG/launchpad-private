# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Security adapters for the oci package."""

__all__ = []

from lp.app.security import (
    AnonymousAuthorization,
    AuthorizationBase,
    DelegatedAuthorization,
)
from lp.buildmaster.security import AdminBuildRecord
from lp.oci.interfaces.ocipushrule import IOCIPushRule
from lp.oci.interfaces.ocirecipe import IOCIRecipe, IOCIRecipeBuildRequest
from lp.oci.interfaces.ocirecipebuild import IOCIRecipeBuild
from lp.oci.interfaces.ocirecipesubscription import IOCIRecipeSubscription
from lp.oci.interfaces.ociregistrycredentials import IOCIRegistryCredentials
from lp.security import AdminByBuilddAdmin


class ViewOCIRecipeBuildRequest(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = IOCIRecipeBuildRequest

    def __init__(self, obj):
        super().__init__(obj, obj.recipe, "launchpad.View")


class ViewOCIRecipe(AnonymousAuthorization):
    """Anyone can view public `IOCIRecipe`, but only subscribers can view
    private ones.
    """

    usedfor = IOCIRecipe

    def checkUnauthenticated(self):
        return self.obj.visibleByUser(None)

    def checkAuthenticated(self, user):
        return self.obj.visibleByUser(user.person)


class EditOCIRecipe(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IOCIRecipe

    def checkAuthenticated(self, user):
        return (
            user.isOwner(self.obj) or user.in_commercial_admin or user.in_admin
        )


class AdminOCIRecipe(AuthorizationBase):
    """Restrict changing build settings on OCI recipes.

    The security of the non-virtualised build farm depends on these
    settings, so they can only be changed by "PPA"/commercial admins, or by
    "PPA" self admins on OCI recipes that they can already edit.
    """

    permission = "launchpad.Admin"
    usedfor = IOCIRecipe

    def checkAuthenticated(self, user):
        if user.in_ppa_admin or user.in_commercial_admin or user.in_admin:
            return True
        return user.in_ppa_self_admins and EditOCIRecipe(
            self.obj
        ).checkAuthenticated(user)


class OCIRecipeSubscriptionEdit(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IOCIRecipeSubscription

    def checkAuthenticated(self, user):
        """Is the user able to edit an OCI recipe subscription?

        Any team member can edit a OCI recipe subscription for their
        team.
        Launchpad Admins can also edit any OCI recipe subscription.
        The owner of the subscribed OCI recipe can edit the subscription. If
        the OCI recipe owner is a team, then members of the team can edit
        the subscription.
        """
        return (
            user.inTeam(self.obj.recipe.owner)
            or user.inTeam(self.obj.person)
            or user.inTeam(self.obj.subscribed_by)
            or user.in_admin
        )


class OCIRecipeSubscriptionView(AuthorizationBase):
    permission = "launchpad.View"
    usedfor = IOCIRecipeSubscription

    def checkUnauthenticated(self):
        return self.obj.recipe.visibleByUser(None)

    def checkAuthenticated(self, user):
        return self.obj.recipe.visibleByUser(user.person)


class ViewOCIRecipeBuild(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = IOCIRecipeBuild

    def iter_objects(self):
        yield self.obj.recipe


class EditOCIRecipeBuild(AdminByBuilddAdmin):
    permission = "launchpad.Edit"
    usedfor = IOCIRecipeBuild

    def checkAuthenticated(self, user):
        """Check edit access for OCI recipe builds.

        Allow admins, buildd admins, and the owner of the OCI recipe.
        (Note that the requester of the build is required to be in the team
        that owns the OCI recipe.)
        """
        auth_recipe = EditOCIRecipe(self.obj.recipe)
        if auth_recipe.checkAuthenticated(user):
            return True
        return super().checkAuthenticated(user)


class AdminOCIRecipeBuild(AdminBuildRecord):
    usedfor = IOCIRecipeBuild


class ViewOCIRegistryCredentials(AuthorizationBase):
    permission = "launchpad.View"
    usedfor = IOCIRegistryCredentials

    def checkAuthenticated(self, user):
        # This must be kept in sync with user_can_edit_credentials_for_owner
        # in lp.oci.interfaces.ociregistrycredentials.
        return user.isOwner(self.obj) or user.in_admin


class ViewOCIPushRule(AnonymousAuthorization):
    """Anyone can view an `IOCIPushRule`."""

    usedfor = IOCIPushRule


class OCIPushRuleEdit(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IOCIPushRule

    def checkAuthenticated(self, user):
        return (
            user.isOwner(self.obj.recipe)
            or user.in_commercial_admin
            or user.in_admin
        )
