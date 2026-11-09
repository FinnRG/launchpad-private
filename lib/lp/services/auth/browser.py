# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""UI for personal access tokens."""

__all__ = [
    "AccessTokensView",
]

from lazr.restful.interface import copy_field, use_template
from zope.component import getUtility
from zope.formlib.form import FormFields
from zope.interface import Interface
from zope.schema import Choice, List
from zope.schema.vocabulary import SimpleVocabulary
from zope.security.interfaces import Unauthorized

from lp import _
from lp.app.browser.launchpadform import LaunchpadFormView, action
from lp.app.errors import UnexpectedFormData
from lp.app.widgets.date import DateTimeWidget
from lp.app.widgets.itemswidgets import LabeledMultiCheckBoxWidget
from lp.code.interfaces.gitrepository import IGitRepository
from lp.registry.interfaces.product import IProduct
from lp.services.auth.enums import AccessTokenScope
from lp.services.auth.interfaces import (
    IAccessToken,
    IAccessTokenSet,
    IAccessTokenTarget,
)
from lp.services.propertycache import cachedproperty
from lp.services.webapp.authorization import check_permission
from lp.services.webapp.publisher import canonical_url


class IAccessTokenCreateSchema(Interface):
    """Schema for creating a personal access token."""

    use_template(
        IAccessToken,
        include=[
            "description",
            "scopes",
        ],
    )

    date_expires = copy_field(
        IAccessToken["date_expires"],
        description=_("When the token should expire."),
    )


class AccessTokensView(LaunchpadFormView):
    schema = IAccessTokenCreateSchema
    custom_widget_scopes = LabeledMultiCheckBoxWidget
    custom_widget_date_expires = DateTimeWidget

    @property
    def label(self):
        return "Personal access tokens for %s" % self.context.display_name

    page_title = "Personal access tokens"

    def initialize(self):
        if self.user is None:
            raise Unauthorized("Please log in to manage access tokens.")
        super().initialize()

    def setUpFields(self):
        super().setUpFields()
        if not check_permission("launchpad.Edit", self.context):
            # People who can't edit the target can't push to it, so don't
            # offer the push scope.
            terms = [
                AccessTokenScope.getTermByToken(item.title)
                for item in AccessTokenScope.items
                if item != AccessTokenScope.REPOSITORY_PUSH
            ]
            field = List(
                __name__="scopes",
                title=IAccessToken["scopes"].title,
                description=IAccessToken["scopes"].description,
                value_type=Choice(vocabulary=SimpleVocabulary(terms)),
                required=True,
            )
            self.form_fields = FormFields(
                *(
                    field if ff.__name__ == "scopes" else ff.field
                    for ff in self.form_fields
                )
            )

    @property
    def context_type(self):
        if IGitRepository.providedBy(self.context):
            return "Git repository"
        elif IProduct.providedBy(self.context):
            return "project"
        return "target"

    @cachedproperty
    def access_tokens(self):
        return list(
            getUtility(IAccessTokenSet).findByTarget(
                self.context, visible_by_user=self.user
            )
        )

    @cachedproperty
    def parent_tokens(self):
        if IGitRepository.providedBy(
            self.context
        ) and IAccessTokenTarget.providedBy(self.context.target):
            return list(
                getUtility(IAccessTokenSet).findByTarget(
                    self.context.target, visible_by_user=self.user
                )
            )
        return []

    @action("Revoke", name="revoke")
    def revoke_action(self, action, data):
        form = self.request.form
        token_id = form.get("token_id")
        if token_id is None:
            raise UnexpectedFormData("Missing token_id")
        try:
            token_id = int(token_id)
        except ValueError:
            raise UnexpectedFormData("token_id is not an integer")
        token = getUtility(IAccessTokenSet).getByTargetAndID(
            self.context, token_id, visible_by_user=self.user
        )
        if token is not None:
            token.revoke(self.user)
            self.request.response.addInfoNotification(
                "Token revoked successfully."
            )
        self.request.response.redirect(
            canonical_url(self.context, view_name="+access-tokens")
        )
