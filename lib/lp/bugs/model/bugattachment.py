# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = ["BugAttachment", "BugAttachmentSet"]

from typing import List

from lazr.lifecycle.event import ObjectCreatedEvent, ObjectDeletedEvent
from storm.databases.postgres import JSON
from storm.locals import Int, Reference, Store, Unicode
from zope.event import notify
from zope.interface import implementer

from lp.app.errors import NotFoundError
from lp.bugs.interfaces.bugattachment import (
    BugAttachmentType,
    IBugAttachment,
    IBugAttachmentSet,
)
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.librarian.browser import ProxiedLibraryFileAlias
from lp.services.propertycache import cachedproperty


@implementer(IBugAttachment)
class BugAttachment(StormBase):
    """A bug attachment."""

    __storm_table__ = "BugAttachment"

    id = Int(primary=True)

    bug_id = Int(name="bug", allow_none=False)
    bug = Reference(bug_id, "Bug.id")
    type = DBEnum(
        enum=BugAttachmentType,
        allow_none=False,
        default=IBugAttachment["type"].default,
    )
    _title = Unicode(name="title", allow_none=True)
    libraryfile_id = Int(name="libraryfile", allow_none=True)
    libraryfile = Reference(libraryfile_id, "LibraryFileAlias.id")
    url = Unicode(allow_none=True)
    _message_id = Int(name="message", allow_none=False)
    _message = Reference(_message_id, "Message.id")
    vulnerability_patches = JSON(allow_none=True)

    def __init__(
        self,
        bug,
        title,
        libraryfile,
        url,
        message,
        type=IBugAttachment["type"].default,
        vulnerability_patches: List[dict] = None,
    ):
        super().__init__()
        self.bug = bug
        self.title = title
        self.libraryfile = libraryfile
        self.url = url
        self._message = message
        self.type = type
        self.vulnerability_patches = vulnerability_patches

    @property
    def title(self) -> str:
        if self._title:
            return self._title
        if self.libraryfile:
            return self.libraryfile.filename
        return self.url

    @title.setter
    def title(self, title) -> None:
        self._title = title

    @cachedproperty
    def message(self):
        """This is a cachedproperty to allow message to be an IIndexedMessage.

        This is needed for the bug/attachments API call which needs to index
        an IIndexedMessage rather than a simple DB model IMessage. See
        Bug.attachments where the injection occurs.
        """
        return self._message

    @property
    def is_patch(self):
        """See IBugAttachment."""
        return self.type == BugAttachmentType.PATCH

    def removeFromBug(self, user):
        """See IBugAttachment."""
        notify(ObjectDeletedEvent(self, user))
        self.destroySelf()

    def destroySelf(self):
        """See IBugAttachment."""
        # Delete the reference to the LibraryFileContent record right now,
        # in order to avoid problems with not deleted files as described
        # in bug 387188.
        if self.libraryfile:
            self.libraryfile.content = None
        Store.of(self).remove(self)

    def getFileByName(self, filename):
        """See IBugAttachment."""
        if self.libraryfile and filename == self.libraryfile.filename:
            return self.libraryfile
        raise NotFoundError(filename)

    @property
    def displayed_url(self):
        if self.vulnerability_patches:
            # An attachment can carry several patch URLs; use the first as
            # the representative link. The UI lists them all separately.
            return self.vulnerability_patches[0].get("value")

        return (
            self.url
            or ProxiedLibraryFileAlias(self.libraryfile, self).http_url
        )


@implementer(IBugAttachmentSet)
class BugAttachmentSet:
    """A set for bug attachments."""

    def __getitem__(self, attach_id):
        """See IBugAttachmentSet."""
        try:
            attach_id = int(attach_id)
        except ValueError:
            raise NotFoundError(attach_id)
        item = IStore(BugAttachment).get(BugAttachment, attach_id)
        if item is None:
            raise NotFoundError(attach_id)
        return item

    def create(
        self,
        bug,
        filealias,
        url,
        title,
        message,
        attach_type=None,
        send_notifications=False,
        vulnerability_patches: List[dict] = None,
    ):
        """See `IBugAttachmentSet`."""
        if not filealias and not url and not vulnerability_patches:
            raise ValueError(
                "Either filealias, url or vulnerability_patches "
                "must be provided"
            )

        if sum([bool(filealias), bool(url), bool(vulnerability_patches)]) != 1:
            raise ValueError(
                "Only one of filealias, url or vulnerability_patches may be "
                "provided"
            )

        if attach_type is None:
            # XXX kiko 2005-08-03 bug=1659: this should use DEFAULT.
            attach_type = IBugAttachment["type"].default
        attachment = BugAttachment(
            bug=bug,
            libraryfile=filealias,
            url=url,
            type=attach_type,
            title=title,
            message=message,
            vulnerability_patches=vulnerability_patches,
        )
        # canonial_url(attachment) (called by notification subscribers
        # to generate the download URL of the attachments) blows up if
        # attachment.id is not (yet) set.
        Store.of(attachment).flush()
        if send_notifications:
            notify(ObjectCreatedEvent(attachment, user=message.owner))
        return attachment
