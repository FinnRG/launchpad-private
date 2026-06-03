# Copyright 2017-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Validators for the .store_channels attribute."""

from lp import _
from lp.app.validators import LaunchpadValidationError
from lp.services.channels import channel_string_to_list
from lp.services.webapp.escaping import html_escape, safe_html


def channels_validator(channels):
    """Return True if the channels in a list are valid, or raise a
    LaunchpadValidationError.
    """
    tracks = set()
    branches = set()
    for name in channels:
        try:
            track, risk, branch = channel_string_to_list(name)
        except ValueError:
            message = _(
                "Invalid channel name '${name}'. Channel names must be of the "
                "form 'track/risk/branch', 'track/risk', 'risk/branch', or "
                "'risk'.",
                mapping={"name": html_escape(name)},
            )
            raise LaunchpadValidationError(safe_html(message))
        tracks.add(track)
        branches.add(branch)

    return True
