# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Communication with Charmhub."""

__all__ = [
    "CharmhubClient",
]

from base64 import b64encode
from urllib.parse import quote

import requests
from lazr.restful.utils import get_current_browser_request
from pymacaroons import Macaroon
from pymacaroons.serializers import JsonSerializer
from requests_toolbelt import MultipartEncoder
from zope.component import getUtility
from zope.interface import implementer

from lp.charms.interfaces.charmhubclient import (
    BadExchangeMacaroonsResponse,
    BadRequestPackageUploadResponse,
    BadReviewStatusResponse,
    ICharmhubClient,
    ReleaseFailedResponse,
    ReviewFailedResponse,
    UnauthorizedUploadResponse,
    UploadFailedResponse,
    UploadNotReviewedYetResponse,
)
from lp.services.config import config
from lp.services.crypto.interfaces import CryptoError, IEncryptedContainer
from lp.services.librarian.utils import EncodableLibraryFileAlias
from lp.services.timeline.requesttimeline import get_request_timeline
from lp.services.timeout import urlfetch
from lp.services.webapp.url import urlappend
from lp.snappy.model.snapstoreclient import MacaroonAuth


def _get_macaroon(recipe):
    """Get the Charmhub macaroon for a recipe."""
    store_secrets = recipe.store_secrets or {}
    macaroon_raw = store_secrets.get("exchanged_encrypted")
    if macaroon_raw is None:
        raise UnauthorizedUploadResponse(
            f"{recipe} is not authorized for upload to Charmhub"
        )
    container = getUtility(IEncryptedContainer, "charmhub-secrets")
    try:
        return container.decrypt(macaroon_raw).decode()
    except CryptoError as e:
        raise UnauthorizedUploadResponse(f"Failed to decrypt macaroon: {e}")


@implementer(ICharmhubClient)
class CharmhubClient:
    """A client for the API provided by Charmhub."""

    @staticmethod
    def _getTimeline():
        # XXX cjwatson 2021-08-05: This can be simplified once jobs have
        # timeline support.
        request = get_current_browser_request()
        if request is None:
            return None
        return get_request_timeline(request)

    @classmethod
    def _makeCharmhubError(cls, error_class, requests_error):
        error_message = requests_error.args[0]
        if requests_error.response.content:
            try:
                response_data = requests_error.response.json()
            except ValueError:
                pass
            else:
                if "error-list" in response_data:
                    error_message = "\n".join(
                        error["message"]
                        for error in response_data["error-list"]
                    )
        detail = requests_error.response.content.decode(errors="replace")
        can_retry = requests_error.response.status_code in (502, 503, 504)
        return error_class(error_message, detail=detail, can_retry=can_retry)

    @classmethod
    def requestPackageUploadPermission(cls, package_name, use_usso=False):
        """See `ICharmhubClient`."""
        assert config.charms.charmhub_url is not None
        request_body = {
            "packages": [{"type": "charm", "name": package_name}],
            "permissions": [
                "package-manage-releases",
                "package-manage-revisions",
                "package-view-revisions",
            ],
        }
        if use_usso:
            request_url = urlappend(
                config.charms.charmhub_url, "v1/tokens/usso"
            )
            # Under Ubuntu SSO, the description is supplied later during the
            # exchange, so here we only set how long the token should last.
            # The new endpoint requires us to state this lifetime
            # explicitly, whereas the old Candid endpoint did not accept one
            # and let the server decide.
            request_body["ttl"] = int(config.charms.charmhub_macaroon_ttl)
        else:
            request_url = urlappend(config.charms.charmhub_url, "v1/tokens")
            request_body["description"] = "{} for {}".format(
                package_name, config.vhost.mainsite.hostname
            )
        request = get_current_browser_request()
        timeline_action = get_request_timeline(request).start(
            "request-charm-upload-macaroon", package_name, allow_nested=True
        )
        try:
            response = urlfetch(
                request_url,
                method="POST",
                json=request_body,
            )
            response_data = response.json()
            if "macaroon" not in response_data:
                raise BadRequestPackageUploadResponse(response.text)
            return response_data["macaroon"]
        except requests.HTTPError as e:
            raise cls._makeCharmhubError(BadRequestPackageUploadResponse, e)
        finally:
            timeline_action.finish()

    @classmethod
    def exchangeMacaroons(
        cls,
        root_macaroon_raw,
        unbound_discharge_macaroon_raw,
        use_usso=False,
        description=None,
    ):
        """See `ICharmhubClient`."""
        assert config.charms.charmhub_url is not None
        if use_usso:
            request_url = urlappend(
                config.charms.charmhub_url, "v1/tokens/usso/exchange"
            )
            # MacaroonAuth binds the discharge to the root and sets the
            # Authorization header for us, so we pass the raw macaroons
            # straight through.
            request_kwargs = {
                "auth": MacaroonAuth(
                    root_macaroon_raw, unbound_discharge_macaroon_raw
                ),
                "json": {"client-description": description},
            }
        else:
            request_url = urlappend(
                config.charms.charmhub_url, "v1/tokens/exchange"
            )
            # Candid expects a nonstandard "Macaroons" header that no request
            # helper builds for us, so we assemble it by hand, which also
            # means binding the discharge to the root here ourselves.
            root_macaroon = Macaroon.deserialize(
                root_macaroon_raw, JsonSerializer()
            )
            unbound_discharge_macaroon = Macaroon.deserialize(
                unbound_discharge_macaroon_raw, JsonSerializer()
            )
            discharge_macaroon_raw = root_macaroon.prepare_for_request(
                unbound_discharge_macaroon
            ).serialize(JsonSerializer())
            all_macaroons = b64encode(
                "[{}, {}]".format(
                    root_macaroon_raw, discharge_macaroon_raw
                ).encode()
            ).decode()
            request_kwargs = {
                "headers": {"Macaroons": all_macaroons},
                "json": {},
            }
        request = get_current_browser_request()
        timeline_action = get_request_timeline(request).start(
            "exchange-macaroons", "", allow_nested=True
        )
        try:
            response = urlfetch(request_url, method="POST", **request_kwargs)
            response_data = response.json()
            if "macaroon" not in response_data:
                raise BadExchangeMacaroonsResponse(response.text)
            return response_data["macaroon"]
        except requests.HTTPError as e:
            raise cls._makeCharmhubError(BadExchangeMacaroonsResponse, e)
        finally:
            timeline_action.finish()

    @classmethod
    def uploadFile(cls, lfa):
        """Upload a single file to Charmhub's storage."""
        assert config.charms.charmhub_storage_url is not None
        unscanned_upload_url = urlappend(
            config.charms.charmhub_storage_url, "unscanned-upload/"
        )
        lfa.open()
        try:
            lfa_wrapper = EncodableLibraryFileAlias(lfa)
            encoder = MultipartEncoder(
                fields={
                    "binary": (
                        lfa.filename,
                        lfa_wrapper,
                        "application/octet-stream",
                    ),
                }
            )
            request = get_current_browser_request()
            timeline_action = get_request_timeline(request).start(
                "charm-storage-push", lfa.filename, allow_nested=True
            )
            try:
                response = urlfetch(
                    unscanned_upload_url,
                    method="POST",
                    data=encoder,
                    headers={
                        "Content-Type": encoder.content_type,
                        "Accept": "application/json",
                    },
                )
                response_data = response.json()
                if not response_data.get("successful", False):
                    raise UploadFailedResponse(response.text)
                return response_data["upload_id"]
            except requests.HTTPError as e:
                raise cls._makeCharmhubError(UploadFailedResponse, e)
            finally:
                timeline_action.finish()
        finally:
            lfa.close()

    @classmethod
    def push(cls, build, upload_id):
        """Push an already-uploaded charm to Charmhub."""
        recipe = build.recipe
        assert recipe.can_upload_to_store
        push_url = urlappend(
            config.charms.charmhub_url,
            f"v1/charm/{quote(recipe.store_name)}/revisions",
        )
        macaroon_raw = _get_macaroon(recipe)
        data = {"upload-id": upload_id}
        request = get_current_browser_request()
        timeline_action = get_request_timeline(request).start(
            "charm-push", recipe.store_name, allow_nested=True
        )
        try:
            response = urlfetch(
                push_url,
                method="POST",
                headers={"Authorization": f"Macaroon {macaroon_raw}"},
                json=data,
            )
            response_data = response.json()
            return response_data["status-url"]
        except requests.HTTPError as e:
            if e.response.status_code == 401:
                raise cls._makeCharmhubError(UnauthorizedUploadResponse, e)
            else:
                raise cls._makeCharmhubError(UploadFailedResponse, e)
        finally:
            timeline_action.finish()

    @classmethod
    def checkStatus(cls, build, status_url):
        """See `ICharmhubClient`."""
        status_url = urlappend(
            config.charms.charmhub_url, status_url.lstrip("/")
        )
        macaroon_raw = _get_macaroon(build.recipe)
        request = get_current_browser_request()
        timeline_action = get_request_timeline(request).start(
            "charm-check-status", status_url, allow_nested=True
        )
        try:
            response = urlfetch(
                status_url,
                headers={"Authorization": f"Macaroon {macaroon_raw}"},
            )
            response_data = response.json()
            # We're asking for a single upload ID, so the response should
            # only have one revision.
            if len(response_data.get("revisions", [])) != 1:
                raise BadReviewStatusResponse(response.text)
            [revision] = response_data["revisions"]
            if revision["status"] == "approved":
                if revision["revision"] is None:
                    raise ReviewFailedResponse(
                        "Review passed but did not assign a revision."
                    )
                return revision["revision"]
            elif revision["status"] == "rejected":
                error_message = "\n".join(
                    error["message"] for error in revision["errors"]
                )
                raise ReviewFailedResponse(error_message)
            else:
                raise UploadNotReviewedYetResponse()
        except requests.HTTPError as e:
            raise cls._makeCharmhubError(BadReviewStatusResponse, e)
        finally:
            timeline_action.finish()

    @classmethod
    def release(cls, build, revision):
        """See `ICharmhubClient`."""
        assert config.charms.charmhub_url is not None
        recipe = build.recipe
        assert recipe.store_name is not None
        assert recipe.store_secrets is not None
        assert recipe.store_channels
        release_url = urlappend(
            config.charms.charmhub_url,
            f"v1/charm/{quote(recipe.store_name)}/releases",
        )
        macaroon_raw = _get_macaroon(recipe)
        data = [
            {"channel": channel, "revision": revision}
            for channel in recipe.store_channels
        ]
        request = get_current_browser_request()
        timeline_action = get_request_timeline(request).start(
            "charm-release", recipe.store_name, allow_nested=True
        )
        try:
            urlfetch(
                release_url,
                method="POST",
                headers={"Authorization": f"Macaroon {macaroon_raw}"},
                json=data,
            )
        except requests.HTTPError as e:
            raise cls._makeCharmhubError(ReleaseFailedResponse, e)
        finally:
            timeline_action.finish()
