# Copyright 2010 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""All the interfaces that are exposed through the webservice."""

__all__ = [
    "DerivationError",
    "ICommercialSubscription",
    "IDistribution",
    "IDistributionMirror",
    "IDistributionSet",
    "IDistributionSourcePackage",
    "IDistroSeries",
    "IDistroSeriesDifference",
    "IDistroSeriesDifferenceComment",
    "IGPGKey",
    "IHasMilestones",
    "IIrcID",
    "IJabberID",
    "IMilestone",
    "IPerson",
    "IPersonSet",
    "IPillar",
    "IPillarNameSet",
    "IPoll",
    "IPollSet",
    "IProduct",
    "IProductRelease",
    "IProductReleaseFile",
    "IProductSeries",
    "IProductSet",
    "IProjectGroup",
    "IProjectGroupMilestone",
    "IProjectGroupSet",
    "IServiceFactory",
    "ISharingService",
    "ISSHKey",
    "ISocialAccount",
    "ISourcePackage",
    "ISourcePackageName",
    "ITeam",
    "ITeamMembership",
    "ITimelineProductSeries",
    "IWikiName",
]

from lazr.restful.fields import Reference

from lp.app.interfaces.services import IServiceFactory
from lp.bugs.interfaces.vulnerability import IVulnerability
from lp.code.interfaces.branch import IBranch
from lp.code.interfaces.sourcepackagerecipe import ISourcePackageRecipe
from lp.registry.interfaces.commercialsubscription import (
    ICommercialSubscription,
)
from lp.registry.interfaces.distribution import IDistribution, IDistributionSet
from lp.registry.interfaces.distributionmirror import IDistributionMirror
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.registry.interfaces.distroseries import DerivationError, IDistroSeries
from lp.registry.interfaces.distroseriesdifference import (
    IDistroSeriesDifference,
)
from lp.registry.interfaces.distroseriesdifferencecomment import (
    IDistroSeriesDifferenceComment,
)
from lp.registry.interfaces.gpg import IGPGKey
from lp.registry.interfaces.irc import IIrcID
from lp.registry.interfaces.jabber import IJabberID
from lp.registry.interfaces.milestone import (
    IHasMilestones,
    IMilestone,
    IProjectGroupMilestone,
)
from lp.registry.interfaces.ociproject import IOCIProject
from lp.registry.interfaces.person import (
    IPerson,
    IPersonEditRestricted,
    IPersonLimitedView,
    IPersonSet,
    IPersonViewRestricted,
    ITeam,
)
from lp.registry.interfaces.pillar import IPillar, IPillarNameSet
from lp.registry.interfaces.poll import IPoll, IPollSet
from lp.registry.interfaces.product import IProduct, IProductSet
from lp.registry.interfaces.productrelease import (
    IProductRelease,
    IProductReleaseFile,
)
from lp.registry.interfaces.productseries import (
    IProductSeries,
    ITimelineProductSeries,
)
from lp.registry.interfaces.projectgroup import IProjectGroup, IProjectGroupSet
from lp.registry.interfaces.sharingservice import ISharingService
from lp.registry.interfaces.socialaccount import ISocialAccount
from lp.registry.interfaces.sourcepackage import (
    ISourcePackage,
    ISourcePackageEdit,
    ISourcePackagePublic,
)
from lp.registry.interfaces.sourcepackagename import ISourcePackageName
from lp.registry.interfaces.ssh import ISSHKey
from lp.registry.interfaces.teammembership import ITeamMembership
from lp.registry.interfaces.wikiname import IWikiName
from lp.services.webservice.apihelpers import (
    patch_collection_property,
    patch_collection_return_type,
    patch_entry_return_type,
    patch_list_parameter_type,
    patch_plain_parameter_type,
    patch_reference_property,
)
from lp.soyuz.interfaces.archive import IArchive
from lp.soyuz.interfaces.archivesubscriber import IArchiveSubscriber
from lp.soyuz.interfaces.distroarchseries import IDistroArchSeries
from lp.soyuz.interfaces.queue import IPackageUpload
from lp.translations.interfaces.distroserieslanguage import (
    IDistroSeriesLanguageEntry,
)

# ICommercialSubscription
patch_reference_property(ICommercialSubscription, "product", IProduct)
patch_reference_property(
    ICommercialSubscription, "distribution", IDistribution
)

# IDistribution
patch_collection_property(IDistribution, "series", IDistroSeries)
patch_collection_property(IDistribution, "derivatives", IDistroSeries)
patch_reference_property(IDistribution, "currentseries", IDistroSeries)
patch_entry_return_type(IDistribution, "getArchive", IArchive)
patch_entry_return_type(IDistribution, "getSeries", IDistroSeries)
patch_collection_return_type(
    IDistribution, "getDevelopmentSeries", IDistroSeries
)
patch_entry_return_type(
    IDistribution, "getSourcePackage", IDistributionSourcePackage
)
patch_entry_return_type(IDistribution, "getOCIProject", IOCIProject)
patch_collection_return_type(
    IDistribution, "searchSourcePackages", IDistributionSourcePackage
)
patch_reference_property(IDistribution, "main_archive", IArchive)
patch_collection_property(IDistribution, "all_distro_archives", IArchive)
patch_entry_return_type(IDistribution, "newOCIProject", IOCIProject)
patch_collection_return_type(IDistribution, "searchOCIProjects", IOCIProject)
patch_collection_property(IDistribution, "vulnerabilities", IVulnerability)

# IDistributionMirror
patch_reference_property(IDistributionMirror, "distribution", IDistribution)

# IDistroSeries
patch_entry_return_type(
    IDistroSeries, "getDistroArchSeries", IDistroArchSeries
)
patch_reference_property(IDistroSeries, "main_archive", IArchive)
patch_collection_property(
    IDistroSeries, "enabled_architectures", IDistroArchSeries
)
patch_collection_property(
    IDistroSeries, "translation_coverage", IDistroSeriesLanguageEntry
)
patch_collection_return_type(
    IDistroSeries, "getTranslationCoverage", IDistroSeriesLanguageEntry
)
patch_reference_property(IDistroSeries, "distribution", IDistribution)
patch_plain_parameter_type(
    IDistroSeries, "getPackageUploads", "archive", IArchive
)
patch_collection_return_type(
    IDistroSeries, "getPackageUploads", IPackageUpload
)
patch_reference_property(
    IDistroSeries, "nominatedarchindep", IDistroArchSeries
)
patch_collection_return_type(
    IDistroSeries, "getDifferencesTo", IDistroSeriesDifference
)
patch_collection_return_type(
    IDistroSeries, "getDifferenceComments", IDistroSeriesDifferenceComment
)

# IDistroSeriesDifference
patch_reference_property(
    IDistroSeriesDifference, "latest_comment", IDistroSeriesDifferenceComment
)

# IPerson
patch_reference_property(IPersonViewRestricted, "archive", IArchive)
patch_collection_property(IPersonViewRestricted, "ppas", IArchive)
patch_plain_parameter_type(
    IPersonLimitedView, "getPPAByName", "distribution", IDistribution
)
patch_entry_return_type(IPersonLimitedView, "getPPAByName", IArchive)
patch_plain_parameter_type(
    IPersonEditRestricted, "createPPA", "distribution", IDistribution
)
patch_entry_return_type(IPersonEditRestricted, "createPPA", IArchive)
patch_entry_return_type(IPerson, "createRecipe", ISourcePackageRecipe)
patch_list_parameter_type(
    IPerson, "createRecipe", "distroseries", Reference(schema=IDistroSeries)
)
patch_plain_parameter_type(
    IPerson, "createRecipe", "daily_build_archive", IArchive
)
patch_plain_parameter_type(
    IPerson, "getArchiveSubscriptionURL", "archive", IArchive
)
patch_collection_return_type(
    IPerson, "getArchiveSubscriptions", IArchiveSubscriber
)
patch_entry_return_type(IPerson, "getRecipe", ISourcePackageRecipe)
patch_collection_return_type(IPerson, "getOwnedProjects", IProduct)
patch_collection_return_type(
    IPerson, "getBugSubscriberPackages", IDistributionSourcePackage
)

# IProductSeries
patch_reference_property(IProductSeries, "product", IProduct)

# ISourcePackage
patch_reference_property(ISourcePackagePublic, "distroseries", IDistroSeries)
patch_reference_property(ISourcePackagePublic, "productseries", IProductSeries)
patch_entry_return_type(ISourcePackagePublic, "getBranch", IBranch)
patch_plain_parameter_type(ISourcePackageEdit, "setBranch", "branch", IBranch)
patch_reference_property(ISourcePackage, "distribution", IDistribution)
