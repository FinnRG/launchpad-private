# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for hardware-bound OAuth refresh tokens."""

from datetime import datetime, timedelta, timezone

from zope.security.proxy import removeSecurityProxy

from lp.services.oauth.hardware import (
    REFRESH_TOKEN_LIFETIME,
    HardwareAuthenticationError,
    HardwareTokenError,
    begin_token_exchange,
    complete_token_exchange,
    issue_refresh_token,
)
from lp.services.oauth.model import OAuthTokenExpiryError
from lp.services.webapp.authentication import check_oauth_signature
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

    def _exchange(self, refresh_token, refresh_secret, **kwargs):
        options, challenge_id = begin_token_exchange(
            refresh_token.key, refresh_secret
        )
        assertion = self.device.get(options, get_origin())
        return complete_token_exchange(
            refresh_token.key,
            refresh_secret,
            challenge_id,
            assertion,
            **kwargs,
        )

    def test_exchange_expiry_is_optional_and_bounded(self):
        refresh_token, refresh_secret = self._issue_refresh_token()
        for lifetime in (timedelta(minutes=10), timedelta(hours=12)):
            date_expires = datetime.now(timezone.utc) + lifetime

            token, _ = self._exchange(
                refresh_token, refresh_secret, date_expires=date_expires
            )

            self.assertEqual(date_expires, token.date_expires)

    def test_invalid_exchange_expiry_preserves_challenge(self):
        refresh_token, refresh_secret = self._issue_refresh_token()
        options, challenge_id = begin_token_exchange(
            refresh_token.key, refresh_secret
        )
        assertion = self.device.get(options, get_origin())
        arguments = (
            refresh_token.key,
            refresh_secret,
            challenge_id,
            assertion,
        )

        self.assertRaises(
            HardwareTokenError,
            complete_token_exchange,
            *arguments,
            date_expires=datetime.now(timezone.utc) + timedelta(hours=13),
        )

        self.assertEqual(0, self.authenticator.sign_count)
        self.assertIsNone(refresh_token.date_last_used)
        token, _ = complete_token_exchange(*arguments)
        self.assertEqual(refresh_token, token.refresh_token)

    def test_exchange_mints_short_lived_oauth_token(self):
        refresh_token, refresh_secret = self._issue_refresh_token()
        options, challenge_id = begin_token_exchange(
            refresh_token.key, refresh_secret
        )
        assertion = self.device.get(options, get_origin())

        before = datetime.now(timezone.utc)
        access_token, access_secret = complete_token_exchange(
            refresh_token.key,
            refresh_secret,
            challenge_id,
            assertion,
        )

        self.assertEqual(refresh_token, access_token.refresh_token)
        self.assertEqual(self.person, access_token.person)
        self.assertEqual(self.consumer, access_token.consumer)
        self.assertEqual(AccessLevel.WRITE_PRIVATE, access_token.permission)
        self.assertTrue(access_token.isSecretValid(access_secret))
        oauth_request = LaunchpadTestRequest(
            form={
                "oauth_signature_method": "PLAINTEXT",
                "oauth_signature": "&%s" % access_secret,
            }
        )
        self.assertTrue(
            check_oauth_signature(
                oauth_request, access_token.consumer, access_token
            )
        )
        self.assertBetween(
            before + timedelta(hours=12),
            access_token.date_expires,
            datetime.now(timezone.utc) + timedelta(hours=12),
        )
        self.assertEqual(1, self.authenticator.sign_count)
        self.assertIsNotNone(self.authenticator.date_last_used)

        self.assertRaises(
            HardwareAuthenticationError,
            complete_token_exchange,
            refresh_token.key,
            refresh_secret,
            challenge_id,
            assertion,
        )

    def test_wrong_refresh_secret_is_rejected(self):
        refresh_token, _ = self._issue_refresh_token()

        self.assertRaisesRegex(
            HardwareAuthenticationError,
            "Invalid OAuth refresh token",
            begin_token_exchange,
            refresh_token.key,
            "wrong secret",
        )

    def test_missing_refresh_credentials_are_rejected(self):
        self.assertRaises(
            HardwareAuthenticationError,
            begin_token_exchange,
            None,
            None,
        )

    def test_expired_refresh_token_is_rejected(self):
        refresh_token, refresh_secret = self._issue_refresh_token()
        refresh_token.date_expires = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )

        self.assertRaises(
            HardwareAuthenticationError,
            begin_token_exchange,
            refresh_token.key,
            refresh_secret,
        )

    def test_revoked_authenticator_is_rejected(self):
        refresh_token, refresh_secret = self._issue_refresh_token()
        self.authenticator.revoke(self.person)

        self.assertRaises(
            HardwareAuthenticationError,
            begin_token_exchange,
            refresh_token.key,
            refresh_secret,
        )

    def test_signature_counter_must_increase(self):
        refresh_token, refresh_secret = self._issue_refresh_token()
        self._exchange(refresh_token, refresh_secret)
        self.device.sign_counts[bytes(self.authenticator.credential_id)] = 0
        options, challenge_id = begin_token_exchange(
            refresh_token.key, refresh_secret
        )
        assertion = self.device.get(options, get_origin())

        self.assertRaisesRegex(
            HardwareAuthenticationError,
            "signature counter did not increase",
            complete_token_exchange,
            refresh_token.key,
            refresh_secret,
            challenge_id,
            assertion,
        )

    def test_counterless_authenticator_may_exchange_repeatedly(self):
        # An authenticator that always reports a zero counter can't be
        # checked for cloning, but must still be usable.
        credential_id = bytes(self.authenticator.credential_id)
        removeSecurityProxy(self.authenticator).sign_count = 0
        self.device.sign_counts[credential_id] = -1
        refresh_token, refresh_secret = self._issue_refresh_token()

        self._exchange(refresh_token, refresh_secret)
        self.device.sign_counts[credential_id] = -1
        self._exchange(refresh_token, refresh_secret)

        self.assertEqual(0, self.authenticator.sign_count)
        self.assertIsNotNone(self.authenticator.date_last_used)

    def test_refresh_revocation_expires_minted_tokens(self):
        refresh_token, refresh_secret = self._issue_refresh_token()
        access_token, _ = self._exchange(refresh_token, refresh_secret)

        refresh_token.revoke(self.person)

        self.assertTrue(refresh_token.is_revoked)
        self.assertIsNotNone(refresh_token.date_revoked)
        self.assertEqual(self.person, refresh_token.revoked_by)
        self.assertTrue(access_token.is_expired)
        self.assertRaises(
            HardwareAuthenticationError,
            begin_token_exchange,
            refresh_token.key,
            refresh_secret,
        )

    def test_authenticator_revocation_cascades_to_tokens(self):
        refresh_token, refresh_secret = self._issue_refresh_token()
        access_token, _ = self._exchange(refresh_token, refresh_secret)

        self.authenticator.revoke(self.person)

        self.assertTrue(refresh_token.is_revoked)
        self.assertEqual(self.person, refresh_token.revoked_by)
        self.assertTrue(access_token.is_expired)

    def test_minted_token_inherits_context(self):
        context = self.factory.makeProduct()
        refresh_token, refresh_secret = self._issue_refresh_token(
            context=context
        )

        access_token, _ = self._exchange(refresh_token, refresh_secret)

        self.assertEqual(context, refresh_token.context)
        self.assertEqual(context, access_token.context)

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
