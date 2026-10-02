# Copyright 2010-2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""All the interfaces that are exposed through the webservice.

There is a declaration in ZCML somewhere that looks like:
  <webservice:register module="lp.bugs.interfaces.webservice" />

which tells `lazr.restful` that it should look for webservice exports here.
"""

__all__ = [
    "BugNominationStatusError",
    "IBug",
    "IBugActivity",
    "IBugAttachment",
    "IBugBranch",
    "IBugLinkTarget",
    "IBugNomination",
    "IBugPresence",
    "IBugSubscription",
    "IBugSubscriptionFilter",
    "IBugTarget",
    "IBugTask",
    "IBugTracker",
    "IBugTrackerComponent",
    "IBugTrackerComponentGroup",
    "IBugTrackerSet",
    "IBugWatch",
    "ICve",
    "ICveReference",
    "ICveSet",
    "IHasBugs",
    "IMaloneApplication",
    "IStructuralSubscription",
    "IStructuralSubscriptionTarget",
    "IllegalRelatedBugTasksParams",
    "IllegalTarget",
    "IVulnerability",
    "IVulnerabilitySet",
    "NominationError",
    "NominationSeriesObsoleteError",
    "UserCannotEditBugTaskAssignee",
    "UserCannotEditBugTaskImportance",
    "UserCannotEditBugTaskMilestone",
    "UserCannotEditBugTaskStatus",
]

from lazr.restful.fields import Reference

from lp.bugs.interfaces.bug import IBug, IFrontPageBugAddForm
from lp.bugs.interfaces.bugactivity import IBugActivity
from lp.bugs.interfaces.bugattachment import IBugAttachment
from lp.bugs.interfaces.bugbranch import IBugBranch
from lp.bugs.interfaces.buglink import IBugLinkTarget
from lp.bugs.interfaces.bugnomination import (
    BugNominationStatusError,
    IBugNomination,
    NominationError,
    NominationSeriesObsoleteError,
)
from lp.bugs.interfaces.bugpresence import IBugPresence
from lp.bugs.interfaces.bugsubscription import IBugSubscription
from lp.bugs.interfaces.bugsubscriptionfilter import IBugSubscriptionFilter
from lp.bugs.interfaces.bugtarget import IBugTarget, IHasBugs
from lp.bugs.interfaces.bugtask import (
    IBugTask,
    IllegalTarget,
    UserCannotEditBugTaskAssignee,
    UserCannotEditBugTaskImportance,
    UserCannotEditBugTaskMilestone,
    UserCannotEditBugTaskStatus,
)
from lp.bugs.interfaces.bugtasksearch import IllegalRelatedBugTasksParams
from lp.bugs.interfaces.bugtracker import (
    IBugTracker,
    IBugTrackerComponent,
    IBugTrackerComponentGroup,
    IBugTrackerSet,
)
from lp.bugs.interfaces.bugwatch import IBugWatch
from lp.bugs.interfaces.cve import ICve, ICveSet
from lp.bugs.interfaces.cvereference import ICveReference
from lp.bugs.interfaces.malone import IMaloneApplication
from lp.bugs.interfaces.structuralsubscription import (
    IStructuralSubscription,
    IStructuralSubscriptionTarget,
)
from lp.bugs.interfaces.vulnerability import IVulnerability, IVulnerabilitySet
from lp.code.interfaces.branchmergeproposal import IBranchMergeProposal
from lp.code.interfaces.gitrepository import IGitRepository
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.registry.interfaces.person import IPerson
from lp.registry.interfaces.product import IProduct
from lp.services.webservice.apihelpers import (
    patch_collection_property,
    patch_collection_return_type,
    patch_entry_return_type,
    patch_list_parameter_type,
    patch_plain_parameter_type,
    patch_reference_property,
)

# IBug
patch_plain_parameter_type(IBug, "addNomination", "target", IBugTarget)
patch_entry_return_type(IBug, "addNomination", IBugNomination)
patch_plain_parameter_type(IBug, "canBeNominatedFor", "target", IBugTarget)
patch_plain_parameter_type(IBug, "getNominationFor", "target", IBugTarget)
patch_entry_return_type(IBug, "getNominationFor", IBugNomination)
patch_plain_parameter_type(IBug, "getNominations", "target", IBugTarget)
patch_list_parameter_type(
    IBug, "getNominations", "nominations", Reference(schema=IBugNomination)
)
patch_collection_return_type(IBug, "getNominations", IBugNomination)
patch_collection_property(IBug, "linked_merge_proposals", IBranchMergeProposal)
patch_plain_parameter_type(
    IBug, "linkMergeProposal", "merge_proposal", IBranchMergeProposal
)
patch_plain_parameter_type(
    IBug, "unlinkMergeProposal", "merge_proposal", IBranchMergeProposal
)

# IBugPresence
patch_reference_property(IBugPresence, "product", IProduct)
patch_reference_property(IBugPresence, "distribution", IDistribution)
patch_reference_property(IBugPresence, "git_repository", IGitRepository)

# IBugTask
patch_reference_property(IBugTask, "owner", IPerson)
patch_collection_return_type(IBugTask, "findSimilarBugs", IBug)

# IBugTracker
patch_reference_property(IBugTracker, "owner", IPerson)

# IBugTrackerComponent
patch_reference_property(
    IBugTrackerComponent, "distro_source_package", IDistributionSourcePackage
)

# IBugWatch
patch_reference_property(IBugWatch, "owner", IPerson)

# IFrontPageBugAddForm
patch_reference_property(IFrontPageBugAddForm, "bugtarget", IBugTarget)

# IHasBugs
patch_plain_parameter_type(IHasBugs, "searchTasks", "assignee", IPerson)
patch_plain_parameter_type(IHasBugs, "searchTasks", "bug_reporter", IPerson)
patch_plain_parameter_type(IHasBugs, "searchTasks", "bug_supervisor", IPerson)
patch_plain_parameter_type(IHasBugs, "searchTasks", "bug_commenter", IPerson)
patch_plain_parameter_type(IHasBugs, "searchTasks", "bug_subscriber", IPerson)
patch_plain_parameter_type(IHasBugs, "searchTasks", "owner", IPerson)
patch_plain_parameter_type(IHasBugs, "searchTasks", "affected_user", IPerson)
patch_plain_parameter_type(
    IHasBugs, "searchTasks", "structural_subscriber", IPerson
)

patch_plain_parameter_type(IHasBugs, "searchTasksCounts", "assignee", IPerson)
patch_plain_parameter_type(
    IHasBugs, "searchTasksCounts", "bug_reporter", IPerson
)
patch_plain_parameter_type(
    IHasBugs, "searchTasksCounts", "bug_supervisor", IPerson
)
patch_plain_parameter_type(
    IHasBugs, "searchTasksCounts", "bug_commenter", IPerson
)
patch_plain_parameter_type(
    IHasBugs, "searchTasksCounts", "bug_subscriber", IPerson
)
patch_plain_parameter_type(IHasBugs, "searchTasksCounts", "owner", IPerson)
patch_plain_parameter_type(
    IHasBugs, "searchTasksCounts", "affected_user", IPerson
)
patch_plain_parameter_type(
    IHasBugs, "searchTasksCounts", "structural_subscriber", IPerson
)

# IStructuralSubscription
patch_collection_property(
    IStructuralSubscription, "bug_filters", IBugSubscriptionFilter
)
patch_entry_return_type(
    IStructuralSubscription, "newBugFilter", IBugSubscriptionFilter
)
patch_reference_property(
    IStructuralSubscription, "target", IStructuralSubscriptionTarget
)

# IStructuralSubscriptionTarget
patch_entry_return_type(
    IStructuralSubscriptionTarget,
    "addBugSubscriptionFilter",
    IBugSubscriptionFilter,
)

# ICve
patch_list_parameter_type(
    ICveSet,
    "advancedSearch",
    "in_distribution",
    Reference(schema=IDistribution),
)
patch_list_parameter_type(
    ICveSet,
    "advancedSearch",
    "not_in_distribution",
    Reference(schema=IDistribution),
)
