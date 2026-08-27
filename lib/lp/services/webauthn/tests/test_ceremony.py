# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the WebAuthn registration ceremony."""

from datetime import datetime, timedelta

from fido2.utils import sha256, websafe_encode
from fido2.webauthn import AttestationObject, AuthenticatorData
from zope.component import getUtility

from lp.services.webapp.interfaces import ISession
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.services.webauthn.ceremony import (
    SESSION_PKG_KEY,
    WebAuthnError,
    begin_registration,
    complete_registration,
    get_origin,
)
from lp.services.webauthn.interfaces import IRegisteredAuthenticatorSet
from lp.services.webauthn.tests.dummyauthenticator import DummyAuthenticator
from lp.testing import TestCaseWithFactory, login_person
from lp.testing.layers import DatabaseFunctionalLayer


class TestWebAuthnRegistration(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def _begin_registration(self, person=None):
        if person is None:
            person = self.factory.makePerson()
        request = LaunchpadTestRequest()
        login_person(person, participation=request)
        options = begin_registration(person, request=request)
        return person, request, options

    def test_register_new_authenticator(self):
        person, request, options = self._begin_registration()

        response = DummyAuthenticator().create(options, get_origin())
        authenticator = complete_registration(
            person, response, "My key", request=request
        )

        self.assertEqual(person, authenticator.owner)
        self.assertEqual("My key", authenticator.name)
        self.assertEqual(0, authenticator.sign_count)
        self.assertEqual("none", authenticator.attestation_format)
        self.assertFalse(authenticator.is_revoked)
        self.assertEqual(
            authenticator,
            getUtility(IRegisteredAuthenticatorSet).getByCredentialID(
                authenticator.credential_id
            ),
        )

    def test_complete_rejects_expired_challenge_and_consumes_it(self):
        person, request, options = self._begin_registration()
        response = DummyAuthenticator().create(options, get_origin())
        session_data = ISession(request)[SESSION_PKG_KEY]
        registration = session_data["registration"]
        registration["expires"] = datetime.utcnow() - timedelta(seconds=1)
        session_data["registration"] = registration

        self.assertRaisesRegex(
            WebAuthnError,
            "This registration challenge has expired.",
            complete_registration,
            person,
            response,
            "My key",
            request=request,
        )
        self.assertNotIn("registration", ISession(request)[SESSION_PKG_KEY])
        self.assertRaisesRegex(
            WebAuthnError,
            "No WebAuthn registration is in progress.",
            complete_registration,
            person,
            response,
            "My key",
            request=request,
        )

    def test_complete_rejects_wrong_origin_scheme(self):
        person, request, options = self._begin_registration()
        origin = get_origin()
        self.assertTrue(origin.startswith("http://"))
        wrong_origin = "https://" + origin[len("http://") :]
        response = DummyAuthenticator().create(options, wrong_origin)

        self.assertRaises(
            WebAuthnError,
            complete_registration,
            person,
            response,
            "My key",
            request=request,
        )

    def test_complete_rejects_wrong_origin_port(self):
        person, request, options = self._begin_registration()
        response = DummyAuthenticator().create(options, get_origin() + ":8443")

        self.assertRaises(
            WebAuthnError,
            complete_registration,
            person,
            response,
            "My key",
            request=request,
        )

    def test_complete_malformed_response_is_bad_request(self):
        person, request, _ = self._begin_registration()

        self.assertRaises(
            WebAuthnError,
            complete_registration,
            person,
            [],
            "My key",
            request=request,
        )

    def test_complete_rejects_missing_credential_data(self):
        person, request, options = self._begin_registration()
        response = DummyAuthenticator().create(options, get_origin())
        rp_id = options["publicKey"]["rp"]["id"]
        auth_data = AuthenticatorData.create(
            sha256(rp_id.encode()),
            AuthenticatorData.FLAG.UP | AuthenticatorData.FLAG.UV,
            0,
        )
        response["response"]["attestationObject"] = websafe_encode(
            AttestationObject.create("none", auth_data, {})
        )

        self.assertRaisesRegex(
            WebAuthnError,
            "Attested credential data is missing.",
            complete_registration,
            person,
            response,
            "My key",
            request=request,
        )

    def test_complete_rejects_empty_attestation_object(self):
        person, request, options = self._begin_registration()
        response = DummyAuthenticator().create(options, get_origin())
        response["response"]["attestationObject"] = ""

        self.assertRaises(
            WebAuthnError,
            complete_registration,
            person,
            response,
            "My key",
            request=request,
        )

    def test_complete_filters_invalid_transports(self):
        person, request, options = self._begin_registration()
        response = DummyAuthenticator().create(options, get_origin())
        response["response"]["transports"] = [
            "usb",
            "hybrid",
            "invalid",
            1,
        ]

        authenticator = complete_registration(
            person, response, "My key", request=request
        )

        self.assertEqual(["usb", "hybrid"], authenticator.transports)

    def test_complete_ignores_non_list_transports(self):
        person, request, options = self._begin_registration()
        response = DummyAuthenticator().create(options, get_origin())
        response["response"]["transports"] = "usb"

        authenticator = complete_registration(
            person, response, "My key", request=request
        )

        self.assertIsNone(authenticator.transports)

    def test_begin_filters_invalid_stored_transports(self):
        person = self.factory.makePerson()
        existing = self.factory.makeRegisteredAuthenticator(
            owner=person,
            transports=["usb", "invalid"],
        )

        _, _, options = self._begin_registration(person)

        [excluded] = options["publicKey"]["excludeCredentials"]
        self.assertEqual(
            websafe_encode(bytes(existing.credential_id)), excluded["id"]
        )
        self.assertEqual(["usb"], excluded["transports"])

    def test_complete_without_begin(self):
        person = self.factory.makePerson()
        request = LaunchpadTestRequest()
        login_person(person, participation=request)

        self.assertRaises(
            WebAuthnError,
            complete_registration,
            person,
            {},
            "My key",
            request=request,
        )

    def test_complete_by_different_person(self):
        person = self.factory.makePerson()
        other_person = self.factory.makePerson()
        request = LaunchpadTestRequest()
        login_person(person, participation=request)
        options = begin_registration(person, request=request)
        response = DummyAuthenticator().create(options, get_origin())

        self.assertRaises(
            WebAuthnError,
            complete_registration,
            other_person,
            response,
            "My key",
            request=request,
        )

    def test_complete_twice_fails(self):
        person = self.factory.makePerson()
        request = LaunchpadTestRequest()
        login_person(person, participation=request)
        options = begin_registration(person, request=request)
        response = DummyAuthenticator().create(options, get_origin())
        complete_registration(person, response, "My key", request=request)

        self.assertRaises(
            WebAuthnError,
            complete_registration,
            person,
            response,
            "My other key",
            request=request,
        )

    def test_complete_wrong_challenge(self):
        # A response signed for a challenge that was never issued to this
        # person's session is rejected.
        person = self.factory.makePerson()
        request = LaunchpadTestRequest()
        login_person(person, participation=request)
        begin_registration(person, request=request)

        other_person = self.factory.makePerson()
        other_request = LaunchpadTestRequest()
        login_person(other_person, participation=other_request)
        other_options = begin_registration(other_person, request=other_request)
        response = DummyAuthenticator().create(other_options, get_origin())

        self.assertRaises(
            WebAuthnError,
            complete_registration,
            person,
            response,
            "My key",
            request=request,
        )

    def test_exclude_credentials_for_already_registered(self):
        # Registering excludes already-registered, non-revoked credentials.
        person = self.factory.makePerson()
        request = LaunchpadTestRequest()
        login_person(person, participation=request)

        existing = self.factory.makeRegisteredAuthenticator(owner=person)
        revoked = self.factory.makeRegisteredAuthenticator(owner=person)
        revoked.revoke(person)

        options = begin_registration(person, request=request)

        excluded_ids = {
            credential["id"]
            for credential in options["publicKey"]["excludeCredentials"]
        }
        self.assertEqual(
            {websafe_encode(bytes(existing.credential_id))}, excluded_ids
        )

    def test_register_second_authenticator(self):
        person = self.factory.makePerson()
        request = LaunchpadTestRequest()
        login_person(person, participation=request)
        options = begin_registration(person, request=request)
        response = DummyAuthenticator().create(options, get_origin())
        complete_registration(person, response, "First key", request=request)

        request2 = LaunchpadTestRequest()
        login_person(person, participation=request2)
        options2 = begin_registration(person, request=request2)
        response2 = DummyAuthenticator().create(options2, get_origin())
        authenticator2 = complete_registration(
            person, response2, "Second key", request=request2
        )

        self.assertEqual("Second key", authenticator2.name)
        self.assertContentEqual(
            ["First key", "Second key"],
            [
                a.name
                for a in getUtility(IRegisteredAuthenticatorSet).findByOwner(
                    person
                )
            ],
        )
