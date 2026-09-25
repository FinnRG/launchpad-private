# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Hardware-bound OAuth refresh-token exchange."""

__all__ = [
    "HardwareAuthenticationError",
    "HardwareTokenError",
    "begin_token_exchange",
    "complete_token_exchange",
    "issue_refresh_token",
]

import http.client
import struct
from binascii import Error as BinasciiError
from datetime import datetime, timedelta, timezone

from fido2 import cbor
from fido2.utils import websafe_decode, websafe_encode
from fido2.webauthn import (
    Aaguid,
    AttestedCredentialData,
    AuthenticationResponse,
    UserVerificationRequirement,
)
from lazr.restful.declarations import error_status

from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.registry.interfaces.product import IProduct
from lp.registry.interfaces.projectgroup import IProjectGroup
from lp.services.database.interfaces import IPrimaryStore
from lp.services.oauth.model import (
    ACCESS_TOKEN_LIFETIME,
    MAX_ACCESS_TOKEN_LIFETIME,
    OAuthAuthenticationChallenge,
    OAuthRefreshToken,
    OAuthTokenExpiryError,
    OAuthValidationError,
    create_token_key_and_secret,
    get_token_expiry,
)
from lp.services.webauthn.ceremony import (
    get_server,
    normalize_webauthn_options,
)

REFRESH_TOKEN_LIFETIME = timedelta(days=365)
MAX_REFRESH_TOKEN_LIFETIME = timedelta(days=365)
CHALLENGE_LIFETIME = timedelta(minutes=5)


@error_status(http.client.UNAUTHORIZED)
class HardwareAuthenticationError(Exception):
    """Raised when a hardware-bound OAuth exchange cannot be authorized."""


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


def _get_refresh_token(key, secret):
    if not isinstance(key, str) or not isinstance(secret, str):
        raise HardwareAuthenticationError("Invalid OAuth refresh token.")
    refresh_token = (
        IPrimaryStore(OAuthRefreshToken)
        .find(OAuthRefreshToken, OAuthRefreshToken.key == key)
        .one()
    )
    if (
        refresh_token is None
        or not refresh_token.isSecretValid(secret)
        or not refresh_token.is_usable
    ):
        raise HardwareAuthenticationError("Invalid OAuth refresh token.")
    return refresh_token


def _credential_data(authenticator):
    return AttestedCredentialData.create(
        Aaguid(bytes(authenticator.aaguid)),
        bytes(authenticator.credential_id),
        cbor.decode(bytes(authenticator.public_key)),
    )


def begin_token_exchange(refresh_token_key, refresh_token_secret):
    """Create a persistent challenge for a hardware-bound token exchange."""
    refresh_token = _get_refresh_token(refresh_token_key, refresh_token_secret)
    options, state = get_server().authenticate_begin(
        [_credential_data(refresh_token.authenticator)],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    now = datetime.now(timezone.utc)
    challenge = OAuthAuthenticationChallenge(
        refresh_token,
        websafe_decode(state["challenge"]),
        now + CHALLENGE_LIFETIME,
    )
    store = IPrimaryStore(OAuthAuthenticationChallenge)
    store.add(challenge)
    store.flush()
    return normalize_webauthn_options(dict(options)), challenge.id


def _consume_challenge(refresh_token, challenge_id):
    store = IPrimaryStore(OAuthAuthenticationChallenge)
    result = store.execute(
        """
        UPDATE OAuthAuthenticationChallenge
        SET consumed = TRUE
        WHERE id IN (
            SELECT id
            FROM OAuthAuthenticationChallenge
            WHERE id = ? AND refresh_token = ? AND NOT consumed
                AND date_expires > CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id
        """,
        params=(challenge_id, refresh_token.id),
    )
    if result.get_one() is None:
        raise HardwareAuthenticationError(
            "The authentication challenge is invalid or has expired."
        )
    return store.get(OAuthAuthenticationChallenge, challenge_id)


def _record_assertion(authenticator, sign_count):
    if not authenticator.recordAssertion(sign_count):
        raise HardwareAuthenticationError(
            "The authenticator signature counter did not increase."
        )


def complete_token_exchange(
    refresh_token_key,
    refresh_token_secret,
    challenge_id,
    response,
    date_expires=None,
):
    """Verify an assertion and mint a short-lived OAuth access token."""
    refresh_token = _get_refresh_token(refresh_token_key, refresh_token_secret)
    try:
        date_expires = get_token_expiry(
            date_expires, ACCESS_TOKEN_LIFETIME, MAX_ACCESS_TOKEN_LIFETIME
        )
    except OAuthTokenExpiryError as error:
        raise HardwareTokenError(str(error)) from error
    challenge = _consume_challenge(refresh_token, challenge_id)
    authenticator = refresh_token.authenticator
    # Built from our own stored data, so failures here are not the client's
    # fault and must not be reported as a bad assertion.
    credential_data = _credential_data(authenticator)
    state = {
        "challenge": websafe_encode(bytes(challenge.challenge)),
        "user_verification": UserVerificationRequirement.REQUIRED,
    }
    try:
        authentication = AuthenticationResponse.from_dict(response)
        credential = get_server().authenticate_complete(
            state, [credential_data], authentication
        )
    except (
        AttributeError,
        BinasciiError,
        IndexError,
        struct.error,
        TypeError,
        ValueError,
    ) as error:
        raise HardwareAuthenticationError(
            "Invalid WebAuthn authentication response."
        ) from error
    if credential.credential_id != bytes(authenticator.credential_id):
        raise HardwareAuthenticationError(
            "The assertion used the wrong authenticator."
        )
    _record_assertion(
        authenticator, authentication.response.authenticator_data.counter
    )
    try:
        return refresh_token.mintAccessToken(date_expires=date_expires)
    except OAuthTokenExpiryError as error:
        raise HardwareTokenError(str(error)) from error
    except OAuthValidationError as error:
        raise HardwareAuthenticationError(str(error)) from error
