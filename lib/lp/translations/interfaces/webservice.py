# Copyright 2009, 2011 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""All the interfaces that are exposed through the webservice.

There is a declaration in ZCML somewhere that looks like:
  <webservice:register module="lp.translations.interfaces.webservice" />

which tells `lazr.restful` that it should look for webservice exports here.
"""

__all__ = [
    "IDistroSeriesLanguageEntry",
    "IHasTranslationImports",
    "IPOFile",
    "IPOTemplate",
    "ITranslationGroup",
    "ITranslationGroupSet",
    "ITranslationImportQueue",
    "ITranslationImportQueueEntry",
]

from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.product import IProduct
from lp.registry.interfaces.productseries import IProductSeries
from lp.services.webservice.apihelpers import (
    patch_collection_property,
    patch_collection_return_type,
    patch_reference_property,
)
from lp.translations.interfaces.distroserieslanguage import (
    IDistroSeriesLanguageEntry,
)
from lp.translations.interfaces.hastranslationimports import (
    IHasTranslationImports,
)
from lp.translations.interfaces.hastranslationtemplates import (
    IHasTranslationTemplates,
)
from lp.translations.interfaces.pofile import IPOFile
from lp.translations.interfaces.potemplate import (
    IPOTemplate,
    IPOTemplateSharingSubset,
    IPOTemplateSubset,
)
from lp.translations.interfaces.translationgroup import (
    ITranslationGroup,
    ITranslationGroupSet,
)
from lp.translations.interfaces.translationimportqueue import (
    ITranslationImportQueue,
    ITranslationImportQueueEntry,
)

# IDistroSeriesLanguageEntry
patch_reference_property(
    IDistroSeriesLanguageEntry, "distroseries", IDistroSeries
)

# IHasTranslationImports
patch_collection_return_type(
    IHasTranslationImports,
    "getTranslationImportQueueEntries",
    ITranslationImportQueueEntry,
)

# IHasTranslationTemplates
patch_collection_return_type(
    IHasTranslationTemplates, "getTranslationTemplates", IPOTemplate
)

# IPOTemplate
patch_collection_property(IPOTemplate, "pofiles", IPOFile)
patch_reference_property(IPOTemplate, "product", IProduct)

# IPOTemplateSubset
patch_reference_property(IPOTemplateSubset, "distroseries", IDistroSeries)
patch_reference_property(IPOTemplateSubset, "productseries", IProductSeries)

# IPOTemplateSharingSubset
patch_reference_property(IPOTemplateSharingSubset, "product", IProduct)
