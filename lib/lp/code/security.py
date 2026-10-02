# Copyright 2010 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Security adapters for the code module."""

__all__ = [
    "AccessBranch",
    "BranchSubscriptionEdit",
    "BranchSubscriptionView",
    "GitSubscriptionEdit",
    "GitSubscriptionView",
]

from lp.app.security import AuthorizationBase, DelegatedAuthorization
from lp.code.interfaces.branch import IBranch, user_has_special_branch_access
from lp.code.interfaces.branchmergeproposal import IBranchMergeProposal
from lp.code.interfaces.branchsubscription import IBranchSubscription
from lp.code.interfaces.cibuild import ICIBuild
from lp.code.interfaces.codeimport import ICodeImport
from lp.code.interfaces.codeimportjob import (
    ICodeImportJobSet,
    ICodeImportJobWorkflow,
)
from lp.code.interfaces.codeimportmachine import ICodeImportMachine
from lp.code.interfaces.codereviewcomment import (
    ICodeReviewComment,
    ICodeReviewCommentDeletion,
)
from lp.code.interfaces.codereviewvote import ICodeReviewVoteReference
from lp.code.interfaces.diff import IPreviewDiff
from lp.code.interfaces.gitactivity import IGitActivity
from lp.code.interfaces.gitref import IGitRef
from lp.code.interfaces.gitrepository import (
    IGitRepository,
    user_has_special_git_repository_access,
)
from lp.code.interfaces.gitrule import IGitRule, IGitRuleGrant
from lp.code.interfaces.gitsubscription import IGitSubscription
from lp.code.interfaces.revisionstatus import (
    IRevisionStatusArtifact,
    IRevisionStatusReport,
)
from lp.code.interfaces.sourcepackagerecipe import ISourcePackageRecipe
from lp.code.interfaces.sourcepackagerecipebuild import (
    ISourcePackageRecipeBuild,
)
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.registry.interfaces.ociproject import IOCIProject
from lp.registry.interfaces.product import IProduct
from lp.security import (
    AdminByBuilddAdmin,
    AdminByCommercialTeamOrAdmins,
    OnlyBazaarExpertsAndAdmins,
    OnlyVcsImportsAndAdmins,
)
from lp.soyuz.interfaces.sourcepackagegitbuild import ISourcePackageGitBuild


class ViewRevisionStatusReport(DelegatedAuthorization):
    """Anyone who can see a Git repository can see its status reports."""

    permission = "launchpad.View"
    usedfor = IRevisionStatusReport

    def __init__(self, obj):
        super().__init__(obj, obj.git_repository, "launchpad.View")


class EditRevisionStatusReport(AuthorizationBase):
    """The owner of a Git repository can edit its status reports."""

    permission = "launchpad.Edit"
    usedfor = IRevisionStatusReport

    def checkAuthenticated(self, user):
        return user.isOwner(self.obj.git_repository)


class ViewRevisionStatusArtifact(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = IRevisionStatusArtifact

    def __init__(self, obj):
        super().__init__(obj, obj.report, "launchpad.View")


class EditRevisionStatusArtifact(DelegatedAuthorization):
    permission = "launchpad.Edit"
    usedfor = IRevisionStatusArtifact

    def __init__(self, obj):
        super().__init__(obj, obj.report, "launchpad.Edit")


class EditCodeImport(AuthorizationBase):
    """Control who can edit the object view of a CodeImport.

    Currently, we restrict the visibility of the new code import
    system to owners, members of ~vcs-imports and Launchpad admins.
    """

    permission = "launchpad.Edit"
    usedfor = ICodeImport

    def checkAuthenticated(self, user):
        return (
            user.inTeam(self.obj.owner) or user.in_admin or user.in_vcs_imports
        )


class ModerateCodeImport(OnlyVcsImportsAndAdmins):
    """Control who can moderate a CodeImport.

    Currently, we restrict the visibility of code import moderation
    system to members of ~vcs-imports and Launchpad admins.
    """

    permission = "launchpad.Moderate"
    usedfor = ICodeImport


class SeeCodeImportJobSet(OnlyVcsImportsAndAdmins):
    """Control who can see the CodeImportJobSet utility.

    Currently, we restrict the visibility of the new code import
    system to members of ~vcs-imports and Launchpad admins.
    """

    permission = "launchpad.View"
    usedfor = ICodeImportJobSet


class EditCodeImportJobWorkflow(OnlyVcsImportsAndAdmins):
    """Control who can use the CodeImportJobWorkflow utility.

    Currently, we restrict the visibility of the new code import
    system to members of ~vcs-imports and Launchpad admins.
    """

    permission = "launchpad.Edit"
    usedfor = ICodeImportJobWorkflow


class EditCodeImportMachine(OnlyBazaarExpertsAndAdmins):
    """Control who can edit the object view of a CodeImportMachine.

    Access is restricted to Launchpad admins.
    """

    permission = "launchpad.Edit"
    usedfor = ICodeImportMachine


class GitRepositoryExpensiveRequest(AuthorizationBase):
    """Restrict git repository repacks."""

    permission = "launchpad.ExpensiveRequest"
    usedfor = IGitRepository

    def checkAuthenticated(self, user):
        return user.in_registry_experts or user.in_admin


class AccessBranch(AuthorizationBase):
    """Controls visibility of branches.

    A person can see the branch if the branch is public, they are the owner
    of the branch, they are in the team that owns the branch, they have an
    access grant to the branch, or a launchpad administrator.
    """

    permission = "launchpad.View"
    usedfor = IBranch

    def checkAuthenticated(self, user):
        return self.obj.visibleByUser(user.person)

    def checkUnauthenticated(self):
        return self.obj.visibleByUser(None)


class EditBranch(AuthorizationBase):
    """The owner or admins can edit branches."""

    permission = "launchpad.Edit"
    usedfor = IBranch

    def checkAuthenticated(self, user):
        return (
            user.inTeam(self.obj.owner)
            or user_has_special_branch_access(user.person, self.obj)
            or can_upload_linked_package(user, self.obj)
        )


class ModerateBranch(EditBranch):
    """The owners, pillar owners, and admins can moderate branches."""

    permission = "launchpad.Moderate"

    def checkAuthenticated(self, user):
        if super().checkAuthenticated(user):
            return True
        branch = self.obj
        pillar = branch.product or branch.distribution
        if pillar is not None and user.inTeam(pillar.owner):
            return True
        return user.in_commercial_admin or user.in_registry_experts


def can_upload_linked_package(person_role, branch):
    """True if person may upload the package linked to `branch`."""
    # No associated `ISuiteSourcePackage` data -> not an official branch.
    # Abort.
    ssp_list = branch.associatedSuiteSourcePackages()
    if len(ssp_list) < 1:
        return False

    # XXX al-maisan, 2009-10-20: a branch may currently be associated with a
    # number of (distroseries, sourcepackagename, pocket) combinations.
    # This does not seem right. But until the database model is fixed we work
    # around this by assuming that things are fine as long as we find at least
    # one combination that allows us to upload the corresponding source
    # package.
    for ssp in ssp_list:
        archive = ssp.sourcepackage.get_default_archive()
        if archive.canUploadSuiteSourcePackage(person_role.person, ssp):
            return True
    return False


class AdminBranch(AuthorizationBase):
    """The admins can administer branches."""

    permission = "launchpad.Admin"
    usedfor = IBranch

    def checkAuthenticated(self, user):
        return user.in_admin


class ViewGitRepository(AuthorizationBase):
    """Controls visibility of Git repositories.

    A person can see the repository if the repository is public, they are
    the owner of the repository, they are in the team that owns the
    repository, they have an access grant to the repository, or they are a
    Launchpad administrator.
    """

    permission = "launchpad.View"
    usedfor = IGitRepository

    def checkAuthenticated(self, user):
        return self.obj.visibleByUser(user.person)

    def checkUnauthenticated(self):
        return self.obj.visibleByUser(None)


class EditGitRepository(AuthorizationBase):
    """The owner or admins can edit Git repositories."""

    permission = "launchpad.Edit"
    usedfor = IGitRepository

    def checkAuthenticated(self, user):
        # XXX cjwatson 2015-01-23: People who can upload source packages to
        # a distribution should be able to push to the corresponding
        # "official" repositories, once those are defined.
        return user.inTeam(
            self.obj.owner
        ) or user_has_special_git_repository_access(user.person, self.obj)


class ModerateGitRepository(EditGitRepository):
    """The owners, pillar owners, and admins can moderate Git repositories."""

    permission = "launchpad.Moderate"

    def checkAuthenticated(self, user):
        if super().checkAuthenticated(user):
            return True
        target = self.obj.target
        if IProduct.providedBy(target):
            pillar = target
        elif IDistributionSourcePackage.providedBy(target):
            pillar = target.distribution
        elif IOCIProject.providedBy(target):
            pillar = target.pillar
        else:
            raise AssertionError("Unknown target: %r" % target)
        if pillar is not None and user.inTeam(pillar.owner):
            return True
        return user.in_commercial_admin


class AdminGitRepository(AdminByCommercialTeamOrAdmins):
    """Restrict changing builder constraints on Git repositories.

    The security of some parts of the build farm depends on these settings,
    so they can only be changed by (commercial) admins.
    """

    permission = "launchpad.Admin"
    usedfor = IGitRepository


class ViewGitRef(DelegatedAuthorization):
    """Anyone who can see a Git repository can see references within it."""

    permission = "launchpad.View"
    usedfor = IGitRef

    def __init__(self, obj):
        super().__init__(obj, obj.repository)

    def checkAuthenticated(self, user):
        if self.obj.repository is not None:
            return super().checkAuthenticated(user)
        else:
            return True

    def checkUnauthenticated(self):
        if self.obj.repository is not None:
            return super().checkUnauthenticated()
        else:
            return True


class EditGitRef(DelegatedAuthorization):
    """Anyone who can edit a Git repository can edit references within it."""

    permission = "launchpad.Edit"
    usedfor = IGitRef

    def __init__(self, obj):
        super().__init__(obj, obj.repository)


class ViewGitRule(DelegatedAuthorization):
    """Anyone who can see a Git repository can see its access rules."""

    permission = "launchpad.View"
    usedfor = IGitRule

    def __init__(self, obj):
        super().__init__(obj, obj.repository)


class EditGitRule(DelegatedAuthorization):
    """Anyone who can edit a Git repository can edit its access rules."""

    permission = "launchpad.Edit"
    usedfor = IGitRule

    def __init__(self, obj):
        super().__init__(obj, obj.repository)


class ViewGitRuleGrant(DelegatedAuthorization):
    """Anyone who can see a Git repository can see its access grants."""

    permission = "launchpad.View"
    usedfor = IGitRuleGrant

    def __init__(self, obj):
        super().__init__(obj, obj.repository)


class EditGitRuleGrant(DelegatedAuthorization):
    """Anyone who can edit a Git repository can edit its access grants."""

    permission = "launchpad.Edit"
    usedfor = IGitRuleGrant

    def __init__(self, obj):
        super().__init__(obj, obj.repository)


class ViewGitActivity(DelegatedAuthorization):
    """Anyone who can see a Git repository can see its activity logs."""

    permission = "launchpad.View"
    usedfor = IGitActivity

    def __init__(self, obj):
        super().__init__(obj, obj.repository)


class BranchMergeProposalView(AuthorizationBase):
    permission = "launchpad.View"
    usedfor = IBranchMergeProposal

    @property
    def branches(self):
        required = [self.obj.source_branch, self.obj.target_branch]
        if self.obj.prerequisite_branch:
            required.append(self.obj.prerequisite_branch)
        return required

    @property
    def git_repositories(self):
        required = [
            self.obj.source_git_repository,
            self.obj.target_git_repository,
        ]
        if self.obj.prerequisite_git_repository:
            required.append(self.obj.prerequisite_git_repository)
        return required

    def checkAuthenticated(self, user):
        """Is the user able to view the branch merge proposal?

        The user can see a merge proposal if they can see the source, target
        and prerequisite branches.
        """
        if self.obj.source_git_repository is not None:
            return all(
                map(
                    lambda r: ViewGitRepository(r).checkAuthenticated(user),
                    self.git_repositories,
                )
            )
        else:
            return all(
                map(
                    lambda b: AccessBranch(b).checkAuthenticated(user),
                    self.branches,
                )
            )

    def checkUnauthenticated(self):
        """Is anyone able to view the branch merge proposal?

        Anyone can see a merge proposal between two public branches.
        """
        if self.obj.source_git_repository is not None:
            return all(
                map(
                    lambda r: ViewGitRepository(r).checkUnauthenticated(),
                    self.git_repositories,
                )
            )
        else:
            return all(
                map(
                    lambda b: AccessBranch(b).checkUnauthenticated(),
                    self.branches,
                )
            )


class PreviewDiffView(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = IPreviewDiff

    def __init__(self, obj):
        super().__init__(obj, obj.branch_merge_proposal)


class CodeReviewVoteReferenceView(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ICodeReviewVoteReference

    def __init__(self, obj):
        super().__init__(obj, obj.branch_merge_proposal)


class CodeReviewVoteReferenceEdit(DelegatedAuthorization):
    permission = "launchpad.Edit"
    usedfor = ICodeReviewVoteReference

    def __init__(self, obj):
        target = (
            obj.branch_merge_proposal.target_branch
            or obj.branch_merge_proposal.target_git_repository
        )
        super().__init__(obj, target)

    def checkAuthenticated(self, user):
        """Only the affected teams may change the review request.

        The registrant may reassign the request to another entity.
        A member of the review team may assign it to themselves.
        A person to whom it is assigned may delegate it to someone else.

        Anyone with edit permissions on the target branch of the merge
        proposal can also edit the reviews.
        """
        return (
            user.inTeam(self.obj.reviewer)
            or user.inTeam(self.obj.registrant)
            or super().checkAuthenticated(user)
        )


class CodeReviewCommentView(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ICodeReviewComment

    def __init__(self, obj):
        super().__init__(obj, obj.branch_merge_proposal)


class CodeReviewCommentOwner(AuthorizationBase):
    permission = "launchpad.Owner"
    usedfor = ICodeReviewComment

    def checkAuthenticated(self, user):
        """Only message owner can edit its content."""
        return user.isOwner(self.obj)


class CodeReviewCommentDelete(DelegatedAuthorization):
    permission = "launchpad.Edit"
    usedfor = ICodeReviewCommentDeletion

    def __init__(self, obj):
        super().__init__(obj, obj.branch_merge_proposal)


class BranchMergeProposalEdit(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IBranchMergeProposal

    def checkAuthenticated(self, user):
        """Is the user able to edit the branch merge request?

        The user is able to edit if they are:
          * the registrant of the merge proposal
          * the owner of the merge_source
          * the owner of the merge_target
          * the reviewer for the merge_target
          * an administrator
        """
        if (
            user.inTeam(self.obj.registrant)
            or user.inTeam(self.obj.merge_source.owner)
            or user.inTeam(self.obj.merge_target.reviewer)
        ):
            return True
        return self.forwardCheckAuthenticated(user, self.obj.merge_target)


class ViewSourcePackageRecipe(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ISourcePackageRecipe

    def iter_objects(self):
        return self.obj.getReferencedBranches()


class DeleteSourcePackageRecipe(AuthorizationBase):
    permission = "launchpad.Delete"
    usedfor = ISourcePackageRecipe

    def checkAuthenticated(self, user):
        return (
            user.isOwner(self.obj) or user.in_registry_experts or user.in_admin
        )


class ViewSourcePackageRecipeBuild(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ISourcePackageRecipeBuild

    def iter_objects(self):
        if self.obj.recipe is not None:
            yield self.obj.recipe
        yield self.obj.archive


class ViewSourcePackageGitBuild(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ISourcePackageGitBuild

    def iter_objects(self):
        yield self.obj.git_repository
        yield self.obj.archive


class EditSourcePackageGitBuild(AdminByBuilddAdmin):
    permission = "launchpad.Edit"
    usedfor = ISourcePackageGitBuild

    def checkAuthenticated(self, user):
        auth_repository = EditGitRepository(self.obj.git_repository)
        if auth_repository.checkAuthenticated(user):
            return True
        return super().checkAuthenticated(user)


class ViewCIBuild(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = ICIBuild

    def iter_objects(self):
        yield self.obj.git_repository


class EditCIBuild(AdminByBuilddAdmin):
    permission = "launchpad.Edit"
    usedfor = ICIBuild

    def checkAuthenticated(self, user):
        """Check edit access for CI builds.

        Allow admins, buildd admins, and people who can edit the originating
        Git repository.
        """
        auth_repository = EditGitRepository(self.obj.git_repository)
        if auth_repository.checkAuthenticated(user):
            return True
        return super().checkAuthenticated(user)


class BranchSubscriptionEdit(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IBranchSubscription

    def checkAuthenticated(self, user):
        """Is the user able to edit a branch subscription?

        Any team member can edit a branch subscription for their team.
        Launchpad Admins can also edit any branch subscription.
        The owner of the subscribed branch can edit the subscription. If the
        branch owner is a team, then members of the team can edit the
        subscription.
        """
        return (
            user.inTeam(self.obj.branch.owner)
            or user.inTeam(self.obj.person)
            or user.inTeam(self.obj.subscribed_by)
            or user.in_admin
        )


class BranchSubscriptionView(BranchSubscriptionEdit):
    permission = "launchpad.View"


class GitSubscriptionEdit(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IGitSubscription

    def checkAuthenticated(self, user):
        """Is the user able to edit a Git repository subscription?

        Any team member can edit a Git repository subscription for their
        team.
        Launchpad Admins can also edit any Git repository subscription.
        The owner of the subscribed repository can edit the subscription. If
        the repository owner is a team, then members of the team can edit
        the subscription.
        """
        return (
            user.inTeam(self.obj.repository.owner)
            or user.inTeam(self.obj.person)
            or user.inTeam(self.obj.subscribed_by)
            or user.in_admin
        )


class GitSubscriptionView(GitSubscriptionEdit):
    permission = "launchpad.View"
