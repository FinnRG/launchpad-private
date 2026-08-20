# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for communication with Charmhub."""

import base64
import hashlib
import io
import json
from urllib.parse import quote

import multipart
import responses
import transaction
from lazr.restful.utils import get_current_browser_request
from nacl.public import PrivateKey
from pymacaroons import Macaroon, Verifier
from pymacaroons.serializers import JsonSerializer
from requests.utils import parse_dict_header
from testtools.matchers import (
    AfterPreprocessing,
    Contains,
    ContainsDict,
    Equals,
    Is,
    Matcher,
    MatchesAll,
    MatchesDict,
    MatchesListwise,
    MatchesStructure,
    Mismatch,
)
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.buildmaster.enums import BuildStatus
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
from lp.charms.interfaces.charmrecipe import CHARM_RECIPE_ALLOW_CREATE
from lp.services.crypto.interfaces import IEncryptedContainer
from lp.services.features.testing import FeatureFixture
from lp.services.timeline.requesttimeline import get_request_timeline
from lp.testing import TestCaseWithFactory
from lp.testing.dbuser import dbuser
from lp.testing.layers import LaunchpadZopelessLayer


class MacaroonVerifies(Matcher):
    """Matches if a serialized macaroon passes verification."""

    def __init__(self, key):
        self.key = key

    def __str__(self):
        return f"MacaroonVerifies({self.key!r})"

    def match(self, macaroon_raw):
        macaroon = Macaroon.deserialize(macaroon_raw)
        try:
            Verifier().verify(macaroon, self.key)
        except Exception as e:
            return Mismatch("Macaroon does not verify: %s" % e)


class MacaroonsVerify(Matcher):
    """Matches if the root and discharge in an auth header verify."""

    def __init__(self, key):
        self.key = key

    def __str__(self):
        return f"MacaroonsVerify({self.key!r})"

    def match(self, macaroons):
        mismatch = Contains("root").match(macaroons)
        if mismatch is not None:
            return mismatch
        root_macaroon = Macaroon.deserialize(macaroons["root"])
        if "discharge" in macaroons:
            discharge_macaroons = [
                Macaroon.deserialize(macaroons["discharge"])
            ]
        else:
            discharge_macaroons = []
        try:
            Verifier().verify(root_macaroon, self.key, discharge_macaroons)
        except Exception as e:
            return Mismatch("Macaroons do not verify: %s" % e)


class RequestMatches(MatchesAll):
    """Matches a request with the specified attributes."""

    def __init__(
        self,
        macaroons=None,
        auth=None,
        json_data=None,
        file_data=None,
        **kwargs,
    ):
        matchers = []
        kwargs = dict(kwargs)
        if macaroons is not None:
            matchers.append(
                MatchesStructure(
                    headers=ContainsDict(
                        {
                            "Macaroons": AfterPreprocessing(
                                lambda v: json.loads(
                                    base64.b64decode(v.encode()).decode()
                                ),
                                Equals([json.loads(m) for m in macaroons]),
                            ),
                        }
                    )
                )
            )
        if auth is not None:
            auth_scheme, auth_params_matcher = auth
            matchers.append(
                MatchesStructure(
                    headers=ContainsDict(
                        {
                            "Authorization": AfterPreprocessing(
                                lambda v: v.split(" ", 1),
                                MatchesListwise(
                                    [
                                        Equals(auth_scheme),
                                        auth_params_matcher,
                                    ]
                                ),
                            ),
                        }
                    )
                )
            )
        if json_data is not None:
            matchers.append(
                MatchesStructure(
                    body=AfterPreprocessing(json.loads, Equals(json_data))
                )
            )
        elif file_data is not None:
            matchers.append(
                AfterPreprocessing(
                    lambda r: multipart.parse_form_data(
                        {
                            "REQUEST_METHOD": r.method,
                            "CONTENT_TYPE": r.headers["Content-Type"],
                            "CONTENT_LENGTH": r.headers["Content-Length"],
                            "wsgi.input": io.BytesIO(
                                r.body.read()
                                if hasattr(r.body, "read")
                                else r.body
                            ),
                        }
                    )[1],
                    MatchesDict(file_data),
                )
            )
        if kwargs:
            matchers.append(MatchesStructure(**kwargs))
        super().__init__(*matchers)


class TestCharmhubClient(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))
        self.pushConfig(
            "charms",
            charmhub_url="http://charmhub.example/",
            charmhub_storage_url="http://storage.charmhub.example/",
        )
        self.client = getUtility(ICharmhubClient)

    def _setUpSecretStorage(self):
        self.private_key = PrivateKey.generate()
        self.pushConfig(
            "charms",
            charmhub_secrets_public_key=base64.b64encode(
                bytes(self.private_key.public_key)
            ).decode(),
            charmhub_secrets_private_key=base64.b64encode(
                bytes(self.private_key)
            ).decode(),
        )

    def _makeStoreSecrets(self):
        self.exchanged_key = hashlib.sha256(
            self.factory.getUniqueBytes()
        ).hexdigest()
        exchanged_macaroon = Macaroon(key=self.exchanged_key)
        container = getUtility(IEncryptedContainer, "charmhub-secrets")
        return {
            "exchanged_encrypted": removeSecurityProxy(
                container.encrypt(exchanged_macaroon.serialize().encode())
            ),
        }

    def _addUnscannedUploadResponse(self):
        responses.add(
            "POST",
            "http://storage.charmhub.example/unscanned-upload/",
            json={"successful": True, "upload_id": 1},
        )

    def _addCharmPushResponse(self, name):
        responses.add(
            "POST",
            "http://charmhub.example/v1/charm/{}/revisions".format(
                quote(name)
            ),
            status=200,
            json={
                "status-url": (
                    "/v1/charm/{}/revisions/review?upload-id=123".format(
                        quote(name)
                    )
                ),
            },
        )

    def _addCharmReleaseResponse(self, name):
        responses.add(
            "POST",
            f"http://charmhub.example/v1/charm/{quote(name)}/releases",
            json={},
        )

    @responses.activate
    def test_requestPackageUploadPermission(self):
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens",
            json={"macaroon": "sentinel"},
        )
        macaroon = self.client.requestPackageUploadPermission("test-charm")
        self.assertThat(
            responses.calls[-1].request,
            RequestMatches(
                url=Equals("http://charmhub.example/v1/tokens"),
                method=Equals("POST"),
                json_data={
                    "description": "test-charm for launchpad.test",
                    "packages": [{"type": "charm", "name": "test-charm"}],
                    "permissions": [
                        "package-manage-releases",
                        "package-manage-revisions",
                        "package-view-revisions",
                    ],
                },
            ),
        )
        self.assertEqual("sentinel", macaroon)
        request = get_current_browser_request()
        start, stop = get_request_timeline(request).actions[-2:]
        self.assertThat(
            start,
            MatchesStructure.byEquality(
                category="request-charm-upload-macaroon-start",
                detail="test-charm",
            ),
        )
        self.assertThat(
            stop,
            MatchesStructure.byEquality(
                category="request-charm-upload-macaroon-stop",
                detail="test-charm",
            ),
        )

    @responses.activate
    def test_requestPackageUploadPermission_missing_macaroon(self):
        responses.add("POST", "http://charmhub.example/v1/tokens", json={})
        self.assertRaisesWithContent(
            BadRequestPackageUploadResponse,
            "{}",
            self.client.requestPackageUploadPermission,
            "test-charm",
        )

    @responses.activate
    def test_requestPackageUploadPermission_error(self):
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens",
            status=503,
            json={"error-list": [{"message": "Failed"}]},
        )
        self.assertRaisesWithContent(
            BadRequestPackageUploadResponse,
            "Failed",
            self.client.requestPackageUploadPermission,
            "test-charm",
        )

    @responses.activate
    def test_requestPackageUploadPermission_404(self):
        responses.add("POST", "http://charmhub.example/v1/tokens", status=404)
        self.assertRaisesWithContent(
            BadRequestPackageUploadResponse,
            "404 Client Error: Not Found",
            self.client.requestPackageUploadPermission,
            "test-charm",
        )

    @responses.activate
    def test_requestPackageUploadPermission_usso(self):
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/usso",
            json={"macaroon": "sentinel"},
        )
        macaroon = self.client.requestPackageUploadPermission(
            "test-charm", use_usso=True
        )
        self.assertThat(
            responses.calls[-1].request,
            RequestMatches(
                url=Equals("http://charmhub.example/v1/tokens/usso"),
                method=Equals("POST"),
                json_data={
                    "ttl": 31536000,
                    "packages": [{"type": "charm", "name": "test-charm"}],
                    "permissions": [
                        "package-manage-releases",
                        "package-manage-revisions",
                        "package-view-revisions",
                    ],
                },
            ),
        )
        self.assertEqual("sentinel", macaroon)

    @responses.activate
    def test_exchangeMacaroons(self):
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/exchange",
            json={"macaroon": "sentinel"},
        )
        root_macaroon = Macaroon(version=2)
        root_macaroon_raw = root_macaroon.serialize(JsonSerializer())
        unbound_discharge_macaroon = Macaroon(version=2)
        unbound_discharge_macaroon_raw = unbound_discharge_macaroon.serialize(
            JsonSerializer()
        )
        discharge_macaroon_raw = root_macaroon.prepare_for_request(
            unbound_discharge_macaroon
        ).serialize(JsonSerializer())
        exchanged_macaroon_raw = self.client.exchangeMacaroons(
            root_macaroon_raw, unbound_discharge_macaroon_raw
        )
        self.assertThat(
            responses.calls[-1].request,
            RequestMatches(
                url=Equals("http://charmhub.example/v1/tokens/exchange"),
                method=Equals("POST"),
                macaroons=[root_macaroon_raw, discharge_macaroon_raw],
                json_data={},
            ),
        )
        self.assertEqual("sentinel", exchanged_macaroon_raw)
        request = get_current_browser_request()
        start, stop = get_request_timeline(request).actions[-2:]
        self.assertThat(
            start,
            MatchesStructure.byEquality(
                category="exchange-macaroons-start", detail=""
            ),
        )
        self.assertThat(
            stop,
            MatchesStructure.byEquality(
                category="exchange-macaroons-stop", detail=""
            ),
        )

    @responses.activate
    def test_exchangeMacaroons_usso(self):
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/usso/exchange",
            json={"macaroon": "sentinel"},
        )
        root_key = hashlib.sha256(b"root").hexdigest()
        root_macaroon = Macaroon(version=2, key=root_key)
        discharge_key = hashlib.sha256(b"discharge").hexdigest()
        discharge_caveat_id = '{"secret": "thing"}'
        root_macaroon.add_third_party_caveat(
            "sso.example", discharge_key, discharge_caveat_id
        )
        root_macaroon_raw = root_macaroon.serialize()
        unbound_discharge_macaroon = Macaroon(
            version=2,
            location="sso.example",
            key=discharge_key,
            identifier=discharge_caveat_id,
        )
        unbound_discharge_macaroon_raw = unbound_discharge_macaroon.serialize()
        exchanged_macaroon_raw = self.client.exchangeMacaroons(
            root_macaroon_raw,
            unbound_discharge_macaroon_raw,
            use_usso=True,
            description="test-charm for launchpad.test",
        )
        self.assertThat(
            responses.calls[-1].request,
            RequestMatches(
                url=Equals("http://charmhub.example/v1/tokens/usso/exchange"),
                method=Equals("POST"),
                auth=(
                    "Macaroon",
                    AfterPreprocessing(
                        parse_dict_header, MacaroonsVerify(root_key)
                    ),
                ),
                json_data={
                    "client-description": "test-charm for launchpad.test"
                },
            ),
        )
        self.assertEqual("sentinel", exchanged_macaroon_raw)

    @responses.activate
    def test_exchangeMacaroons_missing_macaroon(self):
        responses.add(
            "POST", "http://charmhub.example/v1/tokens/exchange", json={}
        )
        root_macaroon_raw = Macaroon(version=2).serialize(JsonSerializer())
        discharge_macaroon_raw = Macaroon(version=2).serialize(
            JsonSerializer()
        )
        self.assertRaisesWithContent(
            BadExchangeMacaroonsResponse,
            "{}",
            self.client.exchangeMacaroons,
            root_macaroon_raw,
            discharge_macaroon_raw,
        )

    @responses.activate
    def test_exchangeMacaroons_error(self):
        responses.add(
            "POST",
            "http://charmhub.example/v1/tokens/exchange",
            status=401,
            json={"error-list": [{"message": "Exchange window expired"}]},
        )
        root_macaroon_raw = Macaroon(version=2).serialize(JsonSerializer())
        discharge_macaroon_raw = Macaroon(version=2).serialize(
            JsonSerializer()
        )
        self.assertRaisesWithContent(
            BadExchangeMacaroonsResponse,
            "Exchange window expired",
            self.client.exchangeMacaroons,
            root_macaroon_raw,
            discharge_macaroon_raw,
        )

    @responses.activate
    def test_exchangeMacaroons_404(self):
        responses.add(
            "POST", "http://charmhub.example/v1/tokens/exchange", status=404
        )
        root_macaroon_raw = Macaroon(version=2).serialize(JsonSerializer())
        discharge_macaroon_raw = Macaroon(version=2).serialize(
            JsonSerializer()
        )
        self.assertRaisesWithContent(
            BadExchangeMacaroonsResponse,
            "404 Client Error: Not Found",
            self.client.exchangeMacaroons,
            root_macaroon_raw,
            discharge_macaroon_raw,
        )

    def makeUploadableCharmRecipeBuild(self, store_secrets=None):
        if store_secrets is None:
            store_secrets = self._makeStoreSecrets()
        recipe = self.factory.makeCharmRecipe(
            store_upload=True,
            store_name="test-charm",
            store_secrets=store_secrets,
        )
        build = self.factory.makeCharmRecipeBuild(recipe=recipe)
        charm_lfa = self.factory.makeLibraryFileAlias(
            filename="test-charm.charm", content="dummy charm content"
        )
        self.factory.makeCharmFile(build=build, library_file=charm_lfa)
        manifest_lfa = self.factory.makeLibraryFileAlias(
            filename="test-charm.manifest", content="dummy manifest content"
        )
        self.factory.makeCharmFile(build=build, library_file=manifest_lfa)
        build.updateStatus(BuildStatus.BUILDING)
        build.updateStatus(BuildStatus.FULLYBUILT)
        return build

    @responses.activate
    def test_uploadFile(self):
        charm_lfa = self.factory.makeLibraryFileAlias(
            filename="test-charm.charm", content="dummy charm content"
        )
        transaction.commit()
        self._addUnscannedUploadResponse()
        # XXX cjwatson 2021-08-19: Use
        # config.ICharmhubUploadJobSource.dbuser once that job exists.
        with dbuser("charm-build-job"):
            self.assertEqual(1, self.client.uploadFile(charm_lfa))
        requests = [call.request for call in responses.calls]
        request_matcher = RequestMatches(
            url=Equals("http://storage.charmhub.example/unscanned-upload/"),
            method=Equals("POST"),
            file_data={
                "binary": MatchesStructure.byEquality(
                    name="binary",
                    filename="test-charm.charm",
                    value="dummy charm content",
                    content_type="application/octet-stream",
                )
            },
        )
        self.assertThat(requests, MatchesListwise([request_matcher]))

    @responses.activate
    def test_uploadFile_error(self):
        charm_lfa = self.factory.makeLibraryFileAlias(
            filename="test-charm.charm", content="dummy charm content"
        )
        transaction.commit()
        responses.add(
            "POST",
            "http://storage.charmhub.example/unscanned-upload/",
            status=502,
            body="The proxy exploded.\n",
        )
        # XXX cjwatson 2021-08-19: Use
        # config.ICharmhubUploadJobSource.dbuser once that job exists.
        with dbuser("charm-build-job"):
            err = self.assertRaises(
                UploadFailedResponse, self.client.uploadFile, charm_lfa
            )
            self.assertEqual("502 Server Error: Bad Gateway", str(err))
            self.assertThat(
                err,
                MatchesStructure(
                    detail=Equals("The proxy exploded.\n"), can_retry=Is(True)
                ),
            )

    @responses.activate
    def test_push(self):
        self._setUpSecretStorage()
        build = self.makeUploadableCharmRecipeBuild()
        transaction.commit()
        self._addCharmPushResponse("test-charm")
        # XXX cjwatson 2021-08-19: Use
        # config.ICharmhubUploadJobSource.dbuser once that job exists.
        with dbuser("charm-build-job"):
            self.assertEqual(
                "/v1/charm/test-charm/revisions/review?upload-id=123",
                self.client.push(build, 1),
            )
        requests = [call.request for call in responses.calls]
        request_matcher = RequestMatches(
            url=Equals(
                "http://charmhub.example/v1/charm/test-charm/revisions"
            ),
            method=Equals("POST"),
            headers=ContainsDict({"Content-Type": Equals("application/json")}),
            auth=(
                "Macaroon",
                MacaroonVerifies(self.exchanged_key),
            ),
            json_data={"upload-id": 1},
        )
        self.assertThat(requests, MatchesListwise([request_matcher]))

    @responses.activate
    def test_push_unauthorized(self):
        self._setUpSecretStorage()
        build = self.makeUploadableCharmRecipeBuild()
        transaction.commit()
        charm_push_error = {
            "code": "permission-required",
            "message": "Missing required permission: package-manage-revisions",
        }
        responses.add(
            "POST",
            "http://charmhub.example/v1/charm/test-charm/revisions",
            status=401,
            json={"error-list": [charm_push_error]},
        )
        # XXX cjwatson 2021-08-19: Use
        # config.ICharmhubUploadJobSource.dbuser once that job exists.
        with dbuser("charm-build-job"):
            self.assertRaisesWithContent(
                UnauthorizedUploadResponse,
                "Missing required permission: package-manage-revisions",
                self.client.push,
                build,
                1,
            )

    @responses.activate
    def test_checkStatus_pending(self):
        self._setUpSecretStorage()
        build = self.makeUploadableCharmRecipeBuild()
        status_url = "/v1/charm/test-charm/revisions/review?upload-id=123"
        responses.add(
            "GET",
            "http://charmhub.example" + status_url,
            json={
                "revisions": [
                    {
                        "upload-id": "123",
                        "status": "new",
                        "revision": None,
                        "errors": None,
                    },
                ],
            },
        )
        self.assertRaises(
            UploadNotReviewedYetResponse,
            self.client.checkStatus,
            build,
            status_url,
        )

    @responses.activate
    def test_checkStatus_error(self):
        self._setUpSecretStorage()
        build = self.makeUploadableCharmRecipeBuild()
        status_url = "/v1/charm/test-charm/revisions/review?upload-id=123"
        responses.add(
            "GET",
            "http://charmhub.example" + status_url,
            json={
                "revisions": [
                    {
                        "upload-id": "123",
                        "status": "rejected",
                        "revision": None,
                        "errors": [
                            {"code": None, "message": "This charm is broken."},
                        ],
                    },
                ],
            },
        )
        self.assertRaisesWithContent(
            ReviewFailedResponse,
            "This charm is broken.",
            self.client.checkStatus,
            build,
            status_url,
        )

    @responses.activate
    def test_checkStatus_approved_no_revision(self):
        self._setUpSecretStorage()
        build = self.makeUploadableCharmRecipeBuild()
        status_url = "/v1/charm/test-charm/revisions/review?upload-id=123"
        responses.add(
            "GET",
            "http://charmhub.example" + status_url,
            json={
                "revisions": [
                    {
                        "upload-id": "123",
                        "status": "approved",
                        "revision": None,
                        "errors": None,
                    },
                ],
            },
        )
        self.assertRaisesWithContent(
            ReviewFailedResponse,
            "Review passed but did not assign a revision.",
            self.client.checkStatus,
            build,
            status_url,
        )

    @responses.activate
    def test_checkStatus_approved(self):
        self._setUpSecretStorage()
        build = self.makeUploadableCharmRecipeBuild()
        status_url = "/v1/charm/test-charm/revisions/review?upload-id=123"
        responses.add(
            "GET",
            "http://charmhub.example" + status_url,
            json={
                "revisions": [
                    {
                        "upload-id": "123",
                        "status": "approved",
                        "revision": 1,
                        "errors": None,
                    },
                ],
            },
        )
        self.assertEqual(1, self.client.checkStatus(build, status_url))
        requests = [call.request for call in responses.calls]
        request_matcher = RequestMatches(
            url=Equals("http://charmhub.example" + status_url),
            method=Equals("GET"),
            auth=(
                "Macaroon",
                MacaroonVerifies(self.exchanged_key),
            ),
        )
        self.assertThat(requests, MatchesListwise([request_matcher]))

    @responses.activate
    def test_checkStatus_404(self):
        self._setUpSecretStorage()
        build = self.makeUploadableCharmRecipeBuild()
        status_url = "/v1/charm/test-charm/revisions/review?upload-id=123"
        responses.add(
            "GET", "http://charmhub.example" + status_url, status=404
        )
        self.assertRaisesWithContent(
            BadReviewStatusResponse,
            "404 Client Error: Not Found",
            self.client.checkStatus,
            build,
            status_url,
        )

    @responses.activate
    def test_release(self):
        self._setUpSecretStorage()
        recipe = self.factory.makeCharmRecipe(
            store_upload=True,
            store_name="test-charm",
            store_secrets=self._makeStoreSecrets(),
            store_channels=["stable", "edge"],
        )
        build = self.factory.makeCharmRecipeBuild(recipe=recipe)
        self._addCharmReleaseResponse("test-charm")
        self.client.release(build, 1)
        self.assertThat(
            responses.calls[-1].request,
            RequestMatches(
                url=Equals(
                    "http://charmhub.example/v1/charm/test-charm/releases"
                ),
                method=Equals("POST"),
                headers=ContainsDict(
                    {"Content-Type": Equals("application/json")}
                ),
                auth=("Macaroon", MacaroonVerifies(self.exchanged_key)),
                json_data=[
                    {"channel": "stable", "revision": 1},
                    {"channel": "edge", "revision": 1},
                ],
            ),
        )

    @responses.activate
    def test_release_error(self):
        self._setUpSecretStorage()
        recipe = self.factory.makeCharmRecipe(
            store_upload=True,
            store_name="test-charm",
            store_secrets=self._makeStoreSecrets(),
            store_channels=["stable", "edge"],
        )
        build = self.factory.makeCharmRecipeBuild(recipe=recipe)
        responses.add(
            "POST",
            "http://charmhub.example/v1/charm/test-charm/releases",
            status=503,
            json={"error-list": [{"message": "Failed to publish"}]},
        )
        self.assertRaisesWithContent(
            ReleaseFailedResponse,
            "Failed to publish",
            self.client.release,
            build,
            1,
        )

    @responses.activate
    def test_release_404(self):
        self._setUpSecretStorage()
        recipe = self.factory.makeCharmRecipe(
            store_upload=True,
            store_name="test-charm",
            store_secrets=self._makeStoreSecrets(),
            store_channels=["stable", "edge"],
        )
        build = self.factory.makeCharmRecipeBuild(recipe=recipe)
        responses.add(
            "POST",
            "http://charmhub.example/v1/charm/test-charm/releases",
            status=404,
        )
        self.assertRaisesWithContent(
            ReleaseFailedResponse,
            "404 Client Error: Not Found",
            self.client.release,
            build,
            1,
        )
