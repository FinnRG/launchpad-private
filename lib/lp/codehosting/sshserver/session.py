# Copyright 2009-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""SSH session implementations for the codehosting SSH server."""

__all__ = [
    "launch_smart_server",
]

import os
from urllib.parse import urlparse

import six
from lazr.sshserver.events import AvatarEvent
from lazr.sshserver.session import DoNothingSession
from twisted.internet import process
from twisted.python import log
from zope.event import notify

from lp.services.config import config


class BazaarSSHStarted(AvatarEvent):
    template = "[%(session_id)s] %(username)s started bzr+ssh session."


class BazaarSSHClosed(AvatarEvent):
    template = "[%(session_id)s] %(username)s closed bzr+ssh session."


class ForbiddenCommand(Exception):
    """Raised when a session is asked to execute a forbidden command."""


class ExecOnlySession(DoNothingSession):
    """Conch session that only allows executing commands."""

    def __init__(self, avatar, reactor, environment=None):
        super().__init__(avatar)
        self.reactor = reactor
        self.environment = environment
        self._transport = None

    @classmethod
    def getAvatarAdapter(klass, environment=None):
        from twisted.internet import reactor

        return lambda avatar: klass(avatar, reactor, environment)

    def closed(self):
        """See ISession."""
        if self._transport is not None:
            # XXX: JonathanLange 2010-04-15: This is something of an
            # abstraction violation. Apart from this line and its twin, this
            # class knows nothing about Bazaar.
            notify(BazaarSSHClosed(self.avatar))
            try:
                self._transport.signalProcess("HUP")
            except (OSError, process.ProcessExitedAlready):
                pass
            self._transport.loseConnection()

    def eofReceived(self):
        """See ISession."""
        if self._transport is not None:
            self._transport.closeStdin()

    def execCommand(self, protocol, command):
        """Executes `command` using `protocol` as the ProcessProtocol.

        See ISession.

        :param protocol: a ProcessProtocol, usually SSHSessionProcessProtocol.
        :param command: A whitespace-separated command line. The first token is
        used as the name of the executable, the rest are used as arguments.
        """
        try:
            executable, arguments = self.getCommandToRun(command)
        except ForbiddenCommand as e:
            self.errorWithMessage(protocol, str(e) + "\r\n")
            return
        log.msg("Running: %r, %r" % (executable, arguments))
        if self._transport is not None:
            log.err(
                "ERROR: %r already running a command on transport %r"
                % (self, self._transport)
            )
        # XXX: JonathanLange 2008-12-23: This is something of an abstraction
        # violation. Apart from this line and its twin, this class knows
        # nothing about Bazaar.
        notify(BazaarSSHStarted(self.avatar))
        self._transport = self._spawn(
            protocol, executable, arguments, env=self.environment
        )

    def _spawn(self, protocol, executable, arguments, env):
        return self.reactor.spawnProcess(
            protocol, executable, arguments, env=env
        )

    def getCommandToRun(self, command):
        """Return the command that will actually be run given `command`.

        :param command: A command line to run.
        :return: `(executable, arguments)` where `executable` is the name of an
            executable and arguments is a sequence of command-line arguments
            with the name of the executable as the first value.
        """
        args = six.ensure_binary(command).split()
        return args[0], args


class RestrictedExecOnlySession(ExecOnlySession):
    """Conch session that only allows specific commands to be executed."""

    def __init__(
        self, avatar, reactor, lookup_command_template, environment=None
    ):
        """Construct a RestrictedExecOnlySession.

        :param avatar: See `ExecOnlySession`.
        :param reactor: See `ExecOnlySession`.
        :param lookup_command_template: Lookup the template for a command.
            A template is a Python format string for the actual command that
            will be run.  '%(user_id)s' will be replaced with the 'user_id'
            attribute of the current avatar. Should raise
            ForbiddenCommand if the command is not allowed.
        """
        ExecOnlySession.__init__(self, avatar, reactor, environment)
        self.lookup_command_template = lookup_command_template

    @classmethod
    def getAvatarAdapter(klass, lookup_command_template, environment=None):
        from twisted.internet import reactor

        return lambda avatar: klass(
            avatar, reactor, lookup_command_template, environment
        )

    def getCommandToRun(self, command):
        """As in ExecOnlySession, but only allow a particular command.

        :raise ForbiddenCommand: when `command` is not the allowed command.
        """
        executed_command_template = self.lookup_command_template(command)
        return ExecOnlySession.getCommandToRun(
            self, executed_command_template % {"user_id": self.avatar.user_id}
        )


def lookup_command_template(command):
    """Map a command to a command template.

    :param command: Command requested by the user
    :return: Command template
    :raise ForbiddenCommand: Raised when command isn't allowed
    """
    python_command = "%s/bin/py -m breezy" % config.root
    args = " lp-serve --inet %(user_id)s"
    command_template = python_command + args

    if command in (
        b"bzr serve --inet --directory=/ --allow-writes",
        b"brz serve --inet --directory=/ --allow-writes",
    ):
        return command_template
    # At the moment, only bzr/brz branch serving is allowed.
    raise ForbiddenCommand("Not allowed to execute %r." % (command,))


def launch_smart_server(avatar):
    from twisted.internet import reactor

    environment = dict(os.environ)

    # Extract the hostname from the supermirror root config.
    hostname = urlparse(config.codehosting.supermirror_root)[1]
    environment["BRZ_EMAIL"] = "%s@%s" % (avatar.username, hostname)

    return RestrictedExecOnlySession(
        avatar, reactor, lookup_command_template, environment=environment
    )
