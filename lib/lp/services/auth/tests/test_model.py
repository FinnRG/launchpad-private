# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test personal access tokens."""

import hashlib
import os
import signal
from datetime import datetime, timedelta, timezone

import transaction
from pymacaroons import Macaroon
from storm.store import Store
from testtools.matchers import (
    Equals,
    Is,
    MatchesListwise,
    MatchesStructure,
    StartsWith,
)
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.code.interfaces.gitrepository import IGitRepository
from lp.registry.interfaces.person import TeamMembershipPolicy
from lp.services.auth.enums import AccessTokenScope
from lp.services.auth.interfaces import IAccessTokenSet
from lp.services.auth.utils import create_access_token_secret
from lp.services.config import config
from lp.services.database.sqlbase import (
    disconnect_stores,
    get_transaction_timestamp,
)
from lp.services.webapp.authorization import check_permission
from lp.services.webapp.interfaces import OAuthPermission
from lp.testing import (
    ANONYMOUS,
    TestCaseWithFactory,
    api_url,
    login,
    login_person,
    person_logged_in,
    record_two_runs,
)
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.matchers import HasQueryCount
from lp.testing.pages import webservice_for_person


class TestAccessTokenBase:
    layer = DatabaseFunctionalLayer

    def test_owner_can_edit(self):
        owner = self.factory.makePerson()
        _, token = self.factory.makeAccessToken(
            owner=owner, target=self.makeTarget()
        )
        login_person(owner)
        self.assertTrue(check_permission("launchpad.Edit", token))

    def test_target_owner_can_edit(self):
        target_owner = self.factory.makePerson()
        _, token = self.factory.makeAccessToken(
            target=self.makeTarget(target_owner)
        )
        login_person(target_owner)
        self.assertTrue(check_permission("launchpad.Edit", token))

    def test_other_user_cannot_edit(self):
        _, token = self.factory.makeAccessToken(target=self.makeTarget())
        login_person(self.factory.makePerson())
        self.assertFalse(check_permission("launchpad.Edit", token))

    def test_updateLastUsed_never_used(self):
        # If the token has never been used, we update its last-used date.
        owner = self.factory.makePerson()
        _, token = self.factory.makeAccessToken(
            owner=owner, target=self.makeTarget()
        )
        login_person(owner)
        self.assertIsNone(token.date_last_used)
        transaction.commit()
        token.updateLastUsed()
        now = get_transaction_timestamp(Store.of(token))
        self.assertEqual(now, token.date_last_used)

    def test_updateLastUsed_recent(self):
        # If the token's last-used date was updated recently, we leave it
        # alone.
        owner = self.factory.makePerson()
        _, token = self.factory.makeAccessToken(
            owner=owner, target=self.makeTarget()
        )
        login_person(owner)
        recent = datetime.now(timezone.utc) - timedelta(minutes=1)
        removeSecurityProxy(token).date_last_used = recent
        transaction.commit()
        token.updateLastUsed()
        self.assertEqual(recent, token.date_last_used)

    def test_updateLastUsed_old(self):
        # If the token's last-used date is outside our update resolution, we
        # update it.
        owner = self.factory.makePerson()
        _, token = self.factory.makeAccessToken(
            owner=owner, target=self.makeTarget()
        )
        login_person(owner)
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        removeSecurityProxy(token).date_last_used = recent
        transaction.commit()
        token.updateLastUsed()
        now = get_transaction_timestamp(Store.of(token))
        self.assertEqual(now, token.date_last_used)

    def test_updateLastUsed_concurrent(self):
        # If the token is locked by another transaction, we leave it alone.
        owner = self.factory.makePerson()
        owner_email = removeSecurityProxy(owner.preferredemail).email
        secret, token = self.factory.makeAccessToken(
            owner=owner, target=self.makeTarget()
        )
        login_person(owner)
        self.assertIsNone(token.date_last_used)
        transaction.commit()
        # Fork so that we can lock the token from a different PostgreSQL
        # session.  We must disconnect the Storm store before forking, as
        # libpq connections are not safe for use across forks.
        disconnect_stores()
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            os.close(read)
            login(owner_email)
            token = getUtility(IAccessTokenSet).getBySecret(secret)
            token.updateLastUsed()
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
                login(owner_email)
                token = getUtility(IAccessTokenSet).getBySecret(secret)
                token.updateLastUsed()
                now = get_transaction_timestamp(Store.of(token))
                # The last-used date is being updated by a different
                # transaction, which hasn't been committed yet.
                self.assertIsNone(token.date_last_used)
            finally:
                os.kill(pid, signal.SIGINT)
                os.waitpid(pid, 0)
            transaction.commit()
            self.assertIsNotNone(token.date_last_used)
            self.assertNotEqual(now, token.date_last_used)

    def test_is_expired(self):
        owner = self.factory.makePerson()
        login_person(owner)
        _, current_token = self.factory.makeAccessToken(
            owner=owner, target=self.makeTarget()
        )
        _, expired_token = self.factory.makeAccessToken(
            owner=owner,
            date_expires=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        self.assertFalse(current_token.is_expired)
        self.assertTrue(expired_token.is_expired)

    def test_revoke(self):
        owner = self.factory.makePerson()
        _, token = self.factory.makeAccessToken(
            owner=owner,
            scopes=[AccessTokenScope.REPOSITORY_BUILD_STATUS],
            target=self.makeTarget(),
        )
        login_person(owner)
        self.assertThat(
            token, MatchesStructure(date_expires=Is(None), revoked_by=Is(None))
        )
        token.revoke(token.owner)
        now = get_transaction_timestamp(Store.of(token))
        self.assertThat(
            token,
            MatchesStructure.byEquality(
                date_expires=now, revoked_by=token.owner
            ),
        )


class TestAccessTokenGitRepository(TestAccessTokenBase, TestCaseWithFactory):
    def makeTarget(self, owner=None):
        return self.factory.makeGitRepository(owner=owner)


class TestAccessTokenProduct(TestAccessTokenBase, TestCaseWithFactory):
    def makeTarget(self, owner=None):
        return self.factory.makeProduct(owner=owner)


class TestAccessTokenSetBase:
    layer = DatabaseFunctionalLayer

    def test_new(self):
        secret = create_access_token_secret()
        self.assertEqual(64, len(secret))
        owner = self.factory.makePerson()
        description = "Test token"
        target = self.makeTarget()
        scopes = [AccessTokenScope.REPOSITORY_BUILD_STATUS]
        _, token = self.factory.makeAccessToken(
            secret=secret,
            owner=owner,
            description=description,
            target=target,
            scopes=scopes,
        )
        self.assertThat(
            removeSecurityProxy(token),
            MatchesStructure.byEquality(
                _token_sha256=hashlib.sha256(secret.encode()).hexdigest(),
                owner=owner,
                description=description,
                target=target,
                scopes=scopes,
            ),
        )

    def test_getByID(self):
        _, token = self.factory.makeAccessToken(target=self.makeTarget())
        token_id = removeSecurityProxy(token).id
        self.assertEqual(token, getUtility(IAccessTokenSet).getByID(token_id))
        self.assertIsNone(getUtility(IAccessTokenSet).getByID(token_id + 1))

    def test_getBySecret(self):
        secret, token = self.factory.makeAccessToken(target=self.makeTarget())
        self.assertEqual(
            token, getUtility(IAccessTokenSet).getBySecret(secret)
        )
        self.assertIsNone(
            getUtility(IAccessTokenSet).getBySecret(
                create_access_token_secret()
            )
        )

    def test_findByOwner(self):
        owners = [self.factory.makePerson() for _ in range(3)]
        tokens = [
            self.factory.makeAccessToken(
                owner=owners[0], target=self.makeTarget()
            )[1],
            self.factory.makeAccessToken(
                owner=owners[0], target=self.makeTarget()
            )[1],
            self.factory.makeAccessToken(
                owner=owners[1], target=self.makeTarget()
            )[1],
        ]
        self.assertContentEqual(
            tokens[:2], getUtility(IAccessTokenSet).findByOwner(owners[0])
        )
        self.assertContentEqual(
            [tokens[2]], getUtility(IAccessTokenSet).findByOwner(owners[1])
        )
        self.assertContentEqual(
            [], getUtility(IAccessTokenSet).findByOwner(owners[2])
        )

    def test_findByTarget(self):
        targets = [self.makeTarget() for _ in range(3)]
        tokens = [
            self.factory.makeAccessToken(target=targets[0])[1],
            self.factory.makeAccessToken(target=targets[0])[1],
            self.factory.makeAccessToken(target=targets[1])[1],
        ]
        self.assertContentEqual(
            tokens[:2], getUtility(IAccessTokenSet).findByTarget(targets[0])
        )
        self.assertContentEqual(
            [tokens[2]], getUtility(IAccessTokenSet).findByTarget(targets[1])
        )
        self.assertContentEqual(
            [], getUtility(IAccessTokenSet).findByTarget(targets[2])
        )

    def test_findByTarget_visible_by_user(self):
        targets = [self.makeTarget() for _ in range(3)]
        owners = [self.factory.makePerson() for _ in range(3)]
        tokens = [
            self.factory.makeAccessToken(
                owner=owners[owner_index], target=targets[target_index]
            )[1]
            for owner_index, target_index in (
                (0, 0),
                (0, 0),
                (1, 0),
                (1, 1),
                (2, 1),
            )
        ]
        for owner_index, target_index, expected_tokens in (
            (0, 0, tokens[:2]),
            (0, 1, []),
            (0, 2, []),
            (1, 0, [tokens[2]]),
            (1, 1, [tokens[3]]),
            (1, 2, []),
            (2, 0, []),
            (2, 1, [tokens[4]]),
            (2, 2, []),
        ):
            self.assertContentEqual(
                expected_tokens,
                getUtility(IAccessTokenSet).findByTarget(
                    targets[target_index], visible_by_user=owners[owner_index]
                ),
            )

    def test_findByTarget_visible_by_owner(self):
        # If a user owns a target, they can see all the tokens for that target
        # regadless if they own the tokens.
        owner = self.factory.makePerson()
        owned_target = self.makeTarget(owner=owner)
        owned_target_tokens = [
            self.factory.makeAccessToken(target=owned_target)[1],
            self.factory.makeAccessToken(target=owned_target)[1],
        ]
        self.factory.makeAccessToken(target=self.makeTarget())[1]
        self.factory.makeAccessToken(target=self.makeTarget())[1]

        self.assertContentEqual(
            owned_target_tokens,
            getUtility(IAccessTokenSet).findByTarget(
                owned_target, visible_by_user=owner
            ),
        )

    def test_findByTarget_visible_by_team_member(self):
        # If a user belongs to a team that owns a target, they can see all the
        # tokens for that target regardless if they own the tokens.

        # Only restricted or moderated teams can own certain targets (Product)
        team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.RESTRICTED
        )
        team_member = self.factory.makePerson()
        with person_logged_in(team.teamowner):
            team.addMember(team_member, team)

        owned_target = self.makeTarget(owner=team)
        owned_target_tokens = [
            self.factory.makeAccessToken(target=owned_target)[1],
            self.factory.makeAccessToken(target=owned_target)[1],
        ]

        self.factory.makeAccessToken(target=self.makeTarget())[1]
        self.factory.makeAccessToken(target=self.makeTarget())[1]

        self.assertContentEqual(
            owned_target_tokens,
            getUtility(IAccessTokenSet).findByTarget(
                owned_target, visible_by_user=team_member
            ),
        )

    def test_findByTarget_visible_by_user_inactive_target(self):
        # User cannot see tokens for inactive targets.

        owner = self.factory.makePerson()
        target = self.makeTarget(owner=owner)

        if IGitRepository.providedBy(target):
            self.skipTest(
                "Currently, Git repositories allow requests without scopes."
            )

        removeSecurityProxy(target).active = False
        self.factory.makeAccessToken(target=target, owner=owner)[1]

        self.assertContentEqual(
            [],
            getUtility(IAccessTokenSet).findByTarget(
                target, visible_by_user=owner
            ),
        )

    def test_findByTarget_visible_by_user_from_a_team_that_owns_token(self):
        # Method with a visible_by_user parameter, returns only tokens that
        # are owned by the `visible_by_user` Person or by a team that the
        # `visible_by_user` Person belongs to.
        target = self.makeTarget()
        team = self.factory.makeTeam()
        team_member = self.factory.makePerson()
        with person_logged_in(team.teamowner):
            team.addMember(team_member, team)
        _, token = self.factory.makeAccessToken(owner=team, target=target)
        self.factory.makeAccessToken(target=target)

        for user in [team.teamowner, team_member]:
            self.assertContentEqual(
                [token],
                getUtility(IAccessTokenSet).findByTarget(
                    target, visible_by_user=user
                ),
            )

    def test_findByTarget_excludes_expired(self):
        target = self.makeTarget()
        _, current_token = self.factory.makeAccessToken(target=target)
        _, expires_soon_token = self.factory.makeAccessToken(
            target=target,
            date_expires=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        _, expired_token = self.factory.makeAccessToken(
            target=target,
            date_expires=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        self.assertContentEqual(
            [current_token, expires_soon_token],
            getUtility(IAccessTokenSet).findByTarget(target),
        )
        self.assertContentEqual(
            [current_token, expires_soon_token, expired_token],
            getUtility(IAccessTokenSet).findByTarget(
                target, include_expired=True
            ),
        )

    def test_getByTargetAndID(self):
        targets = [self.makeTarget() for _ in range(3)]
        tokens = [
            self.factory.makeAccessToken(target=targets[0])[1],
            self.factory.makeAccessToken(target=targets[0])[1],
            self.factory.makeAccessToken(target=targets[1])[1],
        ]
        self.assertEqual(
            tokens[0],
            getUtility(IAccessTokenSet).getByTargetAndID(
                targets[0], removeSecurityProxy(tokens[0]).id
            ),
        )
        self.assertEqual(
            tokens[1],
            getUtility(IAccessTokenSet).getByTargetAndID(
                targets[0], removeSecurityProxy(tokens[1]).id
            ),
        )
        self.assertIsNone(
            getUtility(IAccessTokenSet).getByTargetAndID(
                targets[0], removeSecurityProxy(tokens[2]).id
            )
        )

    def test_getByTargetAndID_visible_by_user(self):
        targets = [self.makeTarget() for _ in range(3)]
        owners = [self.factory.makePerson() for _ in range(3)]
        tokens = [
            self.factory.makeAccessToken(
                owner=owners[owner_index], target=targets[target_index]
            )[1]
            for owner_index, target_index in (
                (0, 0),
                (0, 0),
                (1, 0),
                (1, 1),
                (2, 1),
            )
        ]
        for owner_index, target_index, expected_tokens in (
            (0, 0, tokens[:2]),
            (0, 1, []),
            (0, 2, []),
            (1, 0, [tokens[2]]),
            (1, 1, [tokens[3]]),
            (1, 2, []),
            (2, 0, []),
            (2, 1, [tokens[4]]),
            (2, 2, []),
        ):
            for token in tokens:
                fetched_token = getUtility(IAccessTokenSet).getByTargetAndID(
                    targets[target_index],
                    removeSecurityProxy(token).id,
                    visible_by_user=owners[owner_index],
                )
                if token in expected_tokens:
                    self.assertEqual(token, fetched_token)
                else:
                    self.assertIsNone(fetched_token)

    def test_getByTargetAndID_excludes_expired(self):
        target = self.makeTarget()
        _, current_token = self.factory.makeAccessToken(target=target)
        _, expires_soon_token = self.factory.makeAccessToken(
            target=target,
            date_expires=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        _, expired_token = self.factory.makeAccessToken(
            target=target,
            date_expires=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        self.assertEqual(
            current_token,
            getUtility(IAccessTokenSet).getByTargetAndID(
                target, removeSecurityProxy(current_token).id
            ),
        )
        self.assertEqual(
            expires_soon_token,
            getUtility(IAccessTokenSet).getByTargetAndID(
                target, removeSecurityProxy(expires_soon_token).id
            ),
        )
        self.assertIsNone(
            getUtility(IAccessTokenSet).getByTargetAndID(
                target, removeSecurityProxy(expired_token).id
            )
        )


class TestGitRepositoryAccessTokenSet(
    TestAccessTokenSetBase, TestCaseWithFactory
):
    def makeTarget(self, **kwargs):
        return self.factory.makeGitRepository(**kwargs)


class TestProjectAccessTokenSet(TestAccessTokenSetBase, TestCaseWithFactory):
    def makeTarget(self, **kwargs):
        return self.factory.makeProduct(**kwargs)


class TestAccessTokenTargetBase:
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.target = self.makeTarget()
        self.owner = self.target.owner
        self.target_url = api_url(self.target)
        self.webservice = webservice_for_person(
            self.owner, permission=OAuthPermission.WRITE_PRIVATE
        )

    def _makePerson(self):
        with person_logged_in(ANONYMOUS):
            return self.factory.makePerson()

    def test_getAccessTokens(self):
        with person_logged_in(self.owner):
            for description in ("Test token 1", "Test token 2"):
                self.factory.makeAccessToken(
                    owner=self.owner,
                    description=description,
                    target=self.target,
                )
        response = self.webservice.named_get(
            self.target_url, "getAccessTokens", api_version="devel"
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ["Test token 1", "Test token 2"],
            [entry["description"] for entry in response.jsonBody()["entries"]],
        )

    def test_getAccessTokens_excludes_expired(self):
        with person_logged_in(self.owner):
            self.factory.makeAccessToken(
                owner=self.owner, description="Current", target=self.target
            )
            self.factory.makeAccessToken(
                owner=self.owner,
                description="Expired",
                target=self.target,
                date_expires=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        response = self.webservice.named_get(
            self.target_url, "getAccessTokens", api_version="devel"
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ["Current"],
            [entry["description"] for entry in response.jsonBody()["entries"]],
        )

    def test_getAccessTokens_permissions(self):
        webservice = webservice_for_person(None)
        response = webservice.named_get(
            self.target_url, "getAccessTokens", api_version="devel"
        )
        self.assertEqual(401, response.status)

    def test_getAccessTokens_viewer_sees_only_own(self):
        requester = self._makePerson()
        with person_logged_in(self.owner):
            self.factory.makeAccessToken(
                owner=self.owner, description="Owner", target=self.target
            )
        with person_logged_in(requester):
            self.factory.makeAccessToken(
                owner=requester, description="Mine", target=self.target
            )
        webservice = webservice_for_person(
            requester, permission=OAuthPermission.WRITE_PUBLIC
        )
        response = webservice.named_get(
            self.target_url, "getAccessTokens", api_version="devel"
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ["Mine"],
            [entry["description"] for entry in response.jsonBody()["entries"]],
        )

    def test_issueAccessToken_push_requires_edit(self):
        requester = self._makePerson()
        webservice = webservice_for_person(
            requester,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = webservice.named_post(
            self.target_url,
            "issueAccessToken",
            description="Push token",
            scopes=["repository:push"],
        )
        self.assertEqual(401, response.status)
        response = webservice.named_post(
            self.target_url,
            "issueAccessToken",
            description="Pull token",
            scopes=["repository:pull"],
        )
        self.assertEqual(200, response.status)
        response = self.webservice.named_post(
            self.target_url,
            "issueAccessToken",
            description="Owner push token",
            scopes=["repository:push"],
            api_version="devel",
        )
        self.assertEqual(200, response.status)

    def test_getAccessTokens_query_count(self):
        def get_tokens():
            response = self.webservice.named_get(
                self.target_url, "getAccessTokens", api_version="devel"
            )
            self.assertEqual(200, response.status)
            self.assertIn(len(response.jsonBody()["entries"]), {0, 2, 4})

        def create_token():
            with person_logged_in(self.owner):
                self.factory.makeAccessToken(
                    owner=self.owner, target=self.target
                )

        get_tokens()
        recorder1, recorder2 = record_two_runs(get_tokens, create_token, 2)
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    def test_issueAccessToken(self):
        # A user can request a personal access token via the webservice API.
        # Write access to the repositories isn't checked at this stage
        # (although the access token will only be useful if the user has
        # some kind of write access).
        requester = self._makePerson()
        with person_logged_in(requester):
            target_url = api_url(self.target)
        webservice = webservice_for_person(
            requester,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = webservice.named_post(
            target_url,
            "issueAccessToken",
            description="Test token",
            scopes=["repository:build_status"],
        )
        self.assertEqual(200, response.status)
        secret = response.jsonBody()
        with person_logged_in(requester):
            token = getUtility(IAccessTokenSet).getBySecret(secret)
            self.assertThat(
                token,
                MatchesStructure(
                    owner=Equals(requester),
                    description=Equals("Test token"),
                    target=Equals(self.target),
                    scopes=Equals([AccessTokenScope.REPOSITORY_BUILD_STATUS]),
                    date_expires=Is(None),
                ),
            )

    def test_issueAccessToken_with_expiry(self):
        # A user can set an expiry time when requesting a personal access
        # token via the webservice API.
        # Write access to the repositories isn't checked at this stage
        # (although the access token will only be useful if the user has
        # some kind of write access).
        requester = self._makePerson()
        with person_logged_in(requester):
            target_url = api_url(self.target)
        webservice = webservice_for_person(
            requester,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        date_expires = datetime.now(timezone.utc) + timedelta(days=30)
        response = webservice.named_post(
            target_url,
            "issueAccessToken",
            description="Test token",
            scopes=["repository:build_status"],
            date_expires=date_expires.isoformat(),
        )
        self.assertEqual(200, response.status)
        secret = response.jsonBody()
        with person_logged_in(requester):
            token = getUtility(IAccessTokenSet).getBySecret(secret)
            self.assertThat(
                token,
                MatchesStructure.byEquality(
                    owner=requester,
                    description="Test token",
                    target=self.target,
                    scopes=[AccessTokenScope.REPOSITORY_BUILD_STATUS],
                    date_expires=date_expires,
                ),
            )

    def test_issueAccessToken_anonymous(self):
        # An anonymous user cannot request a personal access token via the
        # webservice API.
        with person_logged_in(self.owner):
            target_url = api_url(self.target)
        webservice = webservice_for_person(None, default_api_version="devel")
        response = webservice.named_post(
            target_url,
            "issueAccessToken",
            description="Test token",
            scopes=["repository:build_status"],
        )
        self.assertEqual(401, response.status)
        self.assertEqual(
            b"Personal access tokens may only be issued for a logged-in user.",
            response.body,
        )

    def test_issueAccessToken_no_scope(self):
        # Scopes are required to request a personal access token

        if IGitRepository.providedBy(self.target):
            self.skipTest(
                "Currently, Git repositories allow requests without scopes."
            )

        with person_logged_in(self.owner):
            target_url = api_url(self.target)
        webservice = webservice_for_person(
            self.owner,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = webservice.named_post(
            target_url,
            "issueAccessToken",
            description="Test token",
        )
        self.assertEqual(400, response.status)
        self.assertEqual(
            b"scopes: Required input is missing.",
            response.body,
        )

    def test_issueAccessToken_no_description(self):
        # A description is required to request a personal access token

        if IGitRepository.providedBy(self.target):
            self.skipTest(
                "Currently, Git repositories allow requests without a "
                "description."
            )

        with person_logged_in(self.owner):
            target_url = api_url(self.target)
        webservice = webservice_for_person(
            self.owner,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = webservice.named_post(
            target_url,
            "issueAccessToken",
            scopes=["repository:build_status"],
        )
        self.assertEqual(400, response.status)
        self.assertEqual(
            b"description: Required input is missing.",
            response.body,
        )


class TestAccessTokenTargetGitRepository(
    TestAccessTokenTargetBase, TestCaseWithFactory
):
    def makeTarget(self):
        return self.factory.makeGitRepository()

    def test_issueAccessTokenMacaroon(self):
        # A user can request a git repository access token via the webservice
        # API without scopes and description (a Macaroon is returned)
        self.pushConfig("codehosting", git_macaroon_secret_key="some-secret")
        # Write access to the repositories isn't checked at this stage
        # (although the access token will only be useful if the user has
        # some kind of write access).
        requester = self._makePerson()
        with person_logged_in(requester):
            target_url = api_url(self.target)
        webservice = webservice_for_person(
            requester,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = webservice.named_post(target_url, "issueAccessToken")
        self.assertEqual(200, response.status)
        macaroon = Macaroon.deserialize(response.jsonBody())
        with person_logged_in(ANONYMOUS):
            self.assertThat(
                macaroon,
                MatchesStructure(
                    location=Equals(config.vhost.mainsite.hostname),
                    identifier=Equals("git-repository"),
                    caveats=MatchesListwise(
                        [
                            MatchesStructure.byEquality(
                                caveat_id="lp.git-repository %s"
                                % self.target.id
                            ),
                            MatchesStructure(
                                caveat_id=StartsWith(
                                    "lp.principal.openid-identifier "
                                )
                            ),
                            MatchesStructure(
                                caveat_id=StartsWith("lp.expires ")
                            ),
                        ]
                    ),
                ),
            )

    def test_issueAccessTokenMacaroon_anonymous(self):
        # An anonymous user cannot request a git repository access token
        # without scopes and description (Macaroon) via the webservice
        with person_logged_in(self.owner):
            target_url = api_url(self.target)
        webservice = webservice_for_person(None, default_api_version="devel")
        response = webservice.named_post(target_url, "issueAccessToken")
        self.assertEqual(401, response.status)
        self.assertEqual(
            b"git-repository macaroons may only be issued for a logged-in "
            b"user.",
            response.body,
        )


class TestAccessTokenTargetProject(
    TestAccessTokenTargetBase, TestCaseWithFactory
):
    def makeTarget(self):
        return self.factory.makeProduct()
