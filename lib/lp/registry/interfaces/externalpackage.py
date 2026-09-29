# Copyright 2009, 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""External package interfaces."""

__all__ = [
    "IExternalPackage",
    "ExternalPackageType",
]

from lazr.restful.declarations import exported, exported_as_webservice_entry
from lazr.restful.fields import Reference
from zope.interface import Attribute
from zope.schema import TextLine

from lp import _
from lp.app.interfaces.launchpad import IHeadingContext
from lp.bugs.interfaces.bugtarget import IBugTarget, IHasOfficialBugTags
from lp.registry.enums import ExternalPackageType
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.role import IHasDrivers


@exported_as_webservice_entry(as_of="beta")
class IExternalPackageView(
    IHeadingContext,
    IBugTarget,
    IHasOfficialBugTags,
    IHasDrivers,
):
    """`IExternalPackage` attributes that require launchpad.View."""

    packagetype = Attribute("The package type")

    channel = Attribute("The package channel")

    display_channel = TextLine(title=_("Display channel name"), readonly=True)

    distribution = exported(
        Reference(IDistribution, title=_("The distribution."))
    )
    sourcepackagename = Attribute("The source package name.")

    name = exported(
        TextLine(title=_("The source package name as text"), readonly=True)
    )
    display_name = exported(
        TextLine(title=_("Display name for this package."), readonly=True)
    )
    displayname = Attribute("Display name (deprecated)")
    title = exported(
        TextLine(title=_("Title for this package."), readonly=True)
    )

    drivers = Attribute("The drivers for the distribution.")

    def __eq__(other):
        """IExternalPackage comparison method.

        Distro sourcepackages compare equal only if their fields compare equal.
        """

    def __ne__(other):
        """IExternalPackage comparison method.

        External packages compare not equal if either of their
        fields compare not equal.
        """


@exported_as_webservice_entry(as_of="beta")
class IExternalPackage(
    IExternalPackageView,
):
    """Represents an ExternalPackage in a distribution.

    Create IExternalPackage by invoking `IDistribution.getExternalPackage()`.
    """
