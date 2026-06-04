# Copyright 2009-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Browser notification messages

Provides an API for displaying arbitrary notifications to users after
an action has been performed, independent of what page the user
ends up on after the action is done.

Note that the current implementation is deliberately broken - the only way
to do this correctly is by passing a token in the URL to identify the
browser window the request came from.
"""

from datetime import datetime

from zope.interface import implementer

from lp.services.config import config
from lp.services.webapp.escaping import html_escape, safe_html
from lp.services.webapp.interfaces import (
    BrowserNotificationLevel,
    INotification,
    INotificationList,
    INotificationRequest,
    INotificationResponse,
    ISession,
)
from lp.services.webapp.login import allowUnauthenticatedSession
from lp.services.webapp.publisher import LaunchpadView

SESSION_KEY = "launchpad"


@implementer(INotificationRequest)
class NotificationRequest:
    """NotificationRequest extracts notifications to display to the user
    from the request and session

    It is designed to be mixed in with an IBrowserRequest.
    """

    @property
    def notifications(self):
        return INotificationResponse(self).notifications


@implementer(INotificationResponse)
class NotificationResponse:
    """The NotificationResponse collects notifications to propagate to the
    next page loaded. Notifications are stored in the session, with a key
    propagated via the URL to load the correct messages in the next loaded
    page.

    It needs to be mixed in with an IHTTPApplicationResponse so its redirect
    method intercepts the default behaviour.
    """

    # We stuff our Notifications here until we are sure we should persist it
    # in the request. This avoids needless calls to the session machinery
    # which would be bad.
    _notifications = None

    def addNotification(self, msg, level=BrowserNotificationLevel.INFO):
        """See `INotificationResponse`."""
        self.notifications.append(Notification(level, html_escape(msg)))

    @property
    def notifications(self):
        # If we have already retrieved our INotificationList this request,
        # just return it
        if self._notifications is not None:
            return self._notifications
        cookie_name = config.launchpad_session.cookie
        request = self._request
        response = self
        # Do some getattr sniffing so that the doctests in this module
        # still pass.  Doing this rather than improving the Mock classes
        # that the mixins are used with, as we'll be moving this hack to
        # the sessions machinery in due course.
        if not (
            getattr(request, "cookies", None)
            and getattr(response, "getCookie", None)
        ) or (
            request.cookies.get(cookie_name) is not None
            or response.getCookie(cookie_name) is not None
        ):
            session = ISession(self)[SESSION_KEY]
            try:
                # Use notifications stored in the session.
                self._notifications = session["notifications"]
                # Remove them from the session so they don't propagate to
                # subsequent pages, unless redirect() is called which will
                # push the notifications back into the session.
                del session["notifications"]
            except KeyError:
                # No stored notifications - create a new NotificationList
                self._notifications = NotificationList()
        else:
            self._notifications = NotificationList()

        return self._notifications

    def removeAllNotifications(self):
        """See lp.services.webapp.interfaces.INotificationResponse"""
        self._notifications = None

    def redirect(self, location, status=None, trusted=True):
        """See lp.services.webapp.interfaces.INotificationResponse"""
        # We are redirecting, so we need to stuff our notifications into
        # the session
        if self._notifications is not None and len(self._notifications) > 0:
            # A dance to assert that we want to break the rules about no
            # unauthenticated sessions. Only after this next line is it safe
            # to set the session.
            allowUnauthenticatedSession(self._request)
            session = ISession(self)[SESSION_KEY]
            session["notifications"] = self._notifications
        return super().redirect(location, status, trusted=trusted)

    def addDebugNotification(self, msg):
        """See `INotificationResponse`."""
        self.addNotification(msg, BrowserNotificationLevel.DEBUG)

    def addInfoNotification(self, msg):
        """See `INotificationResponse`."""
        self.addNotification(msg, BrowserNotificationLevel.INFO)

    def addWarningNotification(self, msg):
        """See `INotificationResponse`."""
        self.addNotification(msg, BrowserNotificationLevel.WARNING)

    def addErrorNotification(self, msg):
        """See `INotificationResponse`."""
        self.addNotification(msg, BrowserNotificationLevel.ERROR)


@implementer(INotificationList)
class NotificationList(list):
    """Collection of INotification instances with a creation date."""

    created = None

    def __init__(self):
        self.created = datetime.utcnow()
        super().__init__()

    def __getitem__(self, index_or_levelname):
        if isinstance(index_or_levelname, int):
            return super().__getitem__(index_or_levelname)

        level = getattr(
            BrowserNotificationLevel, index_or_levelname.upper(), None
        )
        if level is None:
            raise KeyError(index_or_levelname)

        return [
            notification
            for notification in self
            if notification.level == level
        ]


@implementer(INotification)
class Notification:
    level = None
    message = None

    def __init__(self, level, message):
        self.level = level
        self.message = message


class NotificationTestView1(LaunchpadView):
    """Display some notifications.

    This is installed into the real instance, rather than added on the fly
    in the test suite, as this page is useful for adjusting the visual style
    of the notifications
    """

    label = page_title = "Notification test"

    def initialize(self):
        response = self.request.response

        # Add some notifications
        for count in range(1, 3):
            response.addDebugNotification(
                safe_html("Debug notification <b>%d</b>" % count)
            )
            response.addInfoNotification(
                safe_html("Info notification <b>%d</b>" % count)
            )
            response.addWarningNotification(
                safe_html("Warning notification <b>%d</b>" % count)
            )
            response.addErrorNotification(
                safe_html("Error notification <b>%d</b>" % count)
            )


class NotificationTestView2(NotificationTestView1):
    """Redirect to another page, propagating some notification messages.

    This is installed into the real instance, rather than added on the fly
    in the test suite, as this page is useful for adjusting the visual style
    of the notifications
    """

    def initialize(self):
        NotificationTestView1.initialize(self)
        self.request.response.redirect("/")


class NotificationTestView3(NotificationTestView1):
    """Redirect, propagating some notification messages, to another page
    that adds more notifications before rendering.

    This is installed into the real instance, rather than added on the fly
    in the test suite, as this page is useful for adjusting the visual style
    of the notifications
    """

    def initialize(self):
        self.request.response.addErrorNotification("+notificationtest3 error")
        self.request.response.redirect("/+notificationtest1")


class NotificationTestView4(NotificationTestView1):
    """Redirect twice, propagating some notification messages each time,
    ending up at another page that adds more notifications before rendering.

    This is installed into the real instance, rather than added on the fly
    in the test suite, as this page is useful for adjusting the visual style
    of the notifications
    """

    def initialize(self):
        self.request.response.addErrorNotification("+notificationtest4 error")
        self.request.response.redirect("/+notificationtest3")
