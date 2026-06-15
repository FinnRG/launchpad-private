# Copyright 2013-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Mock Swift test fixture."""

__all__ = ["SwiftFixture"]

import os.path
import shutil
import socket
import tempfile
from textwrap import dedent

import testtools.content
import testtools.content_type
from fixtures import FunctionFixture
from swiftclient import client as swiftclient
from txfixtures.tachandler import TacTestFixture

from lp.services.config import config
from lp.services.librarianserver import swift
from lp.testing.layers import BaseLayer
from lp.testing.swift import fakeswift


class SwiftFixture(TacTestFixture):
    tacfile = os.path.join(os.path.dirname(__file__), "fakeswift.tac")
    pidfile = None
    logfile = None
    root = None
    daemon_port = None

    def __init__(self, old_instance=False):
        super().__init__()
        self.old_instance = old_instance

    def _getConfig(self, key):
        return getattr(
            config.librarian_server, "old_" + key if self.old_instance else key
        )

    def setUp(self, spew=False, umask=None):
        # Pick a random, free port.
        if self.daemon_port is None:
            sock = socket.socket()
            sock.bind(("", 0))
            self.daemon_port = sock.getsockname()[1]
            sock.close()
            self.logfile = os.path.join(
                config.root, "logs", "fakeswift-%s.log" % self.daemon_port
            )
            self.pidfile = os.path.join(
                config.root, "logs", "fakeswift-%s.pid" % self.daemon_port
            )
        assert self.daemon_port is not None

        super().setUp(
            spew,
            umask,
            os.path.join(config.root, "env", "bin", "python"),
            os.path.join(config.root, "bin", "twistd"),
        )

        logfile = self.logfile
        self.addCleanup(lambda: os.path.exists(logfile) and os.unlink(logfile))

        testtools.content.attach_file(
            self,
            logfile,
            "swift-log",
            testtools.content_type.UTF8_TEXT,
            buffer_now=False,
        )

        self.addCleanup(swift.reconfigure_connection_pools)
        service_config = dedent(
            """\
            [librarian_server]
            {prefix}os_auth_url: http://localhost:{port}/keystone/v2.0/
            {prefix}os_username: {username}
            {prefix}os_password: {password}
            {prefix}os_tenant_name: {tenant_name}
            """.format(
                prefix=("old_" if self.old_instance else ""),
                port=self.daemon_port,
                username=fakeswift.DEFAULT_USERNAME,
                password=fakeswift.DEFAULT_PASSWORD,
                tenant_name=fakeswift.DEFAULT_TENANT_NAME,
            )
        )
        BaseLayer.config_fixture.add_section(service_config)
        config.reloadConfig()
        self.addCleanup(config.reloadConfig)
        self.addCleanup(
            BaseLayer.config_fixture.remove_section, service_config
        )
        assert self._getConfig("os_tenant_name") == "test"
        swift.reconfigure_connection_pools()

    def setUpRoot(self):
        # Create a root directory.
        if self.root is None or not os.path.isdir(self.root):
            root_fixture = FunctionFixture(tempfile.mkdtemp, shutil.rmtree)
            self.useFixture(root_fixture)
            self.root = root_fixture.fn_result
            os.chmod(self.root, 0o700)
        assert os.path.isdir(self.root)

        # Pass on options to the daemon.
        os.environ["SWIFT_ROOT"] = self.root
        os.environ["SWIFT_PORT"] = str(self.daemon_port)

    def connect(self, **kwargs):
        """Return a valid connection to our mock Swift"""
        connection_kwargs = {
            "authurl": self._getConfig("os_auth_url"),
            "auth_version": self._getConfig("os_auth_version"),
            "tenant_name": self._getConfig("os_tenant_name"),
            "user": self._getConfig("os_username"),
            "key": self._getConfig("os_password"),
            "retries": 0,
            "insecure": True,
        }
        connection_kwargs.update(kwargs)
        return swiftclient.Connection(**connection_kwargs)

    def startup(self):
        self.setUp()

    def shutdown(self):
        self.cleanUp()
