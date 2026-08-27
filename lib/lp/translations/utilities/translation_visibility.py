# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Visibility checks for distribution-series translations."""

__all__ = [
    "check_distroseries_translations_viewable",
]

from lp.app.errors import TranslationUnavailable
from lp.registry.interfaces.series import SeriesStatus
from lp.services.webapp.authorization import check_permission


def check_distroseries_translations_viewable(distroseries):
    """Ensure that the current user may view a series' translations.

    Translation administrators may view translations hidden from other
    users.

    :raises TranslationUnavailable: If translations are hidden from the
        current user.
    """
    if not distroseries.hide_all_translations:
        return

    if check_permission(
        "launchpad.ViewHiddenDistroSeriesTranslations", distroseries
    ):
        return

    future = [
        SeriesStatus.EXPERIMENTAL,
        SeriesStatus.DEVELOPMENT,
        SeriesStatus.FUTURE,
    ]
    if distroseries.status in future:
        raise TranslationUnavailable(
            "Translations for this release series are not available yet."
        )
    elif distroseries.status == SeriesStatus.OBSOLETE:
        raise TranslationUnavailable(
            "This release series is obsolete.  Its translations are no "
            "longer available."
        )
    else:
        raise TranslationUnavailable(
            "Translations for this release series are not currently "
            "available.  Please come back soon."
        )
