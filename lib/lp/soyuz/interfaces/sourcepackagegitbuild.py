# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interfaces for source package Git builds."""

__all__ = [
    "ISourcePackageGitBuild",
    "ISourcePackageGitBuildAdmin",
    "ISourcePackageGitBuildEdit",
    "ISourcePackageGitBuildSource",
    "ISourcePackageGitBuildView",
]

from lazr.restful.declarations import exported, exported_as_webservice_entry
from lazr.restful.fields import Reference
from zope.schema import Int, TextLine

from lp import _
from lp.buildmaster.interfaces.buildfarmjob import (
    IBuildFarmJobAdmin,
    IBuildFarmJobEdit,
    ISpecificBuildFarmJobSource,
)
from lp.buildmaster.interfaces.packagebuild import (
    IPackageBuild,
    IPackageBuildView,
)
from lp.code.interfaces.gitrepository import IGitRepository
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.person import IPerson
from lp.soyuz.interfaces.sourcepackagerelease import ISourcePackageRelease


class ISourcePackageGitBuildView(IPackageBuildView):
    """Attributes of a source package Git build requiring view access."""

    id = Int(title=_("Identifier for this build."), readonly=True)

    git_repository = exported(
        Reference(
            IGitRepository,
            title=_("The Git repository being built."),
            readonly=True,
        )
    )
    commit_sha1 = exported(
        TextLine(title=_("The Git commit being built."), readonly=True)
    )
    # Exported as `distro_series` by `IPackageBuildView`.
    distroseries = Reference(
        IDistroSeries,
        title=_("The distribution series being built for."),
        readonly=True,
    )
    requester = exported(
        Reference(
            IPerson,
            title=_("The person who requested this build."),
            readonly=True,
        )
    )
    version_suffix = exported(
        TextLine(
            title=_("The suffix appended to the package version."),
            readonly=True,
        )
    )
    source_package_release = Reference(
        ISourcePackageRelease,
        title=_("The source package release uploaded by this build."),
        readonly=True,
    )

    def getFileByName(filename):
        """Return a build file with the given name."""


class ISourcePackageGitBuildEdit(IBuildFarmJobEdit):
    """Operations requiring edit access."""

    def destroySelf():
        """Delete this build."""


class ISourcePackageGitBuildAdmin(IBuildFarmJobAdmin):
    """Operations requiring admin access."""


@exported_as_webservice_entry(as_of="devel")
class ISourcePackageGitBuild(
    ISourcePackageGitBuildView,
    ISourcePackageGitBuildEdit,
    ISourcePackageGitBuildAdmin,
    IPackageBuild,
):
    """A source package build from a Git revision."""


class ISourcePackageGitBuildSource(ISpecificBuildFarmJobSource):
    """Create and retrieve source package Git builds."""

    def new(
        git_repository,
        commit_sha1,
        archive,
        distroseries,
        requester,
        version_suffix,
        pocket=None,
        date_created=None,
    ):
        """Create a source package Git build."""
