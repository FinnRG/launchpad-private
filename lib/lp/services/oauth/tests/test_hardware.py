# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for hardware-bound OAuth refresh tokens."""

from datetime import datetime, timedelta, timezone

from lp.services.oauth.hardware import (
    REFRESH_TOKEN_LIFETIME,
    HardwareTokenError,
    issue_refresh_token,
)
from lp.services.oauth.model import OAuthTokenExpiryError
from lp.services.webapp.interfaces import AccessLevel
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.services.webauthn.ceremony import (
    begin_registration,
    complete_registration,
    get_origin,
)
from lp.services.webauthn.tests.dummyauthenticator import DummyAuthenticator
from lp.testing import TestCaseWithFactory, login_person
from lp.testing.layers import DatabaseFunctionalLayer


class TestHardwareOAuthExchange(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()
        self.consumer = self.factory.makeOAuthConsumer()
        request = LaunchpadTestRequest()
        login_person(self.person, participation=request)
        self.device = DummyAuthenticator()
        options = begin_registration(self.person, request=request)
        response = self.device.create(options, get_origin())
        self.authenticator = complete_registration(
            self.person, response, "My key", request=request
        )

    def _issue_refresh_token(self, **kwargs):
        return issue_refresh_token(
            self.person,
            self.consumer,
            self.authenticator,
            AccessLevel.WRITE_PRIVATE,
            **kwargs,
        )

    def test_expired_authenticator_is_rejected_at_issue_time(self):
        self.authenticator.revoke(self.person)

        self.assertRaisesRegex(
            HardwareTokenError,
            "The authenticator is not active for this person.",
            self._issue_refresh_token,
        )

    def test_mint_rejects_invalid_expiry(self):
        refresh_token, _ = self._issue_refresh_token()
        now = datetime.now(timezone.utc)
        for date_expires in (
            now - timedelta(seconds=1),
            now + timedelta(hours=13),
            now.replace(tzinfo=None),
        ):
            with self.subTest(date_expires=date_expires):
                self.assertRaises(
                    OAuthTokenExpiryError,
                    refresh_token.mintAccessToken,
                    date_expires=date_expires,
                )
        self.assertIsNone(refresh_token.date_last_used)

    def test_mint_expiry_is_per_token(self):
        refresh_token, _ = self._issue_refresh_token()
        grant_expiry = refresh_token.date_expires
        for lifetime in (timedelta(minutes=10), timedelta(hours=2)):
            date_expires = datetime.now(timezone.utc) + lifetime

            token, _ = refresh_token.mintAccessToken(date_expires=date_expires)

            self.assertEqual(date_expires, token.date_expires)
            self.assertEqual(grant_expiry, refresh_token.date_expires)

    def test_mint_expiry_is_clipped_to_grant(self):
        grant_expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
        refresh_token, _ = self._issue_refresh_token(date_expires=grant_expiry)

        token, _ = refresh_token.mintAccessToken(
            date_expires=grant_expiry + timedelta(hours=1)
        )

        self.assertEqual(grant_expiry, token.date_expires)

    def test_refresh_expiry_maximum_and_timezone(self):
        date_expires = (
            datetime.now(timezone.utc) + REFRESH_TOKEN_LIFETIME
        ).astimezone(timezone(timedelta(hours=2)))

        refresh_token, _ = self._issue_refresh_token(date_expires=date_expires)

        self.assertEqual(date_expires, refresh_token.date_expires)
        self.assertEqual(timezone.utc, refresh_token.date_expires.tzinfo)

    def test_refresh_expiry_requires_timezone(self):
        self.assertRaisesRegex(
            HardwareTokenError,
            "must include a timezone",
            self._issue_refresh_token,
            date_expires=datetime.now() + timedelta(days=1),
        )

    def test_expiry_date_in_the_past_is_rejected(self):
        self.assertRaisesRegex(
            HardwareTokenError,
            "The expiry date must be in the future.",
            self._issue_refresh_token,
            date_expires=datetime.now(timezone.utc) - timedelta(seconds=1),
        )

    def test_expiry_date_beyond_the_maximum_lifetime_is_rejected(self):
        self.assertRaisesRegex(
            HardwareTokenError,
            "may not be valid for more than 365 days",
            self._issue_refresh_token,
            date_expires=(
                datetime.now(timezone.utc)
                + REFRESH_TOKEN_LIFETIME
                + timedelta(days=1)
            ),
        )

    def test_unknown_context_is_rejected(self):
        self.assertRaisesRegex(
            HardwareTokenError,
            "Unknown OAuth context",
            self._issue_refresh_token,
            context=self.factory.makeSourcePackageName(),
        )
