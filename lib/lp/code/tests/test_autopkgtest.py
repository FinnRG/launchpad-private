# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests and service doubles for the normalized autopkgtest contract."""

import json
from dataclasses import replace
from unittest import mock

from zope.interface.verify import verifyObject

from lp.code.autopkgtest import (
    AutopkgtestAdapterUnavailable,
    AutopkgtestCancellation,
    AutopkgtestLookup,
    AutopkgtestLookupStatus,
    AutopkgtestReadiness,
    AutopkgtestReceipt,
    AutopkgtestRequest,
    AutopkgtestResult,
    AutopkgtestStatus,
    AutopkgtestWatchHandler,
    AutopkgtestWatchReference,
    UnconfiguredAutopkgtestAdapter,
)
from lp.code.enums import RevisionStatusResult
from lp.code.interfaces.autopkgtest import IAutopkgtestAdapter
from lp.services.database.isolation import check_no_transaction
from lp.testing import TestCase
from lp.testing.layers import BaseLayer


def makeRequest():
    return AutopkgtestRequest(
        key="request-key",
        validation_id=1,
        proposal_id=2,
        archive_id=3,
        archive_reference="https://example.test/ppa/",
        distribution="ubuntu",
        series="test-series",
        architecture="amd64",
        pocket="release",
        source_package_name="test-package",
        source_package_version="1.0~ppa1",
        commit_sha1="a" * 40,
        source_package_git_build_id=4,
        parameters={"triggers": ["dependency/2.0"]},
    )


class FakeAutopkgtestAdapter:
    """Assert transaction boundaries without implementing an HTTP service."""

    def __init__(self):
        self.calls = []
        self.readiness = AutopkgtestReadiness(True)
        self.lookup = AutopkgtestLookup(AutopkgtestLookupStatus.UNKNOWN)
        self.reconciliation_metadata = []
        self.submission_error = None
        self.receipt = None
        self.receipt_override = None
        self.result_override = None
        self.result_status = AutopkgtestStatus.PASSED
        self.result_metadata = {}
        self.on_readiness = None
        self.on_submit = None
        self.on_poll = None

    def checkReadiness(self, request):
        check_no_transaction()
        self.calls.append(("readiness", request.key))
        if self.on_readiness is not None:
            self.on_readiness(request)
        return self.readiness

    def reconcile(self, request, service_metadata=None):
        check_no_transaction()
        self.calls.append(("reconcile", request.key))
        self.reconciliation_metadata.append(service_metadata)
        return self.lookup

    def submit(self, request):
        check_no_transaction()
        self.calls.append(("submit", request.key))
        self.receipt = AutopkgtestReceipt(
            request_id="remote-" + request.key,
            request_key=request.key,
            request_fingerprint=request.fingerprint,
            url="https://example.test/result/" + request.key,
            service_metadata={"cursor": "initial"},
        )
        if self.on_submit is not None:
            self.on_submit(request)
        if self.submission_error is not None:
            raise self.submission_error
        return self.receipt_override or self.receipt

    def poll(self, request, receipt):
        check_no_transaction()
        self.calls.append(("poll", request.key))
        if self.on_poll is not None:
            self.on_poll(request)
        return self.result_override or AutopkgtestResult(
            request_id=receipt.request_id,
            request_key=request.key,
            request_fingerprint=request.fingerprint,
            status=self.result_status,
            url=receipt.url,
            summary="External test result",
            service_metadata=self.result_metadata,
        )

    def cancel(self, request, receipt):
        check_no_transaction()
        self.calls.append(("cancel", request.key))
        return AutopkgtestCancellation(True)


class TestAutopkgtestContract(TestCase):
    layer = BaseLayer

    def makeReceipt(self, request):
        return AutopkgtestReceipt(
            "remote-id",
            request.key,
            request.fingerprint,
            service_metadata={"cursor": "initial"},
        )

    def test_request_round_trip_preserves_identity(self):
        request = makeRequest()
        restored = AutopkgtestRequest.from_dict(
            json.loads(json.dumps(request.to_dict()))
        )
        self.assertEqual(request, restored)
        self.assertEqual(request.fingerprint, restored.fingerprint)

    def test_watch_reference_round_trip_preserves_identity(self):
        request = makeRequest()
        reference = AutopkgtestWatchReference(
            5, request, self.makeReceipt(request)
        )
        self.assertEqual(
            reference, AutopkgtestWatchReference.from_dict(reference.to_dict())
        )

    def test_unknown_watch_version_is_rejected(self):
        request = makeRequest()
        self.assertRaises(
            ValueError,
            AutopkgtestWatchReference,
            5,
            request,
            self.makeReceipt(request),
            schema_version=2,
        )

    def test_changed_options_change_fingerprint(self):
        request = makeRequest()
        changed = replace(request, parameters={"all-proposed": True})
        self.assertNotEqual(request.fingerprint, changed.fingerprint)

    def test_nested_input_is_detached(self):
        payload = makeRequest().to_dict()
        request = AutopkgtestRequest.from_dict(payload)
        payload["parameters"]["triggers"].append("other/1.0")
        self.assertEqual(["dependency/2.0"], request.parameters["triggers"])

    def test_unknown_request_version_is_rejected(self):
        self.assertRaises(ValueError, replace, makeRequest(), schema_version=2)

    def test_invalid_commit_is_rejected(self):
        self.assertRaises(
            ValueError, replace, makeRequest(), commit_sha1="bad"
        )

    def test_non_json_parameters_are_rejected(self):
        self.assertRaises(
            TypeError, replace, makeRequest(), parameters={"value": object()}
        )

    def test_wrong_receipt_key_is_rejected(self):
        request = makeRequest()
        receipt = replace(self.makeReceipt(request), request_key="other")
        self.assertRaises(ValueError, receipt.validate, request)

    def test_wrong_receipt_fingerprint_is_rejected(self):
        request = makeRequest()
        receipt = replace(self.makeReceipt(request), request_fingerprint="bad")
        self.assertRaises(ValueError, receipt.validate, request)

    def test_unsafe_result_url_is_rejected(self):
        request = makeRequest()
        receipt = replace(self.makeReceipt(request), url="javascript:alert(1)")
        self.assertRaises(ValueError, receipt.validate, request)

    def test_lookup_requires_a_receipt_for_acceptance(self):
        self.assertRaises(
            ValueError, AutopkgtestLookup, AutopkgtestLookupStatus.ACCEPTED
        )

    def test_lookup_metadata_must_be_a_json_object(self):
        self.assertRaises(
            ValueError,
            AutopkgtestLookup,
            AutopkgtestLookupStatus.UNKNOWN,
            service_metadata=[],
        )
        self.assertRaises(
            TypeError,
            AutopkgtestLookup,
            AutopkgtestLookupStatus.UNKNOWN,
            service_metadata={"cursor": object()},
        )

    def test_readiness_requires_booleans(self):
        self.assertRaises(ValueError, AutopkgtestReadiness, "false")

    def test_cancellation_does_not_acknowledge_unsupported_operations(self):
        self.assertRaises(ValueError, AutopkgtestCancellation, True, False)

    def test_pseudo_adapter_implements_interface(self):
        self.assertTrue(
            verifyObject(IAutopkgtestAdapter, UnconfiguredAutopkgtestAdapter())
        )

    def test_pseudo_adapter_never_fakes_responses(self):
        adapter = UnconfiguredAutopkgtestAdapter()
        request = makeRequest()
        receipt = self.makeReceipt(request)
        for method, arguments in (
            (adapter.checkReadiness, (request,)),
            (adapter.reconcile, (request,)),
            (adapter.submit, (request,)),
            (adapter.poll, (request, receipt)),
            (adapter.cancel, (request, receipt)),
        ):
            self.assertRaises(
                AutopkgtestAdapterUnavailable, method, *arguments
            )

    def test_handler_maps_normalized_statuses(self):
        request = makeRequest()
        receipt = self.makeReceipt(request)
        reference = {
            "schema_version": 1,
            "job_id": 5,
            "request": request.to_dict(),
            "receipt": receipt.to_dict(),
        }
        adapter = mock.Mock()
        for status, expected in (
            (AutopkgtestStatus.PENDING, RevisionStatusResult.WAITING),
            (AutopkgtestStatus.RUNNING, RevisionStatusResult.RUNNING),
            (AutopkgtestStatus.PASSED, RevisionStatusResult.SUCCEEDED),
            (AutopkgtestStatus.FAILED, RevisionStatusResult.FAILED),
            (AutopkgtestStatus.SKIPPED, RevisionStatusResult.FAILED),
            (AutopkgtestStatus.CANCELLED, RevisionStatusResult.CANCELLED),
        ):
            adapter.poll.return_value = AutopkgtestResult(
                receipt.request_id, request.key, request.fingerprint, status
            )
            self.assertEqual(
                expected,
                AutopkgtestWatchHandler(adapter).poll(reference).result,
            )

    def test_cursor_updates_do_not_mutate_the_input_reference(self):
        request = makeRequest()
        receipt = self.makeReceipt(request)
        reference = {
            "schema_version": 1,
            "job_id": 5,
            "request": request.to_dict(),
            "receipt": receipt.to_dict(),
        }
        adapter = mock.Mock()
        adapter.poll.return_value = AutopkgtestResult(
            receipt.request_id,
            request.key,
            request.fingerprint,
            AutopkgtestStatus.PENDING,
            service_metadata={"cursor": "next"},
        )
        result = AutopkgtestWatchHandler(adapter).poll(reference)
        self.assertEqual(
            "initial", reference["receipt"]["service_metadata"]["cursor"]
        )
        self.assertEqual(
            "next", result.remote_ref["receipt"]["service_metadata"]["cursor"]
        )

    def test_result_requires_a_normalized_status(self):
        request = makeRequest()
        receipt = self.makeReceipt(request)
        result = AutopkgtestResult(
            receipt.request_id, request.key, request.fingerprint, "passed"
        )
        self.assertRaises(ValueError, result.validate, request, receipt)
