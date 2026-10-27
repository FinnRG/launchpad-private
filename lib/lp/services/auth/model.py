# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Personal access tokens."""

__all__ = [
    "AccessToken",
    "AccessTokenTargetMixin",
]

import hashlib
from datetime import datetime, timedelta, timezone

from storm.databases.postgres import JSON
from storm.expr import SQL, And, Cast, Or, Select, Update
from storm.locals import DateTime, Int, Reference, Unicode
from zope.component import getUtility
from zope.interface import implementer
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.code.interfaces.gitcollection import IAllGitRepositories
from lp.code.interfaces.gitrepository import IGitRepository
from lp.registry.interfaces.product import IProduct
from lp.registry.model.teammembership import TeamParticipation
from lp.services.auth.enums import AccessTokenScope
from lp.services.auth.interfaces import IAccessToken, IAccessTokenSet
from lp.services.auth.utils import create_access_token_secret
from lp.services.database.constants import UTC_NOW
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.stormbase import StormBase
from lp.services.webapp.authorization import check_permission
from lp.services.webapp.interfaces import ILaunchBag


@implementer(IAccessToken)
class AccessToken(StormBase):
    """See `IAccessToken`."""

    __storm_table__ = "AccessToken"

    id = Int(primary=True)

    date_created = DateTime(
        name="date_created", tzinfo=timezone.utc, allow_none=False
    )

    _token_sha256 = Unicode(name="token_sha256", allow_none=False)

    owner_id = Int(name="owner", allow_none=False)
    owner = Reference(owner_id, "Person.id")

    description = Unicode(name="description", allow_none=False)

    git_repository_id = Int(name="git_repository", allow_none=True)
    git_repository = Reference(git_repository_id, "GitRepository.id")

    project_id = Int(name="project", allow_none=True)
    project = Reference(project_id, "Product.id")

    _scopes = JSON(name="scopes", allow_none=False)

    date_last_used = DateTime(
        name="date_last_used", tzinfo=timezone.utc, allow_none=True
    )
    date_expires = DateTime(
        name="date_expires", tzinfo=timezone.utc, allow_none=True
    )

    revoked_by_id = Int(name="revoked_by", allow_none=True)
    revoked_by = Reference(revoked_by_id, "Person.id")

    resolution = timedelta(minutes=10)

    def __init__(
        self, secret, owner, description, target, scopes, date_expires=None
    ):
        """Construct an `AccessToken`."""
        self._token_sha256 = hashlib.sha256(secret.encode()).hexdigest()
        self.owner = owner
        self.description = description
        if IGitRepository.providedBy(target):
            self.git_repository = target
        elif IProduct.providedBy(target):
            self.project = target
        else:
            raise TypeError(f"Unsupported target: {target!r}")
        self.scopes = scopes
        self.date_created = UTC_NOW
        self.date_expires = date_expires

    @property
    def target(self):
        """See `IAccessToken`."""
        return self.git_repository or self.project

    @property
    def scopes(self):
        """See `IAccessToken`."""
        return [
            AccessTokenScope.getTermByToken(scope).value
            for scope in self._scopes
        ]

    @scopes.setter
    def scopes(self, scopes):
        """See `IAccessToken`."""
        self._scopes = [scope.title for scope in scopes]

    def updateLastUsed(self):
        """See `IAccessToken`."""
        store = IPrimaryStore(AccessToken)
        store.execute(
            Update(
                {AccessToken.date_last_used: UTC_NOW},
                where=And(
                    # Skip the update if the AccessToken row is already locked,
                    # for example by a concurrent request using the same token.
                    AccessToken.id.is_in(
                        SQL(
                            "SELECT id FROM AccessToken WHERE id = ? "
                            "FOR UPDATE SKIP LOCKED",
                            params=(self.id,),
                        )
                    ),
                    # Only update the last-used date every so often, to avoid
                    # bloat.
                    Or(
                        AccessToken.date_last_used == None,
                        AccessToken.date_last_used
                        < UTC_NOW - Cast(self.resolution, "interval"),
                    ),
                ),
                table=AccessToken,
            )
        )
        store.invalidate(self)

    @property
    def is_expired(self):
        now = datetime.now(timezone.utc)
        return self.date_expires is not None and self.date_expires <= now

    def revoke(self, revoked_by):
        """See `IAccessToken`."""
        self.date_expires = UTC_NOW
        self.revoked_by = revoked_by


@implementer(IAccessTokenSet)
class AccessTokenSet:
    def new(
        self, secret, owner, description, target, scopes, date_expires=None
    ):
        """See `IAccessTokenSet`."""
        store = IStore(AccessToken)
        token = AccessToken(
            secret,
            owner,
            description,
            target,
            scopes,
            date_expires=date_expires,
        )
        store.add(token)
        return token

    def getByID(self, token_id):
        """See `IAccessTokenSet`."""
        return IStore(AccessToken).get(AccessToken, token_id)

    def getBySecret(self, secret):
        """See `IAccessTokenSet`."""
        return (
            IStore(AccessToken)
            .find(
                AccessToken,
                _token_sha256=hashlib.sha256(secret.encode()).hexdigest(),
            )
            .one()
        )

    def findByOwner(self, owner):
        """See `IAccessTokenSet`."""
        return IStore(AccessToken).find(AccessToken, owner=owner)

    def findByTarget(
        self, target, visible_by_user=None, include_expired=False
    ):
        """See `IAccessTokenSet`."""

        clauses = []
        if IGitRepository.providedBy(target):
            clauses.append(AccessToken.git_repository == target)
        elif IProduct.providedBy(target):
            clauses.append(AccessToken.project == target)
        else:
            raise TypeError(f"Unsupported target: {target!r}")

        if visible_by_user is not None:
            # Evaluate if user owns the target (directly or indirectly).
            # The queries below might look more complex than they should to
            # evaluate ownership, but they are more performant as this will run
            # in one single query in the end.
            if IGitRepository.providedBy(target):
                user_owns_target = AccessToken.git_repository_id.is_in(
                    removeSecurityProxy(
                        (
                            getUtility(IAllGitRepositories)
                            .visibleByUser(visible_by_user)
                            .ownedByTeamMember(visible_by_user)
                        ).getRepositoryIds()
                    )._get_select()
                )
            else:
                # Avoid circular import
                from lp.registry.model.product import Product, ProductSet

                clauses.extend([Product.active, Product.id == target.id])
                user_owns_target = AccessToken.project_id.is_in(
                    Select(
                        Product.id,
                        where=And(
                            Product._owner_id == TeamParticipation.team_id,
                            TeamParticipation.person_id == visible_by_user.id,
                            ProductSet.getProductPrivacyFilter(
                                visible_by_user
                            ),
                        ),
                    )
                )

            user_owns_token = AccessToken.owner_id.is_in(
                Select(
                    TeamParticipation.team_id,
                    where=TeamParticipation.person_id == visible_by_user.id,
                )
            )

            clauses.append(Or(user_owns_target, user_owns_token))

        if not include_expired:
            clauses.append(
                Or(
                    AccessToken.date_expires == None,
                    AccessToken.date_expires > UTC_NOW,
                )
            )
        return (
            IStore(AccessToken)
            .find(AccessToken, *clauses)
            .order_by(AccessToken.date_created)
        )

    def getByTargetAndID(self, target, token_id, visible_by_user=None):
        """See `IAccessTokenSet`."""
        return (
            self.findByTarget(target, visible_by_user=visible_by_user)
            .find(id=token_id)
            .one()
        )


class AccessTokenTargetMixin:
    """Mix this into classes that implement `IAccessTokenTarget`."""

    def getAccessTokens(self, visible_by_user=None, include_expired=False):
        """See `IAccessTokenTarget`."""
        # This is callable by anyone who can view the target, so tokens must
        # never be listed without restricting them to a user.
        if visible_by_user is None:
            raise Unauthorized(
                "Personal access tokens may only be listed for a logged-in "
                "user."
            )
        return getUtility(IAccessTokenSet).findByTarget(
            self,
            visible_by_user=visible_by_user,
            include_expired=include_expired,
        )

    def issueAccessToken(self, description, scopes, date_expires=None):
        # It's more usual in model code to pass the user as an argument,
        # e.g. using @call_with(user=REQUEST_USER) in the webservice
        # interface.  However, in this case that would allow anyone who
        # constructs a way to call this method not via the webservice to
        # issue a token for any user, which seems like a bad idea.
        user = getUtility(ILaunchBag).user
        if user is None:
            raise Unauthorized(
                "Personal access tokens may only be issued for a logged-in "
                "user."
            )
        if (
            AccessTokenScope.REPOSITORY_PUSH in scopes
            and not check_permission("launchpad.Edit", self)
        ):
            raise Unauthorized(
                "Only people who can edit this target may issue personal "
                "access tokens with the repository:push scope."
            )
        secret = create_access_token_secret()
        getUtility(IAccessTokenSet).new(
            secret,
            owner=user,
            description=description,
            target=self,
            scopes=scopes,
            date_expires=date_expires,
        )
        return secret
