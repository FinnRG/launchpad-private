# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Event subscribers for snaps."""

import sys

from zope.component import getUtility
from zope.error.interfaces import IErrorReportingUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.services.config import config
from lp.services.webhooks.interfaces import IWebhookSet

# The event type used to notify the external SBOM generation service
# about snap builds it should process. Kept in sync with the "fullybuilt"
# subscope registered in lp.services.webhooks.interfaces so that only
# successfully completed builds are delivered.
SBOM_WEBHOOK_EVENT_TYPE = "snap:build:0.1::fullybuilt"


def _get_sbom_webhook_config():
    """Return (delivery_url, secret) for the automatic SBOM webhook.

    Returns (None, None) if automatic SBOM webhook management is not
    configured for this instance, in which case callers should do
    nothing.
    """
    delivery_url = config.snappy.sbom_webhook_url
    secret = config.snappy.sbom_webhook_secret
    if not delivery_url or not secret:
        return None, None
    return delivery_url, secret


def _sync_sbom_webhook(snap, delivery_url, secret):
    """Ensure `snap` has an active SBOM webhook iff it should.

    Snaps with `use_fetch_service` enabled should have an active
    Launchpad-managed webhook delivering
    `snap:build:0.1::fullybuilt` events to the external SBOM generation
    service, so that it can be automatically triggered for those builds
    without needing edit access to every snap recipe. Snaps without
    `use_fetch_service` should not.

    This is a system action performed on behalf of Launchpad itself
    (triggered by an object-modified event, not necessarily by a user
    with edit rights on this specific snap's webhooks), so it operates
    on the unproxied snap and talks to `IWebhookSet` directly rather
    than via the permission-checked `ISnap.webhooks` property.
    """
    naked_snap = removeSecurityProxy(snap)
    webhook_set = getUtility(IWebhookSet)
    existing_hooks = [
        removeSecurityProxy(hook)
        for hook in webhook_set.findByTarget(naked_snap)
        if removeSecurityProxy(hook).delivery_url == delivery_url
    ]
    if naked_snap.use_fetch_service:
        if existing_hooks:
            for hook in existing_hooks:
                if not hook.active:
                    hook.active = True
        else:
            webhook_set.new(
                naked_snap,
                getUtility(ILaunchpadCelebrities).janitor,
                delivery_url,
                [SBOM_WEBHOOK_EVENT_TYPE],
                True,
                secret,
            )
    else:
        for hook in existing_hooks:
            if hook.active:
                hook.active = False


def snap_use_fetch_service_changed(snap, event):
    """Keep the automatic SBOM webhook subscription in sync.

    Registered as a subscriber to `IObjectModifiedEvent` on `ISnap`.
    """
    if not event.edited_fields:
        return
    if "use_fetch_service" not in event.edited_fields:
        return
    delivery_url, secret = _get_sbom_webhook_config()
    if delivery_url is None:
        # Automatic SBOM webhook management isn't configured for this
        # instance; nothing to do.
        return
    try:
        _sync_sbom_webhook(snap, delivery_url, secret)
    except Exception:
        # Never let a problem managing the automatic SBOM webhook block
        # the user's use_fetch_service change; just log it and move on.
        getUtility(IErrorReportingUtility).raising(sys.exc_info())
