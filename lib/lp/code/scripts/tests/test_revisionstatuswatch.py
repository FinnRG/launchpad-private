# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for collecting external revision check results."""

from datetime import datetime, timedelta, timezone

import transaction
from fixtures import FakeLogger
from storm.store import Store
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.code.enums import RevisionStatusResult
from lp.code.interfaces.revisionstatus import IRevisionStatusReportSet
from lp.code.interfaces.revisionstatuswatch import IRevisionStatusWatchSet
from lp.code.scripts.revisionstatuswatch import (
    RevisionStatusWatchCollector,
    RevisionStatusWatchPollResult,
)
from lp.services.database.isolation import check_no_transaction
from lp.testing import TestCaseWithFactory
from lp.testing.layers import DatabaseFunctionalLayer


class FakeWatchHandler:
    def __init__(self):
        self.responses = {}
        self.calls = []
        self.callback = None

    def poll(self, remote_ref):
        check_no_transaction()
        self.calls.append(remote_ref)
        if self.callback is not None:
            self.callback(remote_ref)
        response = self.responses[remote_ref["request_id"]]
        if isinstance(response, Exception):
            raise response
        return response


class TestRevisionStatusWatchCollector(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.watch_set = removeSecurityProxy(
            getUtility(IRevisionStatusWatchSet)
        )
        self.handler = FakeWatchHandler()
        self.log = self.useFixture(FakeLogger())
        self.now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        self.collector = RevisionStatusWatchCollector(
            self.watch_set,
            {"test-service": self.handler},
            clock=lambda: self.now,
        )

    def makeWatch(
        self,
        response=None,
        result=RevisionStatusResult.WAITING,
        remote_system="test-service",
        next_check=None,
    ):
        report = self.factory.makeRevisionStatusReport(result=result)
        Store.of(report).flush()
        request_id = "request-%s" % report.id
        self.handler.responses[request_id] = (
            response
            if response is not None
            else RevisionStatusWatchPollResult()
        )
        watch = self.watch_set.new(
            report,
            remote_system,
            {"request_id": request_id},
            next_check=self.now if next_check is None else next_check,
        )
        Store.of(watch).flush()
        return watch.id, report.id

    def getWatch(self, watch_id):
        return self.watch_set.getByID(watch_id)

    def getReport(self, report_id):
        return removeSecurityProxy(
            getUtility(IRevisionStatusReportSet).getByID(report_id)
        )

    def test_pending_schedules_another_check(self):
        watch_id, report_id = self.makeWatch()
        self.collector.run()
        watch = self.getWatch(watch_id)
        self.assertEqual(self.now, watch.lastchecked)
        self.assertEqual(self.now + timedelta(minutes=5), watch.next_check)
        self.assertEqual(0, watch.failure_count)
        report = self.getReport(report_id)
        self.assertEqual(RevisionStatusResult.WAITING, report.result)
        self.assertIsNone(report.date_started)
        self.assertIsNone(report.date_finished)

    def test_running_updates_report(self):
        watch_id, report_id = self.makeWatch(
            response=RevisionStatusWatchPollResult(
                result=RevisionStatusResult.RUNNING,
                url="https://tests.example/run/123",
                result_summary="Running tests",
            )
        )
        self.collector.run()
        report = self.getReport(report_id)
        self.assertEqual(RevisionStatusResult.RUNNING, report.result)
        self.assertEqual("https://tests.example/run/123", report.url)
        self.assertEqual("Running tests", report.result_summary)
        self.assertIsNotNone(report.date_started)
        self.assertIsNone(report.date_finished)
        self.assertIsNotNone(self.getWatch(watch_id))

    def test_terminal_results_remove_watches_and_keep_reports(self):
        records = []
        for result in (
            RevisionStatusResult.SUCCEEDED,
            RevisionStatusResult.FAILED,
            RevisionStatusResult.SKIPPED,
            RevisionStatusResult.CANCELLED,
        ):
            watch_id, report_id = self.makeWatch(
                response=RevisionStatusWatchPollResult(
                    result=result,
                    url="https://tests.example/result",
                    result_summary=result.title,
                )
            )
            records.append((watch_id, report_id, result))
        self.collector.run()
        for watch_id, report_id, result in records:
            self.assertIsNone(self.getWatch(watch_id))
            report = self.getReport(report_id)
            self.assertEqual(result, report.result)
            self.assertIsNotNone(report.date_finished)
            self.assertEqual("https://tests.example/result", report.url)
            self.assertEqual(result.title, report.result_summary)
            self.assertIn(
                "Completed revision status watch %s (test-service), "
                "report %s: %s" % (watch_id, report_id, result.name),
                self.log.output,
            )

    def test_failure_does_not_fail_package(self):
        watch_id, report_id = self.makeWatch(
            response=RuntimeError("Service unavailable")
        )
        self.collector.run()
        watch = self.getWatch(watch_id)
        self.assertEqual(1, watch.failure_count)
        self.assertEqual(self.now, watch.lastchecked)
        self.assertEqual(self.now + timedelta(minutes=5), watch.next_check)
        self.assertEqual(
            RevisionStatusResult.WAITING, self.getReport(report_id).result
        )
        self.assertIn(
            "Error polling revision status watch %s (test-service), "
            "report %s: RuntimeError" % (watch_id, report_id),
            self.log.output,
        )
        self.assertIn("RuntimeError: Service unavailable", self.log.output)
        self.assertIn(
            "Retry scheduled for revision status watch %s (test-service), "
            "report %s: failure 1, next check %s"
            % (watch_id, report_id, watch.next_check.isoformat()),
            self.log.output,
        )

    def test_timeout_logs_error_and_retry_context(self):
        watch_id, report_id = self.makeWatch(
            response=TimeoutError("Service timed out")
        )
        self.collector.run()
        self.assertIn(
            "Error polling revision status watch %s (test-service), "
            "report %s: TimeoutError" % (watch_id, report_id),
            self.log.output,
        )
        self.assertIn("TimeoutError: Service timed out", self.log.output)
        self.assertIn("failure 1, next check", self.log.output)
        self.assertEqual(1, self.getWatch(watch_id).failure_count)

    def test_update_failure_logs_error_and_continues(self):
        failed_id, unused = self.makeWatch()
        passed_id, unused = self.makeWatch()
        original_poll = self.collector._pollWatch

        def pollWatch(watch_id):
            if watch_id == failed_id:
                raise RuntimeError("Update failed")
            original_poll(watch_id)

        self.patch(self.collector, "_pollWatch", pollWatch)
        self.collector.run()
        self.assertIn(
            "Error updating revision status watch %s (RuntimeError)"
            % failed_id,
            self.log.output,
        )
        self.assertIn("RuntimeError: Update failed", self.log.output)
        self.assertEqual(self.now, self.getWatch(passed_id).lastchecked)

    def test_backoff_increases_and_is_capped(self):
        watch_id, unused = self.makeWatch(response=RuntimeError("Offline"))
        for minutes in (5, 10, 20, 40, 60, 60):
            self.collector.run()
            watch = self.getWatch(watch_id)
            self.assertEqual(
                self.now + timedelta(minutes=minutes), watch.next_check
            )
            self.assertIn(
                "failure %s, next check %s"
                % (watch.failure_count, watch.next_check.isoformat()),
                self.log.output,
            )
            self.now = watch.next_check
        self.assertEqual(6, watch.failure_count)

    def test_successful_contact_resets_failures(self):
        watch_id, unused = self.makeWatch()
        watch = self.getWatch(watch_id)
        watch.failure_count = 3
        self.collector.run()
        watch = self.getWatch(watch_id)
        self.assertEqual(0, watch.failure_count)
        self.assertEqual(self.now + timedelta(minutes=5), watch.next_check)

    def test_failed_watch_does_not_block_next(self):
        failed_id, unused = self.makeWatch(response=RuntimeError("Offline"))
        passed_id, report_id = self.makeWatch(
            response=RevisionStatusWatchPollResult(
                result=RevisionStatusResult.SUCCEEDED
            )
        )
        self.collector.run()
        self.assertEqual(1, self.getWatch(failed_id).failure_count)
        self.assertIsNone(self.getWatch(passed_id))
        self.assertEqual(
            RevisionStatusResult.SUCCEEDED, self.getReport(report_id).result
        )
        self.assertEqual(2, len(self.handler.calls))

    def test_future_and_paused_watches_are_not_polled(self):
        self.makeWatch(next_check=self.now + timedelta(seconds=1))
        watch_id, unused = self.makeWatch()
        self.getWatch(watch_id).next_check = None
        self.collector.run()
        self.assertEqual([], self.handler.calls)

    def test_batch_limit(self):
        first_id, unused = self.makeWatch()
        second_id, unused = self.makeWatch()
        self.collector.run(limit=1)
        self.assertEqual(1, len(self.handler.calls))
        self.assertEqual(self.now, self.getWatch(first_id).lastchecked)
        self.assertIsNone(self.getWatch(second_id).lastchecked)

    def test_unknown_system_is_retryable(self):
        watch_id, report_id = self.makeWatch(remote_system="unknown")
        self.collector.run()
        self.assertEqual(1, self.getWatch(watch_id).failure_count)
        self.assertEqual(
            RevisionStatusResult.WAITING, self.getReport(report_id).result
        )
        self.assertEqual([], self.handler.calls)

    def test_invalid_status_is_retryable(self):
        watch_id, report_id = self.makeWatch(
            response=RevisionStatusWatchPollResult(result="passed")
        )
        self.collector.run()
        self.assertEqual(1, self.getWatch(watch_id).failure_count)
        self.assertEqual(
            RevisionStatusResult.WAITING, self.getReport(report_id).result
        )

    def test_invalid_response_is_retryable(self):
        watch_id, unused = self.makeWatch(response={"result": "passed"})
        self.collector.run()
        self.assertEqual(1, self.getWatch(watch_id).failure_count)

    def test_reference_update_is_saved(self):
        watch_id, unused = self.makeWatch()
        watch = self.getWatch(watch_id)
        remote_ref = dict(watch.remote_ref, cursor="next-page")
        self.handler.responses[watch.remote_ref["request_id"]] = (
            RevisionStatusWatchPollResult(remote_ref=remote_ref)
        )
        self.collector.run()
        self.assertEqual(remote_ref, self.getWatch(watch_id).remote_ref)

    def test_handler_cannot_mutate_saved_reference(self):
        watch_id, unused = self.makeWatch()
        self.handler.callback = lambda remote_ref: remote_ref.update(
            cursor="not-saved"
        )
        self.collector.run()
        self.assertNotIn("cursor", self.getWatch(watch_id).remote_ref)

    def test_pending_does_not_regress_running_report(self):
        watch_id, report_id = self.makeWatch(
            result=RevisionStatusResult.RUNNING,
            response=RevisionStatusWatchPollResult(
                result=RevisionStatusResult.WAITING
            ),
        )
        self.collector.run()
        self.assertEqual(
            RevisionStatusResult.RUNNING, self.getReport(report_id).result
        )
        self.assertIsNone(self.getReport(report_id).date_finished)
        self.assertIsNotNone(self.getWatch(watch_id))

    def test_already_finished_report_is_not_polled(self):
        watch_id, report_id = self.makeWatch(
            result=RevisionStatusResult.CANCELLED
        )
        self.collector.run()
        self.assertEqual([], self.handler.calls)
        self.assertIsNone(self.getWatch(watch_id))
        self.assertEqual(
            RevisionStatusResult.CANCELLED, self.getReport(report_id).result
        )

    def test_deleted_watch_during_poll_is_not_recreated(self):
        watch_id, report_id = self.makeWatch()

        def deleteWatch(remote_ref):
            self.getWatch(watch_id).destroySelf()
            transaction.commit()

        self.handler.callback = deleteWatch
        self.collector.run()
        self.assertIsNone(self.getWatch(watch_id))
        self.assertEqual(
            RevisionStatusResult.WAITING, self.getReport(report_id).result
        )

    def test_cancelled_report_during_poll_is_not_overwritten(self):
        watch_id, report_id = self.makeWatch(
            response=RevisionStatusWatchPollResult(
                result=RevisionStatusResult.SUCCEEDED
            )
        )

        def cancelReport(remote_ref):
            self.getReport(report_id).transitionToNewResult(
                RevisionStatusResult.CANCELLED
            )
            transaction.commit()

        self.handler.callback = cancelReport
        self.collector.run()
        self.assertIsNone(self.getWatch(watch_id))
        self.assertEqual(
            RevisionStatusResult.CANCELLED, self.getReport(report_id).result
        )

    def test_paused_watch_during_poll_is_not_rescheduled(self):
        watch_id, unused = self.makeWatch()

        def pauseWatch(remote_ref):
            self.getWatch(watch_id).next_check = None
            transaction.commit()

        self.handler.callback = pauseWatch
        self.collector.run()
        self.assertIsNone(self.getWatch(watch_id).next_check)
        self.assertIsNone(self.getWatch(watch_id).lastchecked)

    def test_changed_reference_during_poll_discards_result(self):
        watch_id, report_id = self.makeWatch(
            response=RevisionStatusWatchPollResult(
                result=RevisionStatusResult.SUCCEEDED
            )
        )

        def changeReference(remote_ref):
            self.getWatch(watch_id).remote_ref = {"request_id": "replacement"}
            transaction.commit()

        self.handler.callback = changeReference
        self.collector.run()
        self.assertEqual(
            {"request_id": "replacement"}, self.getWatch(watch_id).remote_ref
        )
        self.assertEqual(
            RevisionStatusResult.WAITING, self.getReport(report_id).result
        )

    def test_invalid_intervals(self):
        self.assertRaises(
            ValueError,
            RevisionStatusWatchCollector,
            self.watch_set,
            {},
            poll_interval=timedelta(0),
        )
        self.assertRaises(
            ValueError,
            RevisionStatusWatchCollector,
            self.watch_set,
            {},
            max_backoff=timedelta(minutes=1),
        )
