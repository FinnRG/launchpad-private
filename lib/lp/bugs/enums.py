# Copyright 2010-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Enums for the Bugs app."""

__all__ = [
    "BugLockedStatus",
    "BugLockStatus",
    "BugNotificationLevel",
    "BugNotificationStatus",
    "VulnerabilityStatus",
    "VulnerabilityHandlerEnum",
]

from lazr.enum import DBEnumeratedType, DBItem, use_template


class BugNotificationLevel(DBEnumeratedType):
    """Bug Notification Level.

    The type and volume of bug notification email sent to subscribers.
    """

    LIFECYCLE = DBItem(
        20,
        """
        Lifecycle

        Only send a low volume of notifications about new bugs registered,
        bugs removed or bug targeting.
        """,
    )

    METADATA = DBItem(
        30,
        """
        Details

        Send bug lifecycle notifications, as well as notifications about
        changes to the bug's details like status and description.
        """,
    )

    COMMENTS = DBItem(
        40,
        """
        Discussion

        Send bug lifecycle notifications, detail change notifications and
        notifications about new events in the bugs's discussion, like new
        comments.
        """,
    )


class BugNotificationStatus(DBEnumeratedType):
    """The status of a bug notification.

    A notification may be pending, sent, or omitted."""

    PENDING = DBItem(
        10,
        """
        Pending

        The notification has not yet been sent.
        """,
    )

    OMITTED = DBItem(
        20,
        """
        Omitted

        The system considered sending the notification, but omitted it.
        This is generally because the action reported by the notification
        was immediately undone.
        """,
    )

    SENT = DBItem(
        30,
        """
        Sent

        The notification has been sent.
        """,
    )

    DEFERRED = DBItem(
        40,
        """
        Deferred

        The notification is deferred.  The recipient list was not calculated
        at creation time but is done when processed.
        """,
    )


class BugLockStatus(DBEnumeratedType):
    """
    The lock status of a bug.
    """

    UNLOCKED = DBItem(
        0,
        """
        Unlocked

        The bug is unlocked and the usual permissions apply.
        """,
    )

    COMMENT_ONLY = DBItem(
        1,
        """
        Comment-only

        The bug allows those without a relevant role to comment,
        but not to edit metadata.
        """,
    )


class BugLockedStatus(DBEnumeratedType):
    """
    The various locked status values of a bug.
    """

    use_template(BugLockStatus, exclude=("UNLOCKED",))


class VulnerabilityStatus(DBEnumeratedType):
    """Vulnerability status"""

    NEEDS_TRIAGE = DBItem(
        0,
        """
        Needs triage

        Not looked at yet.
        """,
    )

    ACTIVE = DBItem(
        1,
        """
        Active

        The vulnerability is active.
        """,
    )

    IGNORED = DBItem(
        2,
        """
        Ignored

        The vulnerability is currently ignored.
        """,
    )

    RETIRED = DBItem(
        3,
        """
        Retired

        This vulnerability is now retired.
        """,
    )


class VulnerabilityHandlerEnum(DBEnumeratedType):
    SOSS = DBItem(
        1,
        """
        SOSS Handler

        Specific handler to use for SOSS vulnerability data imports and
        exports.
        """,
    )

    UCT = DBItem(
        2,
        """
        UCT Handler

        Specific handler to use for UCT vulnerability data imports and
        exports.
        """,
    )

    CPPA = DBItem(
        3,
        """
        CPPA Handler

        Specific handler to use for CPPA vulnerability data imports.
        CPPA Handler adds PPA bugtasks to bugs already imported by UCT.
        """,
    )
