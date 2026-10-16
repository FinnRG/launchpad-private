# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests and service doubles for the normalized autopkgtest contract."""

import gzip
import io
import json
import tarfile
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
    UbuntuAutopkgtestAdapter,
    UnconfiguredAutopkgtestAdapter,
    buildUbuntuAutopkgtestParameters,
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
        parameters={"autopkgtest-trigger": "dependency/2.0"},
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


class TestUbuntuAutopkgtestAdapter(TestCase):
    layer = BaseLayer

    remote_id = "11111111-1111-4111-8111-111111111111"

    def setUp(self):
        super().setUp()
        self.pushConfig(
            "personalpackagearchive", base_url="https://ppa.example.test"
        )
        self.pushConfig(
            "autopkgtest",
            service_url="https://autopkgtest.example.test",
            results_url="https://results.example.test",
        )
        self.request = replace(
            makeRequest(),
            archive_reference="https://ppa.example.test/owner/mp/ubuntu",
        )
        self.receipt = AutopkgtestReceipt(
            self.remote_id, self.request.key, self.request.fingerprint
        )
        self.submit_session = mock.Mock()
        self.read_session = mock.Mock()
        self.adapter = UbuntuAutopkgtestAdapter(
            api_key="bot:fake-test-key",
            submit_session=self.submit_session,
            read_session=self.read_session,
        )

    def response(self, body, status=200):
        response = mock.Mock(status_code=status)
        response.iter_content.return_value = [body]
        return response

    def confirmation(self, remote_id=None):
        values = {
            "release": self.request.series,
            "arch": self.request.architecture,
            "package": self.request.source_package_name,
            "ppas": "['owner/mp']",
            "triggers": "['test-package/1.0~ppa1', 'dependency/2.0']",
        }
        if remote_id is not None:
            values["UUID"] = remote_id
        return (
            "<p>Test request submitted.</p><dl>"
            + "".join(
                "<dt>%s</dt><dd>%s</dd>" % pair for pair in values.items()
            )
            + "</dl>"
        ).encode("utf-8")

    def resultTar(
        self,
        remote_id=None,
        version="1.0~ppa1",
        code=0,
        include_uuid=True,
        all_proposed=None,
    ):
        info = {
            "uuid": self.remote_id if remote_id is None else remote_id,
            "custom_environment": [
                "ADT_TEST_TRIGGERS=test-package/1.0~ppa1 dependency/2.0"
            ],
            "all-proposed": all_proposed,
        }
        if not include_uuid:
            del info["uuid"]
        files = {
            "testinfo.json": json.dumps(info).encode("utf-8"),
            "exitcode": str(code).encode("ascii"),
            "testpkg-version": ("test-package %s" % version).encode("utf-8"),
        }
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for name, content in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        return output.getvalue()

    def queueResult(self, artifact):
        prefix = "test-series/amd64/t/test-package/20261012_120000_abcde@/"
        self.read_session.get.side_effect = [
            self.response(json.dumps([{"subdir": prefix}]).encode("utf-8")),
            self.response(artifact),
        ]
        return prefix

    def test_submit_uses_cookie_not_query_authentication(self):
        self.submit_session.get.return_value = self.response(
            self.confirmation(self.remote_id)
        )
        receipt = self.adapter.submit(self.request)
        receipt.validate(self.request)
        arguments = self.submit_session.get.call_args.kwargs
        self.assertEqual(
            {"X-Api-Key": "bot:fake-test-key"}, arguments["cookies"]
        )
        self.assertIn(("ppa", "owner/mp"), arguments["params"])
        self.assertFalse(arguments["allow_redirects"])
        self.assertNotIn("fake-test-key", json.dumps(receipt.to_dict()))
        self.read_session.get.assert_not_called()

    def test_configured_submission_and_results_hosts_are_used(self):
        self.submit_session.get.return_value = self.response(
            self.confirmation()
        )
        receipt = self.adapter.submit(self.request)
        self.assertEqual(
            "https://autopkgtest.example.test/request.cgi",
            self.submit_session.get.call_args.args[0],
        )
        self.read_session.get.return_value = self.response(b"[]")
        self.adapter.poll(self.request, receipt)
        self.assertEqual(
            "https://results.example.test/"
            "autopkgtest-test-series-owner-mp/",
            self.read_session.get.call_args.args[0],
        )

    def test_untrusted_ppa_host_is_rejected_before_network_io(self):
        for reference in (
            "https://untrusted.example.test/owner/mp/ubuntu",
            "https://ppa.example.test.evil.test/owner/mp/ubuntu",
            "https://user@ppa.example.test/owner/mp/ubuntu",
            "https://ppa.example.test:444/owner/mp/ubuntu",
        ):
            self.assertRaises(
                ValueError,
                self.adapter.checkReadiness,
                replace(self.request, archive_reference=reference),
            )
        self.read_session.get.assert_not_called()
        self.submit_session.get.assert_not_called()

    def test_missing_api_cookie_fails_before_readiness_network_io(self):
        self.adapter.api_key = None
        self.assertRaises(
            AutopkgtestAdapterUnavailable,
            self.adapter.checkReadiness,
            self.request,
        )
        self.read_session.get.assert_not_called()
        self.submit_session.get.assert_not_called()

    def test_public_session_authentication_is_cleared(self):
        self.assertIsNone(self.read_session.auth)
        self.read_session.cookies.clear.assert_called_once_with()
        self.read_session.headers.pop.assert_any_call("Authorization", None)
        self.read_session.headers.pop.assert_any_call("Cookie", None)

    def test_missing_uuid_uses_explicit_build_correlation(self):
        self.submit_session.get.return_value = self.response(
            self.confirmation()
        )
        receipt = self.adapter.submit(self.request)
        receipt.validate(self.request)
        self.assertEqual(
            "build:" + self.request.fingerprint, receipt.request_id
        )
        self.assertEqual("build", receipt.service_metadata["correlation"])
        self.read_session.get.return_value = self.response(b"[]")
        self.assertEqual(
            AutopkgtestLookupStatus.UNKNOWN,
            self.adapter.reconcile(self.request).status,
        )
        self.assertEqual(1, self.submit_session.get.call_count)

    def test_confirmation_without_acceptance_is_rejected(self):
        self.submit_session.get.return_value = self.response(
            self.confirmation().replace(
                b"Test request submitted.", b"Rejected"
            )
        )
        self.assertRaises(
            AutopkgtestAdapterUnavailable, self.adapter.submit, self.request
        )

    def test_redirect_is_not_followed(self):
        self.submit_session.get.return_value = self.response(b"", status=302)
        self.assertRaises(
            AutopkgtestAdapterUnavailable, self.adapter.submit, self.request
        )

    def test_public_poll_uses_exact_uuid_without_authentication(self):
        self.queueResult(self.resultTar())
        result = self.adapter.poll(self.request, self.receipt)
        result.validate(self.request, self.receipt)
        self.assertEqual(AutopkgtestStatus.PASSED, result.status)
        self.assertIn("autopkgtest-test-series-owner-mp", result.url)
        for call in self.read_session.get.call_args_list:
            self.assertIsNone(call.kwargs["cookies"])
            self.assertEqual((10, 30), call.kwargs["timeout"])
        self.submit_session.get.assert_not_called()

    def test_other_uuid_stays_pending_and_advances_cursor(self):
        prefix = self.queueResult(self.resultTar(remote_id="other-request"))
        result = self.adapter.poll(self.request, self.receipt)
        self.assertEqual(AutopkgtestStatus.PENDING, result.status)
        self.assertEqual(prefix, result.service_metadata["listing_marker"])

    def test_build_poll_accepts_exact_result_without_uuid(self):
        self.queueResult(self.resultTar(include_uuid=False))
        receipt = self.adapter._buildReceipt(self.request)
        result = self.adapter.poll(self.request, receipt)
        result.validate(self.request, receipt)
        self.assertEqual(AutopkgtestStatus.PASSED, result.status)
        self.submit_session.get.assert_not_called()

    def test_build_poll_ignores_other_package_version(self):
        self.queueResult(self.resultTar(version="2.0", include_uuid=False))
        result = self.adapter.poll(
            self.request, self.adapter._buildReceipt(self.request)
        )
        self.assertEqual(AutopkgtestStatus.PENDING, result.status)
        self.assertEqual({}, result.service_metadata["matched_results"])

    def test_build_poll_ignores_other_trigger_set(self):
        request = replace(self.request, parameters={})
        self.queueResult(self.resultTar(include_uuid=False))
        result = self.adapter.poll(
            request, self.adapter._buildReceipt(request)
        )
        self.assertEqual(AutopkgtestStatus.PENDING, result.status)

    def test_reconcile_recovers_matching_build_without_resubmission(self):
        self.queueResult(self.resultTar(include_uuid=False))
        lookup = self.adapter.reconcile(self.request)
        self.assertEqual(AutopkgtestLookupStatus.ACCEPTED, lookup.status)
        lookup.receipt.validate(self.request)
        self.submit_session.get.assert_not_called()

    def test_reconcile_resumes_after_nonmatching_result_page(self):
        self.adapter.max_result_archives = 1
        first = "test-series/amd64/t/test-package/20261012_120000_abcde@/"
        second = "test-series/amd64/t/test-package/20261012_120001_abcde@/"
        self.read_session.get.side_effect = [
            self.response(
                json.dumps([{"subdir": first}, {"subdir": second}]).encode()
            ),
            self.response(self.resultTar(version="2.0", include_uuid=False)),
            self.response(json.dumps([{"subdir": second}]).encode()),
            self.response(self.resultTar(include_uuid=False)),
        ]
        lookup = self.adapter.reconcile(self.request)
        self.assertEqual(AutopkgtestLookupStatus.UNKNOWN, lookup.status)
        self.assertEqual(first, lookup.service_metadata["listing_marker"])
        recovered = self.adapter.reconcile(
            self.request, service_metadata=lookup.service_metadata
        )
        self.assertEqual(AutopkgtestLookupStatus.ACCEPTED, recovered.status)
        recovered.receipt.validate(self.request)
        self.assertEqual(
            first,
            self.read_session.get.call_args_list[2].kwargs["params"]["marker"],
        )
        self.submit_session.get.assert_not_called()

    def test_any_matching_pass_wins_within_one_poll(self):
        prefix = "test-series/amd64/t/test-package/"
        for receipt in (
            self.receipt,
            self.adapter._buildReceipt(self.request),
        ):
            for codes in ((4, 16, 0), (0, 4, 16), (16, 16, 0)):
                with self.subTest(receipt=receipt.request_id, codes=codes):
                    entries = [
                        {"subdir": prefix + "20261012_12000%s_abcde@/" % index}
                        for index in range(len(codes))
                    ]
                    self.read_session.get.side_effect = [
                        self.response(json.dumps(entries).encode())
                    ] + [
                        self.response(self.resultTar(code=code))
                        for code in codes
                    ]
                    result = self.adapter.poll(self.request, receipt)
                    result.validate(self.request, receipt)
                    self.assertEqual(AutopkgtestStatus.PASSED, result.status)
                    self.assertEqual(
                        0,
                        result.service_metadata["matched_results"]["passed"][
                            "exitcode"
                        ],
                    )

    def test_build_scan_prioritizes_results_across_bounded_polls(self):
        self.adapter.max_result_archives = 1
        first = "test-series/amd64/t/test-package/20261012_120000_abcde@/"
        second = "test-series/amd64/t/test-package/20261012_120001_abcde@/"
        for first_code, second_code, expected in (
            (4, 0, AutopkgtestStatus.PASSED),
            (16, 0, AutopkgtestStatus.PASSED),
            (16, 4, AutopkgtestStatus.FAILED),
            (4, 16, AutopkgtestStatus.FAILED),
            (16, 16, AutopkgtestStatus.INFRASTRUCTURE_ERROR),
        ):
            with self.subTest(first=first_code, second=second_code):
                receipt = self.adapter._buildReceipt(self.request)
                self.read_session.get.side_effect = [
                    self.response(
                        json.dumps(
                            [{"subdir": first}, {"subdir": second}]
                        ).encode()
                    ),
                    self.response(
                        self.resultTar(code=first_code, include_uuid=False)
                    ),
                ]
                result = self.adapter.poll(self.request, receipt)
                self.assertEqual(AutopkgtestStatus.PENDING, result.status)
                self.assertEqual(
                    first, result.service_metadata["listing_marker"]
                )
                receipt.service_metadata.update(result.service_metadata)
                self.read_session.get.side_effect = [
                    self.response(json.dumps([{"subdir": second}]).encode()),
                    self.response(
                        self.resultTar(code=second_code, include_uuid=False)
                    ),
                ]
                result = self.adapter.poll(self.request, receipt)
                result.validate(self.request, receipt)
                self.assertEqual(expected, result.status)

    def test_pass_does_not_wait_for_end_of_listing_page(self):
        self.adapter.listing_limit = 1
        self.queueResult(self.resultTar(include_uuid=False))
        receipt = self.adapter._buildReceipt(self.request)
        result = self.adapter.poll(self.request, receipt)
        self.assertEqual(AutopkgtestStatus.PASSED, result.status)

    def test_failed_build_scan_requires_end_of_full_listing_page(self):
        self.adapter.listing_limit = 1
        self.queueResult(self.resultTar(code=4, include_uuid=False))
        receipt = self.adapter._buildReceipt(self.request)
        result = self.adapter.poll(self.request, receipt)
        self.assertEqual(AutopkgtestStatus.PENDING, result.status)
        receipt.service_metadata.update(result.service_metadata)
        self.read_session.get.side_effect = [self.response(b"[]")]
        result = self.adapter.poll(self.request, receipt)
        self.assertEqual(AutopkgtestStatus.FAILED, result.status)

    def test_request_date_excludes_previous_attempt_results(self):
        request = replace(self.request, date_requested="20261012_110000")
        self.read_session.get.return_value = self.response(b"[]")
        self.adapter.poll(request, self.adapter._buildReceipt(request))
        self.assertEqual(
            "test-series/amd64/t/test-package/20261012_110000",
            self.read_session.get.call_args.kwargs["params"]["marker"],
        )

    def test_all_proposed_requires_verifiable_result_option(self):
        request = replace(
            self.request,
            parameters={
                "autopkgtest-trigger": "dependency/2.0",
                "autopkgtest-all-proposed": "true",
            },
        )
        self.queueResult(self.resultTar(include_uuid=False))
        self.assertRaises(
            ValueError,
            self.adapter.poll,
            request,
            self.adapter._buildReceipt(request),
        )
        self.queueResult(self.resultTar(include_uuid=False, all_proposed="1"))
        self.assertEqual(
            AutopkgtestStatus.PASSED,
            self.adapter.poll(
                request, self.adapter._buildReceipt(request)
            ).status,
        )

    def test_no_tests_returns_error_not_skipped(self):
        self.queueResult(self.resultTar(code=8, include_uuid=False))
        result = self.adapter.poll(
            self.request, self.adapter._buildReceipt(self.request)
        )
        self.assertEqual(AutopkgtestStatus.FAILED, result.status)
        self.assertIn("no tests", result.summary)

    def test_incomplete_upload_does_not_advance_cursor(self):
        prefix = self.queueResult(b"")
        self.read_session.get.side_effect = [
            self.response(json.dumps([{"subdir": prefix}]).encode("utf-8")),
            self.response(b"", status=404),
        ]
        result = self.adapter.poll(self.request, self.receipt)
        self.assertEqual("", result.service_metadata["listing_marker"])

    def test_wrong_version_is_rejected(self):
        self.queueResult(self.resultTar(version="2.0"))
        self.assertRaises(
            ValueError, self.adapter.poll, self.request, self.receipt
        )

    def test_exit_codes_are_normalized(self):
        for code, expected in (
            (0, AutopkgtestStatus.PASSED),
            (4, AutopkgtestStatus.FAILED),
            (8, AutopkgtestStatus.FAILED),
            (16, AutopkgtestStatus.INFRASTRUCTURE_ERROR),
        ):
            self.queueResult(self.resultTar(code=code))
            self.assertEqual(
                expected, self.adapter.poll(self.request, self.receipt).status
            )

    def test_readiness_requires_exact_source_and_binary_version(self):
        source = (
            b"Package: test-package\nVersion: 1.0~ppa1\n"
            b"Binary: test-binary\n\n"
        )
        binary = (
            b"Package: test-binary\nVersion: 1.0~ppa1\n"
            b"Source: test-package (1.0~ppa1)\nArchitecture: amd64\n\n"
        )
        self.read_session.get.side_effect = [
            self.response(gzip.compress(source)),
            self.response(gzip.compress(binary)),
        ]
        self.assertTrue(self.adapter.checkReadiness(self.request).ready)
        self.submit_session.get.assert_not_called()

    def test_missing_indexes_are_not_skipped_tests(self):
        self.read_session.get.return_value = self.response(b"", status=404)
        readiness = self.adapter.checkReadiness(self.request)
        self.assertFalse(readiness.ready)
        self.assertTrue(readiness.eligible)

    def test_readiness_requires_every_expected_binary(self):
        request = replace(
            self.request, expected_binary_packages=["first", "second"]
        )
        source = b"Package: test-package\nVersion: 1.0~ppa1\n\n"
        first = b"Package: first\nVersion: 1.0~ppa1\nSource: test-package\n\n"
        second = (
            b"Package: second\nVersion: 1.0~ppa1\nSource: test-package\n\n"
        )
        for binaries, expected in ((first, False), (first + second, True)):
            self.read_session.get.side_effect = [
                self.response(gzip.compress(source)),
                self.response(gzip.compress(binaries)),
            ]
            self.assertEqual(
                expected, self.adapter.checkReadiness(request).ready
            )

    def test_configured_ppa_url_prefix_uses_https_anonymous_indexes(self):
        self.pushConfig(
            "personalpackagearchive",
            base_url="https://other-ppa.example.test/archives/",
        )
        request = replace(
            self.request,
            archive_reference=(
                "http://other-ppa.example.test/archives/owner/mp/ubuntu"
            ),
        )
        self.read_session.get.return_value = self.response(b"", status=404)
        self.assertFalse(self.adapter.checkReadiness(request).ready)
        root = (
            "https://other-ppa.example.test/archives/owner/mp/ubuntu/"
            "dists/test-series/main/"
        )
        self.assertEqual(
            [root + "source/Sources.gz", root + "binary-amd64/Packages.gz"],
            [call.args[0] for call in self.read_session.get.call_args_list],
        )
        for call in self.read_session.get.call_args_list:
            self.assertIsNone(call.kwargs["cookies"])

    def test_response_size_is_bounded(self):
        self.adapter.max_response_bytes = 1
        self.read_session.get.return_value = self.response(b"too large")
        self.assertRaises(
            AutopkgtestAdapterUnavailable,
            self.adapter.checkReadiness,
            self.request,
        )

    def test_remote_cancellation_is_explicitly_unsupported(self):
        result = self.adapter.cancel(self.request, self.receipt)
        self.assertFalse(result.supported)
        self.assertFalse(result.acknowledged)
        self.read_session.get.assert_not_called()
        self.submit_session.get.assert_not_called()


class TestAutopkgtestContract(TestCase):
    layer = BaseLayer

    def setUp(self):
        super().setUp()
        self.pushConfig(
            "personalpackagearchive", base_url="https://ppa.example.test"
        )

    def test_workflow_all_proposed_option_is_translated(self):
        for saved, expected in (("false", False), ("true", True)):
            request = replace(
                makeRequest(),
                archive_reference="https://ppa.example.test/owner/mp/ubuntu",
                parameters={"autopkgtest-all-proposed": saved},
            )
            self.assertEqual(
                expected,
                ("all-proposed", "1")
                in buildUbuntuAutopkgtestParameters(request),
            )

    def test_registered_adapter_factory_consumes_configuration(self):
        from lp.code.autopkgtest import makeUbuntuAutopkgtestAdapter

        self.pushConfig(
            "autopkgtest",
            api_key="bot:factory-test-key",
            service_url="https://submit.example.test/",
            results_url="https://results.example.test/public/",
            connect_timeout=4,
            read_timeout=19,
        )
        self.pushConfig(
            "launchpad",
            http_proxy="http://proxy.example.test:3128",
            ca_certificates_path="/tmp/autopkgtest-test-ca.pem",
        )
        adapter = makeUbuntuAutopkgtestAdapter()
        self.addCleanup(adapter.submit_session.close)
        self.addCleanup(adapter.read_session.close)
        self.assertIsInstance(adapter, UbuntuAutopkgtestAdapter)
        self.assertEqual("bot:factory-test-key", adapter.api_key)
        self.assertEqual("https://submit.example.test", adapter.service_url)
        self.assertEqual(
            "https://results.example.test/public", adapter.results_url
        )
        self.assertEqual((4, 19), adapter.timeout)
        self.assertIsNot(adapter.submit_session, adapter.read_session)
        for session in (adapter.submit_session, adapter.read_session):
            self.assertFalse(session.trust_env)
            self.assertEqual(
                {
                    "http": "http://proxy.example.test:3128",
                    "https": "http://proxy.example.test:3128",
                },
                session.proxies,
            )
            self.assertEqual("/tmp/autopkgtest-test-ca.pem", session.verify)

    def test_ubuntu_request_includes_ppa_and_exact_built_trigger(self):
        request = replace(
            makeRequest(),
            archive_reference="https://ppa.example.test/owner/mp/ubuntu",
            parameters={
                "autopkgtest-trigger": "dependency/2.0",
                "autopkgtest-all-proposed": "true",
            },
        )
        self.assertEqual(
            [
                ("release", "test-series"),
                ("arch", "amd64"),
                ("package", "test-package"),
                ("ppa", "owner/mp"),
                ("trigger", "test-package/1.0~ppa1"),
                ("trigger", "dependency/2.0"),
                ("all-proposed", "1"),
            ],
            buildUbuntuAutopkgtestParameters(request),
        )

    def test_ubuntu_request_rejects_unknown_options(self):
        request = replace(
            makeRequest(),
            archive_reference="https://ppa.example.test/owner/mp/ubuntu",
            parameters={"unknown-option": True},
        )
        self.assertRaises(
            ValueError, buildUbuntuAutopkgtestParameters, request
        )

    def test_ubuntu_request_splits_and_deduplicates_workflow_triggers(self):
        request = replace(
            makeRequest(),
            archive_reference="https://ppa.example.test/owner/mp/ubuntu",
            parameters={
                "autopkgtest-trigger": (
                    " dependency/2.0\tother/1.0 dependency/2.0 "
                    "test-package/1.0~ppa1 "
                ),
            },
        )
        parameters = buildUbuntuAutopkgtestParameters(request)
        self.assertEqual(
            ["test-package/1.0~ppa1", "dependency/2.0", "other/1.0"],
            [value for key, value in parameters if key == "trigger"],
        )

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
        changed = replace(
            request, parameters={"autopkgtest-all-proposed": "true"}
        )
        self.assertNotEqual(request.fingerprint, changed.fingerprint)

    def test_nested_input_is_detached(self):
        payload = makeRequest().to_dict()
        request = AutopkgtestRequest.from_dict(payload)
        payload["parameters"]["autopkgtest-trigger"] = "other/1.0"
        self.assertEqual(
            "dependency/2.0", request.parameters["autopkgtest-trigger"]
        )

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
