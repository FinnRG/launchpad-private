# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Launchpad-specific patches to zope.testrunner.

Launchpad used to maintain a fork of zope.testrunner
(lp:~launchpad-committers/zope.testrunner) solely to carry a handful of
behavioural changes.  Rather than keep the fork alive, we inline those
changes here as monkey patches that are applied from ``test.py`` before the
runner builds its options and starts discovering tests.

The patches reproduce the following fork changes on top of the upstream
zope.testrunner 6.6.1:

- Report tests by id (``test.id()``) rather than ``str(test)``.  Launchpad
  lists and selects tests by their full dotted id (e.g. via ``--list-tests``,
  ``-t`` and ``--load-list``), so the runner's discovery and human-readable
  output need to agree on that representation.

- Make ``--subunit --list-tests`` only print test ids.  This keeps
  Launchpad's testrepository plumbing simple.

- Turn thread leaks into skips (rather than errors) in subunit output.  It is
  normal for threads not to complete exactly on test boundaries; Launchpad's
  ``BaseLayer`` performs its own thread-leak check.

The remaining fork change (making standard streams provide a ``buffer``
attribute under ``--buffer`` or subunit) is already part of upstream
zope.testrunner 6.6.1 and therefore needs no patching.
"""

__all__ = [
    "apply_patches",
]

import sys
import unittest

from zope.testrunner import find, formatter
from zope.testrunner.find import StartUpFailure, name_from_layer
from zope.testrunner.layer import UnitTests


def _tests_from_suite(
    suite,
    options,
    dlevel=1,
    dlayer=UnitTests,
    accept=None,
    seen_test_ids=None,
    duplicated_test_ids=None,
):
    """Like ``zope.testrunner.find.tests_from_suite`` but keyed on test ids.

    This identifies tests (for uniqueness checking and for ``accept``/``-t``
    selection) by ``test.id()`` rather than ``str(test)``.
    """
    if seen_test_ids is None:
        seen_test_ids = set()
    if duplicated_test_ids is None:
        duplicated_test_ids = set()

    level = getattr(suite, "level", dlevel)
    layer = getattr(suite, "layer", dlayer)
    if not isinstance(layer, str):
        layer = name_from_layer(layer)

    if isinstance(suite, unittest.TestSuite):
        for possible_suite in suite:
            yield from _tests_from_suite(
                possible_suite,
                options,
                level,
                layer,
                accept=accept,
                seen_test_ids=seen_test_ids,
                duplicated_test_ids=duplicated_test_ids,
            )
    elif isinstance(suite, StartUpFailure):
        yield (suite, None)
    else:
        if options.require_unique_ids:
            suite_id = suite.id()
            if suite_id in seen_test_ids:
                duplicated_test_ids.add(suite_id)
            else:
                seen_test_ids.add(suite_id)
        if options.at_level <= 0 or level <= options.at_level:
            if accept is None or accept(str(suite)) or accept(suite.id()):
                yield (suite, layer)


def _getShortDescription(self, test, room):
    """Return a description of a test that fits in ``room`` characters."""
    room -= 1
    s = test.id()
    if len(s) > room:
        pos = s.find(" (")
        if pos >= 0:
            w = room - (pos + 5)
            if w < 1:
                # first portion (test method name) is too long
                s = s[: room - 3] + "..."
            else:
                pre = s[: pos + 2]
                post = s[-w:]
                s = f"{pre}...{post}"
        else:
            w = room - 4
            s = "... " + s[-w:]

    return " " + s[:room]


def _list_of_tests(self, tests, layer_name):
    """Report a list of test names (by id)."""
    print("Listing %s tests:" % layer_name)
    for test in tests:
        print(" ", test.id())


def _start_test(self, test, tests_run, total_tests):
    """Report that we're about to run a test, naming it by id."""
    self.test_width = 0
    if self.progress:
        if self.last_width:
            sys.stdout.write("\r" + (" " * self.last_width) + "\r")

        s = "    %d/%d (%.1f%%)" % (
            tests_run,
            total_tests,
            tests_run * 100.0 / total_tests,
        )
        sys.stdout.write(s)
        self.test_width += len(s)
        if self.verbose == 1:
            room = self.max_width - self.test_width - 1
            s = self.getShortDescription(test, room)
            sys.stdout.write(s)
            self.test_width += len(s)

    elif self.verbose == 1:
        sys.stdout.write("." * test.countTestCases())

    elif self.in_subprocess:
        sys.stdout.write("." * test.countTestCases())
        # Give the parent process a new line so it sees the progress
        # in a timely manner.
        sys.stdout.write("\n")

    if self.verbose > 1:
        s = test.id()
        sys.stdout.write(" ")
        sys.stdout.write(s)
        self.test_width += len(s) + 1

    sys.stdout.flush()


def _subunit_list_of_tests(self, tests, layer_name):
    """Report a list of test names by just printing their ids.

    This is not really correct subunit and should be fixed at some point, but
    it simplifies Launchpad's testrepository plumbing.  It only applies to
    subunit v1; subunit v2 retains the proper stream-based listing (see
    ``_subunit_v2_list_of_tests``).
    """
    for test in tests:
        print(test.id())


def _subunit_test_threads(self, test, new_threads):
    """Report threads left behind by a test as a skip rather than an error.

    This is a skip because it's normal for threads to not complete exactly on
    test boundaries.  We check for this in the Launchpad testrunner baselayer
    anyway.
    """
    self._subunit.startTest(test)
    self._subunit.tags([self.TAG_THREADS], [])
    self._subunit.addSkip(
        test,
        details={"threads": formatter.text_content(str(new_threads))},
    )
    self._subunit.stopTest(test)


def _subunit_v2_list_of_tests(self, tests, layer_name):
    """Report a list of test names using a proper subunit stream.

    Undo the subunit v1 hack of printing test ids directly; we only use that
    in v1 mode.
    """
    self._enter_layer(layer_name)
    for test in tests:
        self._subunit.startTest(test)
        self._emit_exists(test)
        self._subunit.stopTest(test)
    self._exit_layer(layer_name)


def apply_patches():
    """Apply the Launchpad-specific zope.testrunner patches.

    This must be called before the runner builds its options (which
    instantiates the output formatter) and before test discovery runs.
    """
    # Report tests by id rather than str(test).
    find.tests_from_suite = _tests_from_suite
    formatter.OutputFormatter.getShortDescription = _getShortDescription
    formatter.OutputFormatter.list_of_tests = _list_of_tests
    formatter.OutputFormatter.start_test = _start_test

    # Make --subunit --list-tests only print test IDs, and turn thread leaks
    # into skips in subunit output.
    formatter.SubunitOutputFormatter.list_of_tests = _subunit_list_of_tests
    formatter.SubunitOutputFormatter.test_threads = _subunit_test_threads
    formatter.SubunitV2OutputFormatter.list_of_tests = (
        _subunit_v2_list_of_tests
    )
