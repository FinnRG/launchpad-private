# Copyright 2010-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""All the interfaces that are exposed through the webservice.

There is a declaration in ZCML somewhere that looks like:
  <webservice:register module="lp.soyuz.interfaces.webservice" />

which tells `lazr.restful` that it should look for webservice exports here.
"""

__all__ = [
    "AlreadySubscribed",
    "ArchiveDisabled",
    "ArchiveNotPrivate",
    "CannotCopy",
    "CannotSwitchPrivacy",
    "CannotUploadToArchive",
    "CannotUploadToPPA",
    "CannotUploadToPocket",
    "ComponentNotFound",
    "DuplicatePackagesetName",
    "IArchive",
    "IArchiveDependency",
    "IArchivePermission",
    "IArchiveSet",
    "IArchiveSubscriber",
    "IBinaryPackageBuild",
    "IBinaryPackagePublishingHistory",
    "IBinaryPackageReleaseDownloadCount",
    "IDistroArchSeries",
    "IDistroArchSeriesFilter",
    "ILiveFS",
    "ILiveFSBuild",
    "ILiveFSSet",
    "IPackageUpload",
    "IPackageUploadLog",
    "IPackageset",
    "IPackagesetSet",
    "ISourcePackagePublishingHistory",
    "InsufficientUploadRights",
    "InvalidComponent",
    "InvalidPocketForPPA",
    "InvalidPocketForPartnerArchive",
    "NoRightsForArchive",
    "NoRightsForComponent",
    "NoSuchPPA",
    "NoSuchPackageSet",
    "NoTokensForTeams",
    "PocketNotFound",
    "VersionRequiresName",
]

from lp.code.interfaces.cibuild import ICIBuild
from lp.code.interfaces.sourcepackagerecipebuild import (
    ISourcePackageRecipeBuild,
)
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.services.webservice.apihelpers import (
    patch_collection_property,
    patch_collection_return_type,
    patch_entry_return_type,
    patch_plain_parameter_type,
    patch_reference_property,
)
from lp.snappy.interfaces.snapbase import ISnapBase
from lp.soyuz.interfaces.archive import (
    AlreadySubscribed,
    ArchiveDisabled,
    ArchiveNotPrivate,
    CannotCopy,
    CannotSwitchPrivacy,
    CannotUploadToArchive,
    CannotUploadToPocket,
    CannotUploadToPPA,
    ComponentNotFound,
    IArchive,
    IArchiveSet,
    InsufficientUploadRights,
    InvalidComponent,
    InvalidPocketForPartnerArchive,
    InvalidPocketForPPA,
    NoRightsForArchive,
    NoRightsForComponent,
    NoSuchPPA,
    NoTokensForTeams,
    PocketNotFound,
    VersionRequiresName,
)
from lp.soyuz.interfaces.archivedependency import IArchiveDependency
from lp.soyuz.interfaces.archivepermission import IArchivePermission
from lp.soyuz.interfaces.archivesubscriber import IArchiveSubscriber
from lp.soyuz.interfaces.binarypackagebuild import IBinaryPackageBuild
from lp.soyuz.interfaces.binarypackagerelease import (
    IBinaryPackageReleaseDownloadCount,
)
from lp.soyuz.interfaces.buildrecords import IHasBuildRecords
from lp.soyuz.interfaces.distroarchseries import IDistroArchSeries
from lp.soyuz.interfaces.distroarchseriesfilter import IDistroArchSeriesFilter
from lp.soyuz.interfaces.livefs import ILiveFS, ILiveFSSet, ILiveFSView
from lp.soyuz.interfaces.livefsbuild import ILiveFSBuild
from lp.soyuz.interfaces.packageset import (
    DuplicatePackagesetName,
    IPackageset,
    IPackagesetSet,
    NoSuchPackageSet,
)
from lp.soyuz.interfaces.publishing import (
    IBinaryPackagePublishingHistory,
    IBinaryPackagePublishingHistoryEdit,
    ISourcePackagePublishingHistory,
    ISourcePackagePublishingHistoryEdit,
    ISourcePackagePublishingHistoryPublic,
)
from lp.soyuz.interfaces.queue import IPackageUpload, IPackageUploadLog
from lp.soyuz.interfaces.sourcepackagerelease import ISourcePackageRelease

# IArchive
patch_reference_property(IArchive, "distribution", IDistribution)
patch_collection_property(IArchive, "dependencies", IArchiveDependency)
patch_collection_return_type(IArchive, "getAllPermissions", IArchivePermission)
patch_collection_return_type(
    IArchive, "getPermissionsForPerson", IArchivePermission
)
patch_collection_return_type(
    IArchive, "getUploadersForPackage", IArchivePermission
)
patch_collection_return_type(
    IArchive, "getUploadersForPackageset", IArchivePermission
)
patch_collection_return_type(
    IArchive, "getPackagesetsForUploader", IArchivePermission
)
patch_collection_return_type(
    IArchive, "getPackagesetsForSourceUploader", IArchivePermission
)
patch_collection_return_type(
    IArchive, "getPackagesetsForSource", IArchivePermission
)
patch_collection_return_type(
    IArchive, "getUploadersForComponent", IArchivePermission
)
patch_collection_return_type(
    IArchive, "getQueueAdminsForComponent", IArchivePermission
)
patch_collection_return_type(
    IArchive, "getComponentsForQueueAdmin", IArchivePermission
)
patch_collection_return_type(
    IArchive, "getQueueAdminsForPocket", IArchivePermission
)
patch_collection_return_type(
    IArchive, "getPocketsForQueueAdmin", IArchivePermission
)
patch_collection_return_type(
    IArchive, "getPocketsForUploader", IArchivePermission
)
patch_collection_return_type(
    IArchive, "getUploadersForPocket", IArchivePermission
)
patch_entry_return_type(IArchive, "newPackageUploader", IArchivePermission)
patch_entry_return_type(IArchive, "newPackagesetUploader", IArchivePermission)
patch_entry_return_type(IArchive, "newComponentUploader", IArchivePermission)
patch_entry_return_type(IArchive, "newPocketUploader", IArchivePermission)
patch_entry_return_type(IArchive, "newQueueAdmin", IArchivePermission)
patch_entry_return_type(IArchive, "newPocketQueueAdmin", IArchivePermission)
patch_plain_parameter_type(IArchive, "syncSources", "from_archive", IArchive)
patch_plain_parameter_type(IArchive, "syncSource", "from_archive", IArchive)
patch_plain_parameter_type(IArchive, "copyPackage", "from_archive", IArchive)
patch_plain_parameter_type(IArchive, "copyPackages", "from_archive", IArchive)
patch_plain_parameter_type(IArchive, "uploadCIBuild", "ci_build", ICIBuild)
patch_entry_return_type(IArchive, "newSubscription", IArchiveSubscriber)
patch_plain_parameter_type(
    IArchive, "getArchiveDependency", "dependency", IArchive
)
patch_entry_return_type(IArchive, "getArchiveDependency", IArchiveDependency)
patch_collection_return_type(
    IArchive, "api_getPublishedSources", ISourcePackagePublishingHistory
)
patch_plain_parameter_type(
    IArchive, "api_getPublishedSources", "packageset", IPackageset
)
patch_plain_parameter_type(
    IArchive, "getAllPublishedBinaries", "distroarchseries", IDistroArchSeries
)
patch_collection_return_type(
    IArchive, "getAllPublishedBinaries", IBinaryPackagePublishingHistory
)
patch_plain_parameter_type(
    IArchive, "newPackagesetUploader", "packageset", IPackageset
)
patch_plain_parameter_type(
    IArchive, "getUploadersForPackageset", "packageset", IPackageset
)
patch_plain_parameter_type(
    IArchive, "deletePackagesetUploader", "packageset", IPackageset
)
patch_plain_parameter_type(
    IArchive, "removeArchiveDependency", "dependency", IArchive
)
patch_plain_parameter_type(
    IArchive, "_addArchiveDependency", "dependency", IArchive
)
patch_entry_return_type(IArchive, "_addArchiveDependency", IArchiveDependency)

# IArchiveDependency
patch_reference_property(IArchiveDependency, "snap_base", ISnapBase)

# IBinaryPackagePublishingHistory
patch_reference_property(
    IBinaryPackagePublishingHistory, "distroarchseries", IDistroArchSeries
)
patch_reference_property(
    IBinaryPackagePublishingHistory, "build", IBinaryPackageBuild
)
patch_reference_property(IBinaryPackagePublishingHistory, "archive", IArchive)
patch_reference_property(
    IBinaryPackagePublishingHistory, "copied_from_archive", IArchive
)
patch_entry_return_type(
    IBinaryPackagePublishingHistoryEdit,
    "changeOverride",
    IBinaryPackagePublishingHistory,
)

# IDistroArchSeries
patch_reference_property(IDistroArchSeries, "main_archive", IArchive)
patch_plain_parameter_type(
    IDistroArchSeries, "setChrootFromBuild", "livefsbuild", ILiveFSBuild
)
patch_entry_return_type(
    IDistroArchSeries, "getSourceFilter", IDistroArchSeriesFilter
)
patch_plain_parameter_type(
    IDistroArchSeries, "setSourceFilter", "packageset", IPackageset
)

# IHasBuildRecords
patch_collection_return_type(
    IHasBuildRecords, "getBuildRecords", IBinaryPackageBuild
)

# ILiveFSView
patch_entry_return_type(ILiveFSView, "requestBuild", ILiveFSBuild)
patch_collection_property(ILiveFSView, "builds", ILiveFSBuild)
patch_collection_property(ILiveFSView, "completed_builds", ILiveFSBuild)
patch_collection_property(ILiveFSView, "pending_builds", ILiveFSBuild)

# IPackageUpload
patch_reference_property(IPackageUpload, "distroseries", IDistroSeries)
patch_reference_property(IPackageUpload, "archive", IArchive)
patch_reference_property(IPackageUpload, "copy_source_archive", IArchive)

# ISourcePackagePublishingHistory
patch_collection_return_type(
    ISourcePackagePublishingHistoryPublic, "getBuilds", IBinaryPackageBuild
)
patch_collection_return_type(
    ISourcePackagePublishingHistoryPublic,
    "getPublishedBinaries",
    IBinaryPackagePublishingHistory,
)
patch_reference_property(ISourcePackagePublishingHistory, "archive", IArchive)
patch_reference_property(
    ISourcePackagePublishingHistory, "copied_from_archive", IArchive
)
patch_reference_property(
    ISourcePackagePublishingHistory,
    "ancestor",
    ISourcePackagePublishingHistory,
)
patch_reference_property(
    ISourcePackagePublishingHistory, "packageupload", IPackageUpload
)
patch_entry_return_type(
    ISourcePackagePublishingHistoryEdit,
    "changeOverride",
    ISourcePackagePublishingHistory,
)

# ISourcePackageRelease
patch_reference_property(
    ISourcePackageRelease,
    "source_package_recipe_build",
    ISourcePackageRecipeBuild,
)
patch_reference_property(ISourcePackageRelease, "ci_build", ICIBuild)
