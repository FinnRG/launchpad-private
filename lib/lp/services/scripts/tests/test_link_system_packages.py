# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for linking system packages into the virtualenv."""

import subprocess
from pathlib import Path

from lp.services.config import config
from lp.testing import TestCase
from lp.testing.layers import BaseLayer


class TestLinkSystemPackages(TestCase):
    layer = BaseLayer

    def runLinker(self, modules):
        directory = Path(self.makeTemporaryDirectory())
        target = directory / "site-packages"
        target.mkdir()
        module_file = directory / "modules"
        module_file.write_text(modules)
        result = subprocess.run(
            [
                "/usr/bin/python3",
                str(Path(config.root) / "utilities/link-system-packages.py"),
                str(target),
                str(module_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result, target

    def test_link_system_module_with_marker(self):
        result, target = self.runLinker('apt_pkg; python_version >= "3.8"\n')
        self.assertEqual(0, result.returncode, result.stderr)
        links = list(target.glob("apt_pkg*.so"))
        self.assertEqual(1, len(links))
        self.assertTrue(links[0].is_symlink())
        self.assertTrue(links[0].exists())

    def test_skip_module_with_false_marker(self):
        result, target = self.runLinker(
            'missing_module; python_version < "3.8"\n'
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], list(target.iterdir()))

    def test_invalid_line(self):
        result, target = self.runLinker("(\n")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Parse error", result.stderr)
        self.assertEqual([], list(target.iterdir()))
