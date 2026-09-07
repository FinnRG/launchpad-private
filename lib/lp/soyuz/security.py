# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Security adapters for the soyuz package."""

__all__ = []

from operator import methodcaller

from storm.expr import And
from zope.component import getUtility, queryAdapter

from lp.app.interfaces.security import IAuthorization
from lp.app.security import (
    AnonymousAuthorization,
    AuthorizationBase,
    DelegatedAuthorization,
)
from lp.buildmaster.security import AdminBuildRecord, EditPackageBuild
from lp.security import (
    AdminByAdminsTeam,
    AdminByBuilddAdmin,
    AdminByCommercialTeamOrAdmins,
)
from lp.services.database.interfaces import IStore
from lp.soyuz.interfaces.archive import IArchive, IArchiveSet
from lp.soyuz.interfaces.archiveauthtoken import IArchiveAuthToken
from lp.soyuz.interfaces.archivedependency import IArchiveDependency
from lp.soyuz.interfaces.archivepermission import IArchivePermissionSet
from lp.soyuz.interfaces.archivesubscriber import (
    IArchiveSubscriber,
    IArchiveSubscriberSet,
    IPersonalArchiveSubscription,
)
from lp.soyuz.interfaces.binarypackagebuild import IBinaryPackageBuild
from lp.soyuz.interfaces.binarypackagerelease import (
    IBinaryPackageReleaseDownloadCount,
)
from lp.soyuz.interfaces.distroarchseries import IDistroArchSeries
from lp.soyuz.interfaces.distroarchseriesfilter import IDistroArchSeriesFilter
from lp.soyuz.interfaces.livefs import ILiveFS
from lp.soyuz.interfaces.livefsbuild import ILiveFSBuild
from lp.soyuz.interfaces.packagecopyjob import IPlainPackageCopyJob
from lp.soyuz.interfaces.packageset import IPackageset, IPackagesetSet
from lp.soyuz.interfaces.publishing import (
    IBinaryPackagePublishingHistory,
    IPublishingEdit,
    ISourcePackagePublishingHistory,
)
from lp.soyuz.interfaces.queue import (
    IPackageUpload,
    IPackageUploadLog,
    IPackageUploadQueue,
)
from lp.soyuz.interfaces.sourcepackagerelease import ISourcePackageRelease
from lp.soyuz.model.archive import Archive, get_enabled_archive_filter


class ViewDistroArchSeries(AnonymousAuthorization):
    """Anyone can view a DistroArchSeries."""

    usedfor = IDistroArchSeries


class ModerateDistroArchSeries(AuthorizationBase):
    permission = "launchpad.Moderate"
    usedfor = IDistroArchSeries

    def checkAuthenticated(self, user):
        return (
            user.isOwner(self.obj.distroseries.distribution.main_archive)
            or user.in_admin
        )


class ViewDistroArchSeriesFilter(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = IDistroArchSeriesFilter

    def __init__(self, obj):
        super().__init__(obj, obj.distroarchseries, "launchpad.View")


class EditDistroArchSeriesFilter(DelegatedAuthorization):
    permission = "launchpad.Edit"
    usedfor = IDistroArchSeriesFilter

    def __init__(self, obj):
        super().__init__(obj, obj.distroarchseries, "launchpad.Moderate")


class EditPackageUploadQueue(AdminByAdminsTeam):
    permission = "launchpad.Edit"
    usedfor = IPackageUploadQueue

    def checkAuthenticated(self, user):
        """Check user presence in admins or distroseries upload admin team."""
        if AdminByAdminsTeam.checkAuthenticated(self, user):
            return True

        permission_set = getUtility(IArchivePermissionSet)
        component_permissions = permission_set.componentsForQueueAdmin(
            self.obj.distroseries.distribution.all_distro_archives, user.person
        )
        if not component_permissions.is_empty():
            return True
        pocket_permissions = permission_set.pocketsForQueueAdmin(
            self.obj.distroseries.distribution.all_distro_archives, user.person
        )
        for permission in pocket_permissions:
            if permission.distroseries in (None, self.obj.distroseries):
                return True
        return False


class EditPlainPackageCopyJob(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IPlainPackageCopyJob

    def checkAuthenticated(self, user):
        archive = self.obj.target_archive
        if archive.is_ppa:
            return archive.checkArchivePermission(user.person)

        permission_set = getUtility(IArchivePermissionSet)
        permissions = permission_set.componentsForQueueAdmin(
            archive, user.person
        )
        return not permissions.is_empty()


class ViewPackageUpload(AuthorizationBase):
    """Restrict viewing of package uploads.

    Anyone who can see the archive or the sourcepackagerelease can see the
    upload.  The SPR may be visible without the archive being visible if the
    source package has been copied from a private archive.
    """

    permission = "launchpad.View"
    usedfor = IPackageUpload

    def iter_adapters(self):
        yield ViewArchive(self.obj.archive)
        # We cannot use self.obj.sourcepackagerelease, as that causes
        # interference with the property cache if we are called in the
        # process of adding a source or a build.
        if self.obj.sources:
            spr = self.obj.sources[0].sourcepackagerelease
        elif self.obj.builds:
            spr = self.obj.builds[0].build.source_package_release
        else:
            spr = None
        if spr is not None:
            yield ViewSourcePackageRelease(spr)

    def checkAuthenticated(self, user):
        return any(
            map(methodcaller("checkAuthenticated", user), self.iter_adapters())
        )

    def checkUnauthenticated(self):
        return any(
            map(methodcaller("checkUnauthenticated"), self.iter_adapters())
        )


class ViewPackageUploadLog(DelegatedAuthorization):
    """Anyone who can view a package upload can view its logs."""

    permission = "launchpad.View"
    usedfor = IPackageUploadLog

    def __init__(self, obj):
        super().__init__(obj, obj.package_upload)


class EditPackageUpload(AdminByAdminsTeam):
    permission = "launchpad.Edit"
    usedfor = IPackageUpload

    def checkAuthenticated(self, user):
        """Return True if user has an ArchivePermission or is an admin."""
        if AdminByAdminsTeam.checkAuthenticated(self, user):
            return True

        return self.obj.archive.canAdministerQueue(
            user.person,
            self.obj.components,
            self.obj.pocket,
            self.obj.distroseries,
        )


class EditBinaryPackageBuild(EditPackageBuild):
    permission = "launchpad.Edit"
    usedfor = IBinaryPackageBuild

    def checkAuthenticated(self, user):
        """Check write access for user and different kinds of archives.

        Allow
            * BuilddAdmins, for any archive.
            * The PPA owner for PPAs
            * users with upload permissions (for the respective distribution)
              otherwise.
        """
        if EditPackageBuild.checkAuthenticated(self, user):
            return True

        # Primary or partner section here: is the user in question allowed
        # to upload to the respective component, packageset or package? Allow
        # user to retry build if so.
        # strict_component is True because the source package already exists,
        # otherwise, how can they give it back?
        check_perms = self.obj.archive.checkUpload(
            user.person,
            self.obj.distro_series,
            self.obj.source_package_release.sourcepackagename,
            self.obj.current_component,
            self.obj.pocket,
            strict_component=True,
        )
        return check_perms == None


class ViewBinaryPackageBuild(EditBinaryPackageBuild):
    permission = "launchpad.View"

    # This code MUST match the logic in
    # IBinaryPackageBuildSet.getBuildsForBuilder() otherwise users are
    # likely to get 403 errors, or worse.
    def checkAuthenticated(self, user):
        """Private restricts to admins and archive members."""
        if not self.obj.archive.private:
            # Anyone can see non-private archives.
            return True

        if user.inTeam(self.obj.archive.owner):
            # Anyone in the PPA team gets the nod.
            return True

        # LP admins may also see it.
        if user.in_admin:
            return True

        # If the permission check on the sourcepackagerelease for this
        # build passes then it means the build can be released from
        # privacy since the source package is published publicly.
        # This happens when Archive.copyPackage is used to re-publish a
        # private package in the primary archive.
        auth_spr = ViewSourcePackageRelease(self.obj.source_package_release)
        if auth_spr.checkAuthenticated(user):
            return True

        # You're not a celebrity, get out of here.
        return False

    def checkUnauthenticated(self):
        """Unauthenticated users can see the build if it's not private."""
        if not self.obj.archive.private:
            return True

        # See comment above.
        auth_spr = ViewSourcePackageRelease(self.obj.source_package_release)
        return auth_spr.checkUnauthenticated()


class ModerateBinaryPackageBuild(ViewBinaryPackageBuild):
    permission = "launchpad.Moderate"

    def checkAuthenticated(self, user):
        # Only people who can see the build and administer its archive can
        # edit restricted attributes of builds.  (Currently this allows
        # setting BinaryPackageBuild.external_dependencies; people who can
        # administer the archive can already achieve the same effect by
        # setting Archive.external_dependencies.)
        return super().checkAuthenticated(user) and AdminArchive(
            self.obj.archive
        ).checkAuthenticated(user)

    def checkUnauthenticated(self):
        return False


class ViewArchive(AuthorizationBase):
    """Restrict viewing of private archives.

    Only admins or members of a private team can view the archive.
    """

    permission = "launchpad.View"
    usedfor = IArchive

    def checkAuthenticated(self, user):
        """Verify that the user can view the archive."""
        archive_set: IArchiveSet = getUtility(IArchiveSet)
        return archive_set.checkViewPermission([self.obj], user.person)[
            self.obj
        ]

    def checkUnauthenticated(self):
        """Unauthenticated users can see the PPA if it's not private."""
        return not self.obj.private and self.obj.enabled


class SubscriberViewArchive(ViewArchive):
    """Restrict viewing of private archives."""

    permission = "launchpad.SubscriberView"
    usedfor = IArchive

    def checkAuthenticated(self, user):
        if user.person in self.obj._known_subscribers:
            return True
        if super().checkAuthenticated(user):
            return True
        filter = get_enabled_archive_filter(
            user.person, include_subscribed=True
        )
        return (
            not IStore(self.obj)
            .find(Archive.id, And(Archive.id == self.obj.id, filter))
            .is_empty()
        )


class LimitedViewArchive(AuthorizationBase):
    """Restricted existence knowledge of private archives.

    Just delegate to SubscriberView, since that includes View.
    """

    permission = "launchpad.LimitedView"
    usedfor = IArchive

    def checkUnauthenticated(self):
        yield self.obj, "launchpad.SubscriberView"

    def checkAuthenticated(self, user):
        yield self.obj, "launchpad.SubscriberView"


class EditArchive(AuthorizationBase):
    """Restrict archive editing operations.

    If the archive a primary archive then we check the user is in the
    distribution's owning team, otherwise we check the archive owner.
    """

    permission = "launchpad.Edit"
    usedfor = IArchive

    def checkAuthenticated(self, user):
        if self.obj.is_main:
            return user.isOwner(self.obj.distribution) or user.in_admin

        return user.isOwner(self.obj) or user.in_admin


class DeleteArchive(EditArchive):
    """Restrict archive deletion operations.

    People who can edit an archive can delete it.  In addition, registry
    experts can delete non-main archives, as a spam control mechanism.
    """

    permission = "launchpad.Delete"
    usedfor = IArchive

    def checkAuthenticated(self, user):
        return super().checkAuthenticated(user) or (
            not self.obj.is_main and user.in_registry_experts
        )


class AppendArchive(AuthorizationBase):
    """Restrict appending (upload and copy) operations on archives.

    No one can upload to disabled archives.

    PPA upload rights are managed via `IArchive.checkArchivePermission`;

    Appending to PRIMARY, PARTNER or COPY archives is restricted to owners.
    """

    permission = "launchpad.Append"
    usedfor = IArchive

    def checkAuthenticated(self, user):
        if not self.obj.enabled:
            return False

        if user.inTeam(self.obj.owner):
            return True

        if self.obj.is_ppa and self.obj.checkArchivePermission(user.person):
            return True

        return False


class ModerateArchive(AuthorizationBase):
    """Protect site-wide resources for archives.

    Buildd admins can change this, as a site-wide resource that requires
    arbitration, especially between distribution builds and builds in
    non-virtualized PPAs.  PPA/commercial admins can also change this since
    it affects the relative priority of (private) PPAs.  Launchpad developers
    can also change this, as they need to update PPA sizes, the privacy status,
    or make adjustments for the publishing method or the repository format.
    """

    permission = "launchpad.Moderate"
    usedfor = IArchive

    def checkAuthenticated(self, user):
        return (
            user.in_buildd_admin
            or user.in_launchpad_developers
            or AdminArchive(self.obj).checkAuthenticated(user)
        )


class AdminArchive(AuthorizationBase):
    """Restrict changing privacy and build settings on archives.

    The security of the non-virtualised build farm depends on these
    settings, so they can only be changed by PPA/commercial admins, or by
    PPA self admins on PPAs that they can already edit.
    """

    permission = "launchpad.Admin"
    usedfor = IArchive

    def checkAuthenticated(self, user):
        if user.in_ppa_admin or user.in_commercial_admin or user.in_admin:
            return True
        return user.in_ppa_self_admins and EditArchive(
            self.obj
        ).checkAuthenticated(user)


class EditArchiveDependency(DelegatedAuthorization):

    permission = "launchpad.Edit"
    usedfor = IArchiveDependency

    def __init__(self, obj):
        super().__init__(obj, obj.parent)


class ViewArchiveAuthToken(AuthorizationBase):
    """Restrict viewing of archive tokens.

    The user just needs to be mentioned in the token, have append privilege
    to the archive or be an admin.
    """

    permission = "launchpad.View"
    usedfor = IArchiveAuthToken

    def checkAuthenticated(self, user):
        if user.person == self.obj.person:
            return True
        auth_edit = EditArchiveAuthToken(self.obj)
        return auth_edit.checkAuthenticated(user)


class EditArchiveAuthToken(DelegatedAuthorization):
    """Restrict editing of archive tokens.

    The user should have append privileges to the context archive, or be an
    admin.
    """

    permission = "launchpad.Edit"
    usedfor = IArchiveAuthToken

    def __init__(self, obj):
        super().__init__(obj, obj.archive, "launchpad.Append")

    def checkAuthenticated(self, user):
        return user.in_admin or super().checkAuthenticated(user)


class ViewPersonalArchiveSubscription(DelegatedAuthorization):
    """Restrict viewing of personal archive subscriptions (non-db class).

    The user should be the subscriber, have append privilege to the archive
    or be an admin.
    """

    permission = "launchpad.View"
    usedfor = IPersonalArchiveSubscription

    def __init__(self, obj):
        super().__init__(obj, obj.archive, "launchpad.Append")

    def checkAuthenticated(self, user):
        if user.person == self.obj.subscriber or user.in_admin:
            return True
        return super().checkAuthenticated(user)


class ViewArchiveSubscriber(DelegatedAuthorization):
    """Restrict viewing of archive subscribers.

    The user should be the subscriber, have append privilege to the
    archive or be an admin.
    """

    permission = "launchpad.View"
    usedfor = IArchiveSubscriber

    def __init__(self, obj):
        super().__init__(obj, obj, "launchpad.Edit")

    def checkAuthenticated(self, user):
        return (
            user.inTeam(self.obj.subscriber)
            or user.in_commercial_admin
            or super().checkAuthenticated(user)
        )


class EditArchiveSubscriber(DelegatedAuthorization):
    """Restrict editing of archive subscribers.

    The user should have append privilege to the archive or be an admin.
    """

    permission = "launchpad.Edit"
    usedfor = IArchiveSubscriber

    def __init__(self, obj):
        super().__init__(obj, obj.archive, "launchpad.Append")

    def checkAuthenticated(self, user):
        return (
            user.in_admin
            or user.in_commercial_admin
            or super().checkAuthenticated(user)
        )


class AdminArchiveSubscriberSet(AdminByCommercialTeamOrAdmins):
    """Only (commercial) admins can manipulate archive subscribers in bulk."""

    usedfor = IArchiveSubscriberSet


class ViewSourcePackagePublishingHistory(AuthorizationBase):
    """Restrict viewing of source publications."""

    permission = "launchpad.View"
    usedfor = ISourcePackagePublishingHistory

    def checkUnauthenticated(self):
        yield self.obj.archive, "launchpad.SubscriberView"

    def checkAuthenticated(self, user):
        yield self.obj.archive, "launchpad.SubscriberView"


class EditPublishing(DelegatedAuthorization):
    """Restrict editing of source and binary packages.."""

    permission = "launchpad.Edit"
    usedfor = IPublishingEdit

    def __init__(self, obj):
        super().__init__(obj, obj.archive, "launchpad.Append")


class ViewBinaryPackagePublishingHistory(ViewSourcePackagePublishingHistory):
    """Restrict viewing of binary publications."""

    usedfor = IBinaryPackagePublishingHistory


class ViewBinaryPackageReleaseDownloadCount(
    ViewSourcePackagePublishingHistory
):
    """Restrict viewing of binary package download counts."""

    usedfor = IBinaryPackageReleaseDownloadCount


class ViewSourcePackageRelease(AuthorizationBase):
    """Restrict viewing of source packages.

    Packages that are only published in private archives are subject to the
    same viewing rules as the archive (see class ViewArchive).

    If the package is published in any non-private archive, then it is
    automatically viewable even if the package is also published in
    a private archive.
    """

    permission = "launchpad.View"
    usedfor = ISourcePackageRelease

    def checkAuthenticated(self, user):
        """Verify that the user can view the sourcepackagerelease."""
        for archive in self.obj.published_archives:
            adapter = queryAdapter(archive, IAuthorization, self.permission)
            if adapter is not None and adapter.checkAuthenticated(user):
                return True
        return False

    def checkUnauthenticated(self):
        """Check unauthenticated users.

        Unauthenticated users can see the package as long as it's published
        in a non-private archive.
        """
        for archive in self.obj.published_archives:
            if not archive.private:
                return True
        return False


class ViewPackageset(AnonymousAuthorization):
    """Anyone can view an IPackageset."""

    usedfor = IPackageset


class EditPackageset(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IPackageset

    def checkAuthenticated(self, user):
        """The owner of a package set can edit the object."""
        return user.isOwner(self.obj) or user.in_admin


class ModeratePackageset(AdminByBuilddAdmin):
    permission = "launchpad.Moderate"
    usedfor = IPackageset


class EditPackagesetSet(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IPackagesetSet

    def checkAuthenticated(self, user):
        """Users must be an admin or a member of the tech board."""
        return user.in_admin or user.in_ubuntu_techboard


class ViewLiveFS(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ILiveFS

    def __init__(self, obj):
        super().__init__(obj, obj.owner, "launchpad.View")


class EditLiveFS(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = ILiveFS

    def checkAuthenticated(self, user):
        return (
            user.isOwner(self.obj) or user.in_commercial_admin or user.in_admin
        )


class ModerateLiveFS(ModerateArchive):
    """Restrict changing the build score on live filesystems."""

    usedfor = ILiveFS


class AdminLiveFS(AuthorizationBase):
    """Restrict changing build settings on live filesystems.

    The security of the non-virtualised build farm depends on these
    settings, so they can only be changed by "PPA"/commercial admins, or by
    "PPA" self admins on live filesystems that they can already edit.
    """

    permission = "launchpad.Admin"
    usedfor = ILiveFS

    def checkAuthenticated(self, user):
        if user.in_ppa_admin or user.in_commercial_admin or user.in_admin:
            return True
        return user.in_ppa_self_admins and EditLiveFS(
            self.obj
        ).checkAuthenticated(user)


class ViewLiveFSBuild(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ILiveFSBuild

    def iter_objects(self):
        yield self.obj.livefs
        yield self.obj.archive


class EditLiveFSBuild(AdminByBuilddAdmin):
    permission = "launchpad.Edit"
    usedfor = ILiveFSBuild

    def checkAuthenticated(self, user):
        """Check edit access for live filesystem builds.

        Allow admins, buildd admins, and the owner of the live filesystem.
        (Note that the requester of the build is required to be in the team
        that owns the live filesystem.)
        """
        auth_livefs = EditLiveFS(self.obj.livefs)
        if auth_livefs.checkAuthenticated(user):
            return True
        return super().checkAuthenticated(user)


class AdminLiveFSBuild(AdminBuildRecord):
    usedfor = ILiveFSBuild
