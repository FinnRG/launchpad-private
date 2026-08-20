# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from lazr.restful.declarations import exported, exported_as_webservice_entry
from lazr.restful.fields import Reference
from zope.interface import Attribute, Interface
from zope.schema import Datetime, Int, TextLine

from lp import _
from lp.services.worlddata.interfaces.language import ILanguage
from lp.translations.interfaces.rosettastats import IRosettaStats

__all__ = [
    "IDistroSeriesLanguage",
    "IDistroSeriesLanguageEntry",
    "IDistroSeriesLanguageSet",
]


class IDistroSeriesLanguage(IRosettaStats):
    """A placeholder for the statistics in the translation of a
    distroseries into a language, for example, Ubuntu Hoary into French.
    This exists to cache stats, and be a useful object for traversal in
    Rosetta."""

    id = Attribute("A unique ID")

    language = Attribute("The language.")

    distroseries = Attribute("The distro series which has been " "translated.")

    dateupdated = Attribute("The date these statistics were last updated.")

    title = Attribute("The title.")

    pofiles = Attribute(
        "The set of pofiles in this distroseries for this "
        "language. This includes only the real pofiles where translations "
        "exist."
    )

    contributor_count = Attribute(
        "The number of contributors in total "
        "for this language in the distribution."
    )

    def updateStatistics(ztm):
        """Update all the Rosetta stats for this distro series language."""

    def getPOFilesFor(potemplates):
        """Return `POFile`s for each of `potemplates`, in the same order.

        For any `POTemplate` that does not have a translation to the
        required language, a `PlaceholderPOFile` is provided.
        """


@exported_as_webservice_entry(
    singular_name="distro_series_language",
    plural_name="distro_series_languages",
    as_of="devel",
)
class IDistroSeriesLanguageEntry(IDistroSeriesLanguage):
    """Translation coverage for one language in a distribution series."""

    language = exported(
        Reference(
            ILanguage,
            title=_("Language"),
            description=_("The language covered by these statistics."),
            required=True,
            readonly=True,
        ),
        as_of="devel",
    )

    distroseries = exported(
        Reference(
            # Really IDistroSeries, patched in
            # lp.translations.interfaces.webservice.
            Interface,
            title=_("Distribution series"),
            description=_(
                "The distribution series covered by these statistics."
            ),
            required=True,
            readonly=True,
        ),
        as_of="devel",
    )

    language_code = exported(
        TextLine(
            title=_("Language code"),
            description=_("The ISO 639 code for the language."),
            required=True,
            readonly=True,
        ),
        as_of="devel",
    )

    language_name = exported(
        TextLine(
            title=_("Language name"),
            description=_("The English name of the language."),
            required=True,
            readonly=True,
        ),
        as_of="devel",
    )

    date_updated = exported(
        Datetime(
            title=_("Date updated"),
            description=_(
                "When the translation statistics were last updated."
            ),
            required=False,
            readonly=True,
        ),
        as_of="devel",
    )

    translated_percentage = exported(
        Int(
            title=_("Translated percentage"),
            description=_(
                "The percentage of messages that are translated, rounded "
                "down to a whole number."
            ),
            required=True,
            readonly=True,
            min=0,
            max=100,
        ),
        as_of="devel",
    )

    contributor_count = exported(
        Int(
            title=_("Contributor count"),
            description=_(
                "The number of people who contributed translations."
            ),
            required=True,
            readonly=True,
            min=0,
        ),
        as_of="devel",
    )


class IDistroSeriesLanguageSet(Interface):
    """The set of distroserieslanguages."""

    def getEmpty(distroseries, language):
        """Return a new EmptyDistroSeriesLanguage for the given
        distroseries and language.
        """
