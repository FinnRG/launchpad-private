# Copyright 2010-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import os
import subprocess

from breezy import osutils, tests

from lp.codehosting import get_brz_command, get_BRZ_PLUGIN_PATH_for_subprocess


class TestCaseWithSubprocess(tests.TestCaseWithTransport):
    """Override the bzr start_bzr_subprocess command.

    The launchpad infrastructure requires a fair amount of configuration to
    get paths, etc correct. This provides a "start_bzr_subprocess" command
    that has all of those paths appropriately set, but otherwise functions the
    same as the breezy.tests.TestCase version.
    """

    def start_bzr_subprocess(
        self, process_args, env_changes=None, working_dir=None
    ):
        """Start bzr in a subprocess for testing.

        Copied and modified from `breezy.tests.TestCase.start_bzr_subprocess`.
        This version removes some of the skipping stuff, some of the
        irrelevant comments (e.g. about win32) and uses Launchpad's
        virtualenv to run Breezy.

        Comments starting with 'LAUNCHPAD' are comments about our
        modifications.
        """
        if env_changes is None:
            env_changes = {}
        env_changes["BRZ_PLUGIN_PATH"] = get_BRZ_PLUGIN_PATH_for_subprocess()
        old_env = {}

        def cleanup_environment():
            for env_var, value in env_changes.items():
                old_env[env_var] = osutils.set_or_unset_env(env_var, value)

        def restore_environment():
            for env_var, value in old_env.items():
                osutils.set_or_unset_env(env_var, value)

        cwd = None
        if working_dir is not None:
            cwd = osutils.getcwd()
            os.chdir(working_dir)

        try:
            cleanup_environment()
            command = get_brz_command()
            command.extend(process_args)
            process = self._popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        finally:
            restore_environment()
            if cwd is not None:
                os.chdir(cwd)

        return process
