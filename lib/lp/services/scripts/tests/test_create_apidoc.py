# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for API documentation worker handling."""

import runpy
import sys
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from lp.services.config import config
from lp.systemhomes import WebServiceApplication
from lp.testing import TestCase
from lp.testing.layers import BaseLayer


class TestCreateApidoc(TestCase):
    layer = BaseLayer

    def runGeneration(self, exitcodes):
        # The test runner has already initialized the environment.
        with patch.dict(
            sys.modules, {"_pythonpath": ModuleType("_pythonpath")}
        ):
            script = runpy.run_path(
                str(
                    Path(config.root)
                    / "utilities/create-lp-wadl-and-apidoc.py"
                )
            )
        main = script["main"]
        workers = [Mock(exitcode=code) for code in exitcodes]
        context = Mock()
        context.Process.side_effect = workers
        get_context = Mock(return_value=context)
        replacements = {
            "get_context": get_context,
            "execute_zcml_for_scripts": Mock(),
            "getUtility": Mock(
                return_value=SimpleNamespace(
                    active_versions=("beta", "1.0", "devel")
                )
            ),
            "PageTemplateFile": Mock(return_value=Mock(return_value="")),
            "versioninfo": SimpleNamespace(date="2026-11-22 12:00:00 +0000"),
        }
        self.patch(WebServiceApplication, "cached_wadl", None)
        stderr = StringIO()
        with patch.dict(main.__globals__, replacements):
            with redirect_stderr(stderr):
                result = main(self.makeTemporaryDirectory())
        get_context.assert_called_once_with("fork")
        for worker in workers:
            worker.start.assert_called_once_with()
            worker.join.assert_called_once_with()
        return result, stderr.getvalue()

    def test_successful_workers(self):
        result, stderr = self.runGeneration((0, 0, 0))
        self.assertEqual(0, result)
        self.assertEqual("", stderr)

    def test_failed_workers(self):
        result, stderr = self.runGeneration((0, 1, -15))
        self.assertEqual(1, result)
        self.assertIn("worker exited with status 1", stderr)
        self.assertIn("worker exited with status -15", stderr)
