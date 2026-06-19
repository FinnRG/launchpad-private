#!/usr/bin/python3 -S
#
# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Script to print disk locations of modified archives.

This script will be used by IS for the rsync backups.
"""

import _pythonpath  # noqa: F401

from lp.archivepublisher.scripts.modifiedarchives import ModifiedArchivesScript

if __name__ == "__main__":
    script = ModifiedArchivesScript("modified-archives")
    script.run()
