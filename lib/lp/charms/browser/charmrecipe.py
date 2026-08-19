# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Charm recipe views."""

__all__ = [
    "CharmRecipeAddView",
    "CharmRecipeAdminView",
    "CharmRecipeAuthorizeView",
    "CharmRecipeContextMenu",
    "CharmRecipeDeleteView",
    "CharmRecipeEditView",
    "CharmRecipeNavigation",
    "CharmRecipeNavigationMenu",
    "CharmRecipeRequestBuildsView",
    "CharmRecipeURL",
    "CharmRecipeView",
]

from urllib.parse import urlencode, urlsplit, urlunsplit

from lazr.restful.interface import copy_field, use_template
from zope.component import getUtility
from zope.error.interfaces import IErrorReportingUtility
from zope.formlib.widget import CustomWidgetFactory
from zope.interface import Interface, implementer
from zope.schema import TextLine
from zope.security.interfaces import Unauthorized

from lp import _
from lp.app.browser.launchpadform import (
    LaunchpadEditFormView,
    LaunchpadFormView,
    action,
)
from lp.app.browser.lazrjs import InlinePersonEditPickerWidget
from lp.app.browser.tales import format_link
from lp.app.widgets.snapbuildchannels import SnapBuildChannelsWidget
from lp.buildmaster.builderproxy import FetchServicePolicy
from lp.charms.interfaces.charmhubclient import BadRequestPackageUploadResponse
from lp.charms.interfaces.charmrecipe import (
    CHARM_RECIPE_WEBHOOKS_FEATURE_FLAG,
    CannotAuthorizeCharmhubUploads,
    ICharmRecipe,
    ICharmRecipeSet,
    NoSuchCharmRecipe,
)
from lp.charms.interfaces.charmrecipebuild import ICharmRecipeBuildSet
from lp.code.browser.widgets.gitref import GitRefWidget
from lp.code.interfaces.gitref import IGitRef
from lp.registry.interfaces.personproduct import IPersonProductFactory
from lp.registry.interfaces.product import IProduct
from lp.services.features import getFeatureFlag
from lp.services.propertycache import cachedproperty
from lp.services.utils import seconds_since_epoch
from lp.services.webapp import (
    ContextMenu,
    LaunchpadView,
    Link,
    Navigation,
    NavigationMenu,
    canonical_url,
    enabled_with_permission,
    stepthrough,
    structured,
)
from lp.services.webapp.breadcrumb import Breadcrumb, NameBreadcrumb
from lp.services.webapp.candid import request_candid_discharge
from lp.services.webapp.interfaces import ICanonicalUrlData
from lp.services.webapp.url import urlappend
from lp.services.webhooks.browser import WebhookTargetNavigationMixin
from lp.snappy.browser.widgets.storechannels import StoreChannelsWidget
from lp.soyuz.browser.build import get_build_by_id_str


@implementer(ICanonicalUrlData)
class CharmRecipeURL:
    """Charm recipe URL creation rules."""

    rootsite = "mainsite"

    def __init__(self, recipe):
        self.recipe = recipe

    @property
    def inside(self):
        owner = self.recipe.owner
        project = self.recipe.project
        return getUtility(IPersonProductFactory).create(owner, project)

    @property
    def path(self):
        return "+charm/%s" % self.recipe.name


class CharmRecipeNavigation(WebhookTargetNavigationMixin, Navigation):
    usedfor = ICharmRecipe

    @stepthrough("+build-request")
    def traverse_build_request(self, name):
        try:
            job_id = int(name)
        except ValueError:
            return None
        return self.context.getBuildRequest(job_id)

    @stepthrough("+build")
    def traverse_build(self, name):
        build = get_build_by_id_str(ICharmRecipeBuildSet, name)
        if build is None or build.recipe != self.context:
            return None
        return build


class CharmRecipeBreadcrumb(NameBreadcrumb):
    @property
    def inside(self):
        # XXX cjwatson 2021-06-04: This should probably link to an
        # appropriate listing view, but we don't have one of those yet.
        return Breadcrumb(
            self.context.project,
            text=self.context.project.display_name,
            inside=self.context.project,
        )


class CharmRecipeNavigationMenu(NavigationMenu):
    """Navigation menu for charm recipes."""

    usedfor = ICharmRecipe

    facet = "overview"

    links = ("admin", "edit", "webhooks", "authorize", "delete")

    @enabled_with_permission("launchpad.Admin")
    def admin(self):
        return Link("+admin", "Administer charm recipe", icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def edit(self):
        return Link("+edit", "Edit charm recipe", icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def webhooks(self):
        return Link(
            "+webhooks",
            "Manage webhooks",
            icon="edit",
            enabled=bool(getFeatureFlag(CHARM_RECIPE_WEBHOOKS_FEATURE_FLAG)),
        )

    @enabled_with_permission("launchpad.Edit")
    def authorize(self):
        if self.context.store_secrets:
            text = "Reauthorize Charmhub uploads"
        else:
            text = "Authorize Charmhub uploads"
        return Link("+authorize", text, icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def delete(self):
        return Link("+delete", "Delete charm recipe", icon="trash-icon")


class CharmRecipeContextMenu(ContextMenu):
    """Context menu for charm recipes."""

    usedfor = ICharmRecipe

    facet = "overview"

    links = ("request_builds",)

    @enabled_with_permission("launchpad.Edit")
    def request_builds(self):
        return Link("+request-builds", "Request builds", icon="add")


class CharmRecipeView(LaunchpadView):
    """Default view of a charm recipe."""

    @cachedproperty
    def builds_and_requests(self):
        return builds_and_requests_for_recipe(self.context)

    @property
    def person_picker(self):
        field = copy_field(
            ICharmRecipe["owner"],
            vocabularyName="AllUserTeamsParticipationPlusSelfSimpleDisplay",
        )
        return InlinePersonEditPickerWidget(
            self.context,
            field,
            format_link(self.context.owner),
            header="Change owner",
            step_title="Select a new owner",
        )

    @property
    def build_frequency(self):
        if self.context.auto_build:
            return "Built automatically"
        else:
            return "Built on request"

    @property
    def sorted_auto_build_channels_items(self):
        if self.context.auto_build_channels is None:
            return []
        return sorted(self.context.auto_build_channels.items())

    @property
    def store_channels(self):
        return ", ".join(self.context.store_channels)

    @property
    def user_can_see_source(self):
        try:
            return self.context.source.visibleByUser(self.user)
        except Unauthorized:
            return False


def builds_and_requests_for_recipe(recipe):
    """A list of interesting builds and build requests.

    All pending builds and pending build requests are shown, as well as up
    to 10 recent builds and recent failed build requests.  Pending items are
    ordered by the date they were created; recent items are ordered by the
    date they finished (if available) or the date they started (if the date
    they finished is not set due to an error).  This allows started but
    unfinished builds to show up in the view but be discarded as more recent
    builds become available.

    Builds that the user does not have permission to see are excluded (by
    the model code).
    """

    # We need to interleave items of different types, so SQL can't do all
    # the sorting for us.
    def make_sort_key(*date_attrs):
        def _sort_key(item):
            for date_attr in date_attrs:
                if getattr(item, date_attr, None) is not None:
                    return -seconds_since_epoch(getattr(item, date_attr))
            return 0

        return _sort_key

    items = sorted(
        list(recipe.pending_builds) + list(recipe.pending_build_requests),
        key=make_sort_key("date_created", "date_requested"),
    )
    if len(items) < 10:
        # We need to interleave two unbounded result sets, but we only need
        # enough items from them to make the total count up to 10.  It's
        # simplest to just fetch the upper bound from each set and do our
        # own sorting.
        recent_items = sorted(
            list(recipe.completed_builds[: 10 - len(items)])
            + list(recipe.failed_build_requests[: 10 - len(items)]),
            key=make_sort_key(
                "date_finished",
                "date_started",
                "date_created",
                "date_requested",
            ),
        )
        items.extend(recent_items[: 10 - len(items)])
    return items


class ICharmRecipeEditSchema(Interface):
    """Schema for adding or editing a charm recipe."""

    use_template(
        ICharmRecipe,
        include=[
            "owner",
            "name",
            "project",
            "require_virtualized",
            "build_path",
            "auto_build",
            "auto_build_channels",
            "store_upload",
            "use_fetch_service",
            "fetch_service_policy",
        ],
    )

    git_ref = copy_field(ICharmRecipe["git_ref"], required=True)

    # This is only required if store_upload is True.  Later validation takes
    # care of adjusting the required attribute.
    store_name = copy_field(ICharmRecipe["store_name"], required=True)
    store_channels = copy_field(ICharmRecipe["store_channels"], required=True)

    use_fetch_service = copy_field(
        ICharmRecipe["use_fetch_service"], required=True
    )
    fetch_service_policy = copy_field(
        ICharmRecipe["fetch_service_policy"], required=True
    )


def log_oops(error, request):
    """Log an oops report without raising an error."""
    info = (error.__class__, error, None)
    getUtility(IErrorReportingUtility).raising(info, request)


class CharmRecipeAuthorizeMixin:
    def requestAuthorization(self, recipe):
        try:
            self.next_url = CharmRecipeAuthorizeView.requestAuthorization(
                recipe, self.request
            )
        except BadRequestPackageUploadResponse as e:
            self.setFieldError(
                "store_upload",
                "Cannot get permission from Charmhub to upload this package.",
            )
            log_oops(e, self.request)


class CharmRecipeAddView(CharmRecipeAuthorizeMixin, LaunchpadFormView):
    """View for creating charm recipes."""

    page_title = label = "Create a new charm recipe"

    schema = ICharmRecipeEditSchema

    custom_widget_git_ref = GitRefWidget
    custom_widget_auto_build_channels = CustomWidgetFactory(
        SnapBuildChannelsWidget,
        hint=(
            "The channels to use for build tools when building the charm "
            "recipe."
        ),
    )
    custom_widget_store_channels = StoreChannelsWidget

    next_url = None

    @property
    def field_names(self):
        fields = ["owner", "name"]
        if self.is_project_context:
            fields += ["git_ref"]
        else:
            fields += ["project"]
        return fields + [
            "build_path",
            "auto_build",
            "auto_build_channels",
            "store_upload",
            "store_name",
            "store_channels",
        ]

    @property
    def is_project_context(self):
        return IProduct.providedBy(self.context)

    @property
    def cancel_url(self):
        return canonical_url(self.context)

    @property
    def initial_values(self):
        initial_values = {"owner": self.user}
        if IGitRef.providedBy(self.context) and IProduct.providedBy(
            self.context.target
        ):
            initial_values["project"] = self.context.target
        return initial_values

    def validate_widgets(self, data, names=None):
        """See `LaunchpadFormView`."""
        if self.widgets.get("store_upload") is not None:
            # Set widgets as required or optional depending on the
            # store_upload field.
            super().validate_widgets(data, ["store_upload"])
            store_upload = data.get("store_upload", False)
            self.widgets["store_name"].context.required = store_upload
            self.widgets["store_channels"].context.required = store_upload
        super().validate_widgets(data, names=names)

    @action("Create charm recipe", name="create")
    def create_action(self, action, data):
        if IGitRef.providedBy(self.context):
            project = data["project"]
            git_ref = self.context
        elif self.is_project_context:
            project = self.context
            git_ref = data["git_ref"]
        else:
            raise NotImplementedError(
                "Unknown context for charm recipe creation."
            )
        recipe = getUtility(ICharmRecipeSet).new(
            self.user,
            data["owner"],
            project,
            data["name"],
            git_ref=git_ref,
            build_path=data["build_path"],
            auto_build=data["auto_build"],
            auto_build_channels=data["auto_build_channels"],
            store_upload=data["store_upload"],
            store_name=data["store_name"],
            store_channels=data.get("store_channels"),
        )
        if data["store_upload"]:
            self.requestAuthorization(recipe)
        else:
            self.next_url = canonical_url(recipe)

    def validate(self, data):
        super().validate(data)
        owner = data.get("owner", None)
        if self.is_project_context:
            project = self.context
        else:
            project = data.get("project", None)
        name = data.get("name", None)
        if owner and project and name:
            if getUtility(ICharmRecipeSet).exists(owner, project, name):
                self.setFieldError(
                    "name",
                    "There is already a charm recipe owned by %s in %s with "
                    "this name." % (owner.display_name, project.display_name),
                )


class BaseCharmRecipeEditView(
    CharmRecipeAuthorizeMixin, LaunchpadEditFormView
):
    schema = ICharmRecipeEditSchema
    next_url = None

    @property
    def cancel_url(self):
        return canonical_url(self.context)

    def validate_widgets(self, data, names=None):
        """See `LaunchpadFormView`."""
        if self.widgets.get("store_upload") is not None:
            # Set widgets as required or optional depending on the
            # store_upload field.
            super().validate_widgets(data, ["store_upload"])
            store_upload = data.get("store_upload", False)
            self.widgets["store_name"].context.required = store_upload
            self.widgets["store_channels"].context.required = store_upload
        super().validate_widgets(data, names=names)

    def validate(self, data):
        super().validate(data)
        # These are the requirements for public snaps.
        if "owner" in data:
            owner = data.get("owner", self.context.owner)
            if owner is not None and owner.private:
                self.setFieldError(
                    "owner",
                    "A public charm recipe cannot have a private owner.",
                )
        if "git_ref" in data:
            ref = data.get("git_ref", self.context.git_ref)
            if ref is not None and ref.private:
                self.setFieldError(
                    "git_ref",
                    "A public charm recipe cannot have a private repository.",
                )

    def _needCharmhubReauth(self, data):
        """Does this change require reauthorizing to Charmhub?"""
        store_upload = data.get("store_upload", False)
        store_name = data.get("store_name")
        if not store_upload or store_name is None:
            return False
        return (
            not self.context.store_upload
            or store_name != self.context.store_name
        )

    @action("Update charm recipe", name="update")
    def request_action(self, action, data):
        if not data.get("auto_build", False):
            if "auto_build_channels" in data:
                del data["auto_build_channels"]
        store_upload = data.get("store_upload", False)
        if not store_upload:
            if "store_name" in data:
                del data["store_name"]
            if "store_channels" in data:
                del data["store_channels"]
        need_charmhub_reauth = self._needCharmhubReauth(data)
        self.updateContextFromData(data)
        if need_charmhub_reauth:
            self.requestAuthorization(self.context)
        else:
            self.next_url = canonical_url(self.context)

    @property
    def adapters(self):
        """See `LaunchpadFormView`."""
        return {ICharmRecipeEditSchema: self.context}


class CharmRecipeAdminView(BaseCharmRecipeEditView):
    """View for administering charm recipes."""

    @property
    def label(self):
        return "Administer %s charm recipe" % self.context.name

    page_title = "Administer"

    field_names = ["require_virtualized"]


class CharmRecipeEditView(BaseCharmRecipeEditView):
    """View for editing charm recipes."""

    @property
    def label(self):
        return "Edit %s charm recipe" % self.context.name

    page_title = "Edit"

    field_names = [
        "owner",
        "name",
        "project",
        "git_ref",
        "build_path",
        "auto_build",
        "auto_build_channels",
        "store_upload",
        "store_name",
        "store_channels",
        "use_fetch_service",
        "fetch_service_policy",
    ]
    custom_widget_git_ref = GitRefWidget
    custom_widget_auto_build_channels = CustomWidgetFactory(
        SnapBuildChannelsWidget,
        hint=(
            "The channels to use for build tools when building the charm "
            "recipe."
        ),
    )
    custom_widget_store_channels = StoreChannelsWidget

    def validate_widgets(self, data, names=None):
        if (
            self.widgets.get("use_fetch_service") is not None
            and self.widgets.get("fetch_service_policy") is not None
        ):
            super().validate_widgets(data, ["use_fetch_service"])
            self.widgets["fetch_service_policy"].context.required = data.get(
                "use_fetch_service"
            )
        super().validate_widgets(data, names=names)

    def validate(self, data):
        super().validate(data)
        owner = data.get("owner", None)
        project = data.get("project", None)
        name = data.get("name", None)
        if owner and project and name:
            try:
                recipe = getUtility(ICharmRecipeSet).getByName(
                    owner, project, name
                )
                if recipe != self.context:
                    self.setFieldError(
                        "name",
                        "There is already a charm recipe owned by %s in %s "
                        "with this name."
                        % (owner.display_name, project.display_name),
                    )
            except NoSuchCharmRecipe:
                pass

    def updateContextFromData(self, data, context=None, notify_modified=True):
        if not data.get("use_fetch_service"):
            data["fetch_service_policy"] = FetchServicePolicy.STRICT
        super().updateContextFromData(data, context, notify_modified)


class CharmRecipeAuthorizeView(LaunchpadEditFormView):
    """View for authorizing charm recipe uploads to Charmhub."""

    @property
    def label(self):
        return "Authorize Charmhub uploads of %s" % self.context.name

    page_title = "Authorize Charmhub uploads"

    class schema(Interface):
        """Schema for authorizing charm recipe uploads to Charmhub."""

        discharge_macaroon = TextLine(
            title="Serialized discharge macaroon", required=True
        )

    render_context = False

    focusedElementScript = None

    @property
    def cancel_url(self):
        return canonical_url(self.context)

    @classmethod
    def requestAuthorization(cls, recipe, request):
        """Begin the process of authorizing uploads of a charm recipe."""
        try:
            begin_result = recipe.beginAuthorization()
        except CannotAuthorizeCharmhubUploads as e:
            request.response.addInfoNotification(str(e))
            request.response.redirect(canonical_url(recipe))
            return
        base_url = canonical_url(recipe, view_name="+authorize")
        # Branch on the method recorded by beginAuthorization rather than
        # re-reading the feature flag, so this stays consistent with
        # completeAuthorization even if the flag is toggled in between.
        if recipe.store_secrets.get("auth_method") == "usso":
            # As with snap recipes, begin_result is the Ubuntu SSO caveat
            # that must be discharged through Launchpad's own login flow.
            login_url = urlappend(base_url, "+login")
            query_string = urlencode(
                [
                    ("macaroon_caveat_id", begin_result),
                    ("discharge_macaroon_action", "field.actions.complete"),
                    ("discharge_macaroon_field", "field.discharge_macaroon"),
                ]
            )
            return urlunsplit(urlsplit(login_url)._replace(query=query_string))
        else:
            # begin_result is the root macaroon that Candid must discharge.
            return request_candid_discharge(
                request,
                begin_result,
                base_url,
                "field.discharge_macaroon",
                discharge_macaroon_action="field.actions.complete",
            )

    @action("Begin authorization", name="begin")
    def begin_action(self, action, data):
        login_url = self.requestAuthorization(self.context, self.request)
        if login_url is not None:
            self.request.response.redirect(login_url)

    @action("Complete authorization", name="complete")
    def complete_action(self, action, data):
        if not data.get("discharge_macaroon"):
            self.addError(
                structured(
                    _(
                        "Uploads of %(recipe)s to Charmhub were not "
                        "authorized."
                    ),
                    recipe=self.context.name,
                )
            )
            return
        self.context.completeAuthorization(data["discharge_macaroon"])
        self.request.response.addInfoNotification(
            structured(
                _("Uploads of %(recipe)s to Charmhub are now authorized."),
                recipe=self.context.name,
            )
        )
        self.request.response.redirect(canonical_url(self.context))

    @property
    def adapters(self):
        """See `LaunchpadFormView`."""
        return {self.schema: self.context}


class CharmRecipeDeleteView(BaseCharmRecipeEditView):
    """View for deleting charm recipes."""

    @property
    def label(self):
        return "Delete %s charm recipe" % self.context.name

    page_title = "Delete"

    field_names = []
    next_url = None

    @action("Delete charm recipe", name="delete")
    def delete_action(self, action, data):
        owner = self.context.owner
        self.context.destroySelf()
        self.next_url = canonical_url(owner, view_name="+charm-recipes")


class CharmRecipeRequestBuildsView(LaunchpadFormView):
    """A view for requesting builds of a charm recipe."""

    @property
    def label(self):
        return "Request builds for %s" % self.context.name

    next_url = None
    page_title = "Request builds"

    class schema(Interface):
        """Schema for requesting a build."""

        channels = copy_field(
            ICharmRecipe["auto_build_channels"],
            __name__="channels",
            title="Source snap channels",
            required=True,
        )

    custom_widget_channels = CustomWidgetFactory(
        SnapBuildChannelsWidget,
        hint=(
            "The channels to use for build tools when building the charm "
            "recipe."
        ),
    )

    @property
    def cancel_url(self):
        return canonical_url(self.context)

    @property
    def initial_values(self):
        """See `LaunchpadFormView`."""
        return {
            "channels": self.context.auto_build_channels,
        }

    @action("Request builds", name="request")
    def request_action(self, action, data):
        self.context.requestBuilds(self.user, channels=data["channels"])
        self.request.response.addNotification(
            _("Builds will be dispatched soon.")
        )
        self.next_url = self.cancel_url
