# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for IPerson's WebAuthn registration ceremony."""

import json
from datetime import timedelta
from unittest.mock import patch

from lazr.restfulclient.resource import Entry
from testtools.matchers import Equals, Is, MatchesStructure
from zope.component import getUtility
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.services.webapp.interfaces import OAuthPermission
from lp.services.webapp.publisher import canonical_url
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.services.webauthn.ceremony import WebAuthnError, get_origin
from lp.services.webauthn.interfaces import IRegisteredAuthenticatorSet
from lp.services.webauthn.tests.dummyauthenticator import DummyAuthenticator
from lp.testing import (
    ANONYMOUS,
    TestCaseWithFactory,
    api_url,
    launchpadlib_for,
    login_person,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.pages import webservice_for_person


class TestPersonWebAuthnRegistration(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_full_ceremony(self):
        person = self.factory.makePerson()
        login_person(person, participation=LaunchpadTestRequest())

        options = person.beginWebAuthnRegistration()
        response = DummyAuthenticator().create(options, get_origin())
        authenticator = person.completeWebAuthnRegistration(
            json.dumps(response), "My key"
        )

        self.assertEqual(person, authenticator.owner)
        self.assertEqual("My key", authenticator.name)

    def test_revoke(self):
        person = self.factory.makePerson()
        login_person(person, participation=LaunchpadTestRequest())

        options = person.beginWebAuthnRegistration()
        response = DummyAuthenticator().create(options, get_origin())
        authenticator = person.completeWebAuthnRegistration(
            json.dumps(response), "My key"
        )

        person.revokeWebAuthnRegistration(authenticator.id)

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
            [newer, older], list(person.getRegisteredAuthenticators())
        )

    def test_getRegisteredAuthenticators_includes_revoked(self):
        # Revoked authenticators are still listed, so that their owner can
        # see what happened to them.
        person = self.factory.makePerson()
        login_person(person, participation=LaunchpadTestRequest())
        authenticator = self.factory.makeRegisteredAuthenticator(owner=person)
        person.revokeWebAuthnRegistration(
            removeSecurityProxy(authenticator).id
        )

        self.assertEqual(
            [authenticator], list(person.getRegisteredAuthenticators())
        )

    def test_canonical_url(self):
        person = self.factory.makePerson()
        authenticator = self.factory.makeRegisteredAuthenticator(owner=person)
        with person_logged_in(person):
            self.assertEqual(
                "%s/+authenticator/%d"
                % (
                    canonical_url(person, rootsite="api"),
                    removeSecurityProxy(authenticator).id,
                ),
                canonical_url(authenticator),
            )


class TestPersonWebAuthnWebService(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()
        self.person_url = api_url(self.person)
        self.webservice = webservice_for_person(
            self.person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )

    def _makeOtherWebService(self):
        """Return a webservice caller for some other person."""
        with person_logged_in(ANONYMOUS):
            other_person = self.factory.makePerson()
        return webservice_for_person(
            other_person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )

    def test_getRegisteredAuthenticators(self):
        with person_logged_in(self.person):
            for name in ("Older key", "Newer key"):
                self.factory.makeRegisteredAuthenticator(
                    owner=self.person, name=name
                )
            # An authenticator registered by another person is not
            # included.
            self.factory.makeRegisteredAuthenticator()

        response = self.webservice.named_get(
            self.person_url, "getRegisteredAuthenticators"
        )

        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ["Older key", "Newer key"],
            [entry["name"] for entry in response.jsonBody()["entries"]],
        )

    def test_getRegisteredAuthenticators_anonymous(self):
        with person_logged_in(self.person):
            self.factory.makeRegisteredAuthenticator(owner=self.person)
        webservice = webservice_for_person(None, default_api_version="devel")

        response = webservice.named_get(
            self.person_url, "getRegisteredAuthenticators"
        )

        self.assertEqual(401, response.status)

    def test_getRegisteredAuthenticators_other_person(self):
        with person_logged_in(self.person):
            self.factory.makeRegisteredAuthenticator(owner=self.person)
        webservice = self._makeOtherWebService()

        response = webservice.named_get(
            self.person_url, "getRegisteredAuthenticators"
        )

        self.assertEqual(401, response.status)
        self.assertIn(b"launchpad.Edit", response.body)

    def test_entry_fields(self):
        with person_logged_in(self.person):
            authenticator = self.factory.makeRegisteredAuthenticator(
                owner=self.person, name="My key", transports=["usb"]
            )
            authenticator_id = authenticator.id
            date_created = authenticator.date_created.isoformat()
            authenticator_url = api_url(authenticator)
            owner_url = api_url(self.person)

        response = self.webservice.get(authenticator_url)

        self.assertEqual(200, response.status)
        entry = response.jsonBody()
        self.assertEqual(authenticator_id, entry["id"])
        self.assertEqual("My key", entry["name"])
        self.assertEqual(["usb"], entry["transports"])
        self.assertEqual("none", entry["attestation_format"])
        self.assertEqual(date_created, entry["date_created"])
        self.assertIsNone(entry["date_last_used"])
        self.assertFalse(entry["is_revoked"])
        self.assertIsNone(entry["date_revoked"])
        self.assertIsNone(entry["revoked_by_link"])
        self.assertEndsWith(entry["owner_link"], owner_url)
        # Secret and internal material is not exposed over the API.
        self.assertNotIn("public_key", entry)
        self.assertNotIn("credential_id", entry)
        self.assertNotIn("sign_count", entry)
        self.assertNotIn("aaguid", entry)

    def test_entry_other_person(self):
        with person_logged_in(self.person):
            authenticator = self.factory.makeRegisteredAuthenticator(
                owner=self.person
            )
            authenticator_url = api_url(authenticator)
        webservice = self._makeOtherWebService()

        response = webservice.get(authenticator_url)

        self.assertEqual(401, response.status)

    def test_entry_rename(self):
        with person_logged_in(self.person):
            authenticator = self.factory.makeRegisteredAuthenticator(
                owner=self.person, name="Old name"
            )
            authenticator_url = api_url(authenticator)

        response = self.webservice.patch(
            authenticator_url,
            "application/json",
            json.dumps({"name": "New name"}).encode(),
        )

        self.assertEqual(209, response.status)
        with person_logged_in(self.person):
            self.assertEqual("New name", authenticator.name)

    def test_entry_rename_other_person(self):
        with person_logged_in(self.person):
            authenticator = self.factory.makeRegisteredAuthenticator(
                owner=self.person, name="Old name"
            )
            authenticator_url = api_url(authenticator)
        webservice = self._makeOtherWebService()

        response = webservice.patch(
            authenticator_url,
            "application/json",
            json.dumps({"name": "New name"}).encode(),
        )

        self.assertEqual(401, response.status)
        with person_logged_in(self.person):
            self.assertEqual("Old name", authenticator.name)

    def test_beginWebAuthnRegistration(self):
        response = self.webservice.named_post(
            self.person_url, "beginWebAuthnRegistration"
        )

        self.assertEqual(200, response.status)
        self.assertIn("publicKey", response.jsonBody())

    def test_completeWebAuthnRegistration(self):
        begin_response = self.webservice.named_post(
            self.person_url, "beginWebAuthnRegistration"
        )
        session_cookie = begin_response.getHeader("Set-Cookie").split(";", 1)[
            0
        ]
        registration_response = DummyAuthenticator().create(
            begin_response.jsonBody(), get_origin()
        )
        serialized_response = json.dumps(registration_response)

        # WebServiceCaller operates below the schema-aware client layer, so
        # encode the Text parameter as a JSON string for lazr.restful.
        complete_response = self.webservice.named_post(
            self.person_url,
            "completeWebAuthnRegistration",
            headers={"Cookie": session_cookie},
            response=json.dumps(serialized_response),
            name="My key",
        )

        self.assertEqual(201, complete_response.status, complete_response.body)
        authenticator_response = self.webservice.get(
            complete_response.getHeader("Location")
        )
        self.assertEqual(200, authenticator_response.status)
        entry = authenticator_response.jsonBody()
        with person_logged_in(self.person):
            authenticator = getUtility(IRegisteredAuthenticatorSet).getByID(
                entry["id"]
            )
            self.assertEndsWith(entry["self_link"], api_url(authenticator))
            self.assertEqual(self.person, authenticator.owner)
            self.assertEqual("My key", authenticator.name)

    def test_completeWebAuthnRegistration_returns_entry(self):
        with person_logged_in(self.person):
            authenticator = self.factory.makeRegisteredAuthenticator(
                owner=self.person, name="My key"
            )
            authenticator_id = authenticator.id
            authenticator_url = api_url(authenticator)
        launchpad = launchpadlib_for("test", person=self.person)
        person = launchpad.load(launchpad.me.self_link)

        with patch(
            "lp.registry.model.person.complete_registration",
            return_value=authenticator,
        ) as ceremony:
            entry = person.completeWebAuthnRegistration(
                response="{}", name="My key"
            )

        ceremony.assert_called_once()
        self.assertIsInstance(entry, Entry)
        self.assertEqual(authenticator_id, entry.id)
        self.assertEqual("My key", entry.name)
        self.assertEndsWith(entry.self_link, authenticator_url)

    def test_completeWebAuthnRegistration_malformed_json(self):
        response = self.webservice.named_post(
            self.person_url,
            "completeWebAuthnRegistration",
            response="not JSON",
            name="My key",
        )

        self.assertEqual(400, response.status)

    def test_completeWebAuthnRegistration_other_person(self):
        webservice = self._makeOtherWebService()

        response = webservice.named_post(
            self.person_url,
            "completeWebAuthnRegistration",
            response="{}",
            name="My key",
        )

        self.assertEqual(401, response.status)
        self.assertIn(b"launchpad.Special", response.body)

    def test_completeWebAuthnRegistration_admin(self):
        admin = self.factory.makeAdministrator()
        webservice = webservice_for_person(
            admin,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )

        response = webservice.named_post(
            self.person_url,
            "completeWebAuthnRegistration",
            response="{}",
            name="My key",
        )

        self.assertEqual(401, response.status)
        self.assertIn(b"launchpad.Special", response.body)

    def test_beginWebAuthnRegistration_excludes_existing(self):
        with person_logged_in(self.person):
            self.factory.makeRegisteredAuthenticator(
                owner=self.person, transports=["usb", "nfc"]
            )

        response = self.webservice.named_post(
            self.person_url, "beginWebAuthnRegistration"
        )

        self.assertEqual(200, response.status)
        self.assertEqual(
            1,
            len(response.jsonBody()["publicKey"]["excludeCredentials"]),
        )

    def test_revokeWebAuthnRegistration(self):
        with person_logged_in(self.person):
            authenticator = self.factory.makeRegisteredAuthenticator(
                owner=self.person
            )
            authenticator_id = authenticator.id

        response = self.webservice.named_post(
            self.person_url,
            "revokeWebAuthnRegistration",
            authenticator_id=authenticator_id,
        )

        self.assertEqual(200, response.status)
        with person_logged_in(self.person):
            self.assertThat(
                authenticator,
                MatchesStructure(
                    is_revoked=Is(True),
                    revoked_by=Equals(self.person),
                ),
            )
