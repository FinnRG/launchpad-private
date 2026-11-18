# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for test layers."""

import subprocess
import sys
import threading
from typing import List
from unittest.mock import patch

from lp.services.config import config
from lp.testing import TestCase
from lp.testing.layers import (
    BaseLayer,
    LayerIsolationError,
    LayerProcessController,
)

__all__: List[str] = []


class TestThreadWaiting(TestCase):
    layer = BaseLayer

    def test_slow_thread(self):
        # BaseLayer waits a few seconds for threads spawned by the test
        # to shutdown. Test this is working by creating a thread that
        # will shut itself down in 0.5 seconds time.
        t = threading.Timer(0.5, lambda: None)
        t.start()

    def test_disabled_thread_check(self):
        # Confirm the BaseLayer.disable_thread_check code path works.
        BaseLayer.disable_thread_check = True


class TestAppServerOutput(TestCase):
    layer = BaseLayer

    def setUp(self):
        super().setUp()
        self.patch(LayerProcessController, "appserver", None)
        self.patch(LayerProcessController, "appserver_output", None)
        self.patch(LayerProcessController, "appserver_config", config)
        self.addCleanup(LayerProcessController.stopAppServer)

    def test_large_output_does_not_block(self):
        popen = subprocess.Popen
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'x' * 262144)",
        ]
        with patch(
            "lp.testing.layers.subprocess.Popen",
            side_effect=lambda *args, **kwargs: popen(command, **kwargs),
        ):
            LayerProcessController._runAppServer("run-testapp")
        process = LayerProcessController.appserver
        self.assertEqual(0, process.wait(timeout=10))
        self.assertEqual(
            b"x" * 262144, LayerProcessController._readAppServerOutput()
        )
        self.assertRaises(
            LayerIsolationError, LayerProcessController.postTestInvariants
        )
        output = LayerProcessController.appserver_output
        LayerProcessController.stopAppServer()
        self.assertTrue(output.closed)
        self.assertIsNone(LayerProcessController.appserver_output)

    def test_failed_start_closes_output(self):
        with patch(
            "lp.testing.layers.subprocess.Popen",
            side_effect=OSError("cannot start app server"),
        ) as popen:
            self.assertRaises(
                OSError, LayerProcessController._runAppServer, "run-testapp"
            )
        self.assertTrue(popen.call_args.kwargs["stdout"].closed)
        self.assertIsNone(LayerProcessController.appserver_output)
