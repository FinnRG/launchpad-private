# Copyright 2010-2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the testing module."""

import os
from unittest.mock import Mock

from lp.services.config import config
from lp.services.features import getFeatureFlag, uninstall_feature_controller
from lp.testing import (
    TestCase,
    YUIUnitTestCase,
    feature_flags,
    set_feature_flag,
    yuixhr,
)
from lp.testing.html5browser import Browser, Results
from lp.testing.layers import (
    BaseLayer,
    DatabaseFunctionalLayer,
    WebBrowserLayer,
    YUIAppServerLayer,
    YUITestLayer,
)


class TestFeatureFlags(TestCase):
    layer = DatabaseFunctionalLayer

    def test_set_feature_flags_raises_if_not_available(self):
        """set_feature_flags raises an error if there is no feature
        controller available for the current thread.
        """
        # Remove any existing feature controller for the sake of this
        # test (other tests will re-add it). This prevents weird
        # interactions in a parallel test environment.
        uninstall_feature_controller()
        self.assertRaises(AssertionError, set_feature_flag, "name", "value")

    def test_flags_set_within_feature_flags_context(self):
        """In the feature_flags context, set/get works."""
        self.useContext(feature_flags())
        set_feature_flag("name", "value")
        self.assertEqual("value", getFeatureFlag("name"))

    def test_flags_unset_outside_feature_flags_context(self):
        """get fails when used outside the feature_flags context."""
        with feature_flags():
            set_feature_flag("name", "value")
        self.assertIs(None, getFeatureFlag("name"))


class TestYUIUnitTestCase(TestCase):
    def test_id(self):
        test = YUIUnitTestCase()
        test.initialize("foo/bar/baz.html")
        self.assertEqual(test.test_path, test.id())

    def test_id_is_normalized_and_relative_to_root(self):
        test = YUIUnitTestCase()
        test_path = os.path.join(config.root, "../bar/baz/../bob.html")
        test.initialize(test_path)
        self.assertEqual("../bar/bob.html", test.id())


class TestYUIBrowserSelection(TestCase):
    layer = BaseLayer

    def setUp(self):
        super().setUp()
        self.browser = Mock(spec=Browser)
        self.browser.run_tests.return_value = Results(
            Results.Status.SUCCESS,
            results={
                "type": "complete",
                "results": {
                    "suite": {
                        "type": "testcase",
                        "test": {
                            "type": "test",
                            "result": "pass",
                            "message": "",
                        },
                    },
                },
            },
        )

    def assertBrowserUsed(self, test):
        test.checkResults()
        self.browser.run_tests.assert_called_once_with(
            test.html_uri,
            timeout=test.suite_timeout,
            incremental_timeout=test.incremental_timeout,
        )

    def test_unit_tests_inherit_shared_browser(self):
        self.patch(WebBrowserLayer, "browser", self.browser)
        test = YUIUnitTestCase()
        test.layer = YUITestLayer
        test.initialize("foo/bar.html")

        self.assertBrowserUsed(test)

    def test_appserver_tests_use_own_browser(self):
        self.patch(WebBrowserLayer, "browser", None)
        self.patch(YUIAppServerLayer, "browser", self.browser)
        test = yuixhr.YUIAppServerTestCase(
            "lp.testing.tests.test_yuixhr_fixture"
        )
        test.html_uri = "http://launchpad.test/+yuitest/" + test.module_name

        self.assertBrowserUsed(test)
