# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Security adapters for the lp.services.webauthn package."""

__all__ = []

from lp.app.security import AuthorizationBase
from lp.services.webauthn.interfaces import IRegisteredAuthenticator


class EditRegisteredAuthenticator(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IRegisteredAuthenticator

    def checkAuthenticated(self, user):
        return user.id == self.obj.owner.id or user.in_admin
