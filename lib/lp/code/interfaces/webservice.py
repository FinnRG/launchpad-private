# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""All the interfaces that are exposed through the webservice.

There is a declaration in ZCML somewhere that looks like:
  <webservice:register module="lp.code.interfaces.webservice" />

which tells `lazr.restful` that it should look for webservice exports here.
"""

__all__ = [
    "BranchCreatorNotMemberOfOwnerTeam",
    "BranchCreatorNotOwner",
    "BranchExists",
    "BranchMergeProposalExists",
    "BuildAlreadyPending",
    "CodeImportAlreadyRunning",
    "CodeImportNotInReviewedState",
    "IBranch",
    "IBranchMergeProposal",
    "IBranchSet",
    "IBranchSubscription",
    "ICIBuild",
    "ICodeImport",
    "ICodeReviewComment",
    "ICodeReviewVoteReference",
    "IDiff",
    "IGitRef",
    "IGitRepository",
    "IGitRepositorySet",
    "IGitSubscription",
    "IHasGitRepositories",
    "IPreviewDiff",
    "IRevisionStatusReport",
    "ISourcePackageGitBuild",
    "ISourcePackageRecipe",
    "ISourcePackageRecipeBuild",
]

from lp.blueprints.interfaces.specification import ISpecification
from lp.blueprints.interfaces.specificationbranch import ISpecificationBranch
from lp.bugs.interfaces.bug import IBug
from lp.bugs.interfaces.bugbranch import IBugBranch
from lp.bugs.interfaces.bugtask import IBugTask

# The exceptions are imported so that they can produce the special
# status code defined by error_status when they are raised.
from lp.code.errors import (
    BranchCreatorNotMemberOfOwnerTeam,
    BranchCreatorNotOwner,
    BranchExists,
    BranchMergeProposalExists,
    BuildAlreadyPending,
    CodeImportAlreadyRunning,
    CodeImportNotInReviewedState,
)
from lp.code.interfaces.branch import IBranch, IBranchSet
from lp.code.interfaces.branchmergeproposal import IBranchMergeProposal
from lp.code.interfaces.branchsubscription import IBranchSubscription
from lp.code.interfaces.cibuild import ICIBuild
from lp.code.interfaces.codeimport import ICodeImport
from lp.code.interfaces.codereviewcomment import ICodeReviewComment
from lp.code.interfaces.codereviewvote import ICodeReviewVoteReference
from lp.code.interfaces.diff import IDiff, IPreviewDiff
from lp.code.interfaces.gitref import IGitRef
from lp.code.interfaces.gitrepository import IGitRepository, IGitRepositorySet
from lp.code.interfaces.gitrule import IGitNascentRule, IGitNascentRuleGrant
from lp.code.interfaces.gitsubscription import IGitSubscription
from lp.code.interfaces.hasbranches import (
    IHasBranches,
    IHasCodeImports,
    IHasMergeProposals,
    IHasRequestedReviews,
)
from lp.code.interfaces.hasgitrepositories import IHasGitRepositories
from lp.code.interfaces.hasrecipes import IHasRecipes
from lp.code.interfaces.revisionstatus import IRevisionStatusReport
from lp.code.interfaces.sourcepackagerecipe import ISourcePackageRecipe
from lp.code.interfaces.sourcepackagerecipebuild import (
    ISourcePackageRecipeBuild,
)
from lp.registry.interfaces.person import IPerson, IPersonViewRestricted
from lp.registry.interfaces.product import IProduct
from lp.registry.interfaces.sourcepackage import ISourcePackage
from lp.services.fields import InlineObject
from lp.services.webservice.apihelpers import (
    patch_collection_property,
    patch_collection_return_type,
    patch_entry_return_type,
    patch_list_parameter_type,
    patch_plain_parameter_type,
    patch_reference_property,
)
from lp.soyuz.interfaces.sourcepackagegitbuild import ISourcePackageGitBuild

# IBranch
patch_collection_property(IBranch, "bug_branches", IBugBranch)
patch_collection_property(IBranch, "linked_bugs", IBug)
patch_collection_property(IBranch, "dependent_branches", IBranchMergeProposal)
patch_entry_return_type(IBranch, "getSubscription", IBranchSubscription)
patch_collection_property(
    IBranch, "_api_landing_candidates", IBranchMergeProposal
)
patch_collection_property(
    IBranch, "_api_landing_targets", IBranchMergeProposal
)
patch_plain_parameter_type(IBranch, "linkBug", "bug", IBug)
patch_plain_parameter_type(
    IBranch, "linkSpecification", "spec", ISpecification
)
patch_reference_property(IBranch, "product", IProduct)

patch_plain_parameter_type(IBranch, "setTarget", "project", IProduct)
patch_plain_parameter_type(
    IBranch, "setTarget", "source_package", ISourcePackage
)
patch_reference_property(IBranch, "sourcepackage", ISourcePackage)
patch_reference_property(IBranch, "code_import", ICodeImport)
patch_entry_return_type(
    IGitRepository, "buildSourcePackageFromGitRef", ISourcePackageGitBuild
)

patch_collection_property(IBranch, "spec_links", ISpecificationBranch)
patch_entry_return_type(IBranch, "subscribe", IBranchSubscription)
patch_collection_property(IBranch, "subscriptions", IBranchSubscription)
patch_plain_parameter_type(IBranch, "unlinkBug", "bug", IBug)
patch_plain_parameter_type(
    IBranch, "unlinkSpecification", "spec", ISpecification
)

patch_entry_return_type(IBranch, "_createMergeProposal", IBranchMergeProposal)
patch_plain_parameter_type(
    IBranch, "_createMergeProposal", "merge_target", IBranch
)
patch_plain_parameter_type(
    IBranch, "_createMergeProposal", "merge_prerequisite", IBranch
)
patch_collection_return_type(
    IBranch, "getMergeProposals", IBranchMergeProposal
)

patch_collection_return_type(
    IBranchSet, "getMergeProposals", IBranchMergeProposal
)

# IBranchMergeProposal
patch_entry_return_type(IBranchMergeProposal, "getComment", ICodeReviewComment)
patch_plain_parameter_type(
    IBranchMergeProposal, "createComment", "parent", ICodeReviewComment
)
patch_entry_return_type(
    IBranchMergeProposal, "createComment", ICodeReviewComment
)
patch_collection_property(
    IBranchMergeProposal, "all_comments", ICodeReviewComment
)
patch_entry_return_type(
    IBranchMergeProposal, "nominateReviewer", ICodeReviewVoteReference
)
patch_collection_property(
    IBranchMergeProposal, "votes", ICodeReviewVoteReference
)
patch_collection_return_type(
    IBranchMergeProposal, "getRelatedBugTasks", IBugTask
)
patch_plain_parameter_type(IBranchMergeProposal, "linkBug", "bug", IBug)
patch_plain_parameter_type(IBranchMergeProposal, "unlinkBug", "bug", IBug)

# IGitRef
patch_reference_property(IGitRef, "repository", IGitRepository)
patch_plain_parameter_type(
    IGitRef, "createMergeProposal", "merge_target", IGitRef
)
patch_plain_parameter_type(
    IGitRef, "createMergeProposal", "merge_prerequisite", IGitRef
)
patch_collection_property(
    IGitRef, "_api_landing_targets", IBranchMergeProposal
)
patch_collection_property(
    IGitRef, "_api_landing_candidates", IBranchMergeProposal
)
patch_collection_property(IGitRef, "dependent_landings", IBranchMergeProposal)
patch_entry_return_type(IGitRef, "createMergeProposal", IBranchMergeProposal)
patch_collection_return_type(
    IGitRef, "getMergeProposals", IBranchMergeProposal
)
patch_list_parameter_type(
    IGitRef, "setGrants", "grants", InlineObject(schema=IGitNascentRuleGrant)
)

# IGitRepository
patch_collection_property(IGitRepository, "branches", IGitRef)
patch_collection_property(IGitRepository, "refs", IGitRef)
patch_collection_property(IGitRepository, "subscriptions", IGitSubscription)
patch_entry_return_type(IGitRepository, "subscribe", IGitSubscription)
patch_entry_return_type(IGitRepository, "getSubscription", IGitSubscription)
patch_reference_property(IGitRepository, "code_import", ICodeImport)
patch_entry_return_type(IGitRepository, "getRefByPath", IGitRef)
patch_collection_return_type(
    IGitRepository, "getStatusReports", IRevisionStatusReport
)
patch_collection_property(
    IGitRepository, "_api_landing_targets", IBranchMergeProposal
)
patch_collection_property(
    IGitRepository, "_api_landing_candidates", IBranchMergeProposal
)
patch_collection_property(
    IGitRepository, "dependent_landings", IBranchMergeProposal
)
patch_collection_return_type(
    IGitRepository, "getMergeProposals", IBranchMergeProposal
)
patch_list_parameter_type(
    IGitRepository, "setRules", "rules", InlineObject(schema=IGitNascentRule)
)
patch_entry_return_type(IGitRepository, "fork", IGitRepository)

# IHasBranches
patch_collection_return_type(IHasBranches, "getBranches", IBranch)

# IHasCodeImports
patch_entry_return_type(IHasCodeImports, "newCodeImport", ICodeImport)
patch_plain_parameter_type(IHasCodeImports, "newCodeImport", "owner", IPerson)

# IHasMergeProposals
patch_collection_return_type(
    IHasMergeProposals, "getMergeProposals", IBranchMergeProposal
)

# IHasRecipe
patch_collection_property(IHasRecipes, "recipes", ISourcePackageRecipe)

# IHasRequestedReviews
patch_collection_return_type(
    IHasRequestedReviews, "getRequestedReviews", IBranchMergeProposal
)

# IPreviewDiff
patch_reference_property(
    IPreviewDiff, "branch_merge_proposal", IBranchMergeProposal
)

# IRevisionStatusReport
patch_reference_property(
    IRevisionStatusReport, "git_repository", IGitRepository
)
patch_reference_property(IRevisionStatusReport, "ci_build", ICIBuild)

# ISourcePackageRecipe
patch_entry_return_type(
    ISourcePackageRecipe, "requestBuild", ISourcePackageRecipeBuild
)
patch_reference_property(
    ISourcePackageRecipe, "last_build", ISourcePackageRecipeBuild
)
patch_collection_property(
    ISourcePackageRecipe, "builds", ISourcePackageRecipeBuild
)
patch_collection_property(
    ISourcePackageRecipe, "pending_builds", ISourcePackageRecipeBuild
)
patch_collection_property(
    ISourcePackageRecipe, "completed_builds", ISourcePackageRecipeBuild
)

# IPersonViewRestricted
patch_collection_return_type(
    IPersonViewRestricted, "getMergeProposals", IBranchMergeProposal
)
