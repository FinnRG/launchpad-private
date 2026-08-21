# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Security adapters for the snappy package."""

__all__ = []

from lp.app.security import (
    AnonymousAuthorization,
    AuthorizationBase,
    DelegatedAuthorization,
)
from lp.buildmaster.security import AdminBuildRecord
from lp.security import AdminByBuilddAdmin
from lp.services.webapp.security import EditByRegistryExpertsOrAdmins
from lp.snappy.interfaces.snap import ISnap, ISnapBuildRequest
from lp.snappy.interfaces.snapbase import ISnapBase, ISnapBaseSet
from lp.snappy.interfaces.snapbuild import ISnapBuild
from lp.snappy.interfaces.snappyseries import ISnappySeries, ISnappySeriesSet
from lp.snappy.interfaces.snapsubscription import ISnapSubscription


class ViewSnap(AuthorizationBase):
    """Private snaps are only visible to their owners and admins."""

    permission = "launchpad.View"
    usedfor = ISnap

    def checkAuthenticated(self, user):
        return self.obj.visibleByUser(user.person)

    def checkUnauthenticated(self):
        return self.obj.visibleByUser(None)


class DeleteSnap(AuthorizationBase):
    permission = "launchpad.Delete"
    usedfor = ISnap

    def checkAuthenticated(self, user):
        return (
            EditSnap(self.obj).checkAuthenticated(user)
            or user.in_registry_experts
        )


class EditSnap(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = ISnap

    def checkAuthenticated(self, user):
        return (
            user.isOwner(self.obj) or user.in_commercial_admin or user.in_admin
        )


class AdminSnap(AuthorizationBase):
    """Restrict changing build settings on snap packages.

    The security of the non-virtualised build farm depends on these
    settings, so they can only be changed by "PPA"/commercial admins, or by
    "PPA" self admins on snap packages that they can already edit.
    """

    permission = "launchpad.Admin"
    usedfor = ISnap

    def checkAuthenticated(self, user):
        if user.in_ppa_admin or user.in_commercial_admin or user.in_admin:
            return True
        return user.in_ppa_self_admins and EditSnap(
            self.obj
        ).checkAuthenticated(user)


class SnapSubscriptionEdit(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = ISnapSubscription

    def checkAuthenticated(self, user):
        """Is the user able to edit a Snap recipe subscription?

        Any team member can edit a Snap recipe subscription for their
        team.
        Launchpad Admins can also edit any Snap recipe subscription.
        The owner of the subscribed Snap can edit the subscription. If
        the Snap owner is a team, then members of the team can edit
        the subscription.
        """
        return (
            user.inTeam(self.obj.snap.owner)
            or user.inTeam(self.obj.person)
            or user.inTeam(self.obj.subscribed_by)
            or user.in_admin
        )


class SnapSubscriptionView(AuthorizationBase):
    permission = "launchpad.View"
    usedfor = ISnapSubscription

    def checkUnauthenticated(self):
        return self.obj.snap.visibleByUser(None)

    def checkAuthenticated(self, user):
        return self.obj.snap.visibleByUser(user.person)


class ViewSnapBuildRequest(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ISnapBuildRequest

    def __init__(self, obj):
        super().__init__(obj, obj.snap, "launchpad.View")


class ViewSnapBuild(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ISnapBuild

    def iter_objects(self):
        yield self.obj.snap
        yield self.obj.archive


class EditSnapBuild(AdminByBuilddAdmin):
    permission = "launchpad.Edit"
    usedfor = ISnapBuild

    def checkAuthenticated(self, user):
        """Check edit access for snap package builds.

        Allow admins, buildd admins, and the owner of the snap package.
        (Note that the requester of the build is required to be in the team
        that owns the snap package.)
        """
        auth_snap = EditSnap(self.obj.snap)
        if auth_snap.checkAuthenticated(user):
            return True
        return super().checkAuthenticated(user)


class AdminSnapBuild(AdminBuildRecord):
    usedfor = ISnapBuild


class ViewSnappySeries(AnonymousAuthorization):
    """Anyone can view an `ISnappySeries`."""

    usedfor = ISnappySeries


class EditSnappySeries(EditByRegistryExpertsOrAdmins):
    usedfor = ISnappySeries


class EditSnappySeriesSet(EditByRegistryExpertsOrAdmins):
    usedfor = ISnappySeriesSet


class ViewSnapBase(AnonymousAuthorization):
    """Anyone can view an `ISnapBase`."""

    usedfor = ISnapBase


class EditSnapBase(EditByRegistryExpertsOrAdmins):
    usedfor = ISnapBase


class EditSnapBaseSet(EditByRegistryExpertsOrAdmins):
    usedfor = ISnapBaseSet
