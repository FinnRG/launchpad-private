# Copyright 2009-2011 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for SSH session support on the codehosting SSH server."""

from twisted.conch.interfaces import ISession
from twisted.conch.ssh import connection
from twisted.internet.process import ProcessExitedAlready
from twisted.internet.protocol import ProcessProtocol

from lp.codehosting import get_BRZ_PLUGIN_PATH_for_subprocess
from lp.codehosting.sshserver.daemon import CodehostingAvatar
from lp.codehosting.sshserver.session import (
    ExecOnlySession,
    ForbiddenCommand,
    RestrictedExecOnlySession,
    lookup_command_template,
)
from lp.codehosting.tests.helpers import AvatarTestCase
from lp.services.config import config
from lp.testing import TestCase


class MockReactor:
    """Mock reactor used to check that ExecOnlySession asks the reactor to
    spawn processes.
    """

    def __init__(self):
        self.log = []

    def spawnProcess(
        self,
        protocol,
        executable,
        args,
        env=None,
        path=None,
        uid=None,
        gid=None,
        usePTY=0,
        childFDs=None,
    ):
        self.log.append(
            (protocol, executable, args, env, path, uid, gid, usePTY, childFDs)
        )
        return MockProcessTransport(executable)

    def addReader(self, reader):
        self.log.append(("addReader", reader))


class MockSSHSession:
    """Just enough of SSHSession to allow checking of reporting to stderr."""

    def __init__(self, log):
        self.log = log

    def writeExtended(self, channel, data):
        self.log.append(("writeExtended", channel, data))


class MockProcessTransport:
    """Mock transport used to fake speaking with child processes that are
    mocked out in tests.
    """

    def __init__(self, executable):
        self._executable = executable
        self.log = []
        self.session = MockSSHSession(self.log)
        self.status = None

    def closeStdin(self):
        self.log.append(("closeStdin",))

    def loseConnection(self):
        self.log.append(("loseConnection",))

    def childConnectionLost(self, childFD, reason=None):
        self.log.append(("childConnectionLost", childFD, reason))

    def signalProcess(self, signal):
        if self._executable == b"raise-os-error":
            raise OSError()
        if self._executable == b"already-terminated":
            raise ProcessExitedAlready()
        self.log.append(("signalProcess", signal))

    def write(self, data):
        self.log.append(("write", data))

    def processEnded(self, status):
        self.log.append(("processEnded", status))


class TestExecOnlySession(AvatarTestCase):
    """Tests for ExecOnlySession.

    Conch delegates responsibility for executing commands to an object that
    implements ISession. The smart server only needs to handle `execCommand`
    and a couple of other book-keeping methods. The methods that relate to
    running a shell or creating a pseudo-terminal raise NotImplementedErrors.
    """

    def setUp(self):
        AvatarTestCase.setUp(self)
        self.avatar = CodehostingAvatar(self.aliceUserDict, None)
        # The logging system will try to get the id of avatar.transport, so
        # let's give it something to take the id of.
        self.avatar.transport = object()
        self.reactor = MockReactor()
        self.session = ExecOnlySession(self.avatar, self.reactor)

    def test_getPtyIsANoOp(self):
        # getPty is called on the way to establishing a shell. Since we don't
        # give out shells, it should be a no-op. Raising an exception would
        # log an OOPS, so we won't do that.
        self.assertEqual(None, self.session.getPty(None, None, None))

    def test_openShellNotImplemented(self):
        # openShell closes the connection.
        protocol = MockProcessTransport(b"bash")
        self.session.openShell(protocol)
        self.assertEqual(
            [
                (
                    "writeExtended",
                    connection.EXTENDED_DATA_STDERR,
                    "No shells on this server.\r\n",
                ),
                ("loseConnection",),
            ],
            protocol.log,
        )

    def test_windowChangedNotImplemented(self):
        # windowChanged raises a NotImplementedError. It doesn't matter what
        # we pass it.
        self.assertRaises(
            NotImplementedError, self.session.windowChanged, None
        )

    def test_providesISession(self):
        # ExecOnlySession must provide ISession.
        self.assertTrue(
            ISession.providedBy(self.session),
            "ExecOnlySession doesn't implement ISession",
        )

    def test_closedDoesNothingWhenNoCommand(self):
        # When no process has been created, 'closed' is a no-op.
        self.assertEqual(None, self.session._transport)
        self.session.closed()
        self.assertEqual(None, self.session._transport)

    def test_closedTerminatesProcessAndDisconnects(self):
        # ExecOnlySession provides a 'closed' method that is generally
        # responsible for killing the child process and cleaning things up.
        # From the outside, it just looks like a successful no-op. From the
        # inside, it tells the process transport to end the connection between
        # the SSH server and the child process.
        protocol = ProcessProtocol()
        self.session.execCommand(protocol, b"cat /etc/hostname")
        self.session.closed()
        self.assertEqual(
            [("signalProcess", "HUP"), ("loseConnection",)],
            self.session._transport.log,
        )

    def test_closedDisconnectsIfProcessCantBeTerminated(self):
        # 'closed' still calls 'loseConnection' on the transport, even if the
        # OS raises an error when we try to SIGHUP the process.
        protocol = ProcessProtocol()
        # MockTransport will raise an OSError on signalProcess if the executed
        # command is 'raise-os-error'.
        self.session.execCommand(protocol, b"raise-os-error")
        self.session.closed()
        self.assertEqual([("loseConnection",)], self.session._transport.log)

    def test_closedDisconnectsIfProcessAlreadyTerminated(self):
        # 'closed' still calls 'loseConnection' on the transport, even if the
        # process is already terminated
        protocol = ProcessProtocol()
        # MockTransport will raise a ProcessExitedAlready on signalProcess if
        # the executed command is 'already-terminated'.
        self.session.execCommand(protocol, b"already-terminated")
        self.session.closed()
        self.assertEqual([("loseConnection",)], self.session._transport.log)

    def test_getCommandToRunSplitsCommandLine(self):
        # getCommandToRun takes a command line and splits it into the name of
        # an executable to run and a sequence of arguments.
        command = b"cat foo bar"
        executable, arguments = self.session.getCommandToRun(command)
        self.assertEqual(b"cat", executable)
        self.assertEqual([b"cat", b"foo", b"bar"], list(arguments))

    def test_execCommandSpawnsProcess(self):
        # ExecOnlySession.execCommand spawns the appropriate process.
        protocol = ProcessProtocol()
        command = b"cat /etc/hostname"
        self.session.execCommand(protocol, command)
        executable, arguments = self.session.getCommandToRun(command)
        self.assertEqual(
            [
                (
                    protocol,
                    executable,
                    arguments,
                    None,
                    None,
                    None,
                    None,
                    0,
                    None,
                )
            ],
            self.reactor.log,
        )

    def test_eofReceivedDoesNothingWhenNoCommand(self):
        # When no process has been created, 'eofReceived' is a no-op.
        self.assertEqual(None, self.session._transport)
        self.session.eofReceived()
        self.assertEqual(None, self.session._transport)

    def test_eofReceivedClosesStdin(self):
        # 'eofReceived' closes standard input when called while a command is
        # running.
        protocol = ProcessProtocol()
        self.session.execCommand(protocol, b"cat /etc/hostname")
        self.session.eofReceived()
        self.assertEqual([("closeStdin",)], self.session._transport.log)

    def test_getAvatarAdapter(self):
        # getAvatarAdapter is a convenience classmethod so that
        # ExecOnlySession can be easily registered as an adapter for Conch
        # avatars.
        from twisted.internet import reactor

        adapter = ExecOnlySession.getAvatarAdapter()
        session = adapter(self.avatar)
        self.assertTrue(
            isinstance(session, ExecOnlySession),
            "ISession(avatar) doesn't adapt to ExecOnlySession. "
            "Got %r instead." % (session,),
        )
        self.assertIs(self.avatar, session.avatar)
        self.assertIs(reactor, session.reactor)

    def test_environment(self):
        # The environment for the executed process can be specified in the
        # ExecOnlySession constructor.
        session = ExecOnlySession(
            self.avatar, self.reactor, environment={"FOO": "BAR"}
        )
        protocol = ProcessProtocol()
        session.execCommand(protocol, b"yes")
        self.assertEqual({"FOO": "BAR"}, session.environment)
        self.assertEqual(
            [
                (
                    protocol,
                    b"yes",
                    [b"yes"],
                    {"FOO": "BAR"},
                    None,
                    None,
                    None,
                    0,
                    None,
                )
            ],
            self.reactor.log,
        )

    def test_environmentInGetAvatarAdapter(self):
        # We can pass the environment into getAvatarAdapter so that it is used
        # when we adapt the session.
        adapter = ExecOnlySession.getAvatarAdapter(environment={"FOO": "BAR"})
        session = adapter(self.avatar)
        self.assertEqual({"FOO": "BAR"}, session.environment)


class TestRestrictedExecOnlySession(AvatarTestCase):
    """Tests for RestrictedExecOnlySession.

    bzr+ssh requests to the code hosting SSH server ask the server to execute
    a particular command: 'bzr serve --inet /'. The SSH server rejects all
    other commands.

    When it receives the expected command, the SSH server doesn't actually
    execute the exact given command. Instead, it executes another pre-defined
    command.
    """

    def setUp(self):
        AvatarTestCase.setUp(self)
        self.avatar = CodehostingAvatar(self.aliceUserDict, None)
        self.reactor = MockReactor()

        def lookup_template(command):
            if command == "foo":
                return "bar baz %(user_id)s"
            raise ForbiddenCommand("Not allowed to execute %r." % command)

        self.session = RestrictedExecOnlySession(
            self.avatar, self.reactor, lookup_template
        )

    def test_makeRestrictedExecOnlySession(self):
        # A RestrictedExecOnlySession is constructed with an avatar, a reactor
        # and an expected command.
        self.assertTrue(
            isinstance(self.session, RestrictedExecOnlySession),
            "%r not an instance of RestrictedExecOnlySession"
            % (self.session,),
        )
        self.assertEqual(self.avatar, self.session.avatar)
        self.assertEqual(self.reactor, self.session.reactor)
        self.assertEqual(
            "bar baz %(user_id)s", self.session.lookup_command_template("foo")
        )
        self.assertRaises(
            ForbiddenCommand, self.session.lookup_command_template, "notfoo"
        )

    def test_execCommandRejectsUnauthorizedCommands(self):
        # execCommand rejects all commands except for the command specified in
        # the constructor and closes the connection.

        # Note that Conch doesn't have a well-defined way of rejecting
        # commands. Disconnecting in execCommand will do. We don't raise
        # an exception to avoid logging an OOPS.
        protocol = MockProcessTransport(b"cat")
        self.assertEqual(None, self.session.execCommand(protocol, b"cat"))
        self.assertEqual(
            [
                (
                    "writeExtended",
                    connection.EXTENDED_DATA_STDERR,
                    "Not allowed to execute %r.\r\n" % b"cat",
                ),
                ("loseConnection",),
            ],
            protocol.log,
        )

    def test_getCommandToRunReturnsTemplateCommand(self):
        # When passed the allowed command, getCommandToRun always returns the
        # executable and arguments corresponding to the provided executed
        # command template.
        executable, arguments = self.session.getCommandToRun("foo")
        self.assertEqual(b"bar", executable)
        self.assertEqual(
            [b"bar", b"baz", str(self.avatar.user_id).encode("UTF-8")],
            list(arguments),
        )

    def test_getAvatarAdapter(self):
        # getAvatarAdapter is a convenience classmethod so that
        # RestrictedExecOnlySession can be easily registered as an adapter for
        # Conch avatars.
        from twisted.internet import reactor

        def lookup_template(command):
            if command == "foo":
                return "bar baz"
            raise ForbiddenCommand(command)

        adapter = RestrictedExecOnlySession.getAvatarAdapter(lookup_template)
        session = adapter(self.avatar)
        self.assertTrue(
            isinstance(session, RestrictedExecOnlySession),
            "ISession(avatar) doesn't adapt to RestrictedExecOnlySession. "
            "Got %r instead." % (session,),
        )
        self.assertIs(self.avatar, session.avatar)
        self.assertIs(reactor, session.reactor)
        self.assertEqual("bar baz", session.lookup_command_template("foo"))
        self.assertRaises(
            ForbiddenCommand, session.lookup_command_template, "notfoo"
        )


class TestSessionIntegration(AvatarTestCase):
    """Tests for how Conch sessions integrate with the rest of codehosting."""

    def setUp(self):
        AvatarTestCase.setUp(self)
        self.avatar = CodehostingAvatar(self.aliceUserDict, None)

    def test_avatarAdaptsToRestrictedExecOnlySession(self):
        # When Conch tries to adapt the SSH server avatar to ISession, it
        # adapts to a RestrictedExecOnlySession. This means that a
        # RestrictedExecOnlySession handles any requests to execute a command.
        session = ISession(self.avatar)
        self.assertTrue(
            isinstance(session, RestrictedExecOnlySession),
            "ISession(avatar) doesn't adapt to ExecOnlySession. "
            "Got %r instead." % (session,),
        )
        self.assertEqual(
            get_BRZ_PLUGIN_PATH_for_subprocess(),
            session.environment["BRZ_PLUGIN_PATH"],
        )
        self.assertEqual(
            "%s@bazaar.launchpad.test" % self.avatar.username,
            session.environment["BRZ_EMAIL"],
        )

        executable, arguments = session.getCommandToRun(
            b"bzr serve --inet --directory=/ --allow-writes"
        )
        interpreter = ("%s/bin/py" % config.root).encode("UTF-8")
        self.assertEqual(interpreter, executable)
        self.assertEqual(
            [
                interpreter,
                b"-m",
                b"breezy",
                b"lp-serve",
                b"--inet",
                str(self.avatar.user_id).encode("UTF-8"),
            ],
            list(arguments),
        )
        self.assertRaises(
            ForbiddenCommand, session.getCommandToRun, b"rm -rf /"
        )


class TestLookupCommand(TestCase):
    def test_other(self):
        self.assertRaises(ForbiddenCommand, lookup_command_template, "foo")

    def test_bzr(self):
        self.assertEqual(
            config.root + "/bin/py -m breezy lp-serve --inet %(user_id)s",
            lookup_command_template(
                b"bzr serve --inet --directory=/ --allow-writes"
            ),
        )

    def test_brz(self):
        self.assertEqual(
            config.root + "/bin/py -m breezy lp-serve --inet %(user_id)s",
            lookup_command_template(
                b"brz serve --inet --directory=/ --allow-writes"
            ),
        )
