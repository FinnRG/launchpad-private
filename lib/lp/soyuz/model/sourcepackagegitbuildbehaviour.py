# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Build-farm behaviour for source package Git builds."""

__all__ = ["SourcePackageGitBuildBehaviour"]

from typing import Any, Generator

from twisted.internet import defer
from zope.component import adapter
from zope.interface import implementer
from zope.security.proxy import removeSecurityProxy

from lp.buildmaster.interfaces.builder import CannotBuild
from lp.buildmaster.interfaces.buildfarmjobbehaviour import (
    BuildArgs,
    IBuildFarmJobBehaviour,
)
from lp.buildmaster.model.buildfarmjobbehaviour import (
    BuildFarmJobBehaviourBase,
)
from lp.services.config import config
from lp.services.propertycache import cachedproperty
from lp.soyuz.adapters.archivedependencies import (
    get_primary_current_component,
    get_sources_list_for_building,
)
from lp.soyuz.interfaces.sourcepackagegitbuild import ISourcePackageGitBuild


@adapter(ISourcePackageGitBuild)
@implementer(IBuildFarmJobBehaviour)
class SourcePackageGitBuildBehaviour(BuildFarmJobBehaviourBase):
    """Dispatch source package Git builds to build workers."""

    builder_type = "sourcepackagegit"
    ALLOWED_STATUS_NOTIFICATIONS = ["PACKAGEFAIL", "DEPFAIL", "CHROOTFAIL"]

    @cachedproperty
    def distro_arch_series(self):
        if self.build is None or self._builder is None:
            return None
        return self.build.distroseries.getDistroArchSeriesByProcessor(
            self._builder.processor
        )

    @defer.inlineCallbacks
    def extraBuildArgs(self, logger=None) -> Generator[Any, Any, BuildArgs]:
        if self.distro_arch_series is None:
            raise CannotBuild(
                "Unable to find distroarchseries for %s in %s"
                % (
                    self._builder.processor.name,
                    self.build.distroseries.displayname,
                )
            )
        args: BuildArgs = yield super().extraBuildArgs(logger=logger)
        args["suite"] = self.build.distroseries.getSuite(self.build.pocket)
        requester = self.build.requester
        if requester.preferredemail is None:
            args["author_name"] = "Launchpad Package Builder"
            args["author_email"] = config.canonical.noreply_from_address
        else:
            args["author_name"] = requester.displayname
            args["author_email"] = removeSecurityProxy(
                requester
            ).preferredemail.email
        args["archive_purpose"] = self.build.archive.purpose.name
        args["ogrecomponent"] = get_primary_current_component(
            self.build.archive, self.build.distroseries, None
        ).name
        args["git_repository"] = self.build.git_repository.git_https_url
        args["git_ref"] = self.build.commit_sha1
        args["version_suffix"] = self.build.version_suffix
        (
            args["archives"],
            args["trusted_keys"],
        ) = yield get_sources_list_for_building(
            self,
            self.distro_arch_series,
            None,
            tools_source=config.builddmaster.bzr_builder_sources_list,
            logger=logger,
        )
        return args

    def verifyBuildRequest(self, logger):
        assert (
            self._builder.virtualized
        ), "Attempt to build virtual item on a non-virtual builder."
        reason = self.build.archive.checkUploadToPocket(
            self.build.distroseries, self.build.pocket
        )
        assert reason is None, reason
