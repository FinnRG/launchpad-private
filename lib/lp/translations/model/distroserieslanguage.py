# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""An implementation of `DistroSeriesLanguage` objects."""

__all__ = [
    "DistroSeriesLanguage",
    "DistroSeriesLanguageSet",
    "EmptyDistroSeriesLanguage",
]

from datetime import datetime, timezone
from operator import itemgetter

from storm.expr import Is, LeftJoin
from storm.locals import DateTime, Desc, Int, Join, Reference
from zope.interface import implementer

from lp.registry.model.sourcepackagename import SourcePackageName
from lp.services.database.constants import DEFAULT, UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.translations.interfaces.distroserieslanguage import (
    IDistroSeriesLanguage,
    IDistroSeriesLanguageEntry,
    IDistroSeriesLanguageSet,
)
from lp.translations.model.pofile import PlaceholderPOFile, POFile
from lp.translations.model.potemplate import POTemplate, get_pofiles_for
from lp.translations.model.translationgroup import TranslationGroup
from lp.translations.model.translator import Translator
from lp.translations.utilities.rosettastats import RosettaStats


@implementer(IDistroSeriesLanguageEntry)
class DistroSeriesLanguage(StormBase, RosettaStats):
    """See `IDistroSeriesLanguage`.

    A Storm implementation of IDistroSeriesLanguage.
    """

    __storm_table__ = "DistroSeriesLanguage"

    id = Int(primary=True)
    distroseries_id = Int(name="distroseries", allow_none=True, default=None)
    distroseries = Reference(distroseries_id, "DistroSeries.id")
    language_id = Int(name="language", allow_none=False)
    language = Reference(language_id, "Language.id")
    currentcount = Int(allow_none=False, default=0)
    updatescount = Int(allow_none=False, default=0)
    rosettacount = Int(allow_none=False, default=0)
    unreviewed_count = Int(allow_none=False, default=0)
    contributorcount = Int(allow_none=False, default=0)
    dateupdated = DateTime(
        name="dateupdated", tzinfo=timezone.utc, default=DEFAULT
    )

    def __init__(self, distroseries, language):
        super().__init__()
        self.distroseries = distroseries
        self.language = language

    @property
    def title(self):
        return "%s translations of %s %s" % (
            self.language.englishname,
            self.distroseries.distribution.displayname,
            self.distroseries.displayname,
        )

    @property
    def pofiles(self):
        tables = [
            POFile,
            Join(POTemplate, POFile.potemplate == POTemplate.id),
            LeftJoin(
                SourcePackageName,
                POTemplate.sourcepackagename == SourcePackageName.id,
            ),
        ]
        result = (
            IStore(POFile)
            .using(*tables)
            .find(
                (POFile, SourcePackageName),
                POFile.language == self.language,
                POTemplate.distroseries == self.distroseries,
                Is(POTemplate.iscurrent, True),
            )
            .order_by(Desc(POTemplate.priority), POFile.id)
        )
        return DecoratedResultSet(result, itemgetter(0))

    def getPOFilesFor(self, potemplates):
        """See `IDistroSeriesLanguage`."""
        return get_pofiles_for(potemplates, self.language)

    @property
    def translators(self):
        # Circular import.
        from lp.registry.model.distribution import Distribution

        return (
            IStore(Translator)
            .find(
                Translator,
                Translator.translationgroup == TranslationGroup.id,
                Distribution.translationgroup == TranslationGroup.id,
                Distribution.id == self.distroseries.distribution.id,
                Translator.language == self.language,
            )
            .order_by(Translator.id)
            .config(distinct=True)
        )

    @property
    def language_code(self):
        return self.language.code

    @property
    def language_name(self):
        return self.language.englishname

    @property
    def date_updated(self):
        return self.dateupdated

    @property
    def translated_percentage(self):
        message_count = self.messageCount()
        if message_count == 0:
            return 0
        percentage = 100 * self.translatedCount() // message_count
        # Cached counts may be updated separately; keep the exported value
        # within the field's declared range.
        return max(0, min(100, percentage))

    @property
    def contributor_count(self):
        return self.contributorcount

    def messageCount(self):
        return self.distroseries.messagecount

    def currentCount(self, language=None):
        return self.currentcount

    def updatesCount(self, language=None):
        return self.updatescount

    def rosettaCount(self, language=None):
        return self.rosettacount

    def unreviewedCount(self):
        """See `IRosettaStats`."""
        return self.unreviewed_count

    def updateStatistics(self, ztm=None):
        current = 0
        updates = 0
        rosetta = 0
        unreviewed = 0
        for pofile in self.pofiles:
            current += pofile.currentCount()
            updates += pofile.updatesCount()
            rosetta += pofile.rosettaCount()
            unreviewed += pofile.unreviewedCount()
        self.currentcount = current
        self.updatescount = updates
        self.rosettacount = rosetta
        self.unreviewed_count = unreviewed

        contributors = self.distroseries.getPOFileContributorsByLanguage(
            self.language
        )
        self.contributorcount = contributors.count()

        self.dateupdated = UTC_NOW
        ztm.commit()


@implementer(IDistroSeriesLanguage)
class EmptyDistroSeriesLanguage(RosettaStats):
    """See `IDistroSeriesLanguage`

    Represents a DistroSeriesLanguage where we do not yet actually HAVE one
    for that language for this distribution series.
    """

    def __init__(self, distroseries, language):
        assert "en" != language.code, "English is not a translatable language."

        super().__init__()

        self.id = None
        self.language = language
        self.distroseries = distroseries
        self.messageCount = distroseries.messagecount
        self.dateupdated = datetime.now(tz=timezone.utc)
        self.contributor_count = 0
        self.title = "%s translations of %s %s" % (
            self.language.englishname,
            self.distroseries.distribution.displayname,
            self.distroseries.displayname,
        )

    @property
    def pofiles(self):
        """See `IDistroSeriesLanguage`."""
        return self.getPOFilesFor(
            self.distroseries.getCurrentTranslationTemplates()
        )

    def getPOFilesFor(self, potemplates):
        """See `IDistroSeriesLanguage`."""
        templates = list(potemplates)
        language = self.language
        return [
            PlaceholderPOFile(template, language) for template in templates
        ]

    def currentCount(self, language=None):
        return 0

    def rosettaCount(self, language=None):
        return 0

    def updatesCount(self, language=None):
        return 0

    def newCount(self, language=None):
        return 0

    def translatedCount(self, language=None):
        return 0

    def untranslatedCount(self, language=None):
        return self.messageCount

    def unreviewedCount(self):
        return 0

    def currentPercentage(self, language=None):
        return 0.0

    def updatesPercentage(self, language=None):
        return 0.0

    def newPercentage(self, language=None):
        return 0.0

    def translatedPercentage(self, language=None):
        return 0.0

    def untranslatedPercentage(self, language=None):
        return 100.0

    def updateStatistics(self, ztm=None):
        return


@implementer(IDistroSeriesLanguageSet)
class DistroSeriesLanguageSet:
    """See `IDistroSeriesLanguageSet`.

    Implements a means to get an EmptyDistroSeriesLanguage.
    """

    def getEmpty(self, distroseries, language):
        """See IDistroSeriesLanguageSet."""
        return EmptyDistroSeriesLanguage(distroseries, language)
