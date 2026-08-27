# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for IPerson's WebAuthn registration ceremony."""

import json
from datetime import timedelta

from zope.component import getUtility
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.services.webapp.servers import LaunchpadTestRequest
from lp.services.webauthn.ceremony import WebAuthnError, get_origin
from lp.services.webauthn.interfaces import IRegisteredAuthenticatorSet
from lp.services.webauthn.tests.dummyauthenticator import DummyAuthenticator
from lp.testing import TestCaseWithFactory, login_person
from lp.testing.layers import DatabaseFunctionalLayer


class TestPersonWebAuthnRegistration(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_full_ceremony(self):
        person = self.factory.makePerson()
        login_person(person, participation=LaunchpadTestRequest())

        options = person.beginWebAuthnRegistration()
        response = DummyAuthenticator().create(options, get_origin())
        authenticator_id = person.completeWebAuthnRegistration(
            json.dumps(response), "My key"
        )

        authenticator = getUtility(IRegisteredAuthenticatorSet).getByID(
            authenticator_id
        )
        self.assertEqual(person, authenticator.owner)
        self.assertEqual("My key", authenticator.name)

    def test_revoke(self):
        person = self.factory.makePerson()
        login_person(person, participation=LaunchpadTestRequest())

        options = person.beginWebAuthnRegistration()
        response = DummyAuthenticator().create(options, get_origin())
        authenticator_id = person.completeWebAuthnRegistration(
            json.dumps(response), "My key"
        )

        person.revokeWebAuthnRegistration(authenticator_id)

        authenticator = getUtility(IRegisteredAuthenticatorSet).getByID(
            authenticator_id
        )
        self.assertTrue(authenticator.is_revoked)
        self.assertEqual(person, authenticator.revoked_by)

    def test_revoke_someone_elses_authenticator(self):
        owner = self.factory.makePerson()
        authenticator = self.factory.makeRegisteredAuthenticator(owner=owner)
        authenticator_id = removeSecurityProxy(authenticator).id
        other_person = self.factory.makePerson()
        login_person(other_person)

        self.assertRaises(
            WebAuthnError,
            other_person.revokeWebAuthnRegistration,
            authenticator_id,
        )

    def test_admin_cannot_register_for_other_user(self):
        # Unlike most IPerson.Edit operations, WebAuthn registration is
        # launchpad.Special: even admins cannot enroll a key on someone
        # else's account (matching the precedent for SSH keys).
        person = self.factory.makePerson()
        admin = self.factory.makeAdministrator()
        login_person(admin)

        self.assertRaises(
            Unauthorized, lambda: person.beginWebAuthnRegistration()
        )

    def test_admin_cannot_revoke_for_other_user(self):
        person = self.factory.makePerson()
        authenticator = self.factory.makeRegisteredAuthenticator(owner=person)
        authenticator_id = removeSecurityProxy(authenticator).id
        admin = self.factory.makeAdministrator()
        login_person(admin)

        self.assertRaises(
            Unauthorized,
            lambda: person.revokeWebAuthnRegistration(authenticator_id),
        )

    def test_getRegisteredAuthenticators(self):
        person = self.factory.makePerson()
        login_person(person, participation=LaunchpadTestRequest())
        older = self.factory.makeRegisteredAuthenticator(
            owner=person, name="Older key"
        )
        newer = self.factory.makeRegisteredAuthenticator(
            owner=person, name="Newer key"
        )
        removeSecurityProxy(newer).date_created += timedelta(minutes=1)
        # An authenticator registered by another person is not included.
        self.factory.makeRegisteredAuthenticator()

        self.assertEqual(
            ["Newer key", "Older key"],
            [d["name"] for d in person.getRegisteredAuthenticators()],
        )
        self.assertEqual(
            {older.id, newer.id},
            {d["id"] for d in person.getRegisteredAuthenticators()},
        )
