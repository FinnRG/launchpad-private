#!/usr/bin/python3 -S
#
# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Collect results for external revision status watches."""

import _pythonpath  # noqa: F401

from lp.code.scripts.revisionstatuswatch import RevisionStatusWatchCronScript
from lp.services.config import config

if __name__ == "__main__":
    script = RevisionStatusWatchCronScript(
        "update-revision-status-watches",
        dbuser=config.revisionstatuswatch.dbuser,
    )
    script.lock_and_run()
