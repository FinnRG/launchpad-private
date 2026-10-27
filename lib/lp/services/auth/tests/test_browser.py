# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test personal access token views."""

import re

import soupmatchers
from testtools.matchers import (
    Equals,
    Is,
    MatchesAll,
    MatchesListwise,
    MatchesStructure,
    Not,
)
from zope.component import getUtility
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.code.interfaces.gitrepository import IGitRepository
from lp.services.webapp.interfaces import IPlacelessAuthUtility
from lp.services.webapp.publisher import canonical_url
from lp.testing import ANONYMOUS, TestCaseWithFactory, login, login_person
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.views import create_view

breadcrumbs_tag = soupmatchers.Tag(
    "breadcrumbs", "ol", attrs={"class": "breadcrumbs"}
)
tokens_page_crumb_tag = soupmatchers.Tag(
    "tokens page breadcrumb", "li", text=re.compile(r"Personal access tokens")
)
token_listing_constants = soupmatchers.HTMLContains(
    soupmatchers.Within(breadcrumbs_tag, tokens_page_crumb_tag)
)
token_listing_tag = soupmatchers.Tag(
    "tokens table", "table", attrs={"id": "access_tokens"}
)

project_token_listing_tag = soupmatchers.Tag(
    "project tokens table",
    "table",
    attrs={"id": "parent_tokens"},
)


class TestAccessTokenViewBase:
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.target = self.makeTarget()
        self.owner = self.target.owner
        login_person(self.owner)

    def makeView(self, name, **kwargs):
        # XXX cjwatson 2021-10-19: We need to give the view a
        # LaunchpadPrincipal rather than just a person, since otherwise bits
        # of the navigation menu machinery try to use the scope_url
        # attribute on the principal and fail.  This should probably be done
        # in create_view instead, but that approach needs care to avoid
        # adding an extra query to tests that might be sensitive to that.
        principal = getUtility(IPlacelessAuthUtility).getPrincipal(
            self.owner.account_id
        )
        view = create_view(
            self.target,
            name,
            principal=principal,
            current_request=True,
            **kwargs,
        )
        # To test the breadcrumbs we need a correct traversal stack.
        view.request.traversed_objects = self.getTraversalStack(
            self.target
        ) + [view]
        # The navigation menu machinery needs this to find the view from the
        # request.
        view.request._last_obj_traversed = view
        view.initialize()
        return view

    def test_access_tokens_link(self):
        target_url = canonical_url(self.target, rootsite=self.rootsite)
        expected_tokens_url = canonical_url(
            self.target, view_name="+access-tokens", rootsite=self.rootsite
        )
        browser = self.getUserBrowser(target_url, user=self.owner)
        tokens_link = browser.getLink("Manage access tokens")
        self.assertEqual(expected_tokens_url, tokens_link.url)

    def test_viewer_cannot_select_push_scope(self):
        viewer = self.factory.makePerson()
        login_person(viewer)
        principal = getUtility(IPlacelessAuthUtility).getPrincipal(
            viewer.account_id
        )
        view = create_view(
            self.target,
            "+access-tokens",
            principal=principal,
            current_request=True,
        )
        view.initialize()
        scope_titles = [
            term.token for term in view.widgets["scopes"].vocabulary
        ]
        self.assertNotIn("REPOSITORY_PUSH", scope_titles)
        self.assertIn("REPOSITORY_PULL", scope_titles)

    def test_anonymous_unauthorized(self):
        login(ANONYMOUS)
        view = create_view(self.target, "+access-tokens")
        self.assertRaises(Unauthorized, view.initialize)

    def makeTokensAndMatchers(self, count, target=None):
        if target is None:
            target = self.target
        tokens = [
            self.factory.makeAccessToken(target=target)[1]
            for _ in range(count)
        ]
        # There is a row for each token.
        matchers = []
        for token in tokens:
            row_tag = soupmatchers.Tag(
                "token row",
                "tr",
                attrs={"token-id": removeSecurityProxy(token).id},
            )
            column_tags = [
                soupmatchers.Tag("description", "td", text=token.description),
                soupmatchers.Tag(
                    "scopes",
                    "td",
                    text=", ".join(scope.title for scope in token.scopes),
                ),
            ]
            matchers.extend(
                [
                    soupmatchers.Within(row_tag, column_tag)
                    for column_tag in column_tags
                ]
            )
        return matchers

    def test_empty(self):
        self.assertThat(
            self.makeView("+access-tokens")(),
            MatchesAll(*self.getPageContent(token_matchers=[])),
        )

    def test_existing_tokens(self):
        token_matchers = self.makeTokensAndMatchers(10)
        self.assertThat(
            self.makeView("+access-tokens")(),
            MatchesAll(*self.getPageContent(token_matchers)),
        )

    def test_extra_tokens(self):
        if not IGitRepository.providedBy(self.target):
            self.skipTest("Test only relevant for Git Repository tests.")

        project = self.factory.makeProduct(owner=self.owner)
        self.target.setTarget(target=project, user=self.owner)
        token_matchers = self.makeTokensAndMatchers(5)
        extra_matchers = self.makeTokensAndMatchers(5, target=project)
        self.assertThat(
            self.makeView("+access-tokens")(),
            MatchesAll(
                *self.getPageContent(
                    token_matchers, extra_matchers=extra_matchers
                )
            ),
        )

    def test_revoke(self):
        tokens = [
            self.factory.makeAccessToken(target=self.target)[1]
            for _ in range(3)
        ]
        token_ids = [token.id for token in tokens]
        access_tokens_url = canonical_url(
            self.target, view_name="+access-tokens"
        )
        browser = self.getUserBrowser(access_tokens_url, user=self.owner)
        for token_id in token_ids:
            self.assertThat(
                browser.getForm(name="revoke-%s" % token_id).controls,
                MatchesListwise(
                    [
                        MatchesStructure.byEquality(
                            type="hidden", name="token_id", value=str(token_id)
                        ),
                        MatchesStructure.byEquality(
                            type="submit",
                            name="field.actions.revoke",
                            value="Revoke",
                        ),
                    ]
                ),
            )
        browser.getForm(name="revoke-%s" % token_ids[1]).getControl(
            "Revoke"
        ).click()
        login_person(self.owner)
        self.assertEqual(access_tokens_url, browser.url)
        self.assertThat(
            tokens[0],
            MatchesStructure(
                id=Equals(token_ids[0]),
                date_expires=Is(None),
                revoked_by=Is(None),
            ),
        )
        self.assertThat(
            tokens[1],
            MatchesStructure(
                id=Equals(token_ids[1]),
                date_expires=Not(Is(None)),
                revoked_by=Equals(self.owner),
            ),
        )
        self.assertThat(
            tokens[2],
            MatchesStructure(
                id=Equals(token_ids[2]),
                date_expires=Is(None),
                revoked_by=Is(None),
            ),
        )


class TestAccessTokenViewGitRepository(
    TestAccessTokenViewBase, TestCaseWithFactory
):
    rootsite = "code"

    def makeTarget(self):
        return self.factory.makeGitRepository()

    def getTraversalStack(self, obj):
        return [obj.target, obj]

    def getPageContent(self, token_matchers, extra_matchers=None):
        contents = [
            token_listing_constants,
            soupmatchers.HTMLContains(token_listing_tag, *token_matchers),
        ]

        if extra_matchers is not None:
            contents.append(
                soupmatchers.HTMLContains(
                    project_token_listing_tag, *extra_matchers
                )
            )
        return contents


class TestAccessTokenViewProject(TestAccessTokenViewBase, TestCaseWithFactory):
    rootsite = None

    def makeTarget(self):
        return self.factory.makeProduct()

    def getTraversalStack(self, obj):
        return [obj]

    def getPageContent(self, token_matchers):
        return [soupmatchers.HTMLContains(token_listing_tag, *token_matchers)]
