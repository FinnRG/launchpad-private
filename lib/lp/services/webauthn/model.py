# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Model classes for registered FIDO2/WebAuthn authenticators."""

__all__ = [
    "RegisteredAuthenticator",
    "RegisteredAuthenticatorSet",
]

from datetime import timedelta, timezone

from storm.expr import SQL, And, Cast, Desc, Or, Update
from storm.locals import Bytes, DateTime, Int, List, Reference, Unicode
from zope.interface import implementer

from lp.services.database.constants import UTC_NOW
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.stormbase import StormBase
from lp.services.webauthn.interfaces import (
    IRegisteredAuthenticator,
    IRegisteredAuthenticatorSet,
)


@implementer(IRegisteredAuthenticator)
class RegisteredAuthenticator(StormBase):
    """See `IRegisteredAuthenticator`."""

    __storm_table__ = "RegisteredAuthenticator"

    id = Int(primary=True)

    owner_id = Int(name="owner", allow_none=False)
    owner = Reference(owner_id, "Person.id")

    credential_id = Bytes(allow_none=False)
    public_key = Bytes(allow_none=False)
    sign_count = Int(allow_none=False)
    aaguid = Bytes(allow_none=False)
    attestation_format = Unicode(allow_none=False)
    name = Unicode(allow_none=False)
    transports = List(allow_none=True)

    date_created = DateTime(
        tzinfo=timezone.utc, allow_none=False, default=UTC_NOW
    )
    date_last_used = DateTime(
        tzinfo=timezone.utc, allow_none=True, default=None
    )
    date_revoked = DateTime(tzinfo=timezone.utc, allow_none=True, default=None)

    revoked_by_id = Int(name="revoked_by", allow_none=True, default=None)
    revoked_by = Reference(revoked_by_id, "Person.id")

    resolution = timedelta(minutes=10)

    def __init__(
        self,
        owner,
        credential_id,
        public_key,
        sign_count,
        aaguid,
        attestation_format,
        name,
        transports=None,
    ):
        super().__init__()
        self.owner = owner
        self.credential_id = credential_id
        self.public_key = public_key
        self.sign_count = sign_count
        self.aaguid = aaguid
        self.attestation_format = attestation_format
        self.name = name
        self.transports = transports

    @property
    def is_revoked(self):
        """See `IRegisteredAuthenticator`."""
        return self.date_revoked is not None

    def updateLastUsed(self):
        """See `IRegisteredAuthenticator`."""
        store = IPrimaryStore(RegisteredAuthenticator)
        store.execute(
            Update(
                {RegisteredAuthenticator.date_last_used: UTC_NOW},
                where=And(
                    # Skip the update if the RegisteredAuthenticator row is
                    # already locked, for example by a concurrent request
                    # using the same authenticator.
                    RegisteredAuthenticator.id.is_in(
                        SQL(
                            "SELECT id FROM RegisteredAuthenticator "
                            "WHERE id = ? FOR UPDATE SKIP LOCKED",
                            params=(self.id,),
                        )
                    ),
                    # Only update the last-used date every so often, to avoid
                    # bloat.
                    Or(
                        RegisteredAuthenticator.date_last_used == None,
                        RegisteredAuthenticator.date_last_used
                        < UTC_NOW - Cast(self.resolution, "interval"),
                    ),
                ),
                table=RegisteredAuthenticator,
            )
        )
        store.invalidate(self)

    def recordAssertion(self, sign_count):
        """See `IRegisteredAuthenticator`."""
        if sign_count == 0:
            counter_matches = RegisteredAuthenticator.sign_count == 0
        else:
            counter_matches = RegisteredAuthenticator.sign_count < sign_count
        store = IPrimaryStore(RegisteredAuthenticator)
        result = store.execute(
            Update(
                {
                    RegisteredAuthenticator.sign_count: sign_count,
                    RegisteredAuthenticator.date_last_used: UTC_NOW,
                },
                where=And(
                    RegisteredAuthenticator.id == self.id,
                    counter_matches,
                    RegisteredAuthenticator.date_revoked == None,
                ),
                table=RegisteredAuthenticator,
            )
        )
        store.invalidate(self)
        return result.rowcount == 1

    def revoke(self, revoked_by):
        """See `IRegisteredAuthenticator`."""
        self.date_revoked = UTC_NOW
        self.revoked_by = revoked_by


@implementer(IRegisteredAuthenticatorSet)
class RegisteredAuthenticatorSet:
    """See `IRegisteredAuthenticatorSet`."""

    def new(
        self,
        owner,
        credential_id,
        public_key,
        sign_count,
        aaguid,
        attestation_format,
        name,
        transports=None,
    ):
        """See `IRegisteredAuthenticatorSet`."""
        if owner.is_team:
            raise ValueError(
                "Registered authenticators must be owned by a person."
            )
        store = IStore(RegisteredAuthenticator)
        authenticator = RegisteredAuthenticator(
            owner,
            credential_id,
            public_key,
            sign_count,
            aaguid,
            attestation_format,
            name,
            transports=transports,
        )
        store.add(authenticator)
        return authenticator

    def getByID(self, authenticator_id):
        """See `IRegisteredAuthenticatorSet`."""
        return IStore(RegisteredAuthenticator).get(
            RegisteredAuthenticator, authenticator_id
        )

    def getByCredentialID(self, credential_id):
        """See `IRegisteredAuthenticatorSet`."""
        return (
            IStore(RegisteredAuthenticator)
            .find(RegisteredAuthenticator, credential_id=credential_id)
            .one()
        )

    def findByOwner(self, owner):
        """See `IRegisteredAuthenticatorSet`."""
        return (
            IStore(RegisteredAuthenticator)
            .find(RegisteredAuthenticator, owner=owner)
            .order_by(Desc(RegisteredAuthenticator.date_created))
        )
