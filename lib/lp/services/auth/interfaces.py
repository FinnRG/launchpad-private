# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Personal access token interfaces."""

__all__ = [
    "IAccessToken",
    "IAccessTokenSet",
    "IAccessTokenTarget",
    "IAccessTokenVerifiedRequest",
]

from lazr.restful.declarations import (
    REQUEST_USER,
    call_with,
    export_read_operation,
    export_write_operation,
    exported,
    exported_as_webservice_entry,
    operation_for_version,
    operation_parameters,
    operation_returns_collection_of,
)
from lazr.restful.fields import Reference
from zope.interface import Interface
from zope.schema import Bool, Choice, Datetime, Int, List, TextLine

from lp import _
from lp.services.auth.enums import AccessTokenScope
from lp.services.fields import PublicPersonChoice
from lp.services.webservice.apihelpers import patch_reference_property


# XXX cjwatson 2021-10-13 bug=760849: "beta" is a lie to get WADL
# generation working.  Individual attributes must set their version to
# "devel".
@exported_as_webservice_entry(as_of="beta")
class IAccessToken(Interface):
    """A personal access token for the webservice API."""

    id = Int(title=_("ID"), required=True, readonly=True)

    date_created = exported(
        Datetime(
            title=_("Creation date"),
            description=_("When the token was created."),
            required=True,
            readonly=True,
        )
    )

    owner = exported(
        PublicPersonChoice(
            title=_("Owner"),
            description=_("The person who created the token."),
            vocabulary="ValidPersonOrTeam",
            required=True,
            readonly=True,
        )
    )

    description = exported(
        TextLine(
            title=_("Description"),
            description=_("A short description of the token."),
            required=True,
        )
    )

    git_repository = Reference(
        title=_("Git repository"),
        description=_("The Git repository for which the token was issued."),
        # Really IGitRepository, patched in lp.services.auth.webservice.
        schema=Interface,
        required=False,
        readonly=True,
    )

    project = Reference(
        title=_("Project"),
        description=_("The project for which the token was issued."),
        schema=Interface,
        required=False,
        readonly=True,
    )

    target = exported(
        Reference(
            title=_("Target"),
            description=_("The target for which the token was issued."),
            # Really IAccessTokenTarget, patched below.
            schema=Interface,
            required=True,
            readonly=True,
        )
    )

    scopes = exported(
        List(
            value_type=Choice(vocabulary=AccessTokenScope),
            title=_("Scopes"),
            description=_("A list of scopes granted by the token."),
            required=True,
            readonly=True,
        )
    )

    date_last_used = exported(
        Datetime(
            title=_("Date last used"),
            description=_("When the token was last used."),
            required=False,
            readonly=True,
        )
    )

    date_expires = exported(
        Datetime(
            title=_("Expiry date"),
            description=_("When the token should expire or was revoked."),
            required=False,
            readonly=True,
        )
    )

    is_expired = Bool(
        description=_("Whether this token has expired."),
        required=False,
        readonly=True,
    )

    revoked_by = exported(
        PublicPersonChoice(
            title=_("Revoked by"),
            description=_("The person who revoked the token, if any."),
            vocabulary="ValidPersonOrTeam",
            required=False,
            readonly=True,
        )
    )

    def updateLastUsed():
        """Update this token's last-used date, if possible."""

    @call_with(revoked_by=REQUEST_USER)
    @export_write_operation()
    @operation_for_version("devel")
    def revoke(revoked_by):
        """Revoke this token."""


class IAccessTokenSet(Interface):
    """The set of all personal access tokens."""

    def new(secret, owner, description, target, scopes, date_expires=None):
        """Return a new access token with a given secret.

        :param secret: A text string.
        :param owner: An `IPerson` who is creating the token.
        :param description: A short description of the token.
        :param target: An `IAccessTokenTarget` for which the token is being
            issued.
        :param scopes: A list of `AccessTokenScope`s to be granted by the
            token.
        :param date_expires: The time when this token should expire, or
            None.
        """

    def getByID(token_id):
        """Return the access token with this ID, or None.

        :param token_id: An `AccessToken` ID.
        """

    def getBySecret(secret):
        """Return the access token with this secret, or None.

        :param secret: A text string.
        """

    def findByOwner(owner):
        """Return all access tokens for this owner.

        :param owner: An `IPerson`.
        """

    def findByTarget(target, visible_by_user=None, include_expired=False):
        """Return all access tokens for this target.

        :param target: An `IAccessTokenTarget`.
        :param visible_by_user: If given, return only access tokens visible
            by this user.
        :param include_expired: If True, include expired access tokens.
            This must only be used for non-authentication purposes, such as
            deleting database rows.
        """

    def getByTargetAndID(target, token_id, visible_by_user=None):
        """Return the access token with this target and ID, or None.

        :param target: An `IAccessTokenTarget`.
        :param token_id: An `AccessToken` ID.
        :param visible_by_user: If given, return only access tokens visible
            by this user.
        """


class IAccessTokenVerifiedRequest(Interface):
    """Marker interface for a request with a verified access token."""


# XXX cjwatson 2021-10-13 bug=760849: "beta" is a lie to get WADL
# generation working.  Individual attributes must set their version to
# "devel".
@exported_as_webservice_entry(as_of="beta")
class IAccessTokenTarget(Interface):
    """An object that can be a target for access tokens."""

    @operation_parameters(
        description=TextLine(
            title=_("A short description of the token."), required=True
        ),
        scopes=List(
            title=_("A list of scopes to be granted by this token."),
            value_type=Choice(vocabulary=AccessTokenScope),
            required=True,
        ),
        date_expires=Datetime(
            title=_("When the token should expire."), required=False
        ),
    )
    @export_write_operation()
    @operation_for_version("devel")
    def issueAccessToken(description, scopes, date_expires=None):
        """Issue a personal access token for this target.

        Access tokens can be used to push to repositories over HTTPS. These may
        be used in webservice API requests for certain methods in the target's
        repositories.

        They are either non-expiring or with an expiry time given by
        `date_expires`.

        :return: The secret for a new personal access token (Launchpad only
            records the hash of this secret and not the secret itself, so the
            caller must be careful to save this).
        """

    @call_with(visible_by_user=REQUEST_USER)
    @operation_returns_collection_of(IAccessToken)
    @export_read_operation()
    @operation_for_version("devel")
    def getAccessTokens(visible_by_user=None, include_expired=False):
        """Return personal access tokens for this target.

        :param visible_by_user: If given, return only access tokens visible
            by this user.
        :param include_expired: If True, include expired access tokens.
            This must only be used for non-authentication purposes, such as
            deleting database rows.
        """


patch_reference_property(IAccessToken, "target", IAccessTokenTarget)
