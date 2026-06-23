# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the zope.testrunner inline patches."""

import io
import sys
import unittest
from types import SimpleNamespace

from lp.services.testing import testrunner_patches as patches
from lp.testing import TestCase
from lp.testing.layers import BaseLayer


class FakeTestCase(unittest.TestCase):
    """A minimal TestCase that can be instantiated with a custom id."""

    def __init__(self, name, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._name = name

    def id(self):
        return self._name

    def runTest(self):
        pass


class FakeOptions:
    """Minimal options object for tests_from_suite."""

    def __init__(self, require_unique_ids=False, at_level=1):
        self.require_unique_ids = require_unique_ids
        self.at_level = at_level


class TestTestsFromSuite(TestCase):
    layer = BaseLayer

    def _call(self, suite, **kw):
        """Call the patched tests_from_suite."""
        options = FakeOptions(**kw)
        return list(patches._tests_from_suite(suite, options))

    def test_yields_test_id(self):
        # The patched function yields tests keyed by id, not str().
        test = FakeTestCase("foo.bar.Baz.test_qux")
        suite = unittest.TestSuite([test])
        result = self._call(suite)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], test)

    def test_unique_ids_by_id(self):
        # Duplicate detection uses test.id(), not str(test).
        test1 = FakeTestCase("foo.bar.Baz.test_qux")
        test2 = FakeTestCase("foo.bar.Baz.test_qux")
        suite = unittest.TestSuite([test1, test2])
        duplicated = set()
        options = FakeOptions(require_unique_ids=True)
        result = list(
            patches._tests_from_suite(
                suite, options, duplicated_test_ids=duplicated
            )
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(duplicated, {"foo.bar.Baz.test_qux"})

    def test_accept_matches_id(self):
        # The accept predicate is called with both str(test) and test.id().
        test = FakeTestCase("foo.bar.Baz.test_qux")
        suite = unittest.TestSuite([test])
        accepted = []

        def accept(name):
            accepted.append(name)
            return name == "foo.bar.Baz.test_qux"

        result = list(
            patches._tests_from_suite(suite, FakeOptions(), accept=accept)
        )
        self.assertEqual(len(result), 1)
        self.assertIn("foo.bar.Baz.test_qux", accepted)


class TestOutputFormatter(TestCase):
    layer = BaseLayer

    def _make_formatter(self):
        # Build a minimal OutputFormatter-like object with the patched methods.
        class Dummy:
            progress = False
            verbose = 0
            in_subprocess = False
            last_width = 0
            test_width = 0
            max_width = 80

        for name in (
            "getShortDescription",
            "list_of_tests",
            "start_test",
        ):
            setattr(Dummy, name, getattr(patches, "_" + name))
        return Dummy()

    def test_getShortDescription_uses_id(self):
        fmt = self._make_formatter()
        test = FakeTestCase("foo.bar.Baz.test_qux")
        desc = fmt.getShortDescription(test, 40)
        self.assertIn("foo.bar.Baz.test_qux", desc)

    def test_list_of_tests_prints_ids(self):
        fmt = self._make_formatter()
        out = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            fmt.list_of_tests(
                [FakeTestCase("a.b.C.test1"), FakeTestCase("a.b.C.test2")],
                "UnitTests",
            )
        finally:
            sys.stdout = old_stdout
        lines = out.getvalue().strip().split("\n")
        self.assertEqual(lines[0], "Listing UnitTests tests:")
        self.assertEqual(lines[1].strip(), "a.b.C.test1")
        self.assertEqual(lines[2].strip(), "a.b.C.test2")

    def test_start_test_verbose_uses_id(self):
        fmt = self._make_formatter()
        fmt.verbose = 2
        out = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            fmt.start_test(FakeTestCase("a.b.C.test1"), 1, 10)
        finally:
            sys.stdout = old_stdout
        self.assertIn("a.b.C.test1", out.getvalue())


class TestSubunitOutputFormatter(TestCase):
    layer = BaseLayer

    def _make_formatter(self):
        # Build a minimal SubunitOutputFormatter-like object.
        class Dummy:
            TAG_THREADS = "zope:threads"

        for name in ("list_of_tests", "test_threads"):
            setattr(Dummy, name, getattr(patches, "_subunit_" + name))
        return Dummy()

    def test_list_of_tests_prints_ids(self):
        fmt = self._make_formatter()
        out = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            fmt.list_of_tests(
                [FakeTestCase("a.b.C.test1"), FakeTestCase("a.b.C.test2")],
                "UnitTests",
            )
        finally:
            sys.stdout = old_stdout
        lines = out.getvalue().strip().split("\n")
        self.assertEqual(lines[0], "a.b.C.test1")
        self.assertEqual(lines[1], "a.b.C.test2")

    def test_test_threads_uses_addSkip(self):
        fmt = self._make_formatter()
        calls = []

        class FakeSubunit:
            def startTest(self, test):
                calls.append(("startTest", test.id()))

            def tags(self, add, remove):
                calls.append(("tags", add, remove))

            def addSkip(self, test, details=None):
                calls.append(("addSkip", test.id(), details))

            def stopTest(self, test):
                calls.append(("stopTest", test.id()))

        fmt._subunit = FakeSubunit()
        test = FakeTestCase("a.b.C.test1")
        fmt.test_threads(test, ["thread-1"])
        self.assertEqual(calls[0][0], "startTest")
        self.assertEqual(calls[1][0], "tags")
        self.assertEqual(calls[2][0], "addSkip")
        self.assertEqual(calls[3][0], "stopTest")
        # Make sure it was NOT an addError.
        self.assertNotIn("addError", [c[0] for c in calls])


class TestSubunitV2OutputFormatter(TestCase):
    layer = BaseLayer

    def test_list_of_tests_uses_subunit_stream(self):
        fmt = SimpleNamespace()
        fmt._enter_layer = lambda name: fmt.calls.append(("enter", name))
        fmt._exit_layer = lambda name: fmt.calls.append(("exit", name))
        fmt.calls = []

        class FakeSubunit:
            def startTest(self, test):
                fmt.calls.append(("startTest", test.id()))

            def stopTest(self, test):
                fmt.calls.append(("stopTest", test.id()))

        fmt._subunit = FakeSubunit()
        fmt._emit_exists = lambda test: fmt.calls.append(("exists", test.id()))

        # Bind the patched method.
        bound = patches._subunit_v2_list_of_tests.__get__(fmt, type(fmt))
        bound([FakeTestCase("a.b.C.test1")], "UnitTests")

        self.assertEqual(fmt.calls[0], ("enter", "UnitTests"))
        self.assertEqual(fmt.calls[1][0], "startTest")
        self.assertEqual(fmt.calls[2][0], "exists")
        self.assertEqual(fmt.calls[3][0], "stopTest")
        self.assertEqual(fmt.calls[4], ("exit", "UnitTests"))


class TestApplyPatches(TestCase):
    layer = BaseLayer

    def test_apply_patches_sets_functions(self):
        from zope.testrunner import find, formatter

        # Save originals so we can restore them.
        originals = {
            "find.tests_from_suite": find.tests_from_suite,
            "OutputFormatter.getShortDescription": (
                formatter.OutputFormatter.getShortDescription
            ),
            "OutputFormatter.list_of_tests": (
                formatter.OutputFormatter.list_of_tests
            ),
            "OutputFormatter.start_test": formatter.OutputFormatter.start_test,
            "SubunitOutputFormatter.list_of_tests": (
                formatter.SubunitOutputFormatter.list_of_tests
            ),
            "SubunitOutputFormatter.test_threads": (
                formatter.SubunitOutputFormatter.test_threads
            ),
            "SubunitV2OutputFormatter.list_of_tests": (
                formatter.SubunitV2OutputFormatter.list_of_tests
            ),
        }
        try:
            patches.apply_patches()
            self.assertIs(find.tests_from_suite, patches._tests_from_suite)
            self.assertIs(
                formatter.OutputFormatter.getShortDescription,
                patches._getShortDescription,
            )
            self.assertIs(
                formatter.OutputFormatter.list_of_tests,
                patches._list_of_tests,
            )
            self.assertIs(
                formatter.OutputFormatter.start_test,
                patches._start_test,
            )
            self.assertIs(
                formatter.SubunitOutputFormatter.list_of_tests,
                patches._subunit_list_of_tests,
            )
            self.assertIs(
                formatter.SubunitOutputFormatter.test_threads,
                patches._subunit_test_threads,
            )
            self.assertIs(
                formatter.SubunitV2OutputFormatter.list_of_tests,
                patches._subunit_v2_list_of_tests,
            )
        finally:
            # Restore originals so other tests see unpatched behaviour.
            find.tests_from_suite = originals["find.tests_from_suite"]
            formatter.OutputFormatter.getShortDescription = originals[
                "OutputFormatter.getShortDescription"
            ]
            formatter.OutputFormatter.list_of_tests = originals[
                "OutputFormatter.list_of_tests"
            ]
            formatter.OutputFormatter.start_test = originals[
                "OutputFormatter.start_test"
            ]
            formatter.SubunitOutputFormatter.list_of_tests = originals[
                "SubunitOutputFormatter.list_of_tests"
            ]
            formatter.SubunitOutputFormatter.test_threads = originals[
                "SubunitOutputFormatter.test_threads"
            ]
            formatter.SubunitV2OutputFormatter.list_of_tests = originals[
                "SubunitV2OutputFormatter.list_of_tests"
            ]
