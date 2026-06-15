# Copyright 2010-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Fixtures related to configs."""

__all__ = [
    "ConfigFixture",
    "ConfigMismatchError",
    "ConfigUseFixture",
]

import io
import os.path
import shutil
from configparser import RawConfigParser
from textwrap import dedent

from fixtures import Fixture

from lp.services.config import config


class ConfigMismatchError(Exception):
    """Removing configuration failed due to a mismatch in expected contents."""


class ConfigFixture(Fixture):
    """Create a unique launchpad config."""

    _extend_str = dedent(
        """\
        [meta]
        extends: ../%s/launchpad-lazr.conf
        """
    )

    def __init__(self, instance_name, copy_from_instance):
        """Create a ConfigFixture.

        :param instance_name: The name of the instance to create.
        :param copy_from_instance: An existing instance to clone.
        """
        self.instance_name = instance_name
        self.copy_from_instance = copy_from_instance

    def _parseConfigData(self, conf_data, conf_filename):
        """Parse a single chunk of config data, with no inheritance."""
        # Compare https://bugs.launchpad.net/lazr.config/+bug/1397779.
        parser = RawConfigParser(strict=False)
        parser.read_file(io.StringIO(conf_data), conf_filename)
        return parser

    def _parseConfigFile(self, conf_filename):
        """Parse a single config file, with no inheritance."""
        if os.path.exists(conf_filename):
            with open(conf_filename) as conf_file:
                conf_data = conf_file.read()
        else:
            conf_data = ""
        return self._parseConfigData(conf_data, conf_filename)

    def _writeConfigFile(self, parser, conf_filename):
        """Write a parsed config to a file."""
        with open(conf_filename, "w") as conf_file:
            for i, section in enumerate(parser.sections()):
                if i:
                    conf_file.write("\n")
                conf_file.write("[%s]\n" % section)
                for key, value in parser.items(section):
                    conf_file.write(
                        "%s: %s\n" % (key, str(value).replace("\n", "\n\t"))
                    )

    def _refresh(self):
        """Trigger a config refresh if necessary.

        If and only if the config is in use at the moment, we need to
        refresh in order to make changes available.
        """
        if config.instance_name == self.instance_name:
            config._invalidateConfig()

    def add_section(self, sectioncontent):
        """Add sectioncontent to the lazr config."""
        conf_filename = os.path.join(self.absroot, "launchpad-lazr.conf")
        parser = self._parseConfigFile(conf_filename)
        add_parser = self._parseConfigData(
            sectioncontent, "<configuration to add>"
        )
        for section in add_parser.sections():
            if not parser.has_section(section):
                parser.add_section(section)
            for name, value in add_parser.items(section):
                parser.set(section, name, value)
        self._writeConfigFile(parser, conf_filename)
        self._refresh()

    def remove_section(self, sectioncontent):
        """Remove sectioncontent from the lazr config."""
        conf_filename = os.path.join(self.absroot, "launchpad-lazr.conf")
        parser = self._parseConfigFile(conf_filename)
        remove_parser = self._parseConfigData(
            sectioncontent, "<configuration to remove>"
        )
        for section in remove_parser.sections():
            if not parser.has_section(section):
                continue
            for name, value in remove_parser.items(section):
                if not parser.has_option(section, name):
                    continue
                current_value = parser.get(section, name)
                if value != current_value:
                    raise ConfigMismatchError(
                        "Can't remove %s.%s option from %s: "
                        "expected value '%s', current value '%s'"
                        % (section, name, conf_filename, value, current_value)
                    )
                parser.remove_option(section, name)
            if not parser.options(section):
                parser.remove_section(section)
        self._writeConfigFile(parser, conf_filename)
        self._refresh()

    def _setUp(self):
        root = os.path.join(config.root, "configs", self.instance_name)
        os.mkdir(root)
        self.absroot = os.path.abspath(root)
        self.addCleanup(shutil.rmtree, self.absroot)
        source = os.path.join(config.root, "configs", self.copy_from_instance)
        for entry in os.scandir(source):
            if not entry.is_file():
                continue
            if entry.name == "launchpad-lazr.conf":
                self.add_section(self._extend_str % self.copy_from_instance)
                continue
            with open(entry.path) as input:
                with open(os.path.join(root, entry.name), "w") as out:
                    out.write(input.read())


class ConfigUseFixture(Fixture):
    """Use a config and restore the current config after."""

    def __init__(self, instance_name):
        self.instance_name = instance_name

    def _setUp(self):
        self.addCleanup(config.setInstance, config.instance_name)
        config.setInstance(self.instance_name)
