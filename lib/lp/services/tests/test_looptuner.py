# Copyright 2011 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""looptuner.py tests.

These are the edge test cases that don't belong in the doctest.
"""

import io

from zope.interface import implementer

from lp.services.log.logger import FakeLogger
from lp.services.looptuner import ITunableLoop, LoopTuner, TunableLoop
from lp.testing import TestCase
from lp.testing.layers import BaseLayer


class MainException(Exception):
    """Exception raised from the main body of an ITunableLoop."""


class CleanupException(Exception):
    """Exception raised from the cleanup method of an ITunableLoop."""


@implementer(ITunableLoop)
class FailingLoop:
    def __init__(self, fail_main=False, fail_cleanup=False):
        self.fail_main = fail_main
        self.fail_cleanup = fail_cleanup

    _done = False  # Set by __call__ to signal termination

    def isDone(self):
        return self._done

    def __call__(self, chunk_size):
        self._done = True
        if self.fail_main:
            raise MainException()

    def cleanUp(self):
        if self.fail_cleanup:
            raise CleanupException()


class TestSomething(TestCase):
    layer = BaseLayer

    def test_cleanup_exception_on_success(self):
        """Main task succeeded but cleanup failed.

        Exception from cleanup raised.
        """
        log_file = io.StringIO()
        loop = FailingLoop(fail_cleanup=True)
        tuner = LoopTuner(loop, 5, log=FakeLogger(log_file))
        self.assertRaises(CleanupException, tuner.run)
        self.assertEqual(log_file.getvalue(), "")

    def test_cleanup_exception_on_failure(self):
        """Main task failed and cleanup also failed.

        Exception from cleanup is logged.
        Original exception from main task is raised.
        """
        log_file = io.StringIO()
        loop = FailingLoop(fail_main=True, fail_cleanup=True)
        tuner = LoopTuner(loop, 5, log=FakeLogger(log_file))
        self.assertRaises(MainException, tuner.run)
        self.assertEqual(
            log_file.getvalue().strip(), "ERROR Unhandled exception in cleanUp"
        )


class CountingLoop(TunableLoop):
    """A loop that does a fixed number of iterations."""

    tuner_class = LoopTuner
    maximum_chunk_size = 10

    def __init__(self, log, iterations, abort_time=None):
        super().__init__(log, abort_time=abort_time)
        self.remaining = iterations

    def isDone(self):
        return self.remaining == 0

    def __call__(self, chunk_size):
        self.remaining -= 1


class TestLoopTunerProgress(TestCase):
    layer = BaseLayer

    def test_finished_with_no_iterations(self):
        tuner = LoopTuner(CountingLoop(None, 0), 5, log=FakeLogger())
        tuner.run()
        self.assertTrue(tuner.finished)
        self.assertEqual(0, tuner.iterations)

    def test_finished_after_iterations(self):
        tuner = LoopTuner(CountingLoop(None, 3), 5, log=FakeLogger())
        tuner.run()
        self.assertTrue(tuner.finished)
        self.assertEqual(3, tuner.iterations)

    def test_not_finished_when_aborted(self):
        tuner = LoopTuner(
            CountingLoop(None, 3),
            5,
            abort_time=0,
            log=FakeLogger(io.StringIO()),
        )
        tuner.run()
        self.assertFalse(tuner.finished)
        self.assertEqual(0, tuner.iterations)


class TestTunableLoopLogIfIdle(TestCase):
    layer = BaseLayer

    def runLoop(self, iterations, log_if_idle, abort_time=None):
        log_file = io.StringIO()
        loop = CountingLoop(
            FakeLogger(log_file), iterations, abort_time=abort_time
        )
        loop.log_if_idle = log_if_idle
        loop.run()
        return log_file.getvalue()

    def test_logs_when_idle(self):
        self.assertIn("INFO No work to do.", self.runLoop(0, True))

    def test_silent_when_idle_by_default(self):
        self.assertNotIn("No work to do.", self.runLoop(0, False))

    def test_silent_when_work_was_done(self):
        self.assertNotIn("No work to do.", self.runLoop(2, True))

    def test_silent_when_aborted_before_any_work(self):
        log = self.runLoop(2, True, abort_time=0)
        self.assertIn("Task aborted", log)
        self.assertNotIn("No work to do.", log)
