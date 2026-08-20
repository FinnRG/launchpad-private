# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interface for communication with Charmhub."""

__all__ = [
    "BadExchangeMacaroonsResponse",
    "BadRequestPackageUploadResponse",
    "BadReviewStatusResponse",
    "ICharmhubClient",
    "CharmhubError",
    "ReleaseFailedResponse",
    "ReviewFailedResponse",
    "UnauthorizedUploadResponse",
    "UploadFailedResponse",
    "UploadNotReviewedYetResponse",
]

import http.client

from lazr.restful.declarations import error_status
from zope.interface import Interface


class CharmhubError(Exception):
    def __init__(self, message="", detail=None, can_retry=False):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.can_retry = can_retry


@error_status(http.client.INTERNAL_SERVER_ERROR)
class BadRequestPackageUploadResponse(CharmhubError):
    pass


class BadExchangeMacaroonsResponse(CharmhubError):
    pass


class UploadFailedResponse(CharmhubError):
    pass


class UnauthorizedUploadResponse(CharmhubError):
    pass


class BadReviewStatusResponse(CharmhubError):
    pass


class UploadNotReviewedYetResponse(CharmhubError):
    pass


class ReviewFailedResponse(CharmhubError):
    pass


class ReleaseFailedResponse(CharmhubError):
    pass


class ICharmhubClient(Interface):
    """Interface for the API provided by Charmhub."""

    def requestPackageUploadPermission(package_name, use_usso=False):
        """Request permission from Charmhub to upload builds of a charm.

        We need the following permissions: `package-manage-revisions` (to
        upload new blobs), `package-manage-releases` (to release revisions),
        and `package-view-revisions` (to check the status of uploaded
        blobs).

        The returned macaroon will include a third-party caveat that must be
        discharged separately.  When `use_usso` is false, the caveat must be
        discharged by Candid.  When `use_usso` is true, it must be discharged
        by Ubuntu SSO.  This method does not acquire that discharge.

        :param package_name: The registered name of this charm on Charmhub.
        :param use_usso: If true, request a macaroon to be discharged by
            Ubuntu SSO rather than Candid.
        :return: A serialized macaroon appropriate for uploading builds of
            this charm.
        """

    def exchangeMacaroons(
        root_macaroon_raw,
        unbound_discharge_macaroon_raw,
        use_usso=False,
        description=None,
    ):
        """Exchange root+discharge macaroons for a new Charmhub-only macaroon.

        :param root_macaroon: A serialized root macaroon from Charmhub.
        :param unbound_discharge_macaroon: A corresponding serialized
            unbound discharge macaroon from Candid or Ubuntu SSO.
        :param use_usso: If true, the macaroons were discharged by Ubuntu
            SSO and are exchanged using the Ubuntu SSO exchange endpoint.
        :param description: A human-readable label for the token, sent as
            client-description when exchanging Ubuntu SSO macaroons. This
            replaces the description that the Candid flow sent earlier when
            requesting upload permission, and is ignored for Candid.
        :return: A serialized macaroon from Charmhub with no third-party
            caveat.
        """

    def uploadFile(lfa):
        """Upload a file to Charmhub.

        :param lfa: The `ILibraryFileAlias` to upload.
        :return: An upload ID.
        :raises UploadFailedResponse: if uploading the file to Charmhub
            failed.
        """

    def push(build, upload_id):
        """Push a charm recipe build to CharmHub.

        :param build: The `ICharmRecipeBuild` to upload.
        :param upload_id: An upload ID previously returned by `uploadFile`.
        :return: A URL to poll for upload processing status.
        :raises UnauthorizedUploadResponse: if the user who authorised this
            upload is not themselves authorised to upload the snap in
            question.
        :raises UploadFailedResponse: if uploading the build to Charmhub
            failed.
        """

    def checkStatus(build, status_url):
        """Poll Charmhub once for upload scan status.

        :param build: The `ICharmRecipeBuild` being uploaded.
        :param status_url: A URL as returned by `upload`.
        :raises UploadNotReviewedYetResponse: if the upload has not yet been
            reviewed.
        :raises BadReviewStatusResponse: if Charmhub failed to review the
            upload.
        :return: The Charmhub revision number for the upload.
        """

    def release(build, revision):
        """Tell Charmhub to release a build to specified channels.

        :param build: The `ICharmRecipeBuild` to release.
        :param revision: The revision returned by Charmhub when uploading
            the build.
        :raises ReleaseFailedResponse: if Charmhub failed to release the
            build.
        """
