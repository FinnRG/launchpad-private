# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Security adapters for the translations package."""

__all__ = []

from lp.app.security import (
    AnonymousAuthorization,
    AuthorizationBase,
    DelegatedAuthorization,
)
from lp.security import OnlyRosettaExpertsAndAdmins
from lp.translations.interfaces.customlanguagecode import ICustomLanguageCode
from lp.translations.interfaces.distroserieslanguage import (
    IDistroSeriesLanguageEntry,
)
from lp.translations.interfaces.languagepack import ILanguagePack
from lp.translations.interfaces.pofile import IPOFile
from lp.translations.interfaces.potemplate import IPOTemplate
from lp.translations.interfaces.translationgroup import (
    ITranslationGroup,
    ITranslationGroupSet,
)
from lp.translations.interfaces.translationimportqueue import (
    ITranslationImportQueue,
    ITranslationImportQueueEntry,
)
from lp.translations.interfaces.translationsperson import ITranslationsPerson
from lp.translations.interfaces.translationtemplatesbuild import (
    ITranslationTemplatesBuild,
)
from lp.translations.interfaces.translator import IEditTranslator, ITranslator


class EditTranslationsPersonByPerson(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = ITranslationsPerson

    def checkAuthenticated(self, user):
        person = self.obj.person
        return person == user.person or user.in_admin


class ViewDistroSeriesLanguage(AnonymousAuthorization):
    """Anyone can view available distribution-series translations."""

    usedfor = IDistroSeriesLanguageEntry


class ViewPOTemplates(AnonymousAuthorization):
    """Anyone can view an IPOTemplate."""

    usedfor = IPOTemplate


class AdminPOTemplateDetails(OnlyRosettaExpertsAndAdmins):
    """Controls administration of an `IPOTemplate`.

    Allow all persons that can also administer the translations to
    which this template belongs to and also translation group owners.

    Product owners does not have administrative privileges.
    """

    permission = "launchpad.Admin"
    usedfor = IPOTemplate

    def checkAuthenticated(self, user):
        template = self.obj
        if user.in_rosetta_experts or user.in_admin:
            return True
        if template.distroseries is not None:
            # Template is on a distribution.
            return self.forwardCheckAuthenticated(
                user, template.distroseries, "launchpad.TranslationsAdmin"
            )
        else:
            # Template is on a product.
            return False


class EditPOTemplateDetails(AuthorizationBase):
    permission = "launchpad.TranslationsAdmin"
    usedfor = IPOTemplate

    def checkAuthenticated(self, user):
        template = self.obj
        if template.distroseries is not None:
            # Template is on a distribution.
            return user.isOwner(template) or self.forwardCheckAuthenticated(
                user, template.distroseries
            )
        else:
            # Template is on a product.
            return user.isOwner(template) or self.forwardCheckAuthenticated(
                user, template.productseries
            )


class ViewPOFile(AnonymousAuthorization):
    """Anyone can view an IPOFile."""

    usedfor = IPOFile


class EditPOFile(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IPOFile

    def checkAuthenticated(self, user):
        """The `POFile` itself keeps track of this permission."""
        return self.obj.canEditTranslations(user.person)


class AdminTranslator(OnlyRosettaExpertsAndAdmins):
    permission = "launchpad.Admin"
    usedfor = ITranslator

    def checkAuthenticated(self, user):
        """Allow the owner of a translation group to edit the translator
        of any language in the group."""
        return user.inTeam(
            self.obj.translationgroup.owner
        ) or OnlyRosettaExpertsAndAdmins.checkAuthenticated(self, user)


class EditTranslator(OnlyRosettaExpertsAndAdmins):
    permission = "launchpad.Edit"
    usedfor = IEditTranslator

    def checkAuthenticated(self, user):
        """Allow the translator and the group owner to edit parts of
        the translator entry."""
        return (
            user.inTeam(self.obj.translator)
            or user.inTeam(self.obj.translationgroup.owner)
            or OnlyRosettaExpertsAndAdmins.checkAuthenticated(self, user)
        )


class EditTranslationGroup(OnlyRosettaExpertsAndAdmins):
    permission = "launchpad.Edit"
    usedfor = ITranslationGroup

    def checkAuthenticated(self, user):
        """Allow the owner of a translation group to edit the translator
        of any language in the group."""
        return user.inTeam(
            self.obj.owner
        ) or OnlyRosettaExpertsAndAdmins.checkAuthenticated(self, user)


class EditTranslationGroupSet(OnlyRosettaExpertsAndAdmins):
    permission = "launchpad.Admin"
    usedfor = ITranslationGroupSet


class AdminTranslationImportQueueEntry(AuthorizationBase):
    permission = "launchpad.Admin"
    usedfor = ITranslationImportQueueEntry

    def checkAuthenticated(self, user):
        if self.obj.distroseries is not None:
            series = self.obj.distroseries
        else:
            series = self.obj.productseries
        return self.forwardCheckAuthenticated(
            user, series, "launchpad.TranslationsAdmin"
        )


class EditTranslationImportQueueEntry(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = ITranslationImportQueueEntry

    def checkAuthenticated(self, user):
        """Anyone who can admin an entry, plus its owner or the owner of the
        product or distribution, can edit it.
        """
        return self.forwardCheckAuthenticated(
            user, self.obj, "launchpad.Admin"
        ) or user.inTeam(self.obj.importer)


class AdminTranslationImportQueue(OnlyRosettaExpertsAndAdmins):
    permission = "launchpad.Admin"
    usedfor = ITranslationImportQueue


class ViewTranslationTemplatesBuild(DelegatedAuthorization):
    """Permission to view an `ITranslationTemplatesBuild`.

    Delegated to the build's branch.
    """

    permission = "launchpad.View"
    usedfor = ITranslationTemplatesBuild

    def __init__(self, obj):
        super().__init__(obj, obj.branch)


class AdminCustomLanguageCode(AuthorizationBase):
    """Controls administration for a custom language code.

    Whoever can admin a product's or distribution's translations can also
    admin the custom language codes for it.
    """

    permission = "launchpad.TranslationsAdmin"
    usedfor = ICustomLanguageCode

    def checkAuthenticated(self, user):
        return self.forwardCheckAuthenticated(
            user, self.obj.product or self.obj.distribution
        )


class AdminLanguagePack(OnlyRosettaExpertsAndAdmins):
    permission = "launchpad.LanguagePacksAdmin"
    usedfor = ILanguagePack
