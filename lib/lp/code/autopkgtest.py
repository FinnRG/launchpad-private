# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""API-independent autopkgtest messages and transport boundary."""

__all__ = [
    "AutopkgtestAdapterUnavailable",
    "AutopkgtestCancellation",
    "AutopkgtestLookup",
    "AutopkgtestLookupStatus",
    "AutopkgtestReadiness",
    "AutopkgtestReceipt",
    "AutopkgtestRequest",
    "AutopkgtestResult",
    "AutopkgtestStatus",
    "AutopkgtestWatchHandler",
    "AutopkgtestWatchReference",
    "UnconfiguredAutopkgtestAdapter",
    "UbuntuAutopkgtestAdapter",
    "buildUbuntuAutopkgtestParameters",
    "makeUbuntuAutopkgtestAdapter",
]

import ast
import gzip
import hashlib
import io
import json
import re
import tarfile
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from enum import Enum
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import quote, urlparse

import requests
from debian.deb822 import Deb822
from zope.interface import implementer

from lp.code.enums import RevisionStatusResult
from lp.code.interfaces.autopkgtest import IAutopkgtestAdapter
from lp.services.config import config


class AutopkgtestAdapterUnavailable(Exception):
    """No usable service transport is configured."""


def makeUbuntuAutopkgtestAdapter():
    return UbuntuAutopkgtestAdapter.fromConfig()


def buildUbuntuAutopkgtestParameters(request):
    """Translate PPA requests to the autopkgtest request.cgi API."""
    archive = urlparse(request.archive_reference)
    base = urlparse(config.personalpackagearchive.base_url)
    match = re.fullmatch(
        re.escape(base.path.rstrip("/"))
        + r"/([A-Za-z0-9+_.-]+)/([A-Za-z0-9+_.-]+)/ubuntu/?",
        archive.path,
    )
    if (
        request.distribution != "ubuntu"
        or request.pocket != "release"
        or archive.scheme not in ("http", "https")
        or not archive.netloc
        or archive.netloc != base.netloc
        or archive.username is not None
        or archive.password is not None
        or archive.query
        or archive.fragment
        or match is None
    ):
        raise ValueError("Autopkgtest requires a public Ubuntu release PPA")
    if set(request.parameters) - {
        "autopkgtest-trigger",
        "autopkgtest-all-proposed",
    }:
        raise ValueError("Unsupported saved autopkgtest options")
    saved = request.parameters.get("autopkgtest-all-proposed", "false")
    if saved not in ("true", "false"):
        raise ValueError("Invalid saved all-proposed setting")
    all_proposed = saved == "true"
    triggers = request.parameters.get("autopkgtest-trigger", "")
    if not isinstance(triggers, str):
        raise ValueError("Invalid saved autopkgtest triggers")
    triggers = triggers.split()
    own_trigger = "%s/%s" % (
        request.source_package_name,
        request.source_package_version,
    )
    for value in (
        request.series,
        request.architecture,
        request.source_package_name,
    ):
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", value):
            raise ValueError("Invalid Ubuntu request name")
    parameters = [
        ("release", request.series),
        ("arch", request.architecture),
        ("package", request.source_package_name),
        ("ppa", "%s/%s" % match.groups()),
        ("trigger", own_trigger),
    ]
    for trigger in [own_trigger] + triggers:
        if not isinstance(trigger, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9+.-]*/[^\s/]+", trigger
        ):
            raise ValueError("Invalid autopkgtest trigger")
        if trigger != own_trigger and ("trigger", trigger) not in parameters:
            parameters.append(("trigger", trigger))
    if all_proposed:
        parameters.append(("all-proposed", "1"))
    return parameters


class _UbuntuRequestConfirmation(HTMLParser):
    """Read the acceptance message and echoed fields in submission HTML."""

    def __init__(self):
        super().__init__()
        self.fields = {}
        self.tag = None
        self.text = []
        self.key = None
        self.accepted = False

    def handle_starttag(self, tag, attributes):
        if tag in ("dt", "dd"):
            self.tag = tag
            self.text = []

    def handle_data(self, data):
        if data.strip() == "Test request submitted.":
            self.accepted = True
        if self.tag is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag != self.tag:
            return
        text = "".join(self.text).strip()
        if tag == "dt":
            self.key = text
        elif self.key is not None:
            if self.key in self.fields:
                raise ValueError("Duplicate submission confirmation field")
            self.fields[self.key] = text
        self.tag = None


@implementer(IAutopkgtestAdapter)
class UbuntuAutopkgtestAdapter:
    """Bounded request.cgi transport and anonymous public PPA result reads."""

    max_response_bytes = 2 * 1024 * 1024
    max_index_bytes = 16 * 1024 * 1024
    listing_limit = 100
    max_result_archives = 10

    def __init__(
        self,
        api_key=None,
        service_url=None,
        results_url=None,
        timeout=(10, 30),
        submit_session=None,
        read_session=None,
        http_proxy=None,
        ca_certificates_path=None,
    ):
        if service_url is None:
            service_url = config.autopkgtest.service_url
        if results_url is None:
            results_url = config.autopkgtest.results_url
        for url in (service_url, results_url):
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Autopkgtest endpoints must be HTTPS")
        self.api_key = api_key
        self.service_url = service_url.rstrip("/")
        self.results_url = results_url.rstrip("/")
        self.timeout = timeout
        self.submit_session = submit_session or requests.Session()
        self.read_session = read_session or requests.Session()
        if self.read_session is self.submit_session:
            raise ValueError(
                "Submission and public reads need separate sessions"
            )
        self.submit_session.trust_env = False
        self.read_session.trust_env = False
        for session in (self.submit_session, self.read_session):
            if http_proxy:
                session.proxies = {"http": http_proxy, "https": http_proxy}
            if ca_certificates_path:
                session.verify = ca_certificates_path
        self.read_session.auth = None
        self.read_session.cookies.clear()
        for name in ("Authorization", "Cookie", "Proxy-Authorization"):
            self.read_session.headers.pop(name, None)

    @classmethod
    def fromConfig(cls):
        settings = config.autopkgtest
        return cls(
            api_key=settings.api_key,
            service_url=settings.service_url,
            results_url=settings.results_url,
            timeout=(settings.connect_timeout, settings.read_timeout),
            http_proxy=config.launchpad.http_proxy,
            ca_certificates_path=config.launchpad.ca_certificates_path,
        )

    def _get(self, session, url, params=None, cookies=None):
        response = None
        try:
            response = session.get(
                url,
                params=params,
                cookies=cookies,
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            )
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise AutopkgtestAdapterUnavailable(
                    "Autopkgtest HTTP status %s" % response.status_code
                )
            chunks = []
            size = 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise AutopkgtestAdapterUnavailable(
                        "Autopkgtest response exceeds size limit"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
        except requests.RequestException:
            raise AutopkgtestAdapterUnavailable(
                "Autopkgtest transport failed; acceptance may be unknown"
            ) from None
        finally:
            if response is not None:
                response.close()

    def checkReadiness(self, request):
        self._checkSubmissionConfiguration()
        buildUbuntuAutopkgtestParameters(request)
        archive = urlparse(request.archive_reference)
        root = "%s/dists/%s/" % (
            archive._replace(scheme="https").geturl().rstrip("/"),
            request.series,
        )
        source = self._get(self.read_session, root + "main/source/Sources.gz")
        binaries = self._get(
            self.read_session,
            root + "main/binary-%s/Packages.gz" % request.architecture,
        )
        if source is None or binaries is None:
            return AutopkgtestReadiness(False, reason="PPA indexes not ready")

        def records(data):
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                payload = stream.read(self.max_index_bytes + 1)
            if len(payload) > self.max_index_bytes:
                raise AutopkgtestAdapterUnavailable(
                    "PPA index exceeds size limit"
                )
            return list(Deb822.iter_paragraphs(io.BytesIO(payload)))

        source_ready = False
        expected = set(request.expected_binary_packages)
        for record in records(source):
            if (
                record.get("Package") == request.source_package_name
                and record.get("Version") == request.source_package_version
            ):
                source_ready = True
                if not expected:
                    expected.update(
                        name.strip()
                        for name in record.get("Binary", "").split(",")
                        if name.strip()
                    )
        published = set()
        for record in records(binaries):
            source_field = record.get("Source", record.get("Package", ""))
            match = re.fullmatch(r"([^\s]+)(?: \(([^)]+)\))?", source_field)
            if match is not None and (
                match.group(1) == request.source_package_name
                and (match.group(2) or record.get("Version"))
                == request.source_package_version
            ):
                published.add(record.get("Package"))
        ready = source_ready and bool(expected) and expected <= published
        return AutopkgtestReadiness(
            ready, reason=None if ready else "Not all binaries are published"
        )

    def reconcile(self, request, service_metadata=None):
        receipt = self._buildReceipt(request)
        receipt.service_metadata.update(deepcopy(service_metadata or {}))
        result = self.poll(request, receipt)
        if result.service_metadata.get("matched_results"):
            receipt.service_metadata.update(result.service_metadata)
            return AutopkgtestLookup(AutopkgtestLookupStatus.ACCEPTED, receipt)
        return AutopkgtestLookup(
            AutopkgtestLookupStatus.UNKNOWN,
            service_metadata=result.service_metadata,
        )

    def _buildReceipt(self, request):
        return AutopkgtestReceipt(
            "build:" + request.fingerprint,
            request.key,
            request.fingerprint,
            service_metadata={
                "correlation": "build",
                "request_parameters": buildUbuntuAutopkgtestParameters(
                    request
                ),
            },
        )

    def _checkSubmissionConfiguration(self):
        if not isinstance(self.api_key, str) or not re.fullmatch(
            r"[A-Za-z0-9_.+-]+:[^\s;,:]+", self.api_key
        ):
            raise AutopkgtestAdapterUnavailable("API cookie is not configured")

    def submit(self, request):
        self._checkSubmissionConfiguration()
        parameters = buildUbuntuAutopkgtestParameters(request)
        body = self._get(
            self.submit_session,
            self.service_url + "/request.cgi",
            params=parameters,
            cookies={"X-Api-Key": self.api_key},
        )
        if body is None:
            raise AutopkgtestAdapterUnavailable(
                "Submission endpoint not found"
            )
        try:
            confirmation = _UbuntuRequestConfirmation()
            confirmation.feed(body.decode("utf-8"))
            if not confirmation.accepted:
                raise ValueError("Submission was not confirmed")
            fields = confirmation.fields
            for key, value in parameters:
                if key in ("ppa", "trigger"):
                    continue
                if fields.get(key) != value:
                    raise ValueError("Unverified submission response")
            for wire_key, echo_key in (
                ("ppa", "ppas"),
                ("trigger", "triggers"),
            ):
                expected = [
                    value for key, value in parameters if key == wire_key
                ]
                if ast.literal_eval(fields.get(echo_key, "None")) != expected:
                    raise ValueError("Submission identity mismatch")
        except (ValueError, SyntaxError):
            raise AutopkgtestAdapterUnavailable(
                "Submission confirmation does not match the PPA request"
            ) from None
        receipt = self._buildReceipt(request)
        receipt.validate(request)
        return receipt

    def _result(self, request, receipt, data, url):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            members = {}
            for member in archive.getmembers():
                if member.name in (
                    "testinfo.json",
                    "exitcode",
                    "testpkg-version",
                ):
                    if (
                        member.name in members
                        or not member.isfile()
                        or member.size > 65536
                    ):
                        raise ValueError("Invalid result archive member")
                    members[member.name] = archive.extractfile(member).read()
        info = json.loads(members["testinfo.json"])
        build_match = receipt.request_id == "build:" + request.fingerprint
        if not build_match and info.get("uuid") != receipt.request_id:
            return None
        parameters = buildUbuntuAutopkgtestParameters(request)
        expected = {value for key, value in parameters if key == "trigger"}
        environment = info.get("custom_environment", [])
        actual = set()
        for value in environment:
            if value.startswith("ADT_TEST_TRIGGERS="):
                actual.update(value.split("=", 1)[1].split())
        if actual != expected:
            if build_match:
                return None
            raise ValueError("Result trigger identity mismatch")
        version = members["testpkg-version"].decode("utf-8").strip().split()
        code = int(members["exitcode"].strip())
        if version != [
            request.source_package_name,
            request.source_package_version,
        ]:
            if build_match:
                return None
            raise ValueError("Result package identity mismatch")
        all_proposed = ("all-proposed", "1") in parameters
        if (str(info.get("all-proposed")) == "1") != all_proposed:
            if build_match and info.get("all-proposed") is not None:
                return None
            raise ValueError("Result options identity mismatch")
        if code in (0, 2):
            status = AutopkgtestStatus.PASSED
        elif code in (4, 6, 8, 12, 14, 20):
            status = AutopkgtestStatus.FAILED
        else:
            status = AutopkgtestStatus.INFRASTRUCTURE_ERROR
        return AutopkgtestResult(
            receipt.request_id,
            request.key,
            request.fingerprint,
            status,
            url=url,
            summary=(
                "Autopkgtest error: package has no tests"
                if code == 8
                else "autopkgtest exit code %s" % code
            ),
            service_metadata={"exitcode": code},
        )

    def poll(self, request, receipt):
        receipt.validate(request)
        parameters = buildUbuntuAutopkgtestParameters(request)
        owner, ppa = next(
            value for key, value in parameters if key == "ppa"
        ).split("/")
        package = request.source_package_name
        prefix = "%s/%s/%s/%s/" % (
            request.series,
            request.architecture,
            package[:4] if package.startswith("lib") else package[0],
            package,
        )
        container = "autopkgtest-%s-%s-%s" % (request.series, owner, ppa)
        url = self.results_url + "/" + quote(container, safe="")
        marker = receipt.service_metadata.get("listing_marker", "")
        if not marker and request.date_requested is not None:
            marker = prefix + request.date_requested
        if marker and (
            not isinstance(marker, str) or not marker.startswith(prefix)
        ):
            raise ValueError("Invalid saved listing marker")
        raw = self._get(
            self.read_session,
            url + "/",
            params={
                "format": "json",
                "prefix": prefix,
                "marker": marker,
                "delimiter": "/",
                "limit": self.listing_limit,
            },
        )
        entries = [] if raw is None else json.loads(raw)
        if not isinstance(entries, list) or len(entries) > self.listing_limit:
            raise ValueError("Invalid public result listing")
        checked = 0
        complete = len(entries) < self.listing_limit
        matched = deepcopy(receipt.service_metadata.get("matched_results", {}))
        if not isinstance(matched, dict):
            raise ValueError("Invalid saved matching results")
        for entry in entries:
            name = entry["subdir"]
            if (
                not isinstance(name, str)
                or not name.startswith(prefix)
                or ".." in name.split("/")
                or name <= marker
                or not re.fullmatch(re.escape(prefix) + r"[^/]+/", name)
            ):
                raise ValueError("Invalid result object path")
            if checked == self.max_result_archives:
                complete = False
                break
            result_url = url + "/" + quote(name.rstrip("/"), safe="/")
            data = self._get(self.read_session, result_url + "/result.tar")
            if data is None:
                complete = False
                break
            checked += 1
            result = self._result(request, receipt, data, result_url)
            if result is not None:
                matched[result.status.value] = {
                    "url": result.url,
                    "summary": result.summary,
                    "exitcode": result.service_metadata["exitcode"],
                }
            marker = name
            if (
                result is not None
                and result.status == AutopkgtestStatus.PASSED
            ):
                break
        metadata = {"listing_marker": marker, "matched_results": matched}
        status = None
        if AutopkgtestStatus.PASSED.value in matched:
            status = AutopkgtestStatus.PASSED
        elif complete:
            for candidate in (
                AutopkgtestStatus.FAILED,
                AutopkgtestStatus.INFRASTRUCTURE_ERROR,
            ):
                if candidate.value in matched:
                    status = candidate
                    break
        if status is not None:
            details = matched[status.value]
            return AutopkgtestResult(
                receipt.request_id,
                request.key,
                request.fingerprint,
                status,
                url=details["url"],
                summary=details["summary"],
                service_metadata=metadata,
            )
        return AutopkgtestResult(
            receipt.request_id,
            request.key,
            request.fingerprint,
            AutopkgtestStatus.PENDING,
            service_metadata=metadata,
        )

    def cancel(self, request, receipt):
        receipt.validate(request)
        return AutopkgtestCancellation(
            False,
            supported=False,
            reason="No public cancellation API verified",
        )


class AutopkgtestLookupStatus(str, Enum):
    ABSENT = "absent"
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"


class AutopkgtestStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    INFRASTRUCTURE_ERROR = "infrastructure-error"


def _validatePresentation(url, summary=None):
    if url is not None:
        if not isinstance(url, str):
            raise ValueError("Result URL must be a string")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Result URL must be HTTP or HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Result URLs must not contain credentials")
    if summary is not None and not isinstance(summary, str):
        raise ValueError("Result summary must be a string")


@dataclass(frozen=True)
class AutopkgtestRequest:
    key: str
    validation_id: int
    proposal_id: int
    archive_id: int
    archive_reference: str
    distribution: str
    series: str
    architecture: str
    pocket: str
    source_package_name: str
    source_package_version: str
    commit_sha1: str
    source_package_git_build_id: int
    parameters: dict = field(default_factory=dict)
    schema_version: int = 1
    expected_binary_packages: list = field(default_factory=list)
    date_requested: Optional[str] = None

    def __post_init__(self):
        if self.schema_version != 1:
            raise ValueError("Unsupported autopkgtest request version")
        for name in (
            "key",
            "archive_reference",
            "distribution",
            "series",
            "architecture",
            "pocket",
            "source_package_name",
            "source_package_version",
            "commit_sha1",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(
                self, name
            ):
                raise ValueError("Missing request field: %s" % name)
        for name in (
            "validation_id",
            "proposal_id",
            "archive_id",
            "source_package_git_build_id",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError("Invalid request identifier: %s" % name)
        if not isinstance(self.parameters, dict):
            raise ValueError("Request parameters must be a JSON object")
        if not isinstance(self.expected_binary_packages, list) or any(
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name)
            for name in self.expected_binary_packages
        ):
            raise ValueError("Invalid expected binary packages")
        if self.date_requested is not None and not re.fullmatch(
            r"[0-9]{8}_[0-9]{6}", self.date_requested
        ):
            raise ValueError("Invalid request date")
        if len(self.commit_sha1) != 40 or any(
            character not in "0123456789abcdefABCDEF"
            for character in self.commit_sha1
        ):
            raise ValueError("Invalid built commit SHA-1")
        json.dumps(self.to_dict(), allow_nan=False)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**deepcopy(value))

    @property
    def fingerprint(self):
        payload = json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AutopkgtestReadiness:
    ready: bool
    eligible: bool = True
    reason: Optional[str] = None

    def __post_init__(self):
        if type(self.ready) is not bool or type(self.eligible) is not bool:
            raise ValueError("Readiness and eligibility must be booleans")
        _validatePresentation(None, self.reason)


@dataclass(frozen=True)
class AutopkgtestCancellation:
    acknowledged: bool
    supported: bool = True
    reason: Optional[str] = None

    def __post_init__(self):
        if (
            type(self.acknowledged) is not bool
            or type(self.supported) is not bool
            or (self.acknowledged and not self.supported)
        ):
            raise ValueError("Invalid cancellation acknowledgement")
        _validatePresentation(None, self.reason)


@dataclass(frozen=True)
class AutopkgtestReceipt:
    request_id: str
    request_key: str
    request_fingerprint: str
    url: Optional[str] = None
    service_metadata: dict = field(default_factory=dict)

    def validate(self, request):
        if (
            not isinstance(self.request_id, str)
            or not self.request_id
            or self.request_key != request.key
            or self.request_fingerprint != request.fingerprint
        ):
            raise ValueError("Autopkgtest receipt identity mismatch")
        _validatePresentation(self.url)
        if not isinstance(self.service_metadata, dict):
            raise ValueError("Receipt metadata must be a JSON object")
        json.dumps(self.to_dict(), allow_nan=False)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**deepcopy(value))


@dataclass(frozen=True)
class AutopkgtestLookup:
    status: AutopkgtestLookupStatus
    receipt: Optional[AutopkgtestReceipt] = None
    service_metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.status, AutopkgtestLookupStatus):
            raise ValueError("Invalid submission lookup status")
        if (self.status == AutopkgtestLookupStatus.ACCEPTED) != (
            self.receipt is not None
        ):
            raise ValueError("Accepted lookups require a receipt")
        if not isinstance(self.service_metadata, dict):
            raise ValueError("Lookup metadata must be a JSON object")
        json.dumps(self.service_metadata, allow_nan=False)


@dataclass(frozen=True)
class AutopkgtestResult:
    request_id: str
    request_key: str
    request_fingerprint: str
    status: AutopkgtestStatus
    url: Optional[str] = None
    summary: Optional[str] = None
    service_metadata: dict = field(default_factory=dict)

    def validate(self, request, receipt):
        receipt.validate(request)
        if (
            self.request_id != receipt.request_id
            or self.request_key != request.key
            or self.request_fingerprint != request.fingerprint
            or not isinstance(self.status, AutopkgtestStatus)
        ):
            raise ValueError("Autopkgtest result identity mismatch")
        _validatePresentation(self.url, self.summary)
        if not isinstance(self.service_metadata, dict):
            raise ValueError("Result metadata must be a JSON object")
        json.dumps(self.service_metadata, allow_nan=False)


@dataclass(frozen=True)
class AutopkgtestWatchReference:
    job_id: int
    request: AutopkgtestRequest
    receipt: AutopkgtestReceipt
    schema_version: int = 1

    def __post_init__(self):
        if self.schema_version != 1:
            raise ValueError("Unsupported autopkgtest watch version")
        if type(self.job_id) is not int or self.job_id <= 0:
            raise ValueError("Invalid watch job identifier")
        if not isinstance(self.request, AutopkgtestRequest) or not isinstance(
            self.receipt, AutopkgtestReceipt
        ):
            raise ValueError("Watch must contain a request and receipt")
        self.receipt.validate(self.request)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(
            job_id=value["job_id"],
            request=AutopkgtestRequest.from_dict(value["request"]),
            receipt=AutopkgtestReceipt.from_dict(value["receipt"]),
            schema_version=value.get("schema_version"),
        )


class AutopkgtestWatchHandler:
    """Translate verified service results into collector responses."""

    def __init__(self, adapter):
        self.adapter = adapter

    def isActive(self, remote_ref, report_id):
        from lp.code.model.branchmergeproposaljob import (
            AutopkgtestRequestNotReady,
            BranchMergeProposalJob,
            BranchMergeProposalJobType,
            RequestAutopkgtestJob,
        )
        from lp.services.database.interfaces import IPrimaryStore

        reference = AutopkgtestWatchReference.from_dict(remote_ref)
        base_job = IPrimaryStore(BranchMergeProposalJob).get(
            BranchMergeProposalJob, reference.job_id
        )
        if base_job is None:
            return False
        if base_job.job_type != BranchMergeProposalJobType.REQUEST_AUTOPKGTEST:
            raise ValueError("Watch points to a different job type")
        job = RequestAutopkgtestJob(base_job)
        state = job.metadata.get("autopkgtest")
        if (
            state is None
            or state["report_id"] != report_id
            or state["request"] != remote_ref["request"]
            or state.get("receipt") is None
            or state["receipt"]["request_id"]
            != remote_ref["receipt"]["request_id"]
        ):
            raise ValueError("Watch does not belong to the saved request")
        if state["phase"] == "cancelled":
            return False
        try:
            return job.getAutopkgtestContext() is not None
        except AutopkgtestRequestNotReady:
            return False

    def poll(self, remote_ref):
        from lp.code.scripts.revisionstatuswatch import (
            RevisionStatusWatchPollResult,
        )

        watch_reference = AutopkgtestWatchReference.from_dict(remote_ref)
        request = watch_reference.request
        receipt = watch_reference.receipt
        response = self.adapter.poll(
            AutopkgtestRequest.from_dict(request.to_dict()),
            AutopkgtestReceipt.from_dict(receipt.to_dict()),
        )
        if not isinstance(response, AutopkgtestResult):
            raise ValueError("Adapter must return an autopkgtest result")
        response.validate(request, receipt)
        if response.status == AutopkgtestStatus.INFRASTRUCTURE_ERROR:
            raise RuntimeError("Autopkgtest infrastructure failure")
        statuses = {
            AutopkgtestStatus.PENDING: RevisionStatusResult.WAITING,
            AutopkgtestStatus.RUNNING: RevisionStatusResult.RUNNING,
            AutopkgtestStatus.PASSED: RevisionStatusResult.SUCCEEDED,
            AutopkgtestStatus.FAILED: RevisionStatusResult.FAILED,
            AutopkgtestStatus.SKIPPED: RevisionStatusResult.FAILED,
            AutopkgtestStatus.CANCELLED: RevisionStatusResult.CANCELLED,
        }
        reference = None
        receipt.service_metadata.update(response.service_metadata)
        if response.service_metadata:
            reference = AutopkgtestWatchReference(
                watch_reference.job_id, request, receipt
            ).to_dict()
        return RevisionStatusWatchPollResult(
            result=statuses[response.status],
            url=response.url,
            result_summary=response.summary,
            remote_ref=reference,
            properties={
                "autopkgtest": {
                    "request_id": receipt.request_id,
                    "request_key": request.key,
                    "request_fingerprint": request.fingerprint,
                    "status": response.status.value,
                    "service_metadata": deepcopy(receipt.service_metadata),
                },
            },
        )


@implementer(IAutopkgtestAdapter)
class UnconfiguredAutopkgtestAdapter:
    """No HTTP routes, authentication, or payload formats are assumed."""

    def checkReadiness(self, request):
        raise AutopkgtestAdapterUnavailable("Readiness API is not configured")

    def reconcile(self, request, service_metadata=None):
        raise AutopkgtestAdapterUnavailable("Lookup API is not configured")

    def submit(self, request):
        raise AutopkgtestAdapterUnavailable("Submission API is not configured")

    def poll(self, request, receipt):
        raise AutopkgtestAdapterUnavailable("Results API is not configured")

    def cancel(self, request, receipt):
        raise AutopkgtestAdapterUnavailable(
            "Cancellation API is not configured"
        )
