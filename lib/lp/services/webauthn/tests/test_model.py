# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for registered FIDO2/WebAuthn authenticators."""

import os
import signal
from datetime import datetime, timedelta, timezone

import transaction
from storm.exceptions import IntegrityError
from storm.store import Store
from testtools.matchers import MatchesStructure
from zope.component import getUtility
from zope.interface.verify import verifyObject
from zope.security.proxy import removeSecurityProxy

from lp.services.database.interfaces import IStore
from lp.services.database.sqlbase import (
    disconnect_stores,
    get_transaction_timestamp,
)
from lp.services.webapp.authorization import check_permission
from lp.services.webauthn.interfaces import (
    IRegisteredAuthenticator,
    IRegisteredAuthenticatorSet,
)
from lp.services.webauthn.model import RegisteredAuthenticator
from lp.testing import TestCaseWithFactory, login_person
from lp.testing.layers import DatabaseFunctionalLayer, ZopelessDatabaseLayer


class TestRegisteredAuthenticator(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def test_verify_interface(self):
        authenticator = self.factory.makeRegisteredAuthenticator()
        self.assertTrue(verifyObject(IRegisteredAuthenticator, authenticator))

    def test_new_sets_expected_attributes(self):
        owner = self.factory.makePerson()
        authenticator = self.factory.makeRegisteredAuthenticator(
            owner=owner,
            credential_id=b"credential-id",
            public_key=b"public-key",
            sign_count=1,
            aaguid=b"\x00" * 16,
            attestation_format="packed",
            name="My YubiKey",
        )
        self.assertThat(
            authenticator,
            MatchesStructure.byEquality(
                owner=owner,
                credential_id=b"credential-id",
                public_key=b"public-key",
                sign_count=1,
                aaguid=b"\x00" * 16,
                attestation_format="packed",
                name="My YubiKey",
                date_last_used=None,
                date_revoked=None,
                revoked_by=None,
                is_revoked=False,
            ),
        )
        self.assertIsNotNone(authenticator.date_created)

    def test_credential_id_must_be_unique(self):
        credential_id = b"duplicate-credential-id"
        self.factory.makeRegisteredAuthenticator(credential_id=credential_id)

        def make_conflicting_authenticator():
            self.factory.makeRegisteredAuthenticator(
                credential_id=credential_id
            )
            IStore(RegisteredAuthenticator).flush()

        self.assertRaises(IntegrityError, make_conflicting_authenticator)

    def test_updateLastUsed_never_used(self):
        # If the authenticator has never been used, update its last-used date.
        authenticator = self.factory.makeRegisteredAuthenticator()
        self.assertIsNone(authenticator.date_last_used)
        transaction.commit()
        authenticator.updateLastUsed()
        now = get_transaction_timestamp(Store.of(authenticator))
        self.assertEqual(now, authenticator.date_last_used)

    def test_updateLastUsed_recent(self):
        # If the authenticator's last-used date was updated recently, we
        # leave it alone.
        authenticator = self.factory.makeRegisteredAuthenticator()
        recent = datetime.now(timezone.utc) - timedelta(minutes=1)
        removeSecurityProxy(authenticator).date_last_used = recent
        transaction.commit()
        authenticator.updateLastUsed()
        self.assertEqual(recent, authenticator.date_last_used)

    def test_updateLastUsed_old(self):
        # If the authenticator's last-used date is outside our update
        # resolution, we update it.
        authenticator = self.factory.makeRegisteredAuthenticator()
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        removeSecurityProxy(authenticator).date_last_used = old
        transaction.commit()
        authenticator.updateLastUsed()
        now = get_transaction_timestamp(Store.of(authenticator))
        self.assertEqual(now, authenticator.date_last_used)

    def test_updateLastUsed_concurrent(self):
        # If another transaction locks the authenticator, leave it alone.
        credential_id = b"concurrent-credential-id"
        authenticator = self.factory.makeRegisteredAuthenticator(
            credential_id=credential_id
        )
        self.assertIsNone(authenticator.date_last_used)
        transaction.commit()
        # Fork so that we can lock the token from a different PostgreSQL
        # session.  We must disconnect the Storm store before forking, as
        # libpq connections are not safe for use across forks.
        disconnect_stores()
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            os.close(read)
            authenticator = getUtility(
                IRegisteredAuthenticatorSet
            ).getByCredentialID(credential_id)
            authenticator.updateLastUsed()
            os.write(write, b"1")
            try:
                signal.pause()
            except KeyboardInterrupt:
                pass
            transaction.commit()
            os._exit(0)
        else:  # parent
            try:
                os.close(write)
                os.read(read, 1)
                authenticator = getUtility(
                    IRegisteredAuthenticatorSet
                ).getByCredentialID(credential_id)
                authenticator.updateLastUsed()
                now = get_transaction_timestamp(Store.of(authenticator))
                # The last-used date is being updated by a different
                # transaction, which has not been committed yet.
                self.assertIsNone(authenticator.date_last_used)
            finally:
                os.kill(pid, signal.SIGINT)
                os.waitpid(pid, 0)
            transaction.commit()
            self.assertIsNotNone(authenticator.date_last_used)
            self.assertNotEqual(now, authenticator.date_last_used)

    def test_revoke(self):
        authenticator = self.factory.makeRegisteredAuthenticator()
        revoker = self.factory.makePerson()
        self.assertFalse(authenticator.is_revoked)
        authenticator.revoke(revoker)
        self.assertTrue(authenticator.is_revoked)
        self.assertEqual(revoker, authenticator.revoked_by)
        self.assertIsNotNone(authenticator.date_revoked)


class TestRegisteredAuthenticatorSet(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()
        self.authenticator_set = getUtility(IRegisteredAuthenticatorSet)

    def test_new(self):
        owner = self.factory.makePerson()
        authenticator = self.authenticator_set.new(
            owner,
            b"credential-id",
            b"public-key",
            0,
            b"\x00" * 16,
            "none",
            "My key",
        )
        self.assertEqual(owner, authenticator.owner)
        self.assertEqual(
            authenticator,
            self.authenticator_set.getByCredentialID(b"credential-id"),
        )

    def test_new_rejects_team_owner(self):
        team = self.factory.makeTeam()

        self.assertRaisesRegex(
            ValueError,
            "must be owned by a person",
            self.factory.makeRegisteredAuthenticator,
            owner=team,
        )

    def test_getByCredentialID_not_found(self):
        self.assertIsNone(
            self.authenticator_set.getByCredentialID(b"no-such-credential")
        )

    def test_findByOwner(self):
        owner = self.factory.makePerson()
        authenticators = [
            self.factory.makeRegisteredAuthenticator(owner=owner)
            for _ in range(2)
        ]
        # An authenticator registered by another person is not included.
        self.factory.makeRegisteredAuthenticator()

        self.assertContentEqual(
            authenticators, self.authenticator_set.findByOwner(owner)
        )

    def test_findByOwner_orders_newest_first(self):
        owner = self.factory.makePerson()
        older = self.factory.makeRegisteredAuthenticator(owner=owner)
        newer = self.factory.makeRegisteredAuthenticator(owner=owner)
        removeSecurityProxy(newer).date_created += timedelta(minutes=1)

        self.assertEqual(
            [newer, older], list(self.authenticator_set.findByOwner(owner))
        )


class TestRegisteredAuthenticatorSecurity(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_owner_can_edit(self):
        owner = self.factory.makePerson()
        authenticator = self.factory.makeRegisteredAuthenticator(owner=owner)
        login_person(owner)

        self.assertTrue(check_permission("launchpad.Edit", authenticator))

    def test_team_member_cannot_edit_team_owned_authenticator(self):
        member = self.factory.makePerson()
        team = self.factory.makeTeam(members=[member])
        authenticator = self.factory.makeRegisteredAuthenticator()
        # The model rejects this ownership arrangement, but preserve the
        # authorization boundary if inconsistent legacy data ever exists.
        removeSecurityProxy(authenticator).owner = team
        login_person(member)

        self.assertTrue(member.inTeam(team))
        self.assertFalse(check_permission("launchpad.Edit", authenticator))
