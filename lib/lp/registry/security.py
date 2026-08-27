# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Security adapters for the registry module."""

__all__ = [
    "EditByOwnersOrAdmins",
    "PublicOrPrivateTeamsExistence",
    "SecurityAdminDistribution",
]

from storm.expr import Select, Union
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.security import (
    AnonymousAuthorization,
    AuthorizationBase,
    DelegatedAuthorization,
)
from lp.blueprints.model.specificationsubscription import (
    SpecificationSubscription,
)
from lp.bugs.model.bugsubscription import BugSubscription
from lp.bugs.model.bugtaskflat import BugTaskFlat
from lp.bugs.model.bugtasksearch import get_bug_privacy_filter
from lp.code.interfaces.branchcollection import IAllBranches, IBranchCollection
from lp.code.interfaces.gitcollection import IGitCollection
from lp.registry.enums import PersonVisibility
from lp.registry.interfaces.announcement import IAnnouncement
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.distributionmirror import IDistributionMirror
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.distroseriesdifference import (
    IDistroSeriesDifferenceAdmin,
    IDistroSeriesDifferenceEdit,
)
from lp.registry.interfaces.distroseriesparent import IDistroSeriesParent
from lp.registry.interfaces.gpg import IGPGKey
from lp.registry.interfaces.irc import IIrcID
from lp.registry.interfaces.location import IPersonLocation
from lp.registry.interfaces.milestone import IMilestone, IProjectGroupMilestone
from lp.registry.interfaces.nameblocklist import (
    INameBlocklist,
    INameBlocklistSet,
)
from lp.registry.interfaces.ociproject import IOCIProject
from lp.registry.interfaces.ociprojectseries import IOCIProjectSeries
from lp.registry.interfaces.packaging import IPackaging
from lp.registry.interfaces.person import (
    IPerson,
    IPersonLimitedView,
    IPersonSet,
    ITeam,
)
from lp.registry.interfaces.pillar import IPillar, IPillarPerson
from lp.registry.interfaces.poll import IPoll, IPollOption, IPollSubset
from lp.registry.interfaces.product import IProduct, IProductSet
from lp.registry.interfaces.productrelease import (
    IProductRelease,
    IProductReleaseFile,
)
from lp.registry.interfaces.productseries import (
    IProductSeries,
    IProductSeriesLimitedView,
    IProductSeriesView,
    ITimelineProductSeries,
)
from lp.registry.interfaces.projectgroup import IProjectGroup, IProjectGroupSet
from lp.registry.interfaces.role import IHasDrivers, IHasOwner
from lp.registry.interfaces.sourcepackage import ISourcePackage
from lp.registry.interfaces.ssh import ISSHKey
from lp.registry.interfaces.teammembership import (
    ITeamMembership,
    TeamMembershipStatus,
)
from lp.registry.interfaces.wikiname import IWikiName
from lp.registry.model.person import Person
from lp.security import (
    AdminByAdminsTeam,
    AdminByCommercialTeamOrAdmins,
    ModerateByRegistryExpertsOrAdmins,
    OnlyRosettaExpertsAndAdmins,
)
from lp.services.database.interfaces import IStore
from lp.services.identity.interfaces.account import IAccount
from lp.services.webapp.security import EditByRegistryExpertsOrAdmins
from lp.soyuz.interfaces.archivesubscriber import IArchiveSubscriberSet


def can_edit_team(team, user):
    """Return True if the given user has edit rights for the given team."""
    if user.in_admin:
        return True
    else:
        return team in user.person.getAdministratedTeams()


class EditByOwnersOrAdmins(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IHasOwner

    def checkAuthenticated(self, user):
        return user.isOwner(self.obj) or user.in_admin


class ModerateDistroSeries(ModerateByRegistryExpertsOrAdmins):
    usedfor = IDistroSeries


class ModerateProduct(ModerateByRegistryExpertsOrAdmins):
    usedfor = IProduct


class ModerateProductSet(ModerateByRegistryExpertsOrAdmins):
    usedfor = IProductSet


class ModerateProject(ModerateByRegistryExpertsOrAdmins):
    usedfor = IProjectGroup


class ModerateProjectGroupSet(ModerateByRegistryExpertsOrAdmins):
    usedfor = IProjectGroupSet


class ModeratePerson(ModerateByRegistryExpertsOrAdmins):
    permission = "launchpad.Moderate"
    usedfor = IPerson


class ViewPillar(AuthorizationBase):
    usedfor = IPillar
    permission = "launchpad.View"

    def checkUnauthenticated(self):
        return self.obj.active

    def checkAuthenticated(self, user):
        """The Admins & Commercial Admins can see inactive pillars."""
        if self.obj.active:
            return True
        else:
            return (
                user.in_commercial_admin
                or user.in_admin
                or user.in_registry_experts
            )


class PillarPersonSharingDriver(AuthorizationBase):
    usedfor = IPillarPerson
    permission = "launchpad.Driver"

    def checkAuthenticated(self, user):
        """Maintainers, drivers, and admins can drive projects."""
        return (
            user.in_admin
            or user.isOwner(self.obj.pillar)
            or user.isDriver(self.obj.pillar)
        )


class EditAccountBySelfOrAdmin(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IAccount

    def checkAuthenticated(self, user):
        return user.in_admin or user.person.account == self.obj


class ViewAccount(EditAccountBySelfOrAdmin):
    permission = "launchpad.View"

    def checkAuthenticated(self, user):
        """Extend permission to registry experts."""
        return super().checkAuthenticated(user) or user.in_registry_experts


class ModerateAccountByRegistryExpert(AuthorizationBase):
    usedfor = IAccount
    permission = "launchpad.Moderate"

    def checkAuthenticated(self, user):
        return user.in_admin or user.in_registry_experts


class ViewProduct(AuthorizationBase):
    permission = "launchpad.View"
    usedfor = IProduct

    def checkAuthenticated(self, user):
        return self.obj.userCanView(user)

    def checkUnauthenticated(self):
        return self.obj.userCanView(None)


class LimitedViewProduct(ViewProduct):
    permission = "launchpad.LimitedView"
    usedfor = IProduct

    def checkAuthenticated(self, user):
        return super().checkAuthenticated(user) or self.obj.userCanLimitedView(
            user
        )


class EditProduct(EditByOwnersOrAdmins):
    usedfor = IProduct

    def checkAuthenticated(self, user):
        # Commercial admins may help setup commercial projects.
        return (
            super().checkAuthenticated(user)
            or is_commercial_case(self.obj, user)
            or False
        )


class EditPackaging(AuthorizationBase):
    usedfor = IPackaging
    permission = "launchpad.Edit"

    def checkAuthenticated(self, user):
        return self.forwardCheckAuthenticated(
            user, self.obj.productseries
        ) or self.forwardCheckAuthenticated(user, self.obj.sourcepackage)


class DownloadFullSourcePackageTranslations(OnlyRosettaExpertsAndAdmins):
    """Restrict full `SourcePackage` translation downloads.

    Experience shows that the export queue can easily get swamped by
    large export requests.  Email leads us to believe that many of the
    users making these requests are looking for language packs, or for
    individual translations rather than the whole package.  That's why
    this class defines who is allowed to make those requests.
    """

    permission = "launchpad.ExpensiveRequest"
    usedfor = ISourcePackage

    def _userInAnyOfTheTeams(self, user, archive_permissions):
        if archive_permissions is None or len(archive_permissions) == 0:
            return False
        for permission in archive_permissions:
            if user.inTeam(permission.person):
                return True
        return False

    def checkAuthenticated(self, user):
        """Define who may download these translations.

        Admins and Translations admins have access, as does the owner of
        the translation group (if applicable) and distribution uploaders.
        """
        distribution = self.obj.distribution
        translation_group = distribution.translationgroup
        return (
            # User is admin of some relevant kind.
            OnlyRosettaExpertsAndAdmins.checkAuthenticated(self, user)
            or
            # User is part of the 'driver' team for the distribution.
            (self._userInAnyOfTheTeams(user, distribution.uploaders))
            or
            # User is owner of applicable translation group.
            (
                translation_group is not None
                and user.inTeam(translation_group.owner)
            )
        )


class DownloadFullProductSeriesTranslations(OnlyRosettaExpertsAndAdmins):
    """Restrict full `ProductSeries` translation downloads.

    Some product series contain a large number of templates, and requests
    for those can swamp the export queue.  Most translators probably only
    need individual files.
    """

    permission = "launchpad.ExpensiveRequest"
    usedfor = IProductSeries

    def checkAuthenticated(self, user):
        """Define who may download these translations.

        Admins and Translations admins have access, as does the owner of
        the translation group (if applicable) and distribution uploaders.
        """
        translation_group = self.obj.product.translationgroup
        return (
            # User is admin of some relevant kind.
            OnlyRosettaExpertsAndAdmins.checkAuthenticated(self, user)
            # User is the owner of the product, or the release manager of
            # the series.
            or user.isOwner(self.obj.product)
            or user.isDriver(self.obj)
            # User is owner of applicable translation group.
            or (
                translation_group is not None
                and user.inTeam(translation_group.owner)
            )
        )


class EditProductRelease(EditByOwnersOrAdmins):
    permission = "launchpad.Edit"
    usedfor = IProductRelease

    def checkAuthenticated(self, user):
        if user.isOwner(self.obj.productseries.product) or user.isDriver(
            self.obj.productseries
        ):
            # The user is an owner or a release manager.
            return True
        return EditByOwnersOrAdmins.checkAuthenticated(self, user)


class ViewProductRelease(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = IProductRelease

    def __init__(self, obj):
        super().__init__(obj, obj.milestone, "launchpad.View")


class EditProductReleaseFile(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IProductReleaseFile

    def checkAuthenticated(self, user):
        return EditProductRelease(self.obj.productrelease).checkAuthenticated(
            user
        )


class ViewTimelineProductSeries(DelegatedAuthorization):
    """Anyone who can view the related product can also view an
    ITimelineProductSeries.
    """

    permission = "launchpad.View"
    usedfor = ITimelineProductSeries

    def __init__(self, obj):
        super().__init__(obj, obj.product, "launchpad.View")


class ViewProductReleaseFile(AnonymousAuthorization):
    """Anyone can view an IProductReleaseFile."""

    usedfor = IProductReleaseFile


class AdminDistributionMirrorByDistroOwnerOrMirrorAdminsOrAdmins(
    AuthorizationBase
):
    permission = "launchpad.Admin"
    usedfor = IDistributionMirror

    def checkAuthenticated(self, user):
        return (
            user.isOwner(self.obj.distribution)
            or user.in_admin
            or user.inTeam(self.obj.distribution.mirror_admin)
        )


class EditDistributionMirrorByOwnerOrDistroOwnerOrMirrorAdminsOrAdmins(
    AuthorizationBase
):
    permission = "launchpad.Edit"
    usedfor = IDistributionMirror

    def checkAuthenticated(self, user):
        return (
            user.isOwner(self.obj)
            or user.in_admin
            or user.isOwner(self.obj.distribution)
            or user.inTeam(self.obj.distribution.mirror_admin)
        )


class ModerateDistributionMirror(AuthorizationBase):
    permission = "launchpad.Moderate"
    usedfor = IDistributionMirror

    def checkAuthenticated(self, user):
        return (
            self.forwardCheckAuthenticated(user, self.obj, "launchpad.Edit")
            or user.in_launchpad_developers
        )


class ViewDistributionMirror(AnonymousAuthorization):
    """Anyone can view an IDistributionMirror."""

    usedfor = IDistributionMirror


class AdminProjectTranslations(AuthorizationBase):
    permission = "launchpad.TranslationsAdmin"
    usedfor = IProjectGroup

    def checkAuthenticated(self, user):
        """Is the user able to manage `IProjectGroup` translations settings?

        Any Launchpad/Launchpad Translations administrator or owner is
        able to change translation settings for a project group.
        """
        return (
            user.isOwner(self.obj) or user.in_rosetta_experts or user.in_admin
        )


class AdminProductTranslations(AuthorizationBase):
    permission = "launchpad.TranslationsAdmin"
    usedfor = IProduct

    def checkAuthenticated(self, user):
        """Is the user able to manage `IProduct` translations settings?

        Any Launchpad/Launchpad Translations administrator or owners are
        able to change translation settings for a product.
        """
        return (
            user.isOwner(self.obj)
            or user.isDriver(self.obj)
            or user.in_rosetta_experts
            or user.in_admin
        )


class ViewProjectMilestone(DelegatedAuthorization):
    permission = "launchpad.View"
    usedfor = IProjectGroupMilestone

    def __init__(self, obj):
        super().__init__(obj, obj.product, "launchpad.View")


class EditProjectMilestoneNever(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IProjectGroupMilestone

    def checkAuthenticated(self, user):
        """IProjectGroupMilestone is a fake content object."""
        return False


class LimitedViewMilestone(DelegatedAuthorization):
    permission = "launchpad.LimitedView"
    usedfor = IMilestone

    def __init__(self, obj):
        super().__init__(obj, obj.target, "launchpad.LimitedView")


class ViewMilestone(AuthorizationBase):
    permission = "launchpad.View"
    usedfor = IMilestone

    def checkAuthenticated(self, user):
        return self.obj.userCanView(user)

    def checkUnauthenticated(self):
        return self.obj.userCanView(user=None)


class EditMilestoneByTargetOwnerOrAdmins(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IMilestone

    def checkAuthenticated(self, user):
        """Authorize the product or distribution owner."""
        if user.in_admin:
            return True
        if user.isDriver(self.obj.series_target):
            return True
        return user.isOwner(self.obj.target)


class AdminMilestoneByLaunchpadAdmins(AuthorizationBase):
    permission = "launchpad.Admin"
    usedfor = IMilestone

    def checkAuthenticated(self, user):
        """Only the Launchpad admins need this, we are only going to use
        it for connecting up series and distroseries where we did not
        have them.
        """
        return user.in_admin


class ModeratePersonSetByExpertsOrAdmins(ModerateByRegistryExpertsOrAdmins):
    permission = "launchpad.Moderate"
    usedfor = IPersonSet


class EditTeamByTeamOwnerOrLaunchpadAdmins(AuthorizationBase):
    permission = "launchpad.Owner"
    usedfor = ITeam

    def checkAuthenticated(self, user):
        """Only the team owner and Launchpad admins need this."""
        return user.inTeam(self.obj.teamowner) or user.in_admin


class EditTeamByTeamOwnerOrTeamAdminsOrAdmins(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = ITeam

    def checkAuthenticated(self, user):
        """The team owner and team admins have launchpad.Edit on that team.

        The Launchpad admins also have launchpad.Edit on all teams.
        """
        return can_edit_team(self.obj, user)


class ModerateTeam(ModerateByRegistryExpertsOrAdmins):
    permission = "launchpad.Moderate"
    usedfor = ITeam

    def checkAuthenticated(self, user):
        """Is the user a privileged team member or Launchpad staff?

        Return true when the user is a member of Launchpad admins,
        registry experts, team admins, or the team owners.
        """
        return super().checkAuthenticated(user) or can_edit_team(
            self.obj, user
        )


class EditTeamMembershipByTeamOwnerOrTeamAdminsOrAdmins(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = ITeamMembership

    def checkAuthenticated(self, user):
        return can_edit_team(self.obj.team, user)


class ViewTeamMembership(AuthorizationBase):
    permission = "launchpad.View"
    usedfor = ITeamMembership

    def checkUnauthenticated(self):
        """Unauthenticated users can only view public memberships."""
        return self.obj.team.visibility == PersonVisibility.PUBLIC

    def checkAuthenticated(self, user):
        """Verify that the user can view the team's membership.

        Anyone can see a public team's membership. Only a team member or
        commercial admin or a Launchpad admin can view a private team.
        """
        if self.obj.team.visibility == PersonVisibility.PUBLIC:
            return True
        if (
            user.in_admin
            or user.in_commercial_admin
            or user.inTeam(self.obj.team)
        ):
            return True
        return False


class AdminByCommercialTeamOrAdminsOrPerson(AdminByCommercialTeamOrAdmins):
    permission = "launchpad.Commercial"
    usedfor = IPerson

    def checkAuthenticated(self, user):
        """Users can manage their commercial data and admins can help."""
        return self.obj.id == user.id or super().checkAuthenticated(user)


class EditPersonBySelfOrAdmins(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IPerson

    def checkAuthenticated(self, user):
        """A user can edit the Person who is themselves.

        The admin team can also edit any Person.
        """
        return self.obj.id == user.id or user.in_admin


class ViewPersonLocation(AuthorizationBase):
    permission = "launchpad.View"
    usedfor = IPersonLocation

    def checkUnauthenticated(self):
        return self.obj.visible

    def checkAuthenticated(self, user):
        if self.obj.visible:
            return True
        else:
            return user.person == self.obj.person or user.in_admin


class EditPersonBySelf(AuthorizationBase):
    permission = "launchpad.Special"
    usedfor = IPerson

    def checkAuthenticated(self, user):
        """A user can edit the Person who is themselves."""
        return self.obj.id == user.person.id


class ViewPublicOrPrivateTeamMembers(AuthorizationBase):
    """Restrict viewing of private teams.

    Only members of a private team can view the
    membership list.
    """

    permission = "launchpad.View"
    usedfor = IPerson

    def checkUnauthenticated(self):
        """Unauthenticated users can only view public memberships."""
        if self.obj.visibility == PersonVisibility.PUBLIC:
            return True
        return False

    def checkAuthenticated(self, user):
        """Verify that the user can view the team's membership.

        Anyone can see a public team's membership. Only a team member,
        commercial admin, or a Launchpad admin can view a private team's
        members.
        """
        if self.obj.visibility == PersonVisibility.PUBLIC:
            return True
        if user.in_admin or user.in_commercial_admin or user.inTeam(self.obj):
            return True
        # Private team owners have visibility.
        if self.obj.is_team and user.inTeam(self.obj.teamowner):
            return True
        # We also grant visibility of the private team to administrators of
        # other teams that have been invited to join the private team.
        for invitee in self.obj.invited_members:
            if (
                invitee.is_team
                and invitee in user.person.getAdministratedTeams()
            ):
                return True
        return False


class PublicOrPrivateTeamsExistence(AuthorizationBase):
    """Restrict knowing about private teams' existence.

    Knowing the existence of a private team allow traversing to its URL and
    displaying basic information like name, displayname.
    """

    permission = "launchpad.LimitedView"
    usedfor = IPersonLimitedView

    def checkUnauthenticated(self):
        """Unauthenticated users can only view public teams."""
        if self.obj.visibility == PersonVisibility.PUBLIC:
            return True
        return False

    def checkAuthenticated(self, user):
        """By default, we simply perform a View permission check.

        We also grant limited viewability to users who can see PPAs and
        branches owned by the team, and members of parent teams so they can
        see the member-listings.

        In other scenarios, the context in which the permission is required is
        responsible for pre-caching the launchpad.LimitedView permission on
        each team which requires it.
        """
        if self.forwardCheckAuthenticated(user, self.obj, "launchpad.View"):
            return True

        if (
            self.obj.is_team
            and self.obj.visibility == PersonVisibility.PRIVATE
        ):
            # Grant visibility to people with subscriptions on a private
            # team's private PPA.  We can safely skip security checks here: the
            # user can view all their own subscriptions.
            subscriptions = removeSecurityProxy(
                getUtility(IArchiveSubscriberSet).getBySubscriber(user.person)
            )
            subscriber_archive_ids = {sub.archive_id for sub in subscriptions}
            team_ppa_ids = {ppa.id for ppa in self.obj.ppas if ppa.private}
            if len(subscriber_archive_ids.intersection(team_ppa_ids)) > 0:
                return True

            # Grant visibility to people who can see branches owned by the
            # private team.
            team_branches = IBranchCollection(self.obj)
            if not team_branches.visibleByUser(user.person).is_empty():
                return True

            # Grant visibility to people who can see branches subscribed to
            # by the private team.
            team_branches = getUtility(IAllBranches).subscribedBy(self.obj)
            if not team_branches.visibleByUser(user.person).is_empty():
                return True

            # Grant visibility to branches visible to the user and which have
            # review requests for the private team.
            branches = getUtility(IAllBranches)
            visible_branches = branches.visibleByUser(user.person)
            mp = visible_branches.getMergeProposalsForReviewer(self.obj)
            if not mp.is_empty():
                return True

            # Grant visibility to people who can see Git repositories owned
            # by the private team.
            team_repositories = IGitCollection(self.obj)
            if not team_repositories.visibleByUser(user.person).is_empty():
                return True

            # Grant visibility to people who own Git repositories that grant
            # some kind of write access to the private team.
            owned_repositories = IGitCollection(user.person)
            grants = owned_repositories.getRuleGrantsForGrantee(self.obj)
            if not grants.is_empty():
                return True

            # Grant visibility to users in a team that has the private team as
            # a member, so that they can see the team properly in member
            # listings.

            # The easiest check is just to see if the user is in a team that
            # is a super team for the private team.

            # Do comparison by ids because they may be needed for comparison
            # to membership.team.ids later.
            user_teams = [
                team.id for team in user.person.teams_participated_in
            ]
            super_teams = [team.id for team in self.obj.super_teams]
            intersection_teams = set(user_teams) & set(super_teams)

            if len(intersection_teams) > 0:
                return True

            # If it's not, the private team may still be a pending membership,
            # deactivated membership, or an expired membership,
            # which still needs to be visible to team members.
            BAD_STATES = (
                TeamMembershipStatus.DECLINED.value,
                TeamMembershipStatus.INVITATION_DECLINED.value,
            )
            team_memberships_query = """
                SELECT team from TeamMembership WHERE person = %s AND
                status NOT IN %s
                """ % (
                self.obj.id,
                BAD_STATES,
            )
            store = IStore(Person)
            future_super_teams = [
                team[0] for team in store.execute(team_memberships_query)
            ]
            intersection_teams = set(user_teams) & set(future_super_teams)

            if len(intersection_teams) > 0:
                return True

            # Teams subscribed to blueprints are visible. This needs to
            # be taught about privacy eventually.
            specsubs = store.find(SpecificationSubscription, person=self.obj)

            # Teams subscribed or assigned to bugs that the user can see
            # are visible.
            bugs = store.find(
                BugTaskFlat,
                get_bug_privacy_filter(user.person),
                BugTaskFlat.bug_id.is_in(
                    Union(
                        Select(
                            BugSubscription.bug_id,
                            tables=(BugSubscription,),
                            where=BugSubscription.person == self.obj,
                        ),
                        Select(
                            BugTaskFlat.bug_id,
                            tables=(BugTaskFlat,),
                            where=BugTaskFlat.assignee == self.obj,
                        ),
                        all=True,
                    )
                ),
            )

            if not specsubs.is_empty() or not bugs.is_empty():
                return True
        return False


class EditPollByTeamOwnerOrTeamAdminsOrAdmins(
    EditTeamMembershipByTeamOwnerOrTeamAdminsOrAdmins
):
    permission = "launchpad.Edit"
    usedfor = IPoll


class EditPollSubsetByTeamOwnerOrTeamAdminsOrAdmins(
    EditPollByTeamOwnerOrTeamAdminsOrAdmins
):
    permission = "launchpad.Edit"
    usedfor = IPollSubset


class EditPollOptionByTeamOwnerOrTeamAdminsOrAdmins(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IPollOption

    def checkAuthenticated(self, user):
        return can_edit_team(self.obj.poll.team, user)


class ViewDistribution(AuthorizationBase):
    permission = "launchpad.View"
    usedfor = IDistribution

    def checkAuthenticated(self, user):
        return self.obj.userCanView(user)

    def checkUnauthenticated(self):
        return self.obj.userCanView(None)


class LimitedViewDistribution(ViewDistribution):
    permission = "launchpad.LimitedView"
    usedfor = IDistribution

    def checkAuthenticated(self, user):
        return super().checkAuthenticated(user) or self.obj.userCanLimitedView(
            user
        )


class AdminDistribution(AdminByAdminsTeam):
    """Soyuz involves huge chunks of data in the archive and librarian,
    so for the moment we are locking down admin and edit on distributions
    and distroseriess to the Launchpad admin team."""

    permission = "launchpad.Admin"
    usedfor = IDistribution


class EditDistributionByDistroOwnersOrAdmins(AuthorizationBase):
    """The owner of a distribution should be able to edit its
    information; it is mainly administrative data, such as bug supervisors.
    Note that creation of new distributions and distribution
    series is still protected with launchpad.Admin"""

    permission = "launchpad.Edit"
    usedfor = IDistribution

    def checkAuthenticated(self, user):
        # Commercial admins may help setup commercial distributions.
        return (
            user.isOwner(self.obj)
            or is_commercial_case(self.obj, user)
            or user.in_admin
        )


class SecurityAdminDistribution(AuthorizationBase):
    """The security admins of a distribution should be able to create
    and edit vulnerabilities in the distribution."""

    permission = "launchpad.SecurityAdmin"
    usedfor = IDistribution

    def checkAuthenticated(self, user):
        return (
            user.isOwner(self.obj)
            or is_commercial_case(self.obj, user)
            or user.in_admin
            or user.inTeam(self.obj.security_admin)
        )


class ModerateDistributionByDriversOrOwnersOrAdmins(AuthorizationBase):
    """Distribution drivers, owners, and admins may plan releases.

    Drivers of distributions that don't manage their packages in
    Launchpad can create series. Owners and admins can create series for
    all `IDistribution`s.
    """

    permission = "launchpad.Moderate"
    usedfor = IDistribution

    def checkAuthenticated(self, user):
        if user.isDriver(self.obj) and not self.obj.official_packages:
            # Damage to series with packages managed in Launchpad can
            # cause serious strife. Restrict changes to the distro
            # owner.
            return True
        return user.isOwner(self.obj) or user.in_admin


class ViewDistributionSourcePackage(AnonymousAuthorization):
    """Anyone can view a DistributionSourcePackage."""

    usedfor = IDistributionSourcePackage


class BugSuperviseDistributionSourcePackage(AuthorizationBase):
    """The owner of a distribution should be able to edit its source
    package information"""

    permission = "launchpad.BugSupervisor"
    usedfor = IDistributionSourcePackage

    def checkAuthenticated(self, user):
        return (
            user.inTeam(self.obj.distribution.bug_supervisor)
            or user.inTeam(self.obj.distribution.owner)
            or user.in_admin
        )


class EditDistributionSourcePackage(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IDistributionSourcePackage

    def _checkUpload(self, user, archive, distroseries):
        # We use verifyUpload() instead of checkUpload() because we don't
        # have a pocket.  It returns the reason the user can't upload or
        # None if they are allowed.
        if distroseries is None:
            return False
        sourcepackage = distroseries.getSourcePackage(
            self.obj.sourcepackagename
        )
        reason = archive.verifyUpload(
            user.person,
            sourcepackagename=self.obj.sourcepackagename,
            component=sourcepackage.latest_published_component,
            distroseries=distroseries,
        )
        return reason is None

    def checkAuthenticated(self, user):
        """Anyone who can upload a package can edit it.

        Checking upload permission requires a distroseries; a reasonable
        approximation is to check whether the user can upload the package to
        the current series.
        """
        if user.in_admin:
            return True

        distribution = self.obj.distribution
        if user.inTeam(distribution.owner):
            return True

        return self._checkUpload(
            user, distribution.main_archive, distribution.currentseries
        )


class EditSourcePackage(EditDistributionSourcePackage):
    permission = "launchpad.Edit"
    usedfor = ISourcePackage

    def checkAuthenticated(self, user):
        """Anyone who can upload a package can edit it."""
        if user.in_admin:
            return True

        distribution = self.obj.distribution
        if user.inTeam(distribution.owner):
            return True

        return self._checkUpload(
            user, distribution.main_archive, self.obj.distroseries
        )


class ViewSourcePackage(AnonymousAuthorization):
    usedfor = ISourcePackage


class NominateBugForProductSeries(AuthorizationBase):
    """Product's owners and bug supervisors can add bug nominations."""

    permission = "launchpad.BugSupervisor"
    usedfor = IProductSeries

    def checkAuthenticated(self, user):
        return (
            user.inTeam(self.obj.product.bug_supervisor)
            or user.inTeam(self.obj.product.owner)
            or user.in_admin
        )


class NominateBugForDistroSeries(AuthorizationBase):
    """Distro's owners and bug supervisors can add bug nominations."""

    permission = "launchpad.BugSupervisor"
    usedfor = IDistroSeries

    def checkAuthenticated(self, user):
        return (
            user.inTeam(self.obj.distribution.bug_supervisor)
            or user.inTeam(self.obj.distribution.owner)
            or user.in_admin
        )


class AdminDistroSeries(AdminByAdminsTeam):
    """Soyuz involves huge chunks of data in the archive and librarian,
    so for the moment we are locking down admin and edit on distributions
    and distroseriess to the Launchpad admin team.

    NB: Please consult carefully before modifying this permission because
        changing it could cause the archive to get rearranged, with tons of
        files moved to the new namespace, and mirrors would get very very
        upset.
    """

    permission = "launchpad.Admin"
    usedfor = IDistroSeries


class EditDistroSeriesByReleaseManagerOrDistroOwnersOrAdmins(
    AuthorizationBase
):
    """The owner of the distro series (i.e. the owner of the distribution)
    should be able to modify some of the fields on the IDistroSeries

    NB: there is potential for a great mess if this is not done correctly,
    so please consult carefully before modifying these permissions.
    """

    permission = "launchpad.Edit"
    usedfor = IDistroSeries

    def checkAuthenticated(self, user):
        if (
            user.inTeam(self.obj.driver)
            and not self.obj.distribution.official_packages
        ):
            # Damage to series with packages managed in Launchpad can
            # cause serious strife. Restrict changes to the distro
            # owner.
            return True
        return user.inTeam(self.obj.distribution.owner) or user.in_admin


class ViewDistroSeries(AnonymousAuthorization):
    """Anyone can view a DistroSeries."""

    usedfor = IDistroSeries


class EditDistroSeriesParent(AuthorizationBase):
    """DistroSeriesParent can be edited by the same people who can edit
    the derived_distroseries."""

    permission = "launchpad.Edit"
    usedfor = IDistroSeriesParent

    def checkAuthenticated(self, user):
        auth = EditDistroSeriesByReleaseManagerOrDistroOwnersOrAdmins(
            self.obj.derived_series
        )
        return auth.checkAuthenticated(user)


class AdminDistroSeriesDifference(AuthorizationBase):
    """You need to be an archive admin or LP admin to get lp.Admin."""

    permission = "launchpad.Admin"
    usedfor = IDistroSeriesDifferenceAdmin

    def checkAuthenticated(self, user):
        # Archive admin is done by component, so here we just
        # see if the user has that permission on any components
        # at all.
        archive = self.obj.derived_series.main_archive
        return (
            not archive.getComponentsForQueueAdmin(user.person).is_empty()
            or user.in_admin
        )


class EditDistroSeriesDifference(DelegatedAuthorization):
    """Anyone with lp.View on the distribution can edit a DSD."""

    permission = "launchpad.Edit"
    usedfor = IDistroSeriesDifferenceEdit

    def __init__(self, obj):
        super().__init__(
            obj, obj.derived_series.distribution, "launchpad.View"
        )

    def checkUnauthenticated(self):
        return False


class SeriesDrivers(AuthorizationBase):
    """Drivers can approve or decline features and target bugs.

    Drivers exist for distribution and product series.  Distribution and
    product owners are implicitly drivers too.
    """

    permission = "launchpad.Driver"
    usedfor = IHasDrivers

    def checkAuthenticated(self, user):
        return self.obj.personHasDriverRights(user)


class DriveProduct(SeriesDrivers):
    permission = "launchpad.Driver"
    usedfor = IProduct

    def checkAuthenticated(self, user):
        # Commercial admins may help setup commercial projects.
        return (
            super().checkAuthenticated(user)
            or is_commercial_case(self.obj, user)
            or False
        )


class LimitedViewProductSeries(DelegatedAuthorization):
    permission = "launchpad.LimitedView"
    usedfor = IProductSeriesLimitedView

    def __init__(self, obj):
        super().__init__(obj, obj.product, "launchpad.LimitedView")


class ViewProductSeries(AuthorizationBase):
    permission = "launchpad.View"
    usedfor = IProductSeriesView

    def checkAuthenticated(self, user):
        return self.obj.userCanView(user)

    def checkUnauthenticated(self):
        return self.obj.userCanView(None)


class EditProductSeries(EditByOwnersOrAdmins):
    usedfor = IProductSeries

    def checkAuthenticated(self, user):
        """Allow product owner, drivers, some experts, or admins."""
        if user.isOwner(self.obj.product) or user.isDriver(self.obj):
            # The user is the owner of the product, or the release manager.
            return True
        # Rosetta experts need to be able to upload translations.
        # Registry admins are just special.
        if user.in_registry_experts or user.in_rosetta_experts:
            return True
        return EditByOwnersOrAdmins.checkAuthenticated(self, user)


class ViewAnnouncement(AuthorizationBase):
    permission = "launchpad.View"
    usedfor = IAnnouncement

    def checkUnauthenticated(self):
        """Let anonymous users see published announcements."""
        if self.obj.published:
            return True
        return False

    def checkAuthenticated(self, user):
        """Keep project news invisible to end-users unless they are project
        admins, until the announcements are published."""

        # Every user can view published announcements.
        if self.obj.published:
            return True

        # Project drivers can view any project announcements.
        # Launchpad admins can view any announcement.
        assert self.obj.target
        return (
            user.isDriver(self.obj.target)
            or user.isOwner(self.obj.target)
            or user.in_admin
        )


class EditAnnouncement(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IAnnouncement

    def checkAuthenticated(self, user):
        """Allow the project owner and drivers to edit any project news."""

        assert self.obj.target
        return (
            user.isDriver(self.obj.target)
            or user.isOwner(self.obj.target)
            or user.in_admin
        )


class AdminDistributionTranslations(AuthorizationBase):
    """Class for deciding who can administer distribution translations.

    This class is used for `launchpad.TranslationsAdmin` privilege on
    `IDistribution` and `IDistroSeries` and corresponding `IPOTemplate`s,
    and limits access to Rosetta experts, Launchpad admins and distribution
    translation group owner.
    """

    permission = "launchpad.TranslationsAdmin"
    usedfor = IDistribution

    def checkAuthenticated(self, user):
        """Is the user able to manage `IDistribution` translations settings?

        Any Launchpad/Launchpad Translations administrator, translation group
        owner or a person allowed to edit distribution details is able to
        change translations settings for a distribution.
        """
        # Translation group owner for a distribution is also a
        # translations administrator for it.
        translation_group = self.obj.translationgroup
        if translation_group and user.inTeam(translation_group.owner):
            return True
        else:
            return (
                user.in_rosetta_experts
                or EditDistributionByDistroOwnersOrAdmins(
                    self.obj
                ).checkAuthenticated(user)
            )


class AddPOTemplate(OnlyRosettaExpertsAndAdmins):
    permission = "launchpad.Append"
    usedfor = IProductSeries


class ViewNameBlocklist(EditByRegistryExpertsOrAdmins):
    permission = "launchpad.View"
    usedfor = INameBlocklist


class EditNameBlocklist(EditByRegistryExpertsOrAdmins):
    permission = "launchpad.Edit"
    usedfor = INameBlocklist


class ViewNameBlocklistSet(EditByRegistryExpertsOrAdmins):
    permission = "launchpad.View"
    usedfor = INameBlocklistSet


class EditNameBlocklistSet(EditByRegistryExpertsOrAdmins):
    permission = "launchpad.Edit"
    usedfor = INameBlocklistSet


class AdminDistroSeriesTranslations(AuthorizationBase):
    permission = "launchpad.TranslationsAdmin"
    usedfor = IDistroSeries

    def checkAuthenticated(self, user):
        """Is the user able to manage `IDistroSeries` translations.

        Distribution translation managers and distribution series drivers
        can manage IDistroSeries translations.
        """
        return user.isDriver(self.obj) or self.forwardCheckAuthenticated(
            user,
            self.obj.distribution,
            permission="launchpad.TranslationsAdmin",
        )


class ViewHiddenDistroSeriesTranslations(AdminDistroSeriesTranslations):
    """Allow translation administrators to view hidden series translations."""

    permission = "launchpad.ViewHiddenDistroSeriesTranslations"


class AdminDistributionSourcePackageTranslations(DelegatedAuthorization):
    """DistributionSourcePackage objects link to a distribution."""

    permission = "launchpad.TranslationsAdmin"
    usedfor = IDistributionSourcePackage

    def __init__(self, obj):
        super().__init__(obj, obj.distribution)


class AdminProductSeriesTranslations(AuthorizationBase):
    permission = "launchpad.TranslationsAdmin"
    usedfor = IProductSeries

    def checkAuthenticated(self, user):
        """Is the user able to manage `IProductSeries` translations."""

        return (
            user.isOwner(self.obj)
            or user.isDriver(self.obj)
            or self.forwardCheckAuthenticated(user, self.obj.product)
        )


class AdminDistroSeriesLanguagePacks(
    OnlyRosettaExpertsAndAdmins,
    EditDistroSeriesByReleaseManagerOrDistroOwnersOrAdmins,
):
    permission = "launchpad.LanguagePacksAdmin"
    usedfor = IDistroSeries

    def checkAuthenticated(self, user):
        """Is the user able to manage `IDistroSeries` language packs?

        Any Launchpad/Launchpad Translations administrator, people allowed to
        edit distroseries or members of IDistribution.language_pack_admin team
        are able to change the language packs available.
        """
        EditDS = EditDistroSeriesByReleaseManagerOrDistroOwnersOrAdmins
        return (
            OnlyRosettaExpertsAndAdmins.checkAuthenticated(self, user)
            or EditDS.checkAuthenticated(self, user)
            or user.inTeam(self.obj.distribution.language_pack_admin)
        )


class ViewGPGKey(AnonymousAuthorization):
    usedfor = IGPGKey


class ViewSSHKey(AnonymousAuthorization):
    usedfor = ISSHKey


class ViewIrcID(AnonymousAuthorization):
    usedfor = IIrcID


class ViewWikiName(AnonymousAuthorization):
    usedfor = IWikiName


class ViewOCIProject(AnonymousAuthorization):
    """Anyone can view an `IOCIProject`."""

    usedfor = IOCIProject


class EditOCIProject(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IOCIProject

    def checkAuthenticated(self, user):
        """Maintainers, drivers, and admins can drive projects."""
        return (
            user.in_admin
            or user.isDriver(self.obj.pillar)
            or self.obj.pillar.canAdministerOCIProjects(user)
        )


class EditOCIProjectSeries(DelegatedAuthorization):
    permission = "launchpad.Edit"
    usedfor = IOCIProjectSeries

    def __init__(self, obj):
        super().__init__(obj, obj.oci_project)


def is_commercial_case(obj, user):
    """Is this a commercial project and the user is a commercial admin?"""
    return obj.has_current_commercial_subscription and user.in_commercial_admin
