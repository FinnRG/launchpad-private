# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Collect external revision check results in short transactions."""

__all__ = [
    "RevisionStatusWatchCollector",
    "RevisionStatusWatchCronScript",
    "RevisionStatusWatchPollResult",
]

import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import transaction
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.code.enums import RevisionStatusResult
from lp.code.interfaces.revisionstatuswatch import IRevisionStatusWatchSet
from lp.services.scripts.base import (
    LaunchpadCronScript,
    LaunchpadScriptFailure,
)

FINAL_RESULTS = (
    RevisionStatusResult.SUCCEEDED,
    RevisionStatusResult.FAILED,
    RevisionStatusResult.SKIPPED,
    RevisionStatusResult.CANCELLED,
)


class RevisionStatusWatchPollResult:
    """A handler's normalized response, not an external service payload.

    A None result means there is no new status. A None remote_ref means
    retain the existing reference; otherwise replace it with the supplied
    JSON data. The handler must verify result identity and bound network
    requests before returning.
    """

    def __init__(
        self, result=None, url=None, result_summary=None, remote_ref=None
    ):
        self.result = result
        self.url = url
        self.result_summary = result_summary
        self.remote_ref = remote_ref


class RevisionStatusWatchCollector:
    """Poll a bounded batch using handlers keyed by remote system.

    Each handler implements poll(remote_ref). The collector owns commits
    and aborts: use it only in a dedicated worker with no unrelated pending
    changes. Callers supply a trusted watch set. A singleton cron lock must
    prevent overlapping collectors; this is not a multi-worker claim queue.
    """

    def __init__(
        self,
        watch_set,
        handlers,
        logger=None,
        transaction_manager=None,
        clock=None,
        poll_interval=timedelta(minutes=5),
        max_backoff=timedelta(hours=1),
    ):
        if poll_interval <= timedelta(0):
            raise ValueError("poll_interval must be positive")
        if max_backoff < poll_interval:
            raise ValueError("max_backoff must be at least poll_interval")
        self.watch_set = watch_set
        self.handlers = handlers
        self.logger = (
            logger if logger is not None else logging.getLogger(__name__)
        )
        self.transaction_manager = (
            transaction_manager
            if transaction_manager is not None
            else transaction.manager
        )
        self.clock = clock if clock is not None else self._now
        self.poll_interval = poll_interval
        self.max_backoff = max_backoff

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def run(self, limit=100):
        """Process at most limit due watches, isolating per-watch failures."""
        try:
            watch_ids = [
                watch.id
                for watch in self.watch_set.findDue(
                    now=self.clock(), limit=limit
                )
            ]
            self.transaction_manager.commit()
        except Exception:
            self.transaction_manager.abort()
            raise
        for watch_id in watch_ids:
            try:
                self._pollWatch(watch_id)
            except Exception as error:
                self.transaction_manager.abort()
                self.logger.exception(
                    "Error updating revision status watch %s (%s)",
                    watch_id,
                    type(error).__name__,
                )

    def _getActiveWatch(self, watch_id, now):
        watch = self.watch_set.getByID(watch_id)
        if watch is None:
            return None
        if watch.report.result in FINAL_RESULTS:
            watch.destroySelf()
            return None
        if watch.next_check is None or watch.next_check > now:
            return None
        return watch

    def _pollWatch(self, watch_id):
        watch = self._getActiveWatch(watch_id, self.clock())
        if watch is None:
            self.transaction_manager.commit()
            return
        remote_system = watch.remote_system
        remote_ref = deepcopy(watch.remote_ref)
        report_id = watch.report_id
        next_check = watch.next_check
        self.transaction_manager.commit()

        self.logger.debug(
            "Polling revision status watch %s (%s), report %s",
            watch_id,
            remote_system,
            report_id,
        )
        try:
            poll_result = self.handlers[remote_system].poll(
                deepcopy(remote_ref)
            )
            self._validateResult(poll_result)
        except Exception as error:
            poll_result = None
            self.logger.exception(
                "Error polling revision status watch %s (%s), report %s: %s",
                watch_id,
                remote_system,
                report_id,
                type(error).__name__,
            )

        checked_at = self.clock()
        watch = self._getActiveWatch(watch_id, checked_at)
        if watch is None:
            self.transaction_manager.commit()
            return
        if (
            watch.report_id != report_id
            or watch.remote_system != remote_system
            or watch.remote_ref != remote_ref
            or watch.next_check != next_check
        ):
            self.transaction_manager.commit()
            return

        watch.lastchecked = checked_at
        if poll_result is None:
            watch.failure_count += 1
            failure_count = watch.failure_count
            delay = self.poll_interval
            for _ in range(watch.failure_count - 1):
                delay = min(delay * 2, self.max_backoff)
                if delay == self.max_backoff:
                    break
            watch.next_check = checked_at + delay
        else:
            watch.failure_count = 0
            if poll_result.remote_ref is not None:
                watch.remote_ref = poll_result.remote_ref
            result = poll_result.result
            if (
                result == RevisionStatusResult.WAITING
                and watch.report.result == RevisionStatusResult.RUNNING
            ):
                result = None
            watch.report.update(
                result=result,
                url=poll_result.url,
                result_summary=poll_result.result_summary,
            )
            if result in FINAL_RESULTS:
                watch.destroySelf()
            else:
                watch.next_check = checked_at + self.poll_interval
        self.transaction_manager.commit()
        if poll_result is None:
            self.logger.warning(
                "Retry scheduled for revision status watch %s (%s), "
                "report %s: failure %s, next check %s",
                watch_id,
                remote_system,
                report_id,
                failure_count,
                (checked_at + delay).isoformat(),
            )
        elif result in FINAL_RESULTS:
            self.logger.info(
                "Completed revision status watch %s (%s), report %s: %s",
                watch_id,
                remote_system,
                report_id,
                result.name,
            )
        else:
            self.logger.debug(
                "Revision status watch %s (%s), report %s remains pending; "
                "next check %s",
                watch_id,
                remote_system,
                report_id,
                (checked_at + self.poll_interval).isoformat(),
            )

    @staticmethod
    def _validateResult(poll_result):
        if not isinstance(poll_result, RevisionStatusWatchPollResult):
            raise ValueError("Handler must return a watch poll result")
        allowed_results = (
            None,
            RevisionStatusResult.WAITING,
            RevisionStatusResult.RUNNING,
        ) + FINAL_RESULTS
        if poll_result.result not in allowed_results:
            raise ValueError("Handler returned an invalid revision status")


class RevisionStatusWatchCronScript(LaunchpadCronScript):
    """Run a bounded collector batch under the standard cron lock."""

    description = "Collect results for external revision status watches."

    def add_my_options(self):
        self.parser.add_option(
            "--batch-size",
            type="int",
            default=100,
            metavar="COUNT",
            help="Maximum number of watches to check (default: %default).",
        )

    def getHandlers(self):
        """Return handlers when supported external adapters are available."""
        return {}

    def main(self):
        if self.args:
            raise LaunchpadScriptFailure("Unexpected positional arguments.")
        if self.options.batch_size <= 0:
            raise LaunchpadScriptFailure("--batch-size must be positive.")
        handlers = self.getHandlers()
        if not handlers:
            raise LaunchpadScriptFailure(
                "No revision status watch handlers are configured; "
                "no watches were processed."
            )
        watch_set = removeSecurityProxy(getUtility(IRevisionStatusWatchSet))
        collector = RevisionStatusWatchCollector(
            watch_set,
            handlers,
            logger=self.logger,
            transaction_manager=self.txn,
        )
        self.logger.info(
            "Checking up to %s revision status watches.",
            self.options.batch_size,
        )
        collector.run(limit=self.options.batch_size)
        self.logger.info("Finished checking revision status watches.")
