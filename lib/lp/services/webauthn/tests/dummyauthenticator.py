# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""A software FIDO2 authenticator, for exercising ceremonies in tests."""

__all__ = [
    "DummyAuthenticator",
]

import os

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fido2.cose import ES256
from fido2.utils import sha256, websafe_decode
from fido2.webauthn import (
    Aaguid,
    AttestationObject,
    AttestedCredentialData,
    AuthenticationResponse,
    AuthenticatorAssertionResponse,
    AuthenticatorAttestationResponse,
    AuthenticatorData,
    CollectedClientData,
    PublicKeyCredentialType,
    RegistrationResponse,
)

# Ensures fido2's JSON (base64url) Mapping encoding is enabled; see
# lp.services.webauthn.ceremony.
import lp.services.webauthn.ceremony  # noqa: F401


class DummyAuthenticator:
    """A minimal software FIDO2 authenticator.

    This emulates just enough of `navigator.credentials.create()` to drive
    the registration ceremony in tests, without needing real hardware. Only
    "none" attestation is produced, which is all that
    `lp.services.webauthn.ceremony` at the moment requires.
    """

    def __init__(self, aaguid=None):
        self.aaguid = Aaguid(aaguid or b"\x00" * 16)
        # Maps credential_id to EllipticCurvePrivateKey
        self.credentials = {}
        self.sign_counts = {}

    def create(self, options, origin):
        """Emulate `navigator.credentials.create()`.

        :param options: A JSON-safe dict of `CredentialCreationOptions`,
            as returned by `lp.services.webauthn.ceremony.begin_registration`.
        :param origin: The origin to embed in the generated client data.
        :return: A JSON-safe dict of `RegistrationResponseJSON`, suitable
            for passing to `complete_registration`.
        """
        public_key_options = options["publicKey"]
        rp_id = public_key_options["rp"]["id"]
        challenge = public_key_options["challenge"]

        private_key = ec.generate_private_key(
            ec.SECP256R1(), default_backend()
        )
        credential_id = os.urandom(32)
        self.credentials[credential_id] = private_key
        self.sign_counts[credential_id] = 0

        credential_data = AttestedCredentialData.create(
            self.aaguid,
            credential_id,
            ES256.from_cryptography_key(private_key.public_key()),
        )
        auth_data = AuthenticatorData.create(
            sha256(rp_id.encode()),
            AuthenticatorData.FLAG.UP
            | AuthenticatorData.FLAG.UV
            | AuthenticatorData.FLAG.AT,
            0,
            credential_data,
        )
        attestation_object = AttestationObject.create("none", auth_data, {})
        client_data = CollectedClientData.create(
            type=CollectedClientData.TYPE.CREATE,
            challenge=challenge,
            origin=origin,
        )
        response = RegistrationResponse(
            id=credential_id,
            response=AuthenticatorAttestationResponse(
                client_data=client_data,
                attestation_object=attestation_object,
            ),
            type=PublicKeyCredentialType.PUBLIC_KEY,
        )
        return dict(response)

    def get(self, options, origin):
        """Emulate `navigator.credentials.get()`."""
        public_key_options = options["publicKey"]
        credential_id = websafe_decode(
            public_key_options["allowCredentials"][0]["id"]
        )
        private_key = self.credentials[credential_id]
        self.sign_counts[credential_id] += 1
        auth_data = AuthenticatorData.create(
            sha256(public_key_options["rpId"].encode()),
            AuthenticatorData.FLAG.UP | AuthenticatorData.FLAG.UV,
            self.sign_counts[credential_id],
        )
        client_data = CollectedClientData.create(
            type=CollectedClientData.TYPE.GET,
            challenge=public_key_options["challenge"],
            origin=origin,
        )
        signature = private_key.sign(
            auth_data + client_data.hash, ec.ECDSA(hashes.SHA256())
        )
        response = AuthenticationResponse(
            id=credential_id,
            response=AuthenticatorAssertionResponse(
                client_data=client_data,
                authenticator_data=auth_data,
                signature=signature,
            ),
            type=PublicKeyCredentialType.PUBLIC_KEY,
        )
        return dict(response)
