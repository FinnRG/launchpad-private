# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Builder behaviour for binary package builds."""

__all__ = [
    "BinaryPackageBuildBehaviour",
]

from collections import OrderedDict
from typing import Any, Generator

from twisted.internet import defer
from zope.interface import implementer

from lp.buildmaster.interfaces.builder import CannotBuild
from lp.buildmaster.interfaces.buildfarmjobbehaviour import (
    BuildArgs,
    IBuildFarmJobBehaviour,
)
from lp.buildmaster.model.buildfarmjobbehaviour import (
    BuildFarmJobBehaviourBase,
)
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.config import config
from lp.services.twistedsupport import cancel_on_timeout
from lp.soyuz.adapters.archivedependencies import (
    get_pins_for_building,
    get_primary_current_component,
    get_sources_list_for_building,
)
from lp.soyuz.enums import ArchivePurpose


@implementer(IBuildFarmJobBehaviour)
class BinaryPackageBuildBehaviour(BuildFarmJobBehaviourBase):
    """Define the behaviour of binary package builds."""

    builder_type = "binarypackage"

    def getLogFileName(self):
        """See `IBuildPackageJob`."""
        sourcename = self.build.source_package_release.name
        version = self.build.source_package_release.version
        # we rely on previous storage of current buildstate
        # in the state handling methods.
        state = self.build.status.name

        dar = self.build.distro_arch_series
        distroname = dar.distroseries.distribution.name
        distroseriesname = dar.distroseries.name
        archname = dar.architecturetag

        # logfilename format:
        # buildlog_<DISTRIBUTION>_<DISTROSeries>_<ARCHITECTURE>_\
        # <SOURCENAME>_<SOURCEVERSION>_<BUILDSTATE>.txt
        # as:
        # buildlog_ubuntu_dapper_i386_foo_1.0-ubuntu0_FULLYBUILT.txt
        # it fix request from bug # 30617
        return "buildlog_%s-%s-%s.%s_%s_%s.txt" % (
            distroname,
            distroseriesname,
            archname,
            sourcename,
            version,
            state,
        )

    @defer.inlineCallbacks
    def determineFilesToSend(self):
        """See `IBuildFarmJobBehaviour`."""
        # Build filemap structure with the files required in this build
        # and send them to the worker.
        filemap = OrderedDict()
        macaroon_raw = None
        for source_file in self.build.source_package_release.files:
            lfa = source_file.libraryfile
            filemap[lfa.filename] = {
                "sha1": lfa.content.sha1,
                "url": lfa.getURL(),
            }
            if lfa.restricted:
                if macaroon_raw is None:
                    macaroon_raw = yield self.issueMacaroon()
                filemap[lfa.filename].update(
                    username="", password=macaroon_raw
                )
        return filemap

    def verifyBuildRequest(self, logger):
        """Assert some pre-build checks.

        The build request is checked:
         * Virtualized builds can't build on a non-virtual builder
         * Ensure that we have a chroot
         * Ensure that the build pocket allows builds for the current
           distroseries state.
        """
        build = self.build
        if build.archive.require_virtualized and not self._builder.virtualized:
            raise AssertionError(
                "Attempt to build virtual archive on a non-virtual builder."
            )

        # Assert that we are not silently building SECURITY jobs.
        # See findBuildCandidates. Once we start building SECURITY
        # correctly from EMBARGOED archive this assertion can be removed.
        # XXX Julian 2007-12-18 spec=security-in-soyuz: This is being
        # addressed in the work on the blueprint:
        # https://blueprints.launchpad.net/soyuz/+spec/security-in-soyuz
        target_pocket = build.pocket
        assert (
            target_pocket != PackagePublishingPocket.SECURITY
        ), "Soyuz is not yet capable of building SECURITY uploads."

        # Ensure build has the needed chroot
        chroot = build.distro_arch_series.getChroot(pocket=build.pocket)
        if chroot is None:
            raise CannotBuild(
                "Missing CHROOT for %s/%s/%s"
                % (
                    build.distro_series.distribution.name,
                    build.distro_series.name,
                    build.distro_arch_series.architecturetag,
                )
            )

        # This should already have been checked earlier, but just check again
        # here in case of programmer errors.
        reason = build.archive.checkUploadToPocket(
            build.distro_series, build.pocket
        )
        assert reason is None, (
            "%s (%s) can not be built for pocket %s: invalid pocket due "
            "to the series status of %s."
            % (
                build.title,
                build.id,
                build.pocket.name,
                build.distro_series.name,
            )
        )

    def issueMacaroon(self):
        """See `IBuildFarmJobBehaviour`."""
        return cancel_on_timeout(
            self._authserver.callRemote(
                "issueMacaroon",
                "binary-package-build",
                "BinaryPackageBuild",
                self.build.id,
            ),
            config.builddmaster.authentication_timeout,
        )

    @defer.inlineCallbacks
    def extraBuildArgs(self, logger=None) -> Generator[Any, Any, BuildArgs]:
        """
        Return the extra arguments required by the worker for the given build.
        """
        build = self.build
        das = build.distro_arch_series

        # Build extra arguments.
        args: BuildArgs = yield super().extraBuildArgs(logger=logger)
        args["arch_indep"] = build.arch_indep
        args["suite"] = das.distroseries.getSuite(build.pocket)

        archive_purpose = build.archive.purpose
        if (
            archive_purpose == ArchivePurpose.PPA
            and not build.archive.require_virtualized
        ):
            # If we're building a non-virtual PPA, override the purpose
            # to PRIMARY and use the primary component override.
            # This ensures that the package mangling tools will run over
            # the built packages.
            args["archive_purpose"] = ArchivePurpose.PRIMARY.name
            args["ogrecomponent"] = (
                get_primary_current_component(
                    build.archive,
                    build.distro_series,
                    build.source_package_release.name,
                )
            ).name
        else:
            args["archive_purpose"] = archive_purpose.name
            args["ogrecomponent"] = build.current_component.name

        archive_dependencies = list(build.archive.dependencies)
        (
            args["archives"],
            args["trusted_keys"],
        ) = yield get_sources_list_for_building(
            self,
            das,
            build.source_package_release.name,
            archive_dependencies=archive_dependencies,
            logger=logger,
        )
        args["pins"] = get_pins_for_building(archive_dependencies)
        args["build_debug_symbols"] = build.archive.build_debug_symbols

        return args
