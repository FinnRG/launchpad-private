# Copyright 2022-2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from fixtures import MockPatch

from lp.testing import TestCase
from lp.testing.html5browser import Browser
from lp.testing.layers import BaseLayer, WebBrowserLayer


class TestBrowserInitialization(TestCase):
    layer = BaseLayer

    def setUp(self):
        super().setUp()
        self.firefox = self.useFixture(
            MockPatch("lp.testing.html5browser.webdriver.Firefox")
        ).mock
        self.profile = self.useFixture(
            MockPatch("lp.testing.html5browser.webdriver.FirefoxProfile")
        ).mock

    def test_native_firefox(self):
        with TemporaryDirectory() as directory:
            self.useFixture(
                MockPatch(
                    "lp.testing.html5browser.Path",
                    return_value=Path(directory) / "missing",
                )
            )
            browser = Browser()

        self.firefox.assert_called_once()
        kwargs = self.firefox.call_args.kwargs
        self.assertEqual("geckodriver", kwargs["executable_path"])
        self.assertIsNone(kwargs["options"].binary)
        self.assertTrue(kwargs["options"].headless)
        self.assertEqual(
            "none", kwargs["options"].capabilities["pageLoadStrategy"]
        )
        self.assertIs(self.profile.return_value, kwargs["firefox_profile"])
        self.profile.return_value.set_preference.assert_called_once_with(
            "dom.disable_open_during_load", False
        )
        self.assertIs(self.firefox.return_value, browser.driver)

    def test_snap_firefox_uses_same_revision_for_both_binaries(self):
        with TemporaryDirectory() as directory:
            revision = Path(directory) / "1234"
            revision.mkdir()
            current = Path(directory) / "current"
            current.symlink_to(revision, target_is_directory=True)
            self.useFixture(
                MockPatch("lp.testing.html5browser.Path", return_value=current)
            )
            browser = Browser()

        self.firefox.assert_called_once()
        kwargs = self.firefox.call_args.kwargs
        self.assertEqual(
            str(revision / "geckodriver"), kwargs["executable_path"]
        )
        self.assertEqual(
            str(revision / "firefox"), kwargs["options"].binary_location
        )
        self.assertIs(self.firefox.return_value, browser.driver)


class TestBrowser(TestCase):
    """Verify Browser methods."""

    layer = WebBrowserLayer

    def setUp(self):
        super().setUp()
        self.file = NamedTemporaryFile(
            mode="w+", prefix="html5browser_", suffix=".html"
        )
        self.file.write(
            """
            <html><head>
            <script type="text/javascript">
            window.onload = function() {
                // First test
                setTimeout(function() {
                    window.top.test_results = JSON.stringify({
                        testCase: "first",
                        testName: "first",
                        type: "passed"
                    });
                    // Second test
                    setTimeout(function() {
                        window.top.test_results = JSON.stringify({
                            testCase: "second",
                            testName: "second",
                            type: "passed"
                        });
                        // Final results
                        setTimeout(function() {
                            window.top.test_results = JSON.stringify({
                                results: {"spam": "ham"},
                                type: "complete"
                            });
                        }, 2000);
                    }, 2000);
                }, 1000);
            };
            </script>
            </head><body></body></html>
        """
        )
        self.file.flush()
        self.file_uri = f"file://{self.file.name}"
        self.addCleanup(self.file.close)

    def test_load_test_results(self):
        results = self.layer.browser.run_tests(self.file_uri, timeout=10000)
        self.assertEqual(results.status, results.Status.SUCCESS)
        self.assertEqual(
            results.results,
            {
                "type": "complete",
                "results": {"spam": "ham"},
            },
        )

    def test_timeout_error(self):
        results = self.layer.browser.run_tests(self.file_uri, timeout=1500)
        self.assertEqual(results.status, results.Status.TIMEOUT)
        self.assertIsNone(results.results)
        self.assertEqual(
            {"testCase": "first", "testName": "first", "type": "passed"},
            results.last_test_message,
        )

    def test_incremental_timeout_success(self):
        results = self.layer.browser.run_tests(
            self.file_uri, timeout=10000, incremental_timeout=3000
        )
        self.assertEqual(results.status, results.Status.SUCCESS)
        self.assertEqual(
            {
                "type": "complete",
                "results": {"spam": "ham"},
            },
            results.results,
        )

    def test_incremental_timeout_error(self):
        results = self.layer.browser.run_tests(
            self.file_uri, timeout=10000, incremental_timeout=1500
        )
        self.assertEqual(results.status, results.Status.TIMEOUT)
        self.assertIsNone(results.results)
        self.assertEqual(
            {"testCase": "first", "testName": "first", "type": "passed"},
            results.last_test_message,
        )
