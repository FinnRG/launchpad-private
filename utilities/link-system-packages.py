#! /usr/bin/python3

# Copyright 2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Link system-installed Python modules into Launchpad's virtualenv."""

import importlib
import os.path
import re
from argparse import ArgumentParser
from distutils.sysconfig import get_python_lib

from packaging.markers import Marker


def link_module(name, virtualenv_libdir):
    module = importlib.import_module(name)
    path = module.__file__
    if os.path.basename(path).startswith("__init__."):
        path = os.path.dirname(path)
    system_libdir = get_python_lib(plat_specific=path.endswith(".so"))
    if os.path.commonprefix([path, system_libdir]) != system_libdir:
        raise RuntimeError(
            "%s imported from outside %s (%s)" % (name, system_libdir, path)
        )
    target_path = os.path.join(
        virtualenv_libdir, os.path.relpath(path, system_libdir)
    )
    if os.path.lexists(target_path) and os.path.islink(target_path):
        os.unlink(target_path)
    os.symlink(path, target_path)


def main():
    parser = ArgumentParser()
    parser.add_argument("virtualenv_libdir")
    parser.add_argument("module_file", type=open)
    args = parser.parse_args()

    for line in args.module_file:
        line = re.sub(r"#.*", "", line).strip()
        if not line:
            continue
        match = re.match(
            r"^([A-Za-z_][A-Za-z0-9_]*)(?:\s*;\s*(.*))?",
            line,
        )
        if not match:
            raise ValueError("Parse error: %s" % line)
        name = match.group(1)
        if match.group(2) and not Marker(match.group(2)).evaluate():
            continue
        link_module(name, args.virtualenv_libdir)


if __name__ == "__main__":
    main()
