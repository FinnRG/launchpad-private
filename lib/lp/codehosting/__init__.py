# Copyright 2009-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Launchpad code-hosting system.

NOTE: Importing this package will load any system Breezy plugins, as well as
all plugins in the brzplugins/ directory underneath the rocketfuel checkout.
"""

__all__ = [
    "get_brz_command",
    "get_brz_path",
]


import os

import breezy

# This import is needed so that brz's logger gets registered.
import breezy.trace
from breezy import ui as brz_ui
from breezy.branch import Branch
from breezy.library_state import BzrLibraryState as BrzLibraryState
from breezy.plugin import load_plugins as brz_load_plugins
from zope.security import checker

from lp.services.config import config


def get_brz_path():
    """Find the path to the copy of Breezy for this rocketfuel instance"""
    return os.path.join(config.root, "bin", "brz")


def get_brz_command():
    """Return a command that runs Breezy in the Launchpad virtualenv."""
    return [os.path.join(config.root, "bin", "py"), "-m", "breezy"]


def _get_brz_plugins_path():
    """Find the path to the Breezy plugins for this rocketfuel instance."""
    return os.path.join(config.root, "brzplugins")


def get_BRZ_PLUGIN_PATH_for_subprocess():
    """Calculate the appropriate value for the BRZ_PLUGIN_PATH environment.

    The '-site' token tells breezy not to include the 'site specific plugins
    directory' (which is usually something like
    /usr/lib/pythonX.Y/dist-packages/breezy/plugins/) in the plugin search
    path, which would be inappropriate for Launchpad, which may have an
    incompatible version of breezy in its virtualenv.
    """
    return ":".join((_get_brz_plugins_path(), "-site"))


# We must explicitly initialize Breezy, as otherwise it will initialize
# itself with a terminal-oriented UI.
if breezy._global_state is None:
    brz_state = BrzLibraryState(
        ui=brz_ui.SilentUIFactory(), trace=breezy.trace.Config()
    )
    brz_state._start()


os.environ["BRZ_PLUGIN_PATH"] = get_BRZ_PLUGIN_PATH_for_subprocess()

# Disable some Breezy plugins that are likely to cause trouble if used on
# the server.  (Unfortunately there doesn't seem to be a good way to load
# only explicitly-specified plugins at the moment.)
os.environ["BRZ_DISABLE_PLUGINS"] = ":".join(
    [
        "cvs",
        "darcs",
        "email",
        "mtn",
    ]
)

# We want to have full access to Launchpad's Breezy plugins throughout the
# codehosting package.
brz_load_plugins()


def dont_wrap_class_and_subclasses(cls):
    checker.BasicTypes.update({cls: checker.NoProxy})
    for subcls in cls.__subclasses__():
        dont_wrap_class_and_subclasses(subcls)


# Don't wrap Branch or its subclasses in Zope security proxies.  Make sure
# the various LoomBranch classes are present first.
import breezy.plugins.loom.branch  # noqa: E402

breezy.plugins.loom.branch
dont_wrap_class_and_subclasses(Branch)
