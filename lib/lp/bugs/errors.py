# Copyright 2009-2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Errors used in the lp/bugs modules."""

__all__ = [
    "CannotSetVulnerabilityPatches",
    "InvalidBreakFixData",
    "InvalidBugPresenceTarget",
    "InvalidDuplicateValue",
    "InvalidSearchParameters",
]

import http.client

from lazr.restful.declarations import error_status

from lp.app.validators import LaunchpadValidationError


@error_status(http.client.EXPECTATION_FAILED)
class InvalidDuplicateValue(LaunchpadValidationError):
    """A bug cannot be set as the duplicate of another."""


@error_status(http.client.BAD_REQUEST)
class InvalidSearchParameters(ValueError):
    """Invalid search parameters were passed to searchTasks."""


@error_status(http.client.BAD_REQUEST)
class CannotSetVulnerabilityPatches(ValueError):
    """Vulnerability patches conflict with the attachment's file or URL."""


@error_status(http.client.BAD_REQUEST)
class InvalidBugPresenceTarget(ValueError):
    """A bug presence must have exactly one target."""


@error_status(http.client.BAD_REQUEST)
class InvalidBreakFixData(ValueError):
    """The break-fix data is not a list of 'break'/'fix' mappings."""
