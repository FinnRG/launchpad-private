# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interfaces for FIDO2/WebAuthn authenticators registered by a person."""

__all__ = [
    "IRegisteredAuthenticator",
    "IRegisteredAuthenticatorSet",
]

from zope.interface import Interface
from zope.schema import Bool, Bytes, Datetime, Int, List, Object, TextLine

from lp import _
from lp.registry.interfaces.person import IPerson


class IRegisteredAuthenticator(Interface):
    """A FIDO2/WebAuthn authenticator registered by a person.

    Used to hardware-bind a person's OAuth refresh tokens.
    """

    id = Int(title=_("ID"), required=True, readonly=True)

    owner = Object(
        schema=IPerson,
        title=_("Owner"),
        description=_("The person who registered this authenticator."),
        required=True,
        readonly=True,
    )

    credential_id = Bytes(
        title=_("Credential ID"),
        description=_(
            "The credential ID assigned to this authenticator by itself."
        ),
        required=True,
        readonly=True,
    )

    public_key = Bytes(
        title=_("Public key"),
        description=_("The COSE-encoded public key for this authenticator."),
        required=True,
        readonly=True,
    )

    sign_count = Int(
        title=_("Signature counter"),
        description=_(
            "The last signature counter value seen from this "
            "authenticator, used to detect cloned authenticators."
        ),
        required=True,
        readonly=True,
    )

    aaguid = Bytes(
        title=_("AAGUID"),
        description=_(
            "The AAGUID identifying the make and model of this "
            "authenticator."
        ),
        required=True,
        readonly=True,
    )

    attestation_format = TextLine(
        title=_("Attestation format"),
        description=_(
            "The attestation statement format used when this "
            "authenticator was registered."
        ),
        required=True,
        readonly=True,
    )

    transports = List(
        value_type=TextLine(),
        title=_("Transports"),
        description=_(
            "Transport hints (e.g. 'usb', 'nfc') reported by the "
            "authenticator at registration time."
        ),
        required=False,
        readonly=True,
    )

    name = TextLine(
        title=_("Name"),
        description=_(
            "A name chosen by the owner to help them tell their "
            "registered authenticators apart."
        ),
        required=True,
        readonly=False,
    )

    date_created = Datetime(
        title=_("Date created"),
        description=_("When this authenticator was registered."),
        required=True,
        readonly=True,
    )

    date_last_used = Datetime(
        title=_("Date last used"),
        description=_(
            "When this authenticator was last used to sign a challenge."
        ),
        required=False,
        readonly=True,
    )

    date_revoked = Datetime(
        title=_("Date revoked"),
        description=_("When this authenticator was revoked, if it has been."),
        required=False,
        readonly=True,
    )

    revoked_by = Object(
        schema=IPerson,
        title=_("Revoked by"),
        description=_("The person who revoked this authenticator, if any."),
        required=False,
        readonly=True,
    )

    is_revoked = Bool(
        title=_("Revoked?"),
        description=_("Whether this authenticator has been revoked."),
        required=False,
        readonly=True,
    )

    def updateLastUsed():
        """Update this authenticator's last-used date, if possible."""

    def revoke(revoked_by):
        """Revoke this authenticator.

        :param revoked_by: The `IPerson` revoking this authenticator.
        """


class IRegisteredAuthenticatorSet(Interface):
    """The set of all registered FIDO2/WebAuthn authenticators."""

    def new(
        owner,
        credential_id,
        public_key,
        sign_count,
        aaguid,
        attestation_format,
        name,
        transports=None,
    ):
        """Register a new authenticator.

        :param owner: The `IPerson` registering the authenticator.
        :param credential_id: The credential ID, as bytes.
        :param public_key: The COSE-encoded public key, as bytes.
        :param sign_count: The authenticator's initial signature counter.
        :param aaguid: The authenticator's AAGUID, as bytes.
        :param attestation_format: The attestation statement format used.
        :param name: A name chosen by the owner for this authenticator.
        :param transports: An optional list of transport hints (e.g.
            'usb', 'nfc') reported by the authenticator.
        """

    def getByID(authenticator_id):
        """Return the registered authenticator with this ID, or None.

        :param authenticator_id: A `RegisteredAuthenticator` ID.
        """

    def getByCredentialID(credential_id):
        """Return the registered authenticator with this credential ID.

        :param credential_id: A credential ID, as bytes.
        :return: An `IRegisteredAuthenticator`, or None if there is no
            authenticator registered with this credential ID.
        """

    def findByOwner(owner):
        """Return all authenticators registered by this person.

        :param owner: An `IPerson`.
        :return: A `ResultSet` of `IRegisteredAuthenticator`, most
            recently registered first.
        """
