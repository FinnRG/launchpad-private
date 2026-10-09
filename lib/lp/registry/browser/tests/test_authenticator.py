# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Browser tests for hardware authenticator management."""

from unittest.mock import patch

from bs4 import BeautifulSoup
from zope.security.interfaces import Unauthorized

from lp.services.webapp import canonical_url
from lp.testing import (
    BrowserTestCase,
    TestCaseWithFactory,
    api_url,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.views import create_initialized_view


class PersonRegisterAuthenticatorViewTestCase(TestCaseWithFactory):
    """Tests for PersonRegisterAuthenticatorView."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()

    def test_owner_can_access_page(self):
        """The person can access their own authenticator page."""
        with person_logged_in(self.person):
            with patch("lp.registry.browser.person.require_fresh_login"):
                view = create_initialized_view(
                    self.person,
                    "+register-authenticator",
                )

        self.assertEqual(
            "Hardware authenticators",
            view.page_title,
        )

    def test_fresh_login_required(self):
        """The authenticator page requires a fresh login."""
        with person_logged_in(self.person):
            with patch(
                "lp.registry.browser.person.require_fresh_login"
            ) as mock_require_fresh_login:
                view = create_initialized_view(
                    self.person,
                    "+register-authenticator",
                )

        mock_require_fresh_login.assert_called_once_with(
            view.request,
            self.person,
            "+register-authenticator",
        )

    def test_other_person_cannot_access(self):
        """Another person cannot access someone else's authenticator page."""
        other_person = self.factory.makePerson()
        url = canonical_url(
            self.person,
            view_name="+register-authenticator",
        )

        with patch("lp.registry.browser.person.require_fresh_login"):
            self.assertRaises(
                Unauthorized,
                self.getUserBrowser,
                url,
                user=other_person,
            )

    def test_administrator_cannot_access_others_page(self):
        """Administrators cannot access another person's authenticator page."""
        admin = self.factory.makeAdministrator()
        url = canonical_url(
            self.person,
            view_name="+register-authenticator",
        )

        with patch("lp.registry.browser.person.require_fresh_login"):
            self.assertRaises(
                Unauthorized,
                self.getUserBrowser,
                url,
                user=admin,
            )

    def test_cancel_url_points_to_profile(self):
        """The cancel URL points back to the person's profile."""
        with person_logged_in(self.person):
            with patch("lp.registry.browser.person.require_fresh_login"):
                view = create_initialized_view(
                    self.person,
                    "+register-authenticator",
                )

        self.assertEqual(
            canonical_url(self.person),
            view.cancel_url,
        )


class PersonRegisterAuthenticatorBrowserTestCase(BrowserTestCase):
    """Browser tests for the authenticator registration page."""

    layer = DatabaseFunctionalLayer

    def test_revoke_button_uses_authenticator_api_url(self):
        person = self.factory.makePerson()
        with person_logged_in(person):
            authenticator = self.factory.makeRegisteredAuthenticator(
                owner=person
            )
            authenticator_url = api_url(authenticator)
        url = canonical_url(
            person, view_name="+register-authenticator-authenticators-list"
        )

        browser = self.getUserBrowser(url, user=person)

        button = BeautifulSoup(browser.contents, "html.parser").find(
            "button", class_="revoke-button"
        )
        self.assertEndsWith(button["data-auth-url"], authenticator_url)
        self.assertNotIn("data-auth-id", button.attrs)

    def test_page_contains_registration_controls(self):
        """The page contains the necessary registration controls."""
        person = self.factory.makePerson()
        url = canonical_url(
            person,
            view_name="+register-authenticator",
        )

        with patch("lp.registry.browser.person.require_fresh_login"):
            browser = self.getUserBrowser(url, user=person)

        self.assertIn("Hardware authenticators", browser.contents)
        self.assertIn("Registered authenticators", browser.contents)
        self.assertIn("Add hardware authenticator", browser.contents)
        self.assertIn("authenticator-name", browser.contents)
        self.assertIn("register-button", browser.contents)
        self.assertIn("Back to profile", browser.contents)

    def test_page_includes_javascript_setup(self):
        """The page includes the JavaScript module setup."""
        person = self.factory.makePerson()
        url = canonical_url(
            person,
            view_name="+register-authenticator",
        )

        with patch("lp.registry.browser.person.require_fresh_login"):
            browser = self.getUserBrowser(url, user=person)

        self.assertIn(
            "lp.registry.javascript.person_register_authenticator",
            browser.contents,
        )

    def test_page_contains_help_text(self):
        """The page contains helpful explanatory text."""
        person = self.factory.makePerson()
        url = canonical_url(
            person,
            view_name="+register-authenticator",
        )

        with patch("lp.registry.browser.person.require_fresh_login"):
            browser = self.getUserBrowser(url, user=person)

        self.assertIn("FIDO2/WebAuthn devices", browser.contents)
        self.assertIn("two-factor authentication", browser.contents)
        self.assertIn("YubiKeys", browser.contents)
        self.assertIn("security keys", browser.contents)
