# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Security adapters for the lp.services.oauth package."""

__all__ = []

from lp.app.security import AuthorizationBase
from lp.services.oauth.interfaces import (
    IOAuthAccessToken,
    IOAuthRefreshToken,
    IOAuthRequestToken,
)


class EditOAuthAccessToken(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IOAuthAccessToken

    def checkAuthenticated(self, user):
        return self.obj.person == user.person or user.in_admin


class EditOAuthRequestToken(EditOAuthAccessToken):
    permission = "launchpad.Edit"
    usedfor = IOAuthRequestToken


class EditOAuthRefreshToken(EditOAuthAccessToken):
    permission = "launchpad.Edit"
    usedfor = IOAuthRefreshToken
