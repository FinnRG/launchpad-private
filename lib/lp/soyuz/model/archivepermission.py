# Copyright 2009-2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Database class for table ArchivePermission."""

__all__ = [
    "ArchivePermission",
    "ArchivePermissionSet",
]

from datetime import timezone
from operator import attrgetter

from lazr.enum import DBItem
from storm.expr import Exists
from storm.locals import (
    SQL,
    And,
    Bool,
    DateTime,
    Int,
    Or,
    Reference,
    Select,
    Store,
)
from zope.component import getUtility
from zope.interface import alsoProvides, implementer
from zope.security.proxy import isinstance as zope_isinstance

from lp.app.errors import NotFoundError
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.sourcepackagename import (
    ISourcePackageName,
    ISourcePackageNameSet,
)
from lp.registry.model.teammembership import TeamParticipation
from lp.services.database.constants import UTC_NOW
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.sqlbase import sqlvalues
from lp.services.database.stormbase import StormBase
from lp.soyuz.enums import ArchivePermissionType
from lp.soyuz.interfaces.archive import ComponentNotFound, IArchive
from lp.soyuz.interfaces.archivepermission import (
    IArchivePermission,
    IArchivePermissionSet,
    IArchiveQueueAdmin,
    IArchiveUploader,
)
from lp.soyuz.interfaces.component import IComponent, IComponentSet
from lp.soyuz.interfaces.packageset import IPackageset


@implementer(IArchivePermission)
class ArchivePermission(StormBase):
    """See `IArchivePermission`."""

    __storm_table__ = "ArchivePermission"
    __storm_order__ = "id"

    id = Int(primary=True)

    date_created = DateTime(
        tzinfo=timezone.utc,
        name="date_created",
        allow_none=False,
        default=UTC_NOW,
    )

    archive_id = Int(name="archive", allow_none=False)
    archive = Reference(archive_id, "Archive.id")

    permission = DBEnum(
        name="permission", allow_none=False, enum=ArchivePermissionType
    )

    person_id = Int(name="person", allow_none=False)
    person = Reference(person_id, "Person.id")

    component_id = Int(name="component", allow_none=True)
    component = Reference(component_id, "Component.id")

    sourcepackagename_id = Int(name="sourcepackagename", allow_none=True)
    sourcepackagename = Reference(sourcepackagename_id, "SourcePackageName.id")

    packageset_id = Int(name="packageset", allow_none=True)
    packageset = Reference(packageset_id, "Packageset.id")

    explicit = Bool(name="explicit", allow_none=False, default=False)

    pocket = DBEnum(name="pocket", enum=PackagePublishingPocket)

    distroseries_id = Int(name="distroseries", allow_none=True)
    distroseries = Reference(distroseries_id, "DistroSeries.id")

    def __init__(
        self,
        archive,
        permission,
        person,
        component=None,
        sourcepackagename=None,
        packageset=None,
        explicit=False,
        distroseries=None,
        pocket=None,
    ):
        super().__init__()
        self.archive = archive
        self.permission = permission
        self.person = person
        self.component = component
        self.sourcepackagename = sourcepackagename
        self.packageset = packageset
        self.explicit = explicit
        self.distroseries = distroseries
        self.pocket = pocket
        self.__storm_loaded__()

    def __storm_loaded__(self):
        # Provide the additional marker interface depending on what type
        # of archive this is.  See also the lp:url declarations in
        # zcml/archivepermission.zcml.
        if self.permission == ArchivePermissionType.UPLOAD:
            alsoProvides(self, IArchiveUploader)
        elif self.permission == ArchivePermissionType.QUEUE_ADMIN:
            alsoProvides(self, IArchiveQueueAdmin)
        else:
            raise AssertionError(
                "Unknown permission type %s" % self.permission
            )

    @property
    def private(self):
        """See `IPrivacy`."""
        return self.archive.private

    @property
    def component_name(self):
        """See `IArchivePermission`"""
        if self.component:
            return self.component.name
        else:
            return None

    @property
    def source_package_name(self):
        """See `IArchivePermission`"""
        if self.sourcepackagename:
            return self.sourcepackagename.name
        else:
            return None

    @property
    def package_set_name(self):
        """See `IArchivePermission`"""
        if self.packageset:
            return self.packageset.name
        else:
            return None

    @property
    def distro_series_name(self):
        """See `IArchivePermission`"""
        if self.packageset:
            return self.packageset.distroseries.name
        elif self.distroseries:
            return self.distroseries.name
        else:
            return None


@implementer(IArchivePermissionSet)
class ArchivePermissionSet:
    """See `IArchivePermissionSet`."""

    def checkAuthenticated(
        self, person, archive, permission, item, distroseries=None
    ):
        """See `IArchivePermissionSet`."""
        clauses = [
            ArchivePermission.archive == archive,
            ArchivePermission.permission == permission,
            ArchivePermission.person == TeamParticipation.team_id,
            TeamParticipation.person == person,
        ]

        if IComponent.providedBy(item):
            clauses.append(ArchivePermission.component == item)
        elif ISourcePackageName.providedBy(item):
            clauses.append(ArchivePermission.sourcepackagename == item)
        elif IPackageset.providedBy(item):
            clauses.append(ArchivePermission.packageset == item)
        elif (
            zope_isinstance(item, DBItem)
            and item.enum.name == "PackagePublishingPocket"
        ):
            clauses.append(ArchivePermission.pocket == item)
            if distroseries is not None:
                clauses.append(
                    Or(
                        ArchivePermission.distroseries == None,
                        ArchivePermission.distroseries == distroseries,
                    )
                )
        else:
            raise AssertionError(
                "'item' %r is not an IComponent, IPackageset, "
                "ISourcePackageName or PackagePublishingPocket" % item
            )

        return IStore(ArchivePermission).find(ArchivePermission, *clauses)

    def _nameToComponent(self, component):
        """Helper to convert a possible string component to IComponent"""
        try:
            if isinstance(component, str):
                component = getUtility(IComponentSet)[component]
            return component
        except NotFoundError:
            raise ComponentNotFound(component)

    def _nameToSourcePackageName(self, sourcepackagename):
        """Helper to convert a possible string name to ISourcePackageName."""
        if isinstance(sourcepackagename, str):
            sourcepackagename = getUtility(ISourcePackageNameSet)[
                sourcepackagename
            ]
        return sourcepackagename

    def _precachePersonsForPermissions(self, permissions):
        list(
            getUtility(IPersonSet).getPrecachedPersonsFromIDs(
                set(map(attrgetter("person_id"), permissions))
            )
        )
        return permissions

    def permissionsForArchive(self, archive):
        """See `IArchivePermissionSet`."""
        return self._precachePersonsForPermissions(
            IStore(ArchivePermission).find(ArchivePermission, archive=archive)
        )

    def permissionsForPerson(self, archive, person):
        """See `IArchivePermissionSet`."""
        return IStore(ArchivePermission).find(
            ArchivePermission,
            ArchivePermission.archive == archive,
            Exists(
                Select(
                    1,
                    tables=[TeamParticipation],
                    where=And(
                        TeamParticipation.person == person,
                        TeamParticipation.team == ArchivePermission.person_id,
                    ),
                )
            ),
        )

    def _componentsFor(self, archives, person, permission_type):
        """Helper function to get ArchivePermission objects."""
        if IArchive.providedBy(archives):
            archive_ids = [archives.id]
        else:
            archive_ids = [archive.id for archive in archives]

        return IStore(ArchivePermission).find(
            ArchivePermission,
            ArchivePermission.archive_id.is_in(archive_ids),
            ArchivePermission.permission == permission_type,
            ArchivePermission.component != None,
            Exists(
                Select(
                    1,
                    tables=[TeamParticipation],
                    where=And(
                        TeamParticipation.person == person,
                        TeamParticipation.team == ArchivePermission.person_id,
                    ),
                )
            ),
        )

    def componentsForUploader(self, archive, person):
        """See `IArchivePermissionSet`."""
        return self._componentsFor(
            archive, person, ArchivePermissionType.UPLOAD
        )

    def uploadersForComponent(self, archive, component=None):
        """See `IArchivePermissionSet`."""
        clauses = [
            ArchivePermission.archive == archive,
            ArchivePermission.permission == ArchivePermissionType.UPLOAD,
        ]

        if component is not None:
            component = self._nameToComponent(component)
            clauses.append(ArchivePermission.component == component)
        else:
            clauses.append(ArchivePermission.component != None)

        return IStore(ArchivePermission).find(ArchivePermission, *clauses)

    def packagesForUploader(self, archive, person):
        """See `IArchive`."""
        return IStore(ArchivePermission).find(
            ArchivePermission,
            ArchivePermission.archive == archive,
            ArchivePermission.permission == ArchivePermissionType.UPLOAD,
            ArchivePermission.sourcepackagename != None,
            Exists(
                Select(
                    1,
                    tables=[TeamParticipation],
                    where=And(
                        TeamParticipation.person == person,
                        TeamParticipation.team == ArchivePermission.person_id,
                    ),
                )
            ),
        )

    def uploadersForPackage(self, archive, sourcepackagename):
        """See `IArchivePermissionSet`."""
        sourcepackagename = self._nameToSourcePackageName(sourcepackagename)
        return IStore(ArchivePermission).find(
            ArchivePermission,
            archive=archive,
            permission=ArchivePermissionType.UPLOAD,
            sourcepackagename=sourcepackagename,
        )

    def _pocketsFor(self, archives, person, permission_type):
        """Helper function to get ArchivePermission objects."""
        if IArchive.providedBy(archives):
            archive_ids = [archives.id]
        else:
            archive_ids = [archive.id for archive in archives]

        return IStore(ArchivePermission).find(
            ArchivePermission,
            ArchivePermission.archive_id.is_in(archive_ids),
            ArchivePermission.permission == permission_type,
            ArchivePermission.pocket != None,
            Exists(
                Select(
                    1,
                    tables=[TeamParticipation],
                    where=And(
                        TeamParticipation.person == person,
                        TeamParticipation.team == ArchivePermission.person_id,
                    ),
                )
            ),
        )

    def pocketsForUploader(self, archive, person):
        """See `IArchivePermissionSet`."""
        return self._pocketsFor(archive, person, ArchivePermissionType.UPLOAD)

    def uploadersForPocket(self, archive, pocket):
        """See `IArchivePermissionSet`."""
        return IStore(ArchivePermission).find(
            ArchivePermission,
            archive=archive,
            permission=ArchivePermissionType.UPLOAD,
            pocket=pocket,
        )

    def queueAdminsForComponent(self, archive, component):
        """See `IArchivePermissionSet`."""
        component = self._nameToComponent(component)
        return IStore(ArchivePermission).find(
            ArchivePermission,
            archive=archive,
            permission=ArchivePermissionType.QUEUE_ADMIN,
            component=component,
        )

    def componentsForQueueAdmin(self, archive, person):
        """See `IArchivePermissionSet`."""
        return self._componentsFor(
            archive, person, ArchivePermissionType.QUEUE_ADMIN
        )

    def queueAdminsForPocket(self, archive, pocket, distroseries=None):
        """See `IArchivePermissionSet`."""
        kwargs = {}
        if distroseries is not None:
            kwargs["distroseries"] = distroseries
        return IStore(ArchivePermission).find(
            ArchivePermission,
            archive=archive,
            permission=ArchivePermissionType.QUEUE_ADMIN,
            pocket=pocket,
            **kwargs,
        )

    def pocketsForQueueAdmin(self, archive, person):
        """See `IArchivePermissionSet`."""
        return self._pocketsFor(
            archive, person, ArchivePermissionType.QUEUE_ADMIN
        )

    def newPackageUploader(self, archive, person, sourcepackagename):
        """See `IArchivePermissionSet`."""
        sourcepackagename = self._nameToSourcePackageName(sourcepackagename)
        existing = self.checkAuthenticated(
            person, archive, ArchivePermissionType.UPLOAD, sourcepackagename
        )
        try:
            return existing[0]
        except IndexError:
            return ArchivePermission(
                archive=archive,
                person=person,
                sourcepackagename=sourcepackagename,
                permission=ArchivePermissionType.UPLOAD,
            )

    def newComponentUploader(self, archive, person, component):
        """See `IArchivePermissionSet`."""
        component = self._nameToComponent(component)
        existing = self.checkAuthenticated(
            person, archive, ArchivePermissionType.UPLOAD, component
        )
        try:
            return existing[0]
        except IndexError:
            return ArchivePermission(
                archive=archive,
                person=person,
                component=component,
                permission=ArchivePermissionType.UPLOAD,
            )

    def newPocketUploader(self, archive, person, pocket):
        """See `IArchivePermissionSet`."""
        existing = self.checkAuthenticated(
            person, archive, ArchivePermissionType.UPLOAD, pocket
        )
        try:
            return existing[0]
        except IndexError:
            return ArchivePermission(
                archive=archive,
                person=person,
                pocket=pocket,
                permission=ArchivePermissionType.UPLOAD,
            )

    def newQueueAdmin(self, archive, person, component):
        """See `IArchivePermissionSet`."""
        component = self._nameToComponent(component)
        existing = self.checkAuthenticated(
            person, archive, ArchivePermissionType.QUEUE_ADMIN, component
        )
        try:
            return existing[0]
        except IndexError:
            return ArchivePermission(
                archive=archive,
                person=person,
                component=component,
                permission=ArchivePermissionType.QUEUE_ADMIN,
            )

    def newPocketQueueAdmin(self, archive, person, pocket, distroseries=None):
        """See `IArchivePermissionSet`."""
        existing = self.checkAuthenticated(
            person,
            archive,
            ArchivePermissionType.QUEUE_ADMIN,
            pocket,
            distroseries=distroseries,
        )
        try:
            return existing[0]
        except IndexError:
            return ArchivePermission(
                archive=archive,
                person=person,
                pocket=pocket,
                distroseries=distroseries,
                permission=ArchivePermissionType.QUEUE_ADMIN,
            )

    @staticmethod
    def _remove_permission(permission):
        if permission is None:
            # The permission has already been removed, so there's nothing more
            # to do here.
            return
        else:
            Store.of(permission).remove(permission)

    def deletePackageUploader(self, archive, person, sourcepackagename):
        """See `IArchivePermissionSet`."""
        sourcepackagename = self._nameToSourcePackageName(sourcepackagename)
        permission = (
            IStore(ArchivePermission)
            .find(
                ArchivePermission,
                archive=archive,
                person=person,
                sourcepackagename=sourcepackagename,
                permission=ArchivePermissionType.UPLOAD,
            )
            .one()
        )
        self._remove_permission(permission)

    def deleteComponentUploader(self, archive, person, component):
        """See `IArchivePermissionSet`."""
        component = self._nameToComponent(component)
        permission = (
            IStore(ArchivePermission)
            .find(
                ArchivePermission,
                archive=archive,
                person=person,
                component=component,
                permission=ArchivePermissionType.UPLOAD,
            )
            .one()
        )
        self._remove_permission(permission)

    def deletePocketUploader(self, archive, person, pocket):
        permission = (
            IStore(ArchivePermission)
            .find(
                ArchivePermission,
                archive=archive,
                person=person,
                pocket=pocket,
                permission=ArchivePermissionType.UPLOAD,
            )
            .one()
        )
        self._remove_permission(permission)

    def deleteQueueAdmin(self, archive, person, component):
        """See `IArchivePermissionSet`."""
        component = self._nameToComponent(component)
        permission = (
            IStore(ArchivePermission)
            .find(
                ArchivePermission,
                archive=archive,
                person=person,
                component=component,
                permission=ArchivePermissionType.QUEUE_ADMIN,
            )
            .one()
        )
        self._remove_permission(permission)

    def deletePocketQueueAdmin(
        self, archive, person, pocket, distroseries=None
    ):
        """See `IArchivePermissionSet`."""
        kwargs = {}
        if distroseries is not None:
            kwargs["distroseries"] = distroseries
        permission = (
            IStore(ArchivePermission)
            .find(
                ArchivePermission,
                archive=archive,
                person=person,
                pocket=pocket,
                permission=ArchivePermissionType.QUEUE_ADMIN,
                **kwargs,
            )
            .one()
        )
        self._remove_permission(permission)

    def packagesetsForUploader(self, archive, person):
        """See `IArchivePermissionSet`."""
        store = IStore(ArchivePermission)
        query = """
            SELECT ap.id
            FROM archivepermission ap, teamparticipation tp
            WHERE
                ap.person = tp.team AND tp.person = ?
                AND ap.archive = ?
                AND ap.packageset IS NOT NULL
        """
        query = SQL(query, (person.id, archive.id))
        return store.find(ArchivePermission, ArchivePermission.id.is_in(query))

    def uploadersForPackageset(
        self, archive, packageset, direct_permissions=True
    ):
        """See `IArchivePermissionSet`."""
        store = IStore(ArchivePermission)
        if direct_permissions == True:
            query = """
                SELECT ap.id FROM archivepermission ap WHERE ap.packageset = ?
            """
        else:
            query = """
                SELECT ap.id
                FROM archivepermission ap, flatpackagesetinclusion fpsi
                WHERE fpsi.child = ? AND ap.packageset = fpsi.parent
            """
        query += " AND ap.archive = ?"
        query = SQL(query, (packageset.id, archive.id))
        return store.find(ArchivePermission, ArchivePermission.id.is_in(query))

    def newPackagesetUploader(
        self, archive, person, packageset, explicit=False
    ):
        """See `IArchivePermissionSet`."""
        store = IPrimaryStore(ArchivePermission)

        # First see whether we have a matching permission in the database
        # already.
        query = """
            SELECT ap.id
            FROM archivepermission ap, teamparticipation tp
            WHERE
                ap.person = tp.team AND tp.person = ?
                AND ap.packageset = ? AND ap.archive = ?
        """
        query = SQL(query, (person.id, packageset.id, archive.id))
        permissions = list(
            store.find(ArchivePermission, ArchivePermission.id.is_in(query))
        )
        if len(permissions) > 0:
            # Found permissions in the database, does the 'explicit' flag
            # have the requested value?
            conflicting = [
                permission
                for permission in permissions
                if permission.explicit != explicit
            ]
            if len(conflicting) > 0:
                # At least one permission with conflicting 'explicit' flag
                # value exists already.
                cperm = conflicting[0]
                raise ValueError(
                    "Permission for package set '%s' already exists for %s "
                    "but with a different 'explicit' flag value (%s)."
                    % (packageset.name, cperm.person.name, cperm.explicit)
                )
            else:
                # No conflicts, does the requested permission exist already?
                existing = [
                    permission
                    for permission in permissions
                    if (
                        permission.explicit == explicit
                        and permission.person == person
                        and permission.packageset == packageset
                    )
                ]
                assert (
                    len(existing) <= 1
                ), "Too many permissions for %s and %s" % (
                    person.name,
                    packageset.name,
                )
                if len(existing) == 1:
                    # The existing permission matches, just return it.
                    return existing[0]

        # The requested permission does not exist yet. Insert it into the
        # database.
        permission = ArchivePermission(
            archive=archive,
            person=person,
            packageset=packageset,
            permission=ArchivePermissionType.UPLOAD,
            explicit=explicit,
        )
        store.add(permission)

        return permission

    def deletePackagesetUploader(
        self, archive, person, packageset, explicit=False
    ):
        """See `IArchivePermissionSet`."""
        store = IPrimaryStore(ArchivePermission)

        # Do we have the permission the user wants removed in the database?
        permission = store.find(
            ArchivePermission,
            archive=archive,
            person=person,
            packageset=packageset,
            permission=ArchivePermissionType.UPLOAD,
            explicit=explicit,
        ).one()
        self._remove_permission(permission)

    def packagesetsForSourceUploader(self, archive, sourcepackagename, person):
        """See `IArchivePermissionSet`."""
        sourcepackagename = self._nameToSourcePackageName(sourcepackagename)
        store = IStore(ArchivePermission)
        query = """
            SELECT ap.id
            FROM
                archivepermission ap, teamparticipation tp,
                packagesetsources pss, flatpackagesetinclusion fpsi
            WHERE
                ap.person = tp.team AND tp.person = ?
                AND ap.packageset = fpsi.parent
                AND pss.packageset = fpsi.child
                AND pss.sourcepackagename = ?
                AND ap.archive = ?
        """
        query = SQL(query, (person.id, sourcepackagename.id, archive.id))
        return store.find(ArchivePermission, ArchivePermission.id.is_in(query))

    def packagesetsForSource(
        self, archive, sourcepackagename, direct_permissions=True
    ):
        """See `IArchivePermissionSet`."""
        sourcepackagename = self._nameToSourcePackageName(sourcepackagename)
        store = IStore(ArchivePermission)

        if direct_permissions:
            origin = SQL("ArchivePermission, PackagesetSources")
            rset = store.using(origin).find(
                ArchivePermission,
                SQL(
                    """
                ArchivePermission.packageset = PackagesetSources.packageset
                AND PackagesetSources.sourcepackagename = ?
                AND ArchivePermission.archive = ?
                """,
                    (sourcepackagename.id, archive.id),
                ),
            )
        else:
            origin = SQL(
                "ArchivePermission, PackagesetSources, "
                "FlatPackagesetInclusion"
            )
            rset = store.using(origin).find(
                ArchivePermission,
                SQL(
                    """
                ArchivePermission.packageset = FlatPackagesetInclusion.parent
                AND PackagesetSources.packageset =
                    FlatPackagesetInclusion.child
                AND PackagesetSources.sourcepackagename = ?
                AND ArchivePermission.archive = ?
                """,
                    (sourcepackagename.id, archive.id),
                ),
            )
        return rset

    def isSourceUploadAllowed(
        self, archive, sourcepackagename, person, distroseries
    ):
        """See `IArchivePermissionSet`."""
        sourcepackagename = self._nameToSourcePackageName(sourcepackagename)

        # Put together the parameters for the query that follows.
        archive_params = (ArchivePermissionType.UPLOAD, archive.id)
        permission_params = (sourcepackagename.id, person.id, distroseries.id)
        query_params = (
            # Query parameters for the first WHERE clause.
            (archive.id, distroseries.id, sourcepackagename.id)
            +
            # Query parameters for the second WHERE clause.
            permission_params
            + archive_params
            +
            # Query parameters for the third WHERE clause.
            permission_params
            + archive_params
        )

        query = """
        SELECT CASE
          WHEN (
            SELECT COUNT(ap.id)
            FROM packagesetsources pss, archivepermission ap, packageset ps
            WHERE
              ap.archive = %s AND ap.explicit = TRUE
              AND ap.packageset = ps.id AND ps.distroseries = %s
              AND pss.sourcepackagename = %s
              AND pss.packageset = ap.packageset) > 0
          THEN (
            SELECT COUNT(ap.id)
            FROM
              packagesetsources pss, archivepermission ap, packageset ps,
              teamparticipation tp
            WHERE
              pss.sourcepackagename = %s
              AND ap.person = tp.team AND tp.person = %s
              AND ap.packageset = ps.id AND ps.distroseries = %s
              AND pss.packageset = ap.packageset AND ap.explicit = TRUE
              AND ap.permission = %s AND ap.archive = %s)
          ELSE (
            SELECT COUNT(ap.id)
            FROM
              packagesetsources pss, archivepermission ap, packageset ps,
              teamparticipation tp, flatpackagesetinclusion fpsi
            WHERE
              pss.sourcepackagename = %s
              AND ap.person = tp.team AND tp.person = %s
              AND ap.packageset = ps.id AND ps.distroseries = %s
              AND pss.packageset = fpsi.child AND fpsi.parent = ap.packageset
              AND ap.permission = %s AND ap.archive = %s)
        END AS number_of_permitted_package_sets;

        """ % sqlvalues(
            *query_params
        )
        return IStore(ArchivePermission).execute(query).get_one()[0] > 0
