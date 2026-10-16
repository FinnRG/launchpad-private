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
]

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

from zope.interface import implementer

from lp.code.enums import RevisionStatusResult
from lp.code.interfaces.autopkgtest import IAutopkgtestAdapter


class AutopkgtestAdapterUnavailable(Exception):
    """No usable service transport is configured."""


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
