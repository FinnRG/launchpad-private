# Copyright 2010-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Enums for the Registry app."""

__all__ = [
    "BranchSharingPolicy",
    "BugSharingPolicy",
    "DistributionDefaultTraversalPolicy",
    "DistroSeriesDifferenceStatus",
    "DistroSeriesDifferenceType",
    "ExternalPackageType",
    "EXCLUSIVE_TEAM_POLICY",
    "INCLUSIVE_TEAM_POLICY",
    "PersonTransferJobType",
    "PersonVisibility",
    "PollSort",
    "ProductJobType",
    "VCSType",
    "SharingPermission",
    "SpecificationSharingPolicy",
    "StoreRisk",
    "TeamMembershipPolicy",
    "TeamMembershipRenewalPolicy",
]

from lazr.enum import DBEnumeratedType, DBItem, EnumeratedType, Item


class SharingPermission(DBEnumeratedType):
    """Sharing permission.

    The level of access granted for a particular access policy.
    """

    NOTHING = DBItem(
        1,
        """
        Nothing

        Revoke all bug and branch subscriptions.
        """,
    )

    ALL = DBItem(
        2,
        """
        All

        Share all bugs and branches.
        """,
    )

    SOME = DBItem(
        3,
        """
        Some

        Share bug and branch subscriptions.
        """,
    )


class BranchSharingPolicy(DBEnumeratedType):
    PUBLIC = DBItem(
        1,
        """
        Public

        Bazaar branches and Git repositories are public unless they contain
        sensitive security information.
        """,
    )

    PUBLIC_OR_PROPRIETARY = DBItem(
        2,
        """
        Public, can be proprietary

        New Bazaar branches and Git repositories are public, but can be made
        proprietary later.
        """,
    )

    PROPRIETARY_OR_PUBLIC = DBItem(
        3,
        """
        Proprietary, can be public

        New Bazaar branches and Git repositories are proprietary, but can be
        made public later. Only people who can see the project's proprietary
        information can create new Bazaar branches or Git repositories.
        """,
    )

    PROPRIETARY = DBItem(
        4,
        """
        Proprietary

        Bazaar branches and Git repositories are always proprietary. Only
        people who can see the project's proprietary information can create
        new Bazaar branches or Git repositories.
        """,
    )

    EMBARGOED_OR_PROPRIETARY = DBItem(
        5,
        """
        Embargoed, can be proprietary

        New Bazaar branches and Git repositories are embargoed, but can be
        made proprietary later. Only people who can see the project's
        proprietary information can create new Bazaar branches or Git
        repositories.
        """,
    )

    FORBIDDEN = DBItem(
        6,
        """
        Forbidden

        No new Bazaar branches or Git repositories may be created, but
        existing Bazaar branches and Git repositories may still be updated.
        """,
    )


class BugSharingPolicy(DBEnumeratedType):
    PUBLIC = DBItem(
        1,
        """
        Public

        Bugs are public unless they contain sensitive security
        information.
        """,
    )

    PUBLIC_OR_PROPRIETARY = DBItem(
        2,
        """
        Public, can be proprietary

        New bugs are public, but can be made proprietary later.
        """,
    )

    PROPRIETARY_OR_PUBLIC = DBItem(
        3,
        """
        Proprietary, can be public

        New bugs are proprietary, but can be made public later.
        """,
    )

    PROPRIETARY = DBItem(
        4,
        """
        Proprietary

        Bugs are always proprietary.
        """,
    )

    FORBIDDEN = DBItem(
        5,
        """
        Forbidden

        No new bugs may be reported, but existing bugs may still be updated.
        """,
    )

    EMBARGOED_OR_PROPRIETARY = DBItem(
        6,
        """
        Embargoed, can be proprietary

        New bugs are embargoed, but can be made proprietary later.
        Only people who can see the project's proprietary information can
        create new bugs.
        """,
    )


class SpecificationSharingPolicy(DBEnumeratedType):
    PUBLIC = DBItem(
        1,
        """
        Public

        Blueprints are public.
        """,
    )

    PUBLIC_OR_PROPRIETARY = DBItem(
        2,
        """
        Public, can be proprietary

        New blueprints are public, but can be made proprietary later.
        """,
    )

    PROPRIETARY_OR_PUBLIC = DBItem(
        3,
        """
        Proprietary, can be public

        New blueprints are proprietary, but can be made public later. Only
        people who can see the project's proprietary information can create
        new blueprints.
        """,
    )

    PROPRIETARY = DBItem(
        4,
        """
        Proprietary

        Blueprints are always proprietary. Only people who can see the
        project's proprietary information can create new blueprints.
        """,
    )

    EMBARGOED_OR_PROPRIETARY = DBItem(
        5,
        """
        Embargoed, can be proprietary

        New blueprints are embargoed, but can be made proprietary later.
        Only people who can see the project's proprietary information can
        create new blueprints.
        """,
    )

    FORBIDDEN = DBItem(
        6,
        """
        Forbidden

        No new blueprints may be created, but existing blueprints may
        still be updated.
        """,
    )


class TeamMembershipRenewalPolicy(DBEnumeratedType):
    """TeamMembership Renewal Policy.

    How Team Memberships can be renewed on a given team.
    """

    NONE = DBItem(
        10,
        """
        invite them to apply for renewal

        Memberships can be renewed only by team administrators or by going
        through the normal workflow for joining the team.
        """,
    )

    ONDEMAND = DBItem(
        20,
        """
        invite them to renew their own membership

        Memberships can be renewed by the members themselves a few days before
        it expires. After it expires the member has to go through the normal
        workflow for joining the team.
        """,
    )


class TeamMembershipPolicy(DBEnumeratedType):
    """Team Membership Policies

    The policies that describe who can be a member. The choice of policy
    reflects the need to build a community (inclusive) versus the need to
    control Launchpad projects, branches, and PPAs (exclusive).
    """

    OPEN = DBItem(
        2,
        """
        Open Team

        Membership is inclusive; any user or team can join, and no
        approval is required.
        """,
    )

    DELEGATED = DBItem(
        4,
        """
        Delegated Team

        Membership is inclusive; any user or team can join, but team
        administrators approve direct memberships.
        """,
    )

    MODERATED = DBItem(
        1,
        """
        Moderated Team

        Membership is exclusive; users and exclusive teams may ask to join.
        """,
    )

    RESTRICTED = DBItem(
        3,
        """
        Restricted Team

        Membership is exclusive; team administrators can invite users and
        exclusive teams to join.
        """,
    )


INCLUSIVE_TEAM_POLICY = (
    TeamMembershipPolicy.OPEN,
    TeamMembershipPolicy.DELEGATED,
)


EXCLUSIVE_TEAM_POLICY = (
    TeamMembershipPolicy.RESTRICTED,
    TeamMembershipPolicy.MODERATED,
)


class PersonVisibility(DBEnumeratedType):
    """The visibility level of person or team objects.

    Currently, only teams can have their visibility set to something
    besides PUBLIC.
    """

    PUBLIC = DBItem(
        1,
        """
        Public

        Everyone can view all the attributes of this person.
        """,
    )

    PRIVATE = DBItem(
        30,
        """
        Private

        Only Launchpad admins and team members can view the team's data.
        Other users may only know of the team if it is placed
        in a public relationship such as subscribing to a bug.
        """,
    )


class DistroSeriesDifferenceStatus(DBEnumeratedType):
    """Distribution series difference status.

    The status of a package difference between two DistroSeries.
    """

    NEEDS_ATTENTION = DBItem(
        1,
        """
        Needs attention

        This difference is current and needs attention.
        """,
    )

    BLOCKLISTED_CURRENT = DBItem(
        2,
        """
        Blocklisted current version

        This difference is being ignored until a new package is uploaded
        or the status is manually updated.
        """,
    )

    BLOCKLISTED_ALWAYS = DBItem(
        3,
        """
        Blocklisted always

        This difference should always be ignored.
        """,
    )

    RESOLVED = DBItem(
        4,
        """
        Resolved

        This difference has been resolved and versions are now equal.
        """,
    )


class DistroSeriesDifferenceType(DBEnumeratedType):
    """Distribution series difference type."""

    UNIQUE_TO_DERIVED_SERIES = DBItem(
        1,
        """
        Unique to derived series

        This package is present in the derived series but not the parent
        series.
        """,
    )

    MISSING_FROM_DERIVED_SERIES = DBItem(
        2,
        """
        Missing from derived series

        This package is present in the parent series but missing from the
        derived series.
        """,
    )

    DIFFERENT_VERSIONS = DBItem(
        3,
        """
        Different versions

        This package is present in both series with different versions.
        """,
    )


class PersonTransferJobType(DBEnumeratedType):
    """Values that IPersonTransferJob.job_type can take."""

    MEMBERSHIP_NOTIFICATION = DBItem(
        0,
        """
        Add-member notification

        Notify affected users of new team membership.
        """,
    )

    MERGE = DBItem(
        1,
        """
        Person merge

        Merge one person or team into another person or team.
        """,
    )

    DEACTIVATE = DBItem(
        2,
        """
        Deactivate person

        Do the work to deactivate a person, like reassigning bugs and removing
        the user from teams.
        """,
    )

    TEAM_INVITATION_NOTIFICATION = DBItem(
        3,
        """
        Notification of invitation to join team

        Notify team admins that the team has been invited to join another
        team.
        """,
    )

    TEAM_JOIN_NOTIFICATION = DBItem(
        4,
        """
        Notification of new member joining team

        Notify that a new member has been added to a team.
        """,
    )

    EXPIRING_MEMBERSHIP_NOTIFICATION = DBItem(
        5,
        """
        Notification of expiring membership

        Notify a member that their membership of a team is about to expire.
        """,
    )

    SELF_RENEWAL_NOTIFICATION = DBItem(
        6,
        """
        Notification of self-renewal

        Notify team admins that a member renewed their own membership.
        """,
    )

    CLOSE_ACCOUNT = DBItem(
        7,
        """Close account.

        Close account for a given username.
        """,
    )


class ProductJobType(DBEnumeratedType):
    """Values that IProductJob.job_type can take."""

    REVIEWER_NOTIFICATION = DBItem(
        0,
        """
        Reviewer notification

        A notification sent by a project reviewer to the project maintainers.
        """,
    )

    COMMERCIAL_EXPIRATION_30_DAYS = DBItem(
        1,
        """
        Commercial subscription expires in 30 days.

        A notification stating that the project's commercial subscription
        expires in 30 days.
        """,
    )

    COMMERCIAL_EXPIRATION_7_DAYS = DBItem(
        2,
        """
        Commercial subscription expires in 7 days.

        A notification stating that the project's commercial subscription
        expires in 7 days.
        """,
    )

    COMMERCIAL_EXPIRED = DBItem(
        3,
        """
        Commercial subscription expired.

        A notification stating that the project's commercial subscription
        expired.
        """,
    )


class VCSType(DBEnumeratedType):
    """Values that IProduct.vcs and IDistribution.vcs can take."""

    BZR = DBItem(
        0,
        """
        Bazaar

        The Bazaar DVCS is used as the default project or distribution VCS.
        """,
    )

    GIT = DBItem(
        1,
        """
        Git

        The Git DVCS is used as the default project or distribution VCS.
        """,
    )


class DistributionDefaultTraversalPolicy(DBEnumeratedType):
    """Policy for the default traversal from a distribution.

    This determines what the "name" segment in a URL such as
    "/{distro}/{name}" (with no intervening segment such as "+source")
    means.
    """

    SERIES = DBItem(
        0,
        """
        Series

        The default traversal from a distribution is used for series of that
        distribution.
        """,
    )

    SOURCE_PACKAGE = DBItem(
        1,
        """
        Source package

        The default traversal from a distribution is used for source
        packages in that distribution.
        """,
    )

    OCI_PROJECT = DBItem(
        2,
        """
        OCI project

        The default traversal from a distribution is used for OCI projects
        in that distribution.
        """,
    )


class PollSort(EnumeratedType):
    """Choices for how to sort polls."""

    OLDEST_FIRST = Item(
        """
        oldest first

        Sort polls from oldest to newest.
        """
    )

    NEWEST_FIRST = Item(
        """
        newest first

        Sort polls from newest to oldest.
        """
    )

    OPENING = Item(
        """
        by opening date

        Sort polls with the earliest opening date first.
        """
    )

    CLOSING = Item(
        """
        by closing date

        Sort polls with the earliest closing date first.
        """
    )


class StoreRisk(EnumeratedType):
    """The "risk" element of a channel in one of Canonical's stores.

    The snap store, Charmhub, and other similar stores operated by Canonical
    have a model where a "channel" consists of a track, a risk, and a
    branch.  The track and branch elements are both somewhat free-form (the
    track functions a little like a major version number in semver, while
    the branch is for short-lived hotfixes), but the risk element is from a
    fixed vocabulary, modelled here.
    """

    STABLE = Item("stable", "Stable")
    CANDIDATE = Item("candidate", "Candidate")
    BETA = Item("beta", "Beta")
    EDGE = Item("edge", "Edge")


class ExternalPackageType(DBEnumeratedType):
    """ExternalPackageType

    The various possible types for an ExternalPackage.
    """

    UNKNOWN = DBItem(
        0,
        """
        Unknown

        Unknown external package
        """,
    )

    GENERIC = DBItem(
        1,
        """
        Generic

        Generic external package
        """,
    )

    SNAP = DBItem(
        2,
        """
        Snap

        Snap external package
        """,
    )

    CHARM = DBItem(
        3,
        """
        Charm

        Charm external package
        """,
    )

    ROCK = DBItem(
        4,
        """
        Rock

        Rock external package
        """,
    )

    PYTHON = DBItem(
        5,
        """
        Python

        Python external package
        """,
    )

    CONDA = DBItem(
        6,
        """
        Conda

        Conda external package
        """,
    )

    CARGO = DBItem(
        7,
        """
        Cargo

        Cargo external package
        """,
    )

    MAVEN = DBItem(
        8,
        """
        Maven

        Maven external package
        """,
    )
