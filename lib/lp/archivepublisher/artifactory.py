# Copyright 2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Artifactory pool integration for the publisher."""

__all__ = [
    "ArtifactoryPool",
]

import logging
import os
import tempfile
from collections import defaultdict
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote_plus

import requests
from artifactory import ArtifactoryPath
from dohq_artifactory.auth import XJFrogArtApiAuth
from packaging import utils as packaging_utils
from zope.security.proxy import isinstance as zope_isinstance

from lp.archivepublisher.diskpool import FileAddActionEnum, poolify
from lp.archiveuploader.utils import re_no_epoch
from lp.registry.interfaces.sourcepackage import SourcePackageFileType
from lp.services.config import config
from lp.services.librarian.utils import copy_and_close
from lp.soyuz.enums import ArchiveRepositoryFormat, BinaryPackageFileType
from lp.soyuz.interfaces.archive import IArchive
from lp.soyuz.interfaces.files import (
    IBinaryPackageFile,
    IPackageReleaseFile,
    ISourcePackageReleaseFile,
)
from lp.soyuz.interfaces.publishing import (
    IBinaryPackagePublishingHistory,
    IgnorableArtifactoryPoolFileOverwriteError,
    NotInPool,
    PoolFileOverwriteError,
)

# The repository types in which PoolFileOverwriteError exceptions
# are expected and can be ignored.
IGNORE_PFOES_REPOSITORY_TYPES = (
    ArchiveRepositoryFormat.CONDA,
    ArchiveRepositoryFormat.GENERIC,
    ArchiveRepositoryFormat.PYTHON,
)


def _path_for(
    archive: IArchive,
    rootpath: ArtifactoryPath,
    source_name: str,
    source_version: str,
    pub_file: IPackageReleaseFile,
) -> ArtifactoryPath:
    repository_format = archive.repository_format
    if ISourcePackageReleaseFile.providedBy(pub_file):
        release = pub_file.sourcepackagerelease
    elif IBinaryPackageFile.providedBy(pub_file):
        release = pub_file.binarypackagerelease
    else:
        # There are no other kinds of `IPackageReleaseFile` at the moment.
        raise AssertionError("Unsupported file: %r" % pub_file)
    if repository_format == ArchiveRepositoryFormat.DEBIAN:
        path = rootpath / poolify(source_name)
    elif repository_format == ArchiveRepositoryFormat.PYTHON:
        package_name = release.getUserDefinedField("package-name")
        if package_name is None:
            package_name = source_name
        path = (
            rootpath
            / packaging_utils.canonicalize_name(package_name)
            / source_version
        )
    elif repository_format == ArchiveRepositoryFormat.CONDA:
        subdir = release.getUserDefinedField("subdir")
        if subdir is None:
            raise ValueError(
                "Cannot publish the Conda package '%s' "
                "with version '%s', missing the 'subdir' "
                "Conda property)" % (source_name, source_version)
            )
        path = rootpath / subdir
    elif repository_format == ArchiveRepositoryFormat.GO_PROXY:
        module_path = release.getUserDefinedField("module-path")
        if module_path is None:
            raise ValueError("Cannot publish a Go module with no module-path")
        # Base path required by https://go.dev/ref/mod#module-proxy.
        path = rootpath / module_path / "@v"
    elif repository_format == ArchiveRepositoryFormat.GENERIC:
        package_name = release.getUserDefinedField("name")
        if package_name is None:
            package_name = source_name
        path = rootpath / package_name / source_version
    else:
        raise AssertionError(
            "Unsupported repository format: %r" % repository_format
        )
    path = path / pub_file.libraryfile.filename
    return path


class ArtifactoryPoolEntry:
    def __init__(
        self,
        archive: IArchive,
        rootpath: ArtifactoryPath,
        source_name: str,
        source_version: str,
        pub_file: IPackageReleaseFile,
        logger: logging.Logger,
    ) -> None:
        self.archive = archive
        self.rootpath = rootpath
        self.source_name = source_name
        self.source_version = source_version
        self.pub_file = pub_file
        self.logger = logger

    def debug(self, *args, **kwargs) -> None:
        self.logger.debug(*args, **kwargs)

    def pathFor(self, component: Optional[str] = None) -> ArtifactoryPath:
        """Return the path for this file in the given component."""
        # For Artifactory publication, we ignore the component.  There's
        # only marginal benefit in having it be explicitly represented in
        # the pool structure, and doing so would introduce significant
        # complications in terms of having to keep track of components just
        # in order to update an artifact's properties.
        return _path_for(
            self.archive,
            self.rootpath,
            self.source_name,
            self.source_version,
            self.pub_file,
        )

    def makeReleaseID(self, pub_file: IPackageReleaseFile) -> str:
        """
        Return a property describing the xPR that this file belongs to.

        The properties set on a particular file may be derived from multiple
        publications, so it's helpful to have a way to map each file back to
        a single `SourcePackageRelease` or `BinaryPackageRelease` so that we
        can somewhat efficiently decide which files to update with
        information from which publications.  This returns a string that can
        be set as the "launchpad.release-id" property to keep track of this.
        """
        if ISourcePackageReleaseFile.providedBy(pub_file):
            return "source:{:d}".format(
                pub_file.sourcepackagerelease_id
            )  # type: ignore
        elif IBinaryPackageFile.providedBy(pub_file):
            return "binary:{:d}".format(
                pub_file.binarypackagerelease_id
            )  # type: ignore
        else:
            raise AssertionError("Unsupported file: %r" % pub_file)

    # Property names outside the "launchpad." namespace that we expect to
    # overwrite.  Any existing property names other than these will be left
    # alone.
    owned_properties = frozenset(
        {
            "deb.architecture",
            "deb.component",
            "deb.distribution",
        }
    )

    def calculateProperties(self, release_id, publications):
        """Return a dict of Artifactory properties to set for this file.

        Artifactory properties are used (among other things) to describe how
        a file should be indexed, in a similar sort of way to active
        publishing records in Launchpad.  However, the semantics are a
        little different where combinations of publishing dimensions are
        involved.  Launchpad has a publishing history record for each
        position in the matrix of possible publishing locations, while
        Artifactory takes the cross product of a file's properties: for
        example, a package might be published in Launchpad for focal/amd64,
        focal/i386, and jammy/amd64, but if we set `"deb.distribution":
        ["focal", "jammy"], "deb.architecture": ["amd64", "i386"]` then
        Artifactory will add that file to the indexes for all of
        focal/amd64, focal/i386, jammy/amd64, jammy/i386.

        In practice, the particular set of publishing dimensions we use for
        Debian-format PPAs means that this usually only matters in corner
        cases: PPAs only have a single component ("main"), and the only
        files published for more than one architecture are those for
        "Architecture: all" packages, so this effectively only matters when
        architectures are added or removed between series.  It does mean
        that we can't produce a faithful rendition of PPAs in Artifactory
        without splitting up the pool, since we also need to do things like
        overriding the section and phasing differently across series.

        For other indexing formats, we care about channels, which are set as
        properties and then used to generate subsidiary Artifactory
        repositories.  Those potentially intersect with series: a given file
        might be in "stable" for focal but in "candidate" for jammy.  To
        avoid problems due to this, we need to express channels to
        Artifactory in a way that doesn't cause files to end up in incorrect
        locations in the series/channel matrix.  The simplest approach is to
        prefix the channel with the series (actually suite), so that the
        above example might end up as `"launchpad.channel": ["focal:stable",
        "jammy:candidate"]`.  This can easily be matched by AQL queries and
        used to generate more specific repositories.

        Note that all the property values here must be sorted lists, even if
        those are single-item lists.  If we use simple strings, then things
        mostly work because `artifactory.encode_properties` will still
        encode them properly, but they won't match the values returned by
        the AQL search via `ArtifactoryPool.getAllArtifacts`, and so
        `updateProperties` will always try to update them even if there
        aren't really any changes.

        We also set an assortment of additional metadata items, specified in
        https://docs.google.com/spreadsheets/d/15Xkdi-CRu2NiQfLoclP5PKW63Zw6syiuao8VJG7zxvw
        (private).
        """
        properties = {}
        properties["launchpad.release-id"] = [release_id]
        properties["launchpad.source-name"] = [self.source_name]
        properties["launchpad.source-version"] = [self.source_version]
        if publications:
            archives = {publication.archive for publication in publications}
            if len(archives) > 1:
                raise AssertionError(
                    "Can't calculate properties across multiple archives: %s"
                    % archives
                )
            repository_format = tuple(archives)[0].repository_format
            if repository_format == ArchiveRepositoryFormat.DEBIAN:
                properties["deb.distribution"] = sorted(
                    {
                        pub.distroseries.getSuite(pub.pocket)
                        for pub in publications
                    }
                )
                properties["deb.component"] = sorted(
                    {pub.component.name for pub in publications}
                )
                architectures = sorted(
                    {
                        pub.distroarchseries.architecturetag
                        for pub in publications
                        if IBinaryPackagePublishingHistory.providedBy(pub)
                    }
                )
                if architectures:
                    properties["deb.architecture"] = architectures
                if release_id.startswith("source:"):
                    # Artifactory doesn't support publishing Sources files
                    # for Debian-format source packages
                    # (https://www.jfrog.com/jira/browse/RTFACT-9206), and
                    # so also doesn't automatically populate name and
                    # version properties for them.  Partially work around
                    # this by setting those properties manually (although
                    # this doesn't cause Sources files to exist).
                    properties["deb.name"] = [self.source_name]
                    properties["deb.version"] = [self.source_version]
                    # Debian-format source packages have a license path
                    # required by policy.
                    properties["soss.license"] = ["debian/copyright"]
                elif release_id.startswith("binary:"):
                    # Debian-format binary packages have a license path
                    # required by policy (although examining it may require
                    # dereferencing symlinks).
                    properties["soss.license"] = [
                        "/usr/share/doc/%s/copyright"
                        % publications[0].binary_package_name
                    ]
                    # Point to the corresponding .dsc, so that binaries
                    # uniformly have a property referring to their source
                    # code.  We don't have the .dsc conveniently in hand
                    # here without a cumbersome database walk, but
                    # fortunately it has a predictable file name.
                    properties["soss.source_url"] = [
                        (
                            self.rootpath
                            / poolify(self.source_name)
                            / "{}_{}.dsc".format(
                                self.source_name,
                                re_no_epoch.sub("", self.source_version),
                            )
                        ).as_posix()
                    ]
            else:
                properties["launchpad.channel"] = sorted(
                    {
                        "%s:%s"
                        % (pub.distroseries.getSuite(pub.pocket), pub.channel)
                        for pub in publications
                    }
                )
        # Additional metadata per
        # https://docs.google.com/spreadsheets/d/15Xkdi-CRu2NiQfLoclP5PKW63Zw6syiuao8VJG7zxvw
        # (private).
        if ISourcePackageReleaseFile.providedBy(self.pub_file):
            release = self.pub_file.sourcepackagerelease
            # Allow automation built on top of Artifactory to tell that this
            # is a source package.
            properties["soss.type"] = ["source"]
        elif IBinaryPackageFile.providedBy(self.pub_file):
            release = self.pub_file.binarypackagerelease
            # Allow automation built on top of Artifactory to tell that this
            # is a binary package.
            properties["soss.type"] = ["binary"]
        else:
            # There are no other kinds of `IPackageReleaseFile` at the moment.
            raise AssertionError("Unsupported file: %r" % self.pub_file)
        if release.ci_build is not None:
            properties.update(
                {
                    "soss.source_url": [
                        release.ci_build.git_repository.getCodebrowseUrl()
                    ],
                    "soss.commit_id": [release.ci_build.commit_sha1],
                }
            )
        if "soss.license" not in properties:
            license_field = release.getUserDefinedField("license")
            if zope_isinstance(license_field, dict):
                if "spdx" in license_field:
                    properties["soss.license"] = [
                        "spdx:%s" % license_field["spdx"]
                    ]
                elif "path" in license_field:
                    # Not ideal for parsing, but we have precedent for
                    # putting unadorned paths here, and it doesn't seem very
                    # likely that a path will begin with "spdx:" so there
                    # probably won't be significant confusion in practice.
                    properties["soss.license"] = [license_field["path"]]
        if self.pub_file.filetype in (
            SourcePackageFileType.GENERIC,
            BinaryPackageFileType.GENERIC,
        ):
            # For most artifact types, Artifactory sets "name" and "version"
            # properties itself by scanning the artifact.  However, for
            # generic artifacts it obviously can't do this, since the
            # content doesn't necessarily have any particular known
            # structure.  Since we already require generic artifacts to have
            # "name" and "version" output properties set by the build job,
            # fill in this gap by passing those on to Artifactory.
            package_name = release.getUserDefinedField("name")
            if package_name is None:
                package_name = self.source_name
            properties["generic.name"] = [package_name]
            properties["generic.version"] = [self.source_version]
        return properties

    def addFile(self):
        targetpath = self.pathFor()
        if not targetpath.parent.exists():
            targetpath.parent.mkdir()
        lfa = self.pub_file.libraryfile

        if targetpath.exists():
            file_hash = targetpath.stat().sha1
            sha1 = lfa.content.sha1
            if sha1 != file_hash:
                error_message = f"{sha1} != {file_hash} for {targetpath}"
                # XXX 2025-03-24 lgp171188
                # Due to issues in modelling the relationship between a source
                # package and a binary package for certain package types
                # getting built on multiple architectures, we ended up with
                # a lot of these PoolFileOverwriteError exceptions getting
                # raised for the same affected files in each run of the
                # Artifactory publisher. This is unnecessary and is flooding
                # the OOPS system with too many OOPSes for the same issue.
                # So as a temporary, stop-gap solution, we are suppressing the
                # PoolFileOverwriteError exception here and instead raise
                # IgnorableArtifactoryPoolFileOverwriteError which is getting
                # ignored in the appropriate upper layers.
                if (
                    self.archive.repository_format
                    in IGNORE_PFOES_REPOSITORY_TYPES
                ):
                    self.logger.info(
                        f"Ignoring PoolFileOverwriteError: {error_message} "
                        "as it is a known limitation for "
                        f"{self.archive.repository_format.title} packages."
                    )
                    raise IgnorableArtifactoryPoolFileOverwriteError(
                        error_message
                    )
                raise PoolFileOverwriteError(error_message)
            return FileAddActionEnum.NONE

        self.debug("Deploying %s", targetpath)
        properties = self.calculateProperties(
            self.makeReleaseID(self.pub_file), []
        )
        # Property values must be URL-quoted for
        # `ArtifactoryPath.deploy_file`; it does not handle that itself.
        properties = {
            key: [quote_plus(v) for v in value]
            for key, value in properties.items()
        }
        fd, name = tempfile.mkstemp(prefix="temp-download.")
        f = os.fdopen(fd, "wb")
        try:
            lfa.open()
            copy_and_close(lfa, f)
            targetpath.deploy_file(name, parameters=properties)
        finally:
            f.close()
            Path(name).unlink()
        return FileAddActionEnum.FILE_ADDED

    def updateProperties(self, publications, old_properties=None):
        targetpath = self.pathFor()
        if old_properties is None:
            old_properties = targetpath.properties
        release_id = old_properties.get("launchpad.release-id")
        if not release_id:
            raise AssertionError(
                "Cannot update properties: launchpad.release-id is not in %s"
                % old_properties
            )
        properties = self.calculateProperties(release_id[0], publications)
        new_properties = {
            key: value
            for key, value in old_properties.items()
            if not key.startswith("launchpad.")
            and key not in self.owned_properties
        }
        new_properties.update(properties)
        if old_properties != new_properties:
            # We could use the ArtifactoryPath.properties setter, but that
            # will fetch the old properties again when we already have them
            # in hand; this approach saves an HTTP request.
            properties_to_remove = set(old_properties) - set(new_properties)
            if properties_to_remove:
                targetpath.del_properties(
                    properties_to_remove, recursive=False
                )
            targetpath.set_properties(new_properties, recursive=False)

    def removeFile(self) -> int:
        targetpath = self.pathFor()
        try:
            size = targetpath.stat().size
        except OSError:
            raise NotInPool("%s does not exist; skipping." % targetpath)
        targetpath.unlink()
        return size


class ArtifactoryPool:
    """A representation of a pool of packages in Artifactory."""

    results = FileAddActionEnum

    def __init__(
        self, archive: IArchive, rootpath, logger: logging.Logger
    ) -> None:
        self.archive = archive
        if not isinstance(rootpath, ArtifactoryPath):
            rootpath = ArtifactoryPath(rootpath)
        rootpath.session = self._makeSession()
        rootpath.timeout = config.launchpad.urlfetch_timeout
        self.rootpath = rootpath
        self.logger = logger

    def _makeSession(self) -> requests.Session:
        """Make a suitable requests session for talking to Artifactory."""
        # XXX cjwatson 2022-04-01: This somewhat duplicates parts of
        # lp.services.timeout.URLFetcher.fetch; we should work out a better
        # abstraction so that we can reuse code more directly.  (The
        # Artifactory bindings can't be told to use
        # lp.services.timeout.urlfetch directly, but only given a substitute
        # session.)
        # XXX cjwatson 2022-11-25: Nothing ever closes this session
        # explicitly.
        session = requests.Session()
        session.trust_env = False
        if config.launchpad.http_proxy:
            session.proxies = {
                "http": config.launchpad.http_proxy,
                "https": config.launchpad.http_proxy,
            }
        if config.launchpad.ca_certificates_path is not None:
            session.verify = config.launchpad.ca_certificates_path
        write_creds = config.artifactory.write_credentials
        if write_creds is not None:
            # The X-JFrog-Art-Api header only needs the API key, not the
            # username.
            session.auth = XJFrogArtApiAuth(write_creds.split(":", 1)[1])
        return session

    def _getEntry(
        self,
        source_name: str,
        source_version: str,
        pub_file: IPackageReleaseFile,
    ) -> ArtifactoryPoolEntry:
        """See `DiskPool._getEntry`."""
        return ArtifactoryPoolEntry(
            self.archive,
            self.rootpath,
            source_name,
            source_version,
            pub_file,
            self.logger,
        )

    def pathFor(
        self,
        comp: str,
        source_name: str,
        source_version: str,
        pub_file: Optional[IPackageReleaseFile] = None,
    ) -> Path:
        """Return the path for the given pool file."""
        # For Artifactory publication, we ignore the component.  There's
        # only marginal benefit in having it be explicitly represented in
        # the pool structure, and doing so would introduce significant
        # complications in terms of having to keep track of components just
        # in order to update an artifact's properties.
        if TYPE_CHECKING:
            assert pub_file is not None
        return _path_for(
            self.archive, self.rootpath, source_name, source_version, pub_file
        )

    def addFile(
        self,
        component: str,
        source_name: str,
        source_version: str,
        pub_file: IPackageReleaseFile,
    ):
        """Add a file with the given contents to the pool.

        `source_name`, `source_version`, and `filename` are used to
        calculate the location.

        pub_file is an `IPackageReleaseFile` providing the file's contents
        and SHA-1 hash.  The SHA-1 hash is used to compare the given file
        with an existing file, if one exists.

        There are three possible outcomes:
        - If the file doesn't exist in the pool, it will be written from the
        given contents and results.ADDED_FILE will be returned.

        - If the file already exists in the pool, the hash of the file on
        disk will be calculated and compared with the hash provided. If they
        fail to match, PoolFileOverwriteError will be raised.

        - If the file already exists and the hash check passes, results.NONE
        will be returned and nothing will be done.

        This is similar to `DiskPool.addFile`, except that there is no
        symlink handling and the component is ignored.
        """
        entry = self._getEntry(source_name, source_version, pub_file)
        return entry.addFile()

    def removeFile(
        self,
        component: str,
        source_name: str,
        source_version: str,
        pub_file: IPackageReleaseFile,
    ) -> int:
        """Remove the specified file from the pool.

        There are two possible outcomes:
        - If the specified file does not exist, NotInPool will be raised.

        - If the specified file exists, it will simply be deleted, and its
        size will be returned.

        This is similar to `DiskPool.removeFile`, except that there is no
        symlink handling and the component is ignored.
        """
        entry = self._getEntry(source_name, source_version, pub_file)
        return entry.removeFile()

    def updateProperties(
        self,
        source_name: str,
        source_version: str,
        pub_file: IPackageReleaseFile,
        publications,
        old_properties=None,
    ):
        """Update a file's properties in Artifactory."""
        entry = self._getEntry(source_name, source_version, pub_file)
        entry.updateProperties(publications, old_properties=old_properties)

    def getArtifactPatterns(self, repository_format):
        """Get patterns matching artifacts in a repository of this format.

        The returned patterns are AQL wildcards matching the artifacts that
        may be pushed for this repository format.  They do not match
        indexes.
        """
        # XXX cjwatson 2023-02-13: The non-Debian patterns here must
        # currently be kept in sync with the patterns matched by
        # lp.soyuz.model.archivejob.CIBuildUploadJob._scan*.  This is
        # cumbersome, and it would be helpful if we could somehow ensure
        # that there's no drift.
        if repository_format == ArchiveRepositoryFormat.DEBIAN:
            return [
                "*.ddeb",
                "*.deb",
                "*.diff.*",
                "*.dsc",
                "*.tar.*",
                "*.udeb",
            ]
        elif repository_format == ArchiveRepositoryFormat.PYTHON:
            return [
                "*.tar.gz",
                "*.whl",
                "*.zip",
            ]
        elif repository_format == ArchiveRepositoryFormat.CONDA:
            return [
                "*.tar.bz2",
                "*.conda",
            ]
        elif repository_format == ArchiveRepositoryFormat.GO_PROXY:
            return [
                "*.info",
                "*.mod",
                "*.zip",
            ]
        elif repository_format == ArchiveRepositoryFormat.RUST:
            return [
                "*.crate",
            ]
        elif repository_format == ArchiveRepositoryFormat.GENERIC:
            return ["*"]
        else:
            raise AssertionError(
                "Unknown repository format %r" % repository_format
            )

    def getAllArtifacts(self, repository_name, repository_format):
        """Get a mapping of all artifacts to their current properties.

        Returns a mapping of path names relative to the repository root to a
        key/value mapping of properties for each path.
        """
        # See the JFrog AQL documentation (URL backslash-newline-wrapped for
        # length):
        #   https://www.jfrog.com/confluence/display/JFROG/\
        #     Artifactory+Query+Language

        offset = 0
        limit = 500000
        artifacts_by_path = {}
        iterations = 100

        while iterations > 0:
            artifacts = self.rootpath.aql(
                "items.find",
                {
                    "repo": repository_name,
                    "$or": [
                        {"name": {"$match": pattern}}
                        for pattern in self.getArtifactPatterns(
                            repository_format
                        )
                    ],
                },
                ".include",
                # We don't use "repo", but the AQL documentation says that
                # non-admin users must include all of "name", "repo",
                # and "path" in the include directive.
                ["repo", "path", "name", "property"],
                ".offset(%s)" % offset,
                ".limit(%s)" % limit,
            )
            if len(artifacts) == 0:
                break
            for artifact in artifacts:
                path = PurePath(artifact["path"], artifact["name"])
                properties = defaultdict(set)
                for prop in artifact["properties"]:
                    properties[prop["key"]].add(prop.get("value", ""))
                # AQL returns each value of multi-value properties separately
                # and in an undefined order.  Always sort them to ensure that
                # we can compare properties reliably.
                artifacts_by_path[path] = {
                    key: sorted(values) for key, values in properties.items()
                }
            offset += limit
            iterations -= 1

        if iterations == 0:
            self.logger.warning(
                "getAllArtifacts(%r, %r) exceeded the maximum number of "
                "iterations (%s); the results may be incomplete."
            )
        return artifacts_by_path
