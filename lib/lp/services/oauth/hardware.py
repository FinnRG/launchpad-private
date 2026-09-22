# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Hardware-bound OAuth refresh-token exchange."""

__all__ = ["HardwareTokenError", "issue_refresh_token"]

import http.client
from datetime import timedelta

from lazr.restful.declarations import error_status

from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.registry.interfaces.product import IProduct
from lp.registry.interfaces.projectgroup import IProjectGroup
from lp.services.database.interfaces import IPrimaryStore
from lp.services.oauth.model import (
    OAuthRefreshToken,
    OAuthTokenExpiryError,
    create_token_key_and_secret,
    get_token_expiry,
)

REFRESH_TOKEN_LIFETIME = timedelta(days=365)
MAX_REFRESH_TOKEN_LIFETIME = timedelta(days=365)


@error_status(http.client.BAD_REQUEST)
class HardwareTokenError(Exception):
    """Raised when a hardware-bound OAuth token cannot be issued."""


def _context_arguments(context):
    arguments = {
        "product": None,
        "projectgroup": None,
        "distribution": None,
        "sourcepackagename": None,
    }
    if IProduct.providedBy(context):
        arguments["product"] = context
    elif IProjectGroup.providedBy(context):
        arguments["projectgroup"] = context
    elif IDistribution.providedBy(context):
        arguments["distribution"] = context
    elif IDistributionSourcePackage.providedBy(context):
        arguments["distribution"] = context.distribution
        arguments["sourcepackagename"] = context.sourcepackagename
    elif context is not None:
        raise HardwareTokenError("Unknown OAuth context: %r" % context)
    return arguments


def issue_refresh_token(
    person,
    consumer,
    authenticator,
    permission,
    context=None,
    date_expires=None,
):
    """Issue a long-lived OAuth refresh token bound to an authenticator."""
    if person.is_team:
        raise HardwareTokenError(
            "OAuth refresh tokens must be owned by a person."
        )
    if authenticator.owner != person or authenticator.is_revoked:
        raise HardwareTokenError(
            "The authenticator is not active for this person."
        )
    if consumer.disabled:
        raise HardwareTokenError("The OAuth consumer is disabled.")
    try:
        date_expires = get_token_expiry(
            date_expires, REFRESH_TOKEN_LIFETIME, MAX_REFRESH_TOKEN_LIFETIME
        )
    except OAuthTokenExpiryError as error:
        raise HardwareTokenError(str(error)) from error
    key, secret = create_token_key_and_secret(table=OAuthRefreshToken)
    refresh_token = OAuthRefreshToken(
        consumer,
        person,
        authenticator,
        permission,
        key,
        secret,
        date_expires,
        **_context_arguments(context),
    )
    IPrimaryStore(OAuthRefreshToken).add(refresh_token)
    return refresh_token, secret
