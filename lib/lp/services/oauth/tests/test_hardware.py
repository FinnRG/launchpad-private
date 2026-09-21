# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for hardware-bound OAuth refresh tokens."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from zope.interface import alsoProvides
from zope.publisher.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.services.oauth.hardware import (
    REFRESH_TOKEN_LIFETIME,
    HardwareAuthenticationError,
    HardwareTokenError,
    begin_token_exchange,
    complete_token_exchange,
    issue_refresh_token,
)
from lp.services.oauth.interfaces import IOAuthSignedRequest
from lp.services.oauth.model import OAuthTokenExpiryError
from lp.services.webapp.authentication import check_oauth_signature
from lp.services.webapp.interfaces import AccessLevel, OAuthPermission
from lp.services.webapp.publisher import canonical_url
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.services.webauthn.ceremony import (
    begin_registration,
    complete_registration,
    get_origin,
)
from lp.services.webauthn.tests.dummyauthenticator import DummyAuthenticator
from lp.testing import TestCaseWithFactory, api_url, login_person
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.pages import LaunchpadWebServiceCaller, webservice_for_person


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

    def test_person_issues_token_with_named_context(self):
        product = self.factory.makeProduct()

        result = self.person.issueHardwareBoundOAuthToken(
            self.consumer.key,
            self.authenticator.id,
            OAuthPermission.WRITE_PRIVATE,
            context=product.name,
        )

        [refresh_token] = self.person.oauth_refresh_tokens
        self.assertEqual(result["oauth_token"], refresh_token.key)
        self.assertEqual(product, refresh_token.context)

    def test_person_rejects_unknown_named_context(self):
        self.assertRaisesRegex(
            HardwareTokenError,
            "No such OAuth context: no-such-pillar",
            self.person.issueHardwareBoundOAuthToken,
            self.consumer.key,
            self.authenticator.id,
            OAuthPermission.WRITE_PRIVATE,
            context="no-such-pillar",
        )

    def test_person_issues_and_revokes_refresh_token(self):
        result = self.person.issueHardwareBoundOAuthToken(
            self.consumer.key,
            self.authenticator.id,
            OAuthPermission.WRITE_PRIVATE,
        )

        options, _ = begin_token_exchange(
            result["oauth_token"], result["oauth_token_secret"]
        )
        self.assertIn("publicKey", options)

        self.person.revokeHardwareBoundOAuthToken(result["oauth_token"])
        self.assertRaises(
            HardwareAuthenticationError,
            begin_token_exchange,
            result["oauth_token"],
            result["oauth_token_secret"],
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

    def test_oauth_context_must_be_retained(self):
        context = self.factory.makeProduct()
        other_context = self.factory.makeProduct()
        authenticator_id = self.authenticator.id
        request = LaunchpadTestRequest()
        alsoProvides(request, IOAuthSignedRequest)
        request.setPrincipal(
            SimpleNamespace(
                access_level=AccessLevel.WRITE_PRIVATE,
                scope_url=canonical_url(context, force_local_path=True),
            )
        )
        request_patch = patch(
            "lp.registry.model.person.get_current_browser_request",
            return_value=request,
        )
        request_patch.start()
        self.addCleanup(request_patch.stop)
        person = removeSecurityProxy(self.person)
        for requested_context in (None, other_context.name):
            with self.subTest(context=requested_context):
                self.assertRaisesRegex(
                    Unauthorized,
                    "must retain the caller's OAuth context",
                    person.issueHardwareBoundOAuthToken,
                    self.consumer.key,
                    authenticator_id,
                    OAuthPermission.WRITE_PRIVATE,
                    context=requested_context,
                )
        self.assertEqual(0, person.oauth_refresh_tokens.count())

        result = person.issueHardwareBoundOAuthToken(
            self.consumer.key,
            authenticator_id,
            OAuthPermission.WRITE_PRIVATE,
            context=context.name,
        )

        [grant] = person.oauth_refresh_tokens
        self.assertEqual(result["oauth_token"], grant.key)
        self.assertEqual(context, grant.context)

    def test_unauthorized_permission_is_rejected(self):
        self.assertRaisesRegex(
            HardwareTokenError,
            "Cannot issue an unauthorized OAuth refresh token.",
            self.person.issueHardwareBoundOAuthToken,
            self.consumer.key,
            self.authenticator.id,
            OAuthPermission.UNAUTHORIZED,
        )


class TestHardwareOAuthWebService(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_browser_can_issue_refresh_token(self):
        person = self.factory.makePerson()
        authenticator = self.factory.makeRegisteredAuthenticator(owner=person)
        url = "http://launchpad.test/api/devel/~%s" % person.name
        data = urlencode(
            {
                "ws.op": "issueHardwareBoundOAuthToken",
                "consumer_key": "hardware-client",
                "authenticator_id": removeSecurityProxy(authenticator).id,
                "permission": OAuthPermission.WRITE_PRIVATE.title,
            }
        )
        browser = self.getUserBrowser(user=person)
        browser.addHeader("Referer", "http://launchpad.test/")

        browser.open(url, data=data)

        result = json.loads(browser.contents)
        login_person(person)
        [refresh_token] = person.oauth_refresh_tokens
        self.assertEqual(result["oauth_token"], refresh_token.key)
        self.assertEqual(AccessLevel.WRITE_PRIVATE, refresh_token.permission)
        self.assertTrue(
            removeSecurityProxy(refresh_token).isSecretValid(
                result["oauth_token_secret"]
            )
        )

    def test_oauth_issuance_permission_ceiling(self):
        person = self.factory.makePerson()
        person_url = api_url(person)
        authenticator = self.factory.makeRegisteredAuthenticator(owner=person)
        authenticator_id = removeSecurityProxy(authenticator).id
        for caller_permission in (
            OAuthPermission.READ_PUBLIC,
            OAuthPermission.READ_PRIVATE,
            OAuthPermission.WRITE_PUBLIC,
            OAuthPermission.WRITE_PRIVATE,
            OAuthPermission.DESKTOP_INTEGRATION,
        ):
            for grant_permission in (
                OAuthPermission.READ_PUBLIC,
                OAuthPermission.WRITE_PUBLIC,
                OAuthPermission.READ_PRIVATE,
                OAuthPermission.WRITE_PRIVATE,
                OAuthPermission.DESKTOP_INTEGRATION,
            ):
                with self.subTest(
                    caller=caller_permission, grant=grant_permission
                ):
                    login_person(person)
                    count = person.oauth_refresh_tokens.count()
                    webservice = webservice_for_person(
                        person,
                        permission=caller_permission,
                        default_api_version="devel",
                    )
                    response = webservice.named_post(
                        person_url,
                        "issueHardwareBoundOAuthToken",
                        consumer_key="hardware-client",
                        authenticator_id=authenticator_id,
                        permission=grant_permission.title,
                    )
                    allowed = caller_permission in (
                        OAuthPermission.WRITE_PRIVATE,
                        OAuthPermission.DESKTOP_INTEGRATION,
                    ) or (
                        caller_permission == OAuthPermission.WRITE_PUBLIC
                        and grant_permission
                        in (
                            OAuthPermission.READ_PUBLIC,
                            OAuthPermission.WRITE_PUBLIC,
                        )
                    )
                    self.assertEqual(200 if allowed else 401, response.status)
                    login_person(person)
                    self.assertEqual(
                        count + int(allowed),
                        person.oauth_refresh_tokens.count(),
                    )
                    if allowed:
                        result = response.jsonBody()
                        grant = next(
                            token
                            for token in person.oauth_refresh_tokens
                            if token.key == result["oauth_token"]
                        )
                        self.assertEqual(
                            grant_permission.name, grant.permission.name
                        )

    def test_refresh_grant_can_outlive_bootstrap_token(self):
        person = self.factory.makePerson()
        person_url = api_url(person)
        authenticator = self.factory.makeRegisteredAuthenticator(owner=person)
        authenticator_id = removeSecurityProxy(authenticator).id
        token, secret = self.factory.makeOAuthAccessToken(
            owner=person, access_level=OAuthPermission.WRITE_PRIVATE
        )
        bootstrap_expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
        removeSecurityProxy(token).date_expires = bootstrap_expiry
        webservice = LaunchpadWebServiceCaller(
            oauth_consumer_key=token.consumer.key,
            oauth_access_key=token.key,
            oauth_access_secret=secret,
            default_api_version="devel",
        )

        before = datetime.now(timezone.utc)
        response = webservice.named_post(
            person_url,
            "issueHardwareBoundOAuthToken",
            consumer_key="hardware-client",
            authenticator_id=authenticator_id,
            permission=OAuthPermission.WRITE_PRIVATE.title,
        )

        self.assertEqual(200, response.status)
        login_person(person)
        [grant] = person.oauth_refresh_tokens
        self.assertBetween(
            before + REFRESH_TOKEN_LIFETIME,
            grant.date_expires,
            datetime.now(timezone.utc) + REFRESH_TOKEN_LIFETIME,
        )
        self.assertGreater(grant.date_expires, bootstrap_expiry)
