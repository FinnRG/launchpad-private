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

# Event type delivered to the external SBOM generation service.
# Must match the "fullybuilt" subscope in lp.services.webhooks.interfaces.
SBOM_WEBHOOK_EVENT_TYPE = "snap:build:0.1::fullybuilt"


def _get_sbom_webhook_config():
    """Return (delivery_url, secret), or (None, None) if unconfigured.

    Automatic SBOM webhook management is opt-in per instance; callers
    should no-op when this returns (None, None).
    """
    delivery_url = config.snappy.sbom_webhook_url
    secret = config.snappy.sbom_webhook_secret
    if not delivery_url or not secret:
        return None, None
    return delivery_url, secret


def _sync_sbom_webhook(snap, delivery_url, secret):
    """Ensure `snap` has an active SBOM webhook iff it is public and uses
    the fetch service.

    Lets the external SBOM service receive fullybuilt events without
    needing launchpad.Edit on every snap, so this operates on the
    unproxied snap via IWebhookSet directly, not ISnap.webhooks.

    Known limitation, accepted for this proof of concept: no locking.
    Concurrent use_fetch_service edits could create two active hooks.
    Worst case is a duplicate SBOM generation, not data loss, so this
    isn't guarded against.
    """
    # XXX kelsharkawy 2026-09-11: The hook managed here isn't hidden or
    # protected: a snap owner can delete or disable it via the normal
    # +webhooks UI/API, and this function has no way to notice until the
    # next use_fetch_service toggle (off/on is the workaround). Add a
    # "Launchpad-managed" UI label so this is at least visible -
    # registrant is already the janitor, so no schema change is needed.
    naked_snap = removeSecurityProxy(snap)
    webhook_set = getUtility(IWebhookSet)
    janitor = getUtility(ILaunchpadCelebrities).janitor
    existing_hooks = [
        removeSecurityProxy(hook)
        for hook in webhook_set.findByTarget(naked_snap)
        if removeSecurityProxy(hook).delivery_url == delivery_url
        and removeSecurityProxy(hook).registrant == janitor
    ]
    if naked_snap.use_fetch_service and not naked_snap.private:
        if existing_hooks:
            for hook in existing_hooks:
                if not hook.active:
                    hook.active = True
                # Apply a rotated sbom_webhook_secret to existing hooks.
                if hook.secret != secret:
                    hook.setSecret(secret)
        else:
            webhook_set.new(
                naked_snap,
                janitor,
                delivery_url,
                [SBOM_WEBHOOK_EVENT_TYPE],
                True,
                secret,
            )
    else:
        for hook in existing_hooks:
            if hook.active:
                hook.active = False


def sync_sbom_webhook(snap):
    """Bring the automatic SBOM webhook on `snap` up to date.

    Does nothing if automatic SBOM webhooks aren't configured.  Errors are
    logged as OOPSes rather than raised, so that they never block the snap
    change that triggered the sync.
    """
    delivery_url, secret = _get_sbom_webhook_config()
    if delivery_url is None:
        return
    try:
        _sync_sbom_webhook(snap, delivery_url, secret)
    except Exception:
        getUtility(IErrorReportingUtility).raising(sys.exc_info())


def snap_use_fetch_service_changed(snap, event):
    """Keep the automatic SBOM webhook subscription in sync.

    Registered as a subscriber to `IObjectModifiedEvent` on `ISnap`.
    Creation and privacy changes are handled in the model, since they
    don't reliably send this event.
    """
    if not event.edited_fields:
        return
    if "use_fetch_service" not in event.edited_fields:
        return
    sync_sbom_webhook(snap)
