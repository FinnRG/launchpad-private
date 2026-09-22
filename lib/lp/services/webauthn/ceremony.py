# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""The FIDO2/WebAuthn registration ceremony.

This wires `fido2`'s `Fido2Server` to Launchpad's server-side session
storage to implement the two-call WebAuthn registration handshake
(`navigator.credentials.create()`, or an equivalent non-browser client):

- `begin_registration` starts the ceremony and stashes the challenge state
  in the session, keyed by the person, mirroring how
  `lp.services.webapp.candid` stashes its own short-lived state.
- `complete_registration` verifies the client's response against that
  state and persists a new `RegisteredAuthenticator`.
"""

__all__ = [
    "WebAuthnError",
    "begin_registration",
    "complete_registration",
    "get_origin",
    "get_rp_id",
    "get_server",
    "normalize_webauthn_options",
]

import http.client
import struct
from binascii import Error as BinasciiError
from datetime import datetime, timedelta
from enum import Enum

from fido2 import cbor, features
from fido2.server import Fido2Server
from fido2.webauthn import (
    AttestationConveyancePreference,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialRpEntity,
    PublicKeyCredentialType,
    PublicKeyCredentialUserEntity,
    RegistrationResponse,
    UserVerificationRequirement,
)
from lazr.restful.declarations import error_status
from lazr.restful.utils import get_current_browser_request
from zope.component import getUtility
from zope.security.proxy import isinstance as zope_isinstance

from lp.services.webapp.interfaces import ISession
from lp.services.webapp.vhosts import allvhosts
from lp.services.webauthn.interfaces import IRegisteredAuthenticatorSet

# We always want JSON-safe (base64url)
# values, both for our own webservice API and because `RegistrationResponse`
# refuses to be used at all otherwise.
features.webauthn_json_mapping.enabled = True

SESSION_PKG_KEY = "launchpad.webauthn"
CHALLENGE_LIFETIME = timedelta(minutes=5)


@error_status(http.client.BAD_REQUEST)
class WebAuthnError(Exception):
    """Raised when a WebAuthn ceremony fails."""


def get_rp_id():
    """Return the WebAuthn Relying Party ID: Launchpad's main hostname."""
    return allvhosts.configs["mainsite"].hostname


def get_origin():
    """Return the origin Launchpad's WebAuthn ceremonies run under."""
    return allvhosts.configs["mainsite"].rooturl.rstrip("/")


def get_server():
    rp_id = get_rp_id()
    expected_origin = get_origin()
    return Fido2Server(
        PublicKeyCredentialRpEntity(id=rp_id, name="Launchpad"),
        attestation=AttestationConveyancePreference.NONE,
        verify_origin=lambda origin: origin == expected_origin,
    )


def _get_session(request):
    if request is None:
        request = get_current_browser_request()
    if request is None:
        raise WebAuthnError("No request in context to store session state.")
    return ISession(request)[SESSION_PKG_KEY]


def normalize_webauthn_options(value):
    """Return `value` with fido2's enums reduced to plain strings."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            key: normalize_webauthn_options(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [normalize_webauthn_options(item) for item in value]
    return value


def _valid_transports(transports):
    """Return recognized authenticator transport strings."""
    if not zope_isinstance(transports, (list, tuple)):
        return None
    valid_transports = []
    for transport in transports:
        if not isinstance(transport, str):
            continue
        parsed_transport = AuthenticatorTransport(transport)
        if parsed_transport is not None:
            valid_transports.append(parsed_transport.value)
    return valid_transports


def begin_registration(person, request=None):
    """Start a WebAuthn registration ceremony for `person`.

    :param request: The current browser request, used to store the
        challenge state in the session.
    :return: A JSON-safe dict of `PublicKeyCredentialCreationOptions`,
        to be passed to `navigator.credentials.create()`
        (or an equivalent client).
    """
    session_data = _get_session(request)
    existing_credentials = [
        PublicKeyCredentialDescriptor(
            PublicKeyCredentialType.PUBLIC_KEY,
            authenticator.credential_id,
            transports=_valid_transports(authenticator.transports),
        )
        for authenticator in getUtility(
            IRegisteredAuthenticatorSet
        ).findByOwner(person)
        if not authenticator.is_revoked
    ]
    user = PublicKeyCredentialUserEntity(
        id=str(person.id).encode(),
        name=person.name,
        display_name=person.displayname,
    )
    options, state = get_server().register_begin(
        user,
        credentials=existing_credentials,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    session_data["registration"] = {
        "state": state,
        "person_id": person.id,
        # Naive UTC: the session pickling shim in pgsession.py cannot
        # unpickle timezone-aware datetimes.
        "expires": datetime.utcnow() + CHALLENGE_LIFETIME,
    }
    return normalize_webauthn_options(dict(options))


def complete_registration(person, response, name, request=None):
    """Finish a WebAuthn registration ceremony for `person`.

    :param response: A JSON-safe dict of `RegistrationResponseJSON`, as
        produced by `navigator.credentials.create()` (or an equivalent
        client) in response to the options from `begin_registration`.
    :param name: A name for the new authenticator, to help its owner tell
        their registered authenticators apart.
    :param request: The current browser request.
    :return: The newly created `IRegisteredAuthenticator`.
    """
    session_data = _get_session(request)
    registration = session_data.get("registration")
    if registration is None:
        raise WebAuthnError("No WebAuthn registration is in progress.")
    # The challenge is single-use: discard it before we even attempt to
    # verify the response, regardless of the outcome.
    del session_data["registration"]
    if registration["person_id"] != person.id:
        raise WebAuthnError(
            "This registration was started by a different person."
        )
    if datetime.utcnow() > registration["expires"]:
        raise WebAuthnError("This registration challenge has expired.")

    try:
        registration_response = RegistrationResponse.from_dict(response)
    except (
        AttributeError,
        BinasciiError,
        IndexError,
        struct.error,
        TypeError,
        ValueError,
    ) as e:
        raise WebAuthnError("Invalid WebAuthn registration response.") from e

    attestation_object = registration_response.response.attestation_object
    credential_data = attestation_object.auth_data.credential_data
    if credential_data is None:
        raise WebAuthnError("Attested credential data is missing.")

    try:
        auth_data = get_server().register_complete(
            registration["state"], registration_response
        )
    except (TypeError, ValueError) as e:
        raise WebAuthnError(str(e)) from e

    authenticator_set = getUtility(IRegisteredAuthenticatorSet)
    if authenticator_set.getByCredentialID(credential_data.credential_id):
        raise WebAuthnError("This authenticator is already registered.")

    # fido2's WebAuthn dataclasses don't model the "transports" member, so
    # pull it directly from the raw response rather than from
    # `registration_response`.
    transports = _valid_transports(
        response.get("response", {}).get("transports")
    )

    return authenticator_set.new(
        person,
        credential_data.credential_id,
        cbor.encode(credential_data.public_key),
        auth_data.counter,
        bytes(credential_data.aaguid),
        attestation_object.fmt,
        name,
        transports=transports,
    )
