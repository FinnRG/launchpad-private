# Copyright 2016-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Communication with the snap store."""

__all__ = [
    "MacaroonAuth",
    "SnapStoreClient",
]

import base64
import json
import string

import requests
import six
from lazr.restful.utils import get_current_browser_request
from pymacaroons import Macaroon
from requests_toolbelt import MultipartEncoder
from zope.component import getUtility
from zope.interface import implementer
from zope.security.proxy import removeSecurityProxy

from lp.services.config import config
from lp.services.crypto.interfaces import CryptoError, IEncryptedContainer
from lp.services.librarian.utils import EncodableLibraryFileAlias
from lp.services.scripts import log
from lp.services.timeline.requesttimeline import get_request_timeline
from lp.services.timeout import urlfetch
from lp.services.webapp.url import urlappend
from lp.snappy.interfaces.snapstoreclient import (
    BadRefreshResponse,
    BadRequestPackageUploadResponse,
    BadScanStatusResponse,
    ISnapStoreClient,
    NeedsRefreshResponse,
    ScanFailedResponse,
    SnapNotFoundResponse,
    UnauthorizedUploadResponse,
    UploadFailedResponse,
    UploadNotScannedYetResponse,
)


class InvalidStoreSecretsError(Exception):
    pass


class MacaroonAuth(requests.auth.AuthBase):
    """Attaches macaroon authentication to a given Request object."""

    # The union of the base64 and URL-safe base64 alphabets.
    allowed_chars = set(string.digits + string.ascii_letters + "+/=-_")

    def __init__(
        self,
        root_macaroon_raw,
        unbound_discharge_macaroon_raw=None,
        logger=log,
    ):
        self.root_macaroon_raw = root_macaroon_raw
        self.unbound_discharge_macaroon_raw = unbound_discharge_macaroon_raw
        self.logger = logger

    def _logMacaroon(self, macaroon_name, macaroon_raw):
        """Log relevant information from the authorising macaroons.

        This shouldn't be trusted for anything since we can't verify the
        macaroons here, but it's helpful when debugging.
        """
        macaroon = Macaroon.deserialize(macaroon_raw)
        for caveat in macaroon.first_party_caveats():
            try:
                _, key, value = caveat.caveat_id.split("|")
                if key == "account":
                    account = json.loads(
                        base64.b64decode(value.encode("UTF-8")).decode("UTF-8")
                    )
                    if "openid" in account:
                        self.logger.debug(
                            "%s macaroon: OpenID identifier: %s"
                            % (macaroon_name, account["openid"])
                        )
                elif key == "acl":
                    self.logger.debug(
                        "%s macaroon: permissions: %s" % (macaroon_name, value)
                    )
                elif key == "channel":
                    self.logger.debug(
                        "%s macaroon: channels: %s" % (macaroon_name, value)
                    )
                elif key == "expires":
                    self.logger.debug(
                        "%s macaroon: expires: %s" % (macaroon_name, value)
                    )
                elif key == "package_id":
                    self.logger.debug(
                        "%s macaroon: snap-ids: %s" % (macaroon_name, value)
                    )
                elif key == "valid_since":
                    self.logger.debug(
                        "%s macaroon: valid since: %s" % (macaroon_name, value)
                    )
            except ValueError:
                pass

    def _makeAuthParam(self, key, value):
        # Check framing.
        if not set(key).issubset(self.allowed_chars):
            raise InvalidStoreSecretsError(
                "Key contains unsafe characters: %r" % key
            )
        if not set(value).issubset(self.allowed_chars):
            # Don't include secrets in exception arguments.
            raise InvalidStoreSecretsError("Value contains unsafe characters")
        self._logMacaroon(key, value)
        return '%s="%s"' % (key, value)

    @property
    def discharge_macaroon_raw(self):
        root_macaroon = Macaroon.deserialize(self.root_macaroon_raw)
        unbound_discharge_macaroon = Macaroon.deserialize(
            self.unbound_discharge_macaroon_raw
        )
        discharge_macaroon = root_macaroon.prepare_for_request(
            unbound_discharge_macaroon
        )
        return discharge_macaroon.serialize()

    def __call__(self, r):
        params = []
        params.append(self._makeAuthParam("root", self.root_macaroon_raw))
        if self.unbound_discharge_macaroon_raw is not None:
            params.append(
                self._makeAuthParam("discharge", self.discharge_macaroon_raw)
            )
        r.headers["Authorization"] = "Macaroon " + ", ".join(params)
        return r


def _get_discharge_macaroon_raw(snap):
    """Get the serialised discharge macaroon for a snap, if any.

    This copes with either unencrypted (the historical default) or encrypted
    macaroons.
    """
    if snap.store_secrets is None:
        raise AssertionError("snap.store_secrets is None")
    if "discharge_encrypted" in snap.store_secrets:
        container = getUtility(IEncryptedContainer, "snap-store-secrets")
        try:
            return container.decrypt(
                snap.store_secrets["discharge_encrypted"]
            ).decode("UTF-8")
        except CryptoError as e:
            raise UnauthorizedUploadResponse(
                "Failed to decrypt discharge macaroon: %s" % e
            )
    else:
        return snap.store_secrets.get("discharge")


def _set_discharge_macaroon_raw(snap, discharge_macaroon_raw):
    """Set the serialised discharge macaroon for a snap.

    The macaroon is encrypted if possible.
    """
    # Set a new dict here to avoid problems with security proxies.
    new_secrets = dict(snap.store_secrets)
    container = getUtility(IEncryptedContainer, "snap-store-secrets")
    if container.can_encrypt:
        new_secrets["discharge_encrypted"] = removeSecurityProxy(
            container.encrypt(discharge_macaroon_raw.encode("UTF-8"))
        )
        new_secrets.pop("discharge", None)
    else:
        new_secrets["discharge"] = discharge_macaroon_raw
        new_secrets.pop("discharge_encrypted", None)
    snap.store_secrets = new_secrets


# Hardcoded fallback.
_default_store_channels = [
    {"name": "candidate", "display_name": "Candidate"},
    {"name": "edge", "display_name": "Edge"},
    {"name": "beta", "display_name": "Beta"},
    {"name": "stable", "display_name": "Stable"},
]


@implementer(ISnapStoreClient)
class SnapStoreClient:
    """A client for the API provided by the snap store."""

    @staticmethod
    def _getTimeline():
        # XXX cjwatson 2016-06-29: This can be simplified once jobs have
        # timeline support.
        request = get_current_browser_request()
        if request is None:
            return None
        return get_request_timeline(request)

    @classmethod
    def _makeSnapStoreError(cls, error_class, requests_error):
        error_message = requests_error.args[0]
        if requests_error.response.content:
            try:
                response_data = requests_error.response.json()
            except ValueError:
                pass
            else:
                if "error_list" in response_data:
                    error_message = "\n".join(
                        error["message"]
                        for error in response_data["error_list"]
                    )
        detail = six.ensure_text(
            requests_error.response.content, errors="replace"
        )
        can_retry = requests_error.response.status_code in (502, 503, 504)
        return error_class(error_message, detail=detail, can_retry=can_retry)

    @classmethod
    def requestPackageUploadPermission(cls, snappy_series, snap_name):
        assert config.snappy.store_url is not None
        request_url = urlappend(config.snappy.store_url, "dev/api/acl/")
        request = get_current_browser_request()
        timeline_action = get_request_timeline(request).start(
            "request-snap-upload-macaroon",
            "%s/%s" % (snappy_series.name, snap_name),
            allow_nested=True,
        )
        try:
            response = urlfetch(
                request_url,
                method="POST",
                json={
                    "packages": [
                        {"name": snap_name, "series": snappy_series.name}
                    ],
                    "permissions": ["package_upload"],
                },
            )
            response_data = response.json()
            if "macaroon" not in response_data:
                raise BadRequestPackageUploadResponse(response.text)
            return response_data["macaroon"]
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                raise cls._makeSnapStoreError(SnapNotFoundResponse, e)
            raise cls._makeSnapStoreError(BadRequestPackageUploadResponse, e)
        finally:
            timeline_action.finish()

    @classmethod
    def uploadFile(cls, lfa):
        """Upload a single file."""
        assert config.snappy.store_upload_url is not None
        unscanned_upload_url = urlappend(
            config.snappy.store_upload_url, "unscanned-upload/"
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
            # XXX cjwatson 2016-05-09: This should add timeline information,
            # but that's currently difficult in jobs.
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
                raise cls._makeSnapStoreError(UploadFailedResponse, e)
        finally:
            lfa.close()

    @classmethod
    def _push(cls, snapbuild, upload_id, components=None):
        """Create a new store upload based on the uploaded file."""
        snap = snapbuild.snap
        assert snap.can_upload_to_store
        assert snapbuild.date_started is not None
        upload_url = urlappend(config.snappy.store_url, "dev/api/snap-push/")
        data = {
            "name": snap.store_name,
            "updown_id": upload_id,
            "series": snap.store_series.name,
            "built_at": snapbuild.date_started.isoformat(),
        }

        if components:
            data["components"] = components

        # The security proxy is useless and breaks JSON serialisation.
        channels = removeSecurityProxy(snap.store_channels)
        if channels:
            # This will cause a release
            data.update(
                {
                    "channels": channels,
                    "only_if_newer": True,
                }
            )
        # XXX cjwatson 2016-05-09: This should add timeline information, but
        # that's currently difficult in jobs.
        try:
            response = urlfetch(
                upload_url,
                method="POST",
                json=data,
                auth=MacaroonAuth(
                    snap.store_secrets["root"],
                    _get_discharge_macaroon_raw(snap),
                ),
            )
            response_data = response.json()
            return response_data["status_details_url"]
        except requests.HTTPError as e:
            if e.response.status_code == 401:
                if (
                    e.response.headers.get("WWW-Authenticate")
                    == "Macaroon needs_refresh=1"
                ):
                    raise NeedsRefreshResponse()
                else:
                    raise cls._makeSnapStoreError(
                        UnauthorizedUploadResponse, e
                    )
            raise cls._makeSnapStoreError(UploadFailedResponse, e)

    @classmethod
    def push(cls, snapbuild, upload_id, components=None):
        """See `ISnapStoreClient`."""
        return cls.refreshIfNecessary(
            snapbuild.snap, cls._push, snapbuild, upload_id, components
        )

    @classmethod
    def refreshDischargeMacaroon(cls, snap):
        """See `ISnapStoreClient`."""
        assert config.launchpad.openid_provider_root is not None
        assert snap.store_secrets is not None
        refresh_url = urlappend(
            config.launchpad.openid_provider_root, "api/v2/tokens/refresh"
        )
        discharge_macaroon_raw = _get_discharge_macaroon_raw(snap)
        if discharge_macaroon_raw is None:
            raise UnauthorizedUploadResponse(
                "Tried to refresh discharge for snap with no discharge "
                "macaroon"
            )
        data = {"discharge_macaroon": discharge_macaroon_raw}
        try:
            response = urlfetch(refresh_url, method="POST", json=data)
        except requests.HTTPError as e:
            raise cls._makeSnapStoreError(BadRefreshResponse, e)
        response_data = response.json()
        if "discharge_macaroon" not in response_data:
            raise BadRefreshResponse(response.text)
        _set_discharge_macaroon_raw(snap, response_data["discharge_macaroon"])

    @classmethod
    def refreshIfNecessary(cls, snap, f, *args, **kwargs):
        """See `ISnapStoreClient`."""
        try:
            return f(*args, **kwargs)
        except NeedsRefreshResponse:
            cls.refreshDischargeMacaroon(snap)
            return f(*args, **kwargs)

    @classmethod
    def checkStatus(cls, status_url):
        """See `ISnapStoreClient`."""
        try:
            response = urlfetch(status_url)
            response_data = response.json()
            if not response_data["processed"]:
                raise UploadNotScannedYetResponse()
            elif "errors" in response_data:
                # This is returned as error in the upload,
                # but there is nothing we can do about it,
                # our upload has been successful
                if response_data["code"] == "need_manual_review":
                    return response_data["url"], response_data["revision"]
                # The review-queued state is a little odd.  It shows up as a
                # processing error of sorts, and it doesn't contain a URL or
                # a revision; on the other hand, it means that there's no
                # point waiting any longer because a manual review might
                # take an arbitrary amount of time.  We'll just return
                # (None, None) to indicate that we have no information but
                # that it's OK to continue.
                if response_data["code"] == "processing_error" and any(
                    error["code"] == "review-queued"
                    for error in response_data["errors"]
                ):
                    return None, None
                error_message = "\n".join(
                    error["message"] for error in response_data["errors"]
                )
                error_messages = []
                for error in response_data["errors"]:
                    error_detail = {"message": error["message"]}
                    if "link" in error:
                        error_detail["link"] = error["link"]
                    error_messages.append(error_detail)
                raise ScanFailedResponse(
                    error_message, messages=error_messages
                )
            else:
                return response_data["url"], response_data["revision"]
        except requests.HTTPError as e:
            raise cls._makeSnapStoreError(BadScanStatusResponse, e)
