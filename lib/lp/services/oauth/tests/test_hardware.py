# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for hardware-bound OAuth refresh tokens."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode

from zope.interface import alsoProvides
from zope.publisher.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.app.errors import UnexpectedFormData
from lp.registry.browser.person import PersonOAuthTokensView
from lp.services.database.interfaces import IPrimaryStore
from lp.services.oauth.browser import (
    OAuthHardwareAccessTokenView,
    OAuthHardwareChallengeView,
)
from lp.services.oauth.hardware import (
    REFRESH_TOKEN_LIFETIME,
    HardwareAuthenticationError,
    HardwareTokenError,
    begin_token_exchange,
    complete_token_exchange,
    issue_refresh_token,
)
from lp.services.oauth.interfaces import IOAuthSignedRequest
from lp.services.oauth.model import (
    OAuthAuthenticationChallenge,
    OAuthTokenExpiryError,
)
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

    def test_expired_challenge_is_rejected(self):
        refresh_token, refresh_secret = self._issue_refresh_token()
        options, challenge_id = begin_token_exchange(
            refresh_token.key, refresh_secret
        )
        challenge = IPrimaryStore(OAuthAuthenticationChallenge).get(
            OAuthAuthenticationChallenge, challenge_id
        )
        challenge.date_expires = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )

        self.assertRaises(
            HardwareAuthenticationError,
            complete_token_exchange,
            refresh_token.key,
            refresh_secret,
            challenge_id,
            self.device.get(options, get_origin()),
        )

    def test_challenge_cannot_be_completed_with_other_refresh_token(self):
        refresh_token_a, refresh_secret_a = self._issue_refresh_token()
        refresh_token_b, refresh_secret_b = self._issue_refresh_token()
        options, challenge_id = begin_token_exchange(
            refresh_token_a.key, refresh_secret_a
        )

        self.assertRaises(
            HardwareAuthenticationError,
            complete_token_exchange,
            refresh_token_b.key,
            refresh_secret_b,
            challenge_id,
            self.device.get(options, get_origin()),
        )

        token, _ = complete_token_exchange(
            refresh_token_a.key,
            refresh_secret_a,
            challenge_id,
            self.device.get(options, get_origin()),
        )
        self.assertEqual(refresh_token_a, token.refresh_token)

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

    def test_views_exchange_refresh_token(self):
        date_expires = datetime.now(timezone.utc) + timedelta(hours=1)
        refresh_token, refresh_secret = self._issue_refresh_token(
            date_expires=date_expires
        )
        challenge_request = LaunchpadTestRequest(
            form={
                "oauth_token": refresh_token.key,
                "oauth_token_secret": refresh_secret,
            },
            method="POST",
        )
        challenge_body = OAuthHardwareChallengeView(None, challenge_request)()
        challenge = json.loads(challenge_body)
        self.assertEqual(
            "application/json",
            challenge_request.response.getHeader("Content-Type"),
        )
        challenge_id = challenge.pop("challenge_id")
        assertion = self.device.get(challenge, get_origin())
        access_request = LaunchpadTestRequest(
            form={
                "oauth_token": refresh_token.key,
                "oauth_token_secret": refresh_secret,
                "challenge_id": str(challenge_id),
                "assertion": json.dumps(assertion),
            },
            method="POST",
        )

        body = OAuthHardwareAccessTokenView(None, access_request)()

        response = parse_qs(body)
        self.assertEqual(["None"], response["lp.context"])
        self.assertEqual(
            date_expires, datetime.fromisoformat(response["expires_at"][0])
        )
        [access_token_key] = response["oauth_token"]
        access_token = self.consumer.getAccessToken(access_token_key)
        self.assertEqual(refresh_token, access_token.refresh_token)
        self.assertTrue(
            access_token.isSecretValid(response["oauth_token_secret"][0])
        )

    def test_views_require_post(self):
        for view_class, exchange in (
            (OAuthHardwareChallengeView, "begin_token_exchange"),
            (OAuthHardwareAccessTokenView, "complete_token_exchange"),
        ):
            for method in ("GET", "HEAD", "PUT", "DELETE", "OPTIONS"):
                with self.subTest(view=view_class.__name__, method=method):
                    request = LaunchpadTestRequest(method=method)
                    with patch(
                        "lp.services.oauth.browser." + exchange
                    ) as mocked_exchange:
                        view_class(None, request)()
                    self.assertEqual(405, request.response.getStatus())
                    self.assertEqual(
                        "POST", request.response.getHeader("Allow")
                    )
                    mocked_exchange.assert_not_called()

    def test_access_token_view_expiry_validation(self):
        refresh_token, refresh_secret = self._issue_refresh_token()
        options, challenge_id = begin_token_exchange(
            refresh_token.key, refresh_secret
        )
        form = {
            "oauth_token": refresh_token.key,
            "oauth_token_secret": refresh_secret,
            "challenge_id": str(challenge_id),
            "assertion": json.dumps(self.device.get(options, get_origin())),
        }
        now = datetime.now(timezone.utc)
        for expiry in (
            "not-a-date",
            "",
            now.replace(tzinfo=None).isoformat(),
            (now - timedelta(seconds=1)).isoformat(),
            (now + timedelta(hours=13)).isoformat(),
        ):
            with self.subTest(expiry=expiry):
                request = LaunchpadTestRequest(
                    method="POST", form=dict(form, date_expires=expiry)
                )
                OAuthHardwareAccessTokenView(None, request)()
                self.assertEqual(400, request.response.getStatus())
                self.assertIsNone(refresh_token.date_last_used)

        date_expires = now + timedelta(minutes=10)
        request = LaunchpadTestRequest(
            method="POST",
            form=dict(form, date_expires=date_expires.isoformat()),
        )
        response = parse_qs(OAuthHardwareAccessTokenView(None, request)())
        self.assertIn("oauth_token", response)
        self.assertEqual([date_expires.isoformat()], response["expires_at"])

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

    def test_failed_view_exchange_consumes_challenge(self):
        refresh_token, refresh_secret = self._issue_refresh_token()
        options, challenge_id = begin_token_exchange(
            refresh_token.key, refresh_secret
        )
        assertion = self.device.get(options, get_origin())
        assertion["response"]["signature"] = "invalid"
        request = LaunchpadTestRequest(
            form={
                "oauth_token": refresh_token.key,
                "oauth_token_secret": refresh_secret,
                "challenge_id": str(challenge_id),
                "assertion": json.dumps(assertion),
            },
            method="POST",
        )

        OAuthHardwareAccessTokenView(None, request)()

        self.assertEqual(401, request.response.getStatus())
        valid_assertion = self.device.get(options, get_origin())
        self.assertRaises(
            HardwareAuthenticationError,
            complete_token_exchange,
            refresh_token.key,
            refresh_secret,
            challenge_id,
            valid_assertion,
        )

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

    def test_challenge_view_rejects_bad_credentials(self):
        request = LaunchpadTestRequest(
            form={"oauth_token": "nope", "oauth_token_secret": "nope"},
            method="POST",
        )

        body = OAuthHardwareChallengeView(None, request)()

        self.assertEqual(401, request.response.getStatus())
        self.assertEqual("Invalid OAuth refresh token.", body)

    def test_access_token_view_rejects_malformed_assertion(self):
        refresh_token, refresh_secret = self._issue_refresh_token()
        request = LaunchpadTestRequest(
            form={
                "oauth_token": refresh_token.key,
                "oauth_token_secret": refresh_secret,
                "challenge_id": "not a number",
                "assertion": "{}",
            },
            method="POST",
        )

        body = OAuthHardwareAccessTokenView(None, request)()

        self.assertEqual(401, request.response.getStatus())
        self.assertEqual("Invalid hardware token exchange request.", body)

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

    def test_browser_can_exchange_refresh_token_without_referer(self):
        person = self.factory.makePerson()
        consumer = self.factory.makeOAuthConsumer()
        request = LaunchpadTestRequest()
        login_person(person, participation=request)
        device = DummyAuthenticator()
        options = begin_registration(person, request=request)
        authenticator = complete_registration(
            person,
            device.create(options, get_origin()),
            "My key",
            request=request,
        )
        refresh_token, refresh_secret = issue_refresh_token(
            person,
            consumer,
            authenticator,
            AccessLevel.WRITE_PRIVATE,
        )
        browser = self.getUserBrowser()

        browser.open(
            "http://launchpad.test/+hw-challenge",
            data=urlencode(
                {
                    "oauth_token": refresh_token.key,
                    "oauth_token_secret": refresh_secret,
                }
            ),
        )
        challenge = json.loads(browser.contents)
        challenge_id = challenge.pop("challenge_id")
        assertion = device.get(challenge, get_origin())

        browser.open(
            "http://launchpad.test/+hw-access-token",
            data=urlencode(
                {
                    "oauth_token": refresh_token.key,
                    "oauth_token_secret": refresh_secret,
                    "challenge_id": challenge_id,
                    "assertion": json.dumps(assertion),
                }
            ),
        )

        response = parse_qs(browser.contents)
        [access_token_key] = response["oauth_token"]
        access_token = consumer.getAccessToken(access_token_key)
        self.assertEqual(refresh_token, access_token.refresh_token)
        self.assertTrue(
            access_token.isSecretValid(response["oauth_token_secret"][0])
        )

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


class TestPersonOAuthTokensView(TestCaseWithFactory):
    """Tests for revoking hardware-bound grants from `+oauth-tokens`."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()
        self.consumer = self.factory.makeOAuthConsumer()
        request = LaunchpadTestRequest()
        login_person(self.person, participation=request)
        device = DummyAuthenticator()
        options = begin_registration(self.person, request=request)
        self.authenticator = complete_registration(
            self.person,
            device.create(options, get_origin()),
            "My key",
            request=request,
        )

    def _revoke(self, **form):
        request = LaunchpadTestRequest(form=form, method="POST")
        view = PersonOAuthTokensView(self.person, request)
        view.initialize()
        return view

    def test_revokes_refresh_token(self):
        refresh_token, _ = issue_refresh_token(
            self.person,
            self.consumer,
            self.authenticator,
            AccessLevel.WRITE_PRIVATE,
        )

        self._revoke(
            consumer_key=self.consumer.key,
            token_key=refresh_token.key,
            token_type="refresh_token",
        )

        self.assertTrue(refresh_token.is_revoked)
        self.assertEqual(self.person, refresh_token.revoked_by)
        self.assertEqual([], list(self.person.oauth_refresh_tokens))

    def test_lists_refresh_tokens(self):
        refresh_token, _ = issue_refresh_token(
            self.person,
            self.consumer,
            self.authenticator,
            AccessLevel.WRITE_PRIVATE,
        )
        request = LaunchpadTestRequest()

        view = PersonOAuthTokensView(self.person, request)

        self.assertEqual([refresh_token], view.refresh_tokens)

    def test_revoking_unknown_refresh_token_is_harmless(self):
        view = self._revoke(
            consumer_key=self.consumer.key,
            token_key="no-such-token",
            token_type="refresh_token",
        )

        self.assertIn(
            "Couldn&#x27;t find authorization given to %s" % self.consumer.key,
            view.request.response.notifications[0].message,
        )

    def test_unknown_token_type_is_rejected(self):
        self.assertRaisesRegex(
            UnexpectedFormData,
            "Invalid form value for token_type: 'nonsense'",
            self._revoke,
            token_key="whatever",
            token_type="nonsense",
        )

    def test_unknown_consumer_is_rejected(self):
        self.assertRaisesRegex(
            UnexpectedFormData,
            "Invalid OAuth consumer.",
            self._revoke,
            consumer_key="no-such-consumer",
            token_key="whatever",
            token_type="access_token",
        )
