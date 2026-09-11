# Copyright 2015-2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Webhook interfaces."""

__all__ = [
    "AnyWebhookEventTypeVocabulary",
    "IWebhook",
    "IWebhookClient",
    "IWebhookDeliveryJob",
    "IWebhookDeliveryJobSource",
    "IWebhookJob",
    "IWebhookJobSource",
    "IWebhookSet",
    "IWebhookTarget",
    "WEBHOOK_EVENT_TYPES",
    "WebhookDeliveryFailure",
    "WebhookDeliveryRetry",
    "ValidWebhookEventTypeVocabulary",
]

from lazr.lifecycle.snapshot import doNotSnapshot
from lazr.restful.declarations import (
    REQUEST_USER,
    call_with,
    export_destructor_operation,
    export_factory_operation,
    export_write_operation,
    exported,
    exported_as_webservice_entry,
    operation_for_version,
    operation_parameters,
)
from lazr.restful.fields import CollectionField, Reference
from lazr.restful.interface import copy_field
from zope.interface import Attribute, Interface
from zope.schema import Bool, Choice, Datetime, Dict, Int, List, TextLine
from zope.schema.vocabulary import SimpleVocabulary

from lp import _
from lp.app.validators import LaunchpadValidationError
from lp.registry.interfaces.person import IPerson
from lp.services.fields import URIField
from lp.services.job.interfaces.job import IJob, IJobSource, IRunnableJob
from lp.services.webservice.apihelpers import (
    patch_collection_property,
    patch_entry_return_type,
    patch_reference_property,
)

WEBHOOK_EVENT_TYPES = {
    "bug:0.1": "Bug creation/change",
    "bug:comment:0.1": "Bug comment",
    "bzr:push:0.1": "Bazaar push",
    "charm-recipe:build:0.1": "Charm recipe build",
    "ci:build:0.1": "CI build",
    "git:push:0.1": "Git push",
    "livefs:build:0.1": "Live filesystem build",
    "merge-proposal:0.1": "Merge proposal",
    # Merge proposal subscopes
    "merge-proposal:0.1::create": "Merge proposal Created",
    "merge-proposal:0.1::push": "Merge proposal Pushed",
    "merge-proposal:0.1::review": "Merge proposal Reviewed",
    "merge-proposal:0.1::edit": "Merge proposal Edited",
    "merge-proposal:0.1::status-change": "Merge proposal Status Changed",
    "merge-proposal:0.1::delete": "Merge proposal Deleted",
    "oci-recipe:build:0.1": "OCI recipe build",
    "snap:build:0.1": "Snap build",
    # Snap build subscopes
    "snap:build:0.1::fullybuilt": "Snap build fully built",
    "craft-recipe:build:0.1": "Craft recipe build",
    "archive:source-package-upload:0.1": "Source package upload",
    "archive:source-package-upload:0.1::accepted": "Source package"
    "upload accepted",
    "archive:source-package-upload:0.1::rejected": "Source package"
    "upload rejected",
    "archive:binary-package-upload:0.1": "Binary package upload",
    "archive:binary-build:0.1": "Binary build",
    "archive:binary-build:0.1::fullybuilt": "Binary build fully built",
    "archive:binary-build:0.1::failedtobuild": "Binary build failed to build",
    "archive:binary-build:0.1::chrootwait": "Binary build failed due"
    " to chroot problem",
    "archive:binary-build:0.1::cancelled": "Binary build cancelled",
    "archive:binary-build:0.1::failedtoupload": "Binary build failed"
    " to upload",
    "archive:binary-build:0.1::superseded": "Binary build for"
    " superseded source",
}


class WebhookDeliveryFailure(Exception):
    """A webhook delivery failed and should not be retried."""

    pass


class WebhookDeliveryRetry(Exception):
    """A webhook delivery failed and should be retried."""

    pass


class AnyWebhookEventTypeVocabulary(SimpleVocabulary):
    def __init__(self, context):
        terms = [
            self.createTerm(key, key, value)
            for key, value in WEBHOOK_EVENT_TYPES.items()
        ]
        super().__init__(terms)


class ValidWebhookEventTypeVocabulary(SimpleVocabulary):
    """Vocabulary that returns the valid webhook event types in
    parent-subscopes order.

    This ensures that top-level parent event types are always listed before
    their corresponding sub-scopes, which is particularly important for UI
    rendering where an ordered display improves usability.
    """

    def _createTerm(self, scope):
        """Shorthand for creating a schema term for a scope."""
        return self.createTerm(scope, scope, WEBHOOK_EVENT_TYPES[scope])

    def __init__(self, context):
        # When creating a webhook, the context is the target; when editing
        # an existing webhook, the context is the webhook itself.
        if IWebhook.providedBy(context):
            target = context.target
        else:
            target = context
        subscopes_by_parent = {}
        top_level = []
        for key in target.valid_webhook_event_types:
            # Group subscopes under their parent
            if "::" in key:
                parent = key.split("::")[0]
                subscopes_by_parent.setdefault(parent, []).append(key)
            else:
                top_level.append(key)
        terms = []
        # Create terms for parents and for their corresponding subscopes
        for parent in top_level:
            terms.append(self._createTerm(parent))
            for subscope in subscopes_by_parent.get(parent, []):
                terms.append(self._createTerm(subscope))
        super().__init__(terms)


def validate_event_type_parent_subscope(event_types):
    """Validator to handle parent and its subscopes selection."""
    # Subscopes are identified by the presence of '::' in the event type
    # string.
    parents = {et.split("::")[0] for et in event_types if "::" in et}
    for parent in parents:
        if parent in event_types:
            raise LaunchpadValidationError(
                f"Please, select either the parent event type ({parent}) "
                "or its subscopes - not both."
            )
    return True


@exported_as_webservice_entry(as_of="beta")
class IWebhook(Interface):
    id = Int(title=_("ID"), readonly=True, required=True)

    target = exported(
        Reference(
            title=_("Target"),
            schema=Interface,  # Actually IWebhookTarget.
            required=True,
            readonly=True,
            description=_(
                "The object for which this webhook receives events."
            ),
        )
    )
    event_types = exported(
        List(
            Choice(vocabulary="ValidWebhookEventType"),
            title=_("Event types"),
            required=True,
            readonly=False,
            constraint=validate_event_type_parent_subscope,
        )
    )
    registrant = exported(
        Reference(
            title=_("Registrant"),
            schema=IPerson,
            required=True,
            readonly=True,
            description=_("The person who created this webhook."),
        )
    )
    registrant_id = Int(title=_("Registrant ID"))
    date_created = exported(
        Datetime(title=_("Date created"), required=True, readonly=True)
    )
    date_last_modified = exported(
        Datetime(title=_("Date last modified"), required=True, readonly=True)
    )

    delivery_url = exported(
        URIField(
            title=_("Delivery URL"),
            allowed_schemes=["http", "https"],
            required=True,
            readonly=False,
        )
    )
    active = exported(
        Bool(
            title=_("Active"),
            description=_("Deliver details of subscribed events."),
            required=True,
            readonly=False,
        )
    )

    # Do not export this.
    secret = TextLine(
        title=_("Secret"),
        required=False,
        description=_(
            "An optional string used to sign delivery bodies with HMAC-SHA1 "
            "in the X-Hub-Signature header. This must be less than 200 bytes "
            "long."
        ),
        max_length=200,
    )

    deliveries = exported(
        doNotSnapshot(
            CollectionField(
                title=_("Recent deliveries for this webhook."),
                value_type=Reference(schema=Interface),
                readonly=True,
            )
        )
    )

    git_ref_pattern = exported(
        TextLine(
            title=_("Git ref pattern"),
            required=False,
            description=_(
                "Pattern to match against git-ref/branch name of an event, "
                "to filter webhook triggers"
            ),
            max_length=200,
        )
    )

    def getDelivery(id):
        """Retrieve a delivery by ID, or None if it doesn't exist."""

    @export_factory_operation(Interface, [])  # Actually IWebhookDelivery.
    @operation_for_version("devel")
    def ping():
        """Send a test event."""

    @export_destructor_operation()
    @operation_for_version("devel")
    def destroySelf():
        """Delete this webhook."""

    @export_write_operation()
    @operation_parameters(secret=copy_field(secret))
    @operation_for_version("devel")
    def setSecret(secret):
        """Set the secret used to sign deliveries."""


class IWebhookSet(Interface):
    def new(
        target,
        registrant,
        delivery_url,
        event_types,
        active,
        secret,
        git_ref_pattern=None,
    ):
        """Create a new webhook."""

    def delete(hooks):
        """Delete a collection of webhooks."""

    def getByID(id):
        """Get a webhook by its ID."""

    def findByTarget(target):
        """Find all webhooks for the given target."""

    def trigger(target, event_type, payload, context=None, git_refs=None):
        """Trigger subscribed webhooks to deliver a payload."""


@exported_as_webservice_entry(as_of="beta")
class IWebhookTarget(Interface):
    webhooks = exported(
        doNotSnapshot(
            CollectionField(
                title=_("Webhooks for this target."),
                value_type=Reference(schema=IWebhook),
                readonly=True,
            )
        )
    )

    valid_webhook_event_types = List(
        Choice(vocabulary="AnyWebhookEventType"),
        title=_("Valid event types"),
        description=_("Valid event types for this object type."),
        required=True,
        readonly=True,
    )

    default_webhook_event_types = List(
        Choice(vocabulary="ValidWebhookEventType"),
        title=_("Default event types"),
        description=_(
            "Default event types for new webhooks attached to this object "
            "type."
        ),
        required=True,
        readonly=True,
    )

    @call_with(registrant=REQUEST_USER)
    @export_factory_operation(
        IWebhook, ["delivery_url", "active", "event_types", "secret"]
    )
    @operation_for_version("devel")
    def newWebhook(
        registrant,
        delivery_url,
        event_types,
        active=True,
        secret=None,
        git_ref_pattern=None,
    ):
        """Create a new webhook."""


class IWebhookJob(Interface):
    """A job related to a webhook."""

    job = Reference(
        title=_("The common Job attributes."),
        schema=IJob,
        required=True,
        readonly=True,
    )

    webhook = Reference(
        title=_("The webhook that this job is for."),
        schema=IWebhook,
        required=True,
        readonly=True,
    )

    json_data = Attribute(_("A dict of data about the job."))


class IWebhookJobSource(IJobSource):
    def deleteByIDs(webhookjob_ids):
        """Delete `IWebhookJob`s by their primary key (`Job.id`)."""

    def deleteByWebhooks(webhooks):
        """Delete all `IWebhookJob`s for the given `IWebhook`."""


@exported_as_webservice_entry("webhook_delivery", as_of="beta")
class IWebhookDeliveryJob(IRunnableJob):
    """A Job that delivers an event to a webhook consumer."""

    webhook = exported(
        Reference(
            title=_("Webhook"),
            description=_("The webhook that this delivery is for."),
            schema=IWebhook,
            required=True,
            readonly=True,
        )
    )

    pending = exported(
        Bool(
            title=_("Pending"),
            description=_("Whether a delivery attempt is in progress."),
            required=True,
            readonly=True,
        )
    )

    successful = exported(
        Bool(
            title=_("Successful"),
            description=_(
                "Whether the most recent delivery attempt succeeded, or null "
                "if no attempts have been made yet."
            ),
            required=False,
            readonly=True,
        )
    )

    error_message = exported(
        TextLine(
            title=_("Error message"),
            description=_(
                "Details of the error encountered by the most recent delivery "
                "attempt."
            ),
            required=False,
            readonly=True,
        )
    )

    date_created = exported(
        Datetime(title=_("Date created"), required=True, readonly=True)
    )

    date_scheduled = exported(
        Datetime(
            title=_("Date scheduled"),
            description=_("Timestamp of the next delivery attempt."),
            required=False,
            readonly=True,
        )
    )

    date_first_sent = exported(
        Datetime(
            title=_("Date first sent"),
            description=_("Timestamp of the first delivery attempt."),
            required=False,
            readonly=True,
        )
    )

    date_sent = exported(
        Datetime(
            title=_("Date sent"),
            description=_("Timestamp of the last delivery attempt."),
            required=False,
            readonly=True,
        )
    )

    event_type = exported(
        TextLine(title=_("Event type"), required=True, readonly=True)
    )

    payload = exported(
        Dict(
            title=_("Event payload"),
            key_type=TextLine(),
            required=True,
            readonly=True,
        )
    )

    @export_write_operation()
    @operation_parameters(
        reset=Bool(
            title=_("Reset automatic retries"),
            description=_(
                "Restart the 24 hour automatic retry window as well as trying "
                "again now."
            ),
        )
    )
    @operation_for_version("devel")
    def retry(reset=False):
        """Attempt to deliver the event again.

        Launchpad will automatically retry regularly for 24 hours, but
        this can be used after it gives up or to avoid waiting for the
        next automatic attempt.
        """


class IWebhookDeliveryJobSource(IJobSource):
    def create(webhook):
        """Deliver an event to a webhook consumer.

        :param webhook: The webhook to deliver to.
        """


class IWebhookClient(Interface):
    def deliver(
        self,
        url,
        proxy,
        user_agent,
        timeout,
        secret,
        delivery_id,
        event_type,
        payload,
    ):
        """Deliver a payload to a webhook endpoint.

        Returns a dict of request and response details. The 'request' key
        and one of either 'response' or 'connection_error' are always
        present.

        An exception will be raised if an internal error has occurred that
        cannot be the fault of the remote endpoint. For example, a 404 will
        return a response, and a DNS error returns a connection_error, but
        the proxy being offline will raise an exception.

        The timeout is just given to the underlying requests library, so
        it only provides connect and inter-read timeouts. A reliable
        overall request timeout will require another mechanism.

        If secret is not None, a PubSubHubbub-compatible X-Hub-Signature
        header will be sent using HMAC-SHA1.
        """


patch_collection_property(IWebhook, "deliveries", IWebhookDeliveryJob)
patch_entry_return_type(IWebhook, "ping", IWebhookDeliveryJob)
patch_reference_property(IWebhook, "target", IWebhookTarget)
