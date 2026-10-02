# Copyright 2009-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "SourcePackageRelease",
]


import io
import json
import operator
import re
from datetime import datetime, timezone

import apt_pkg
from debian.changelog import (
    Changelog,
    ChangelogCreateError,
    ChangelogParseError,
)
from storm.expr import Coalesce, Join, LeftJoin, Sum
from storm.locals import DateTime, Desc, Int, Reference, Unicode
from storm.store import Store
from zope.component import getUtility
from zope.interface import implementer

from lp.app.errors import NotFoundError
from lp.archiveuploader.utils import determine_source_file_type
from lp.buildmaster.enums import BuildStatus
from lp.registry.interfaces.person import validate_public_person
from lp.registry.interfaces.sourcepackage import (
    SourcePackageType,
    SourcePackageUrgency,
)
from lp.services.database.constants import DEFAULT, UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.librarian.model import LibraryFileAlias, LibraryFileContent
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.soyuz.interfaces.archive import MAIN_ARCHIVE_PURPOSES
from lp.soyuz.interfaces.packagediff import PackageDiffAlreadyRequested
from lp.soyuz.interfaces.packagediffjob import IPackageDiffJobSource
from lp.soyuz.interfaces.queue import QueueInconsistentStateError
from lp.soyuz.interfaces.sourcepackagerelease import ISourcePackageRelease
from lp.soyuz.model.binarypackagebuild import BinaryPackageBuild
from lp.soyuz.model.files import SourcePackageReleaseFile
from lp.soyuz.model.packagediff import PackageDiff
from lp.soyuz.model.queue import PackageUpload, PackageUploadSource


@implementer(ISourcePackageRelease)
class SourcePackageRelease(StormBase):
    __storm_table__ = "SourcePackageRelease"

    id = Int(primary=True)
    # DB constraint: non-nullable for SourcePackageType.DPKG.
    section_id = Int(name="section", allow_none=True)
    section = Reference(section_id, "Section.id")
    creator_id = Int(
        name="creator", validator=validate_public_person, allow_none=False
    )
    creator = Reference(creator_id, "Person.id")
    # DB constraint: non-nullable for SourcePackageType.DPKG.
    component_id = Int(name="component", allow_none=True)
    component = Reference(component_id, "Component.id")
    sourcepackagename_id = Int(name="sourcepackagename", allow_none=False)
    sourcepackagename = Reference(sourcepackagename_id, "SourcePackageName.id")
    maintainer_id = Int(
        name="maintainer", validator=validate_public_person, allow_none=True
    )
    maintainer = Reference(maintainer_id, "Person.id")
    signing_key_owner_id = Int(name="signing_key_owner")
    signing_key_owner = Reference(signing_key_owner_id, "Person.id")
    signing_key_fingerprint = Unicode()
    # DB constraint: non-nullable for SourcePackageType.DPKG.
    urgency = DBEnum(
        name="urgency",
        enum=SourcePackageUrgency,
        default=SourcePackageUrgency.LOW,
        allow_none=True,
    )
    dateuploaded = DateTime(
        name="dateuploaded",
        allow_none=False,
        default=UTC_NOW,
        tzinfo=timezone.utc,
    )
    dsc = Unicode(name="dsc")
    version = Unicode(name="version", allow_none=False)
    changelog_id = Int(name="changelog")
    changelog = Reference(changelog_id, "LibraryFileAlias.id")
    changelog_entry = Unicode(name="changelog_entry")
    buildinfo_id = Int(name="buildinfo")
    buildinfo = Reference(buildinfo_id, "LibraryFileAlias.id")
    builddepends = Unicode(name="builddepends")
    builddependsindep = Unicode(name="builddependsindep")
    build_conflicts = Unicode(name="build_conflicts")
    build_conflicts_indep = Unicode(name="build_conflicts_indep")
    architecturehintlist = Unicode(
        name="architecturehintlist", allow_none=False
    )
    homepage = Unicode(name="homepage")
    format = DBEnum(
        name="format",
        enum=SourcePackageType,
        default=SourcePackageType.DPKG,
        allow_none=False,
    )
    upload_distroseries_id = Int(name="upload_distroseries", allow_none=False)
    upload_distroseries = Reference(upload_distroseries_id, "DistroSeries.id")
    upload_archive_id = Int(name="upload_archive", allow_none=False)
    upload_archive = Reference(upload_archive_id, "Archive.id")

    # DB constraint: at most one of source_package_recipe_build, ci_build
    # and source_package_git_build is non-NULL.
    source_package_recipe_build_id = Int(
        name="sourcepackage_recipe_build", allow_none=True
    )
    source_package_recipe_build = Reference(
        source_package_recipe_build_id, "SourcePackageRecipeBuild.id"
    )
    ci_build_id = Int(name="ci_build", allow_none=True)
    ci_build = Reference(ci_build_id, "CIBuild.id")
    source_package_git_build_id = Int(
        name="source_package_git_build", allow_none=True
    )
    source_package_git_build = Reference(
        source_package_git_build_id, "SourcePackageGitBuild.id"
    )

    dsc_maintainer_rfc822 = Unicode(name="dsc_maintainer_rfc822")
    dsc_standards_version = Unicode(name="dsc_standards_version")
    # DB constraint: non-nullable for SourcePackageType.DPKG.
    dsc_format = Unicode(name="dsc_format")
    dsc_binaries = Unicode(name="dsc_binaries")

    _user_defined_fields = Unicode(name="user_defined_fields")

    def __init__(
        self,
        creator,
        sourcepackagename,
        version,
        architecturehintlist,
        format,
        upload_distroseries,
        upload_archive,
        section=None,
        component=None,
        maintainer=None,
        signing_key_owner=None,
        signing_key_fingerprint=None,
        urgency=None,
        dateuploaded=DEFAULT,
        dsc=None,
        changelog=None,
        changelog_entry=None,
        buildinfo=None,
        builddepends=None,
        builddependsindep=None,
        build_conflicts=None,
        build_conflicts_indep=None,
        homepage=None,
        source_package_recipe_build=None,
        ci_build=None,
        source_package_git_build=None,
        dsc_maintainer_rfc822=None,
        dsc_standards_version=None,
        dsc_format=None,
        dsc_binaries=None,
        user_defined_fields=None,
        copyright=None,
    ):
        super().__init__()
        self.creator = creator
        self.sourcepackagename = sourcepackagename
        self.version = version
        self.architecturehintlist = architecturehintlist
        self.format = format
        self.upload_distroseries = upload_distroseries
        self.upload_archive = upload_archive
        self.section = section
        self.component = component
        self.maintainer = maintainer
        self.signing_key_owner = signing_key_owner
        self.signing_key_fingerprint = signing_key_fingerprint
        self.urgency = urgency
        self.dateuploaded = dateuploaded
        self.dsc = dsc
        self.changelog = changelog
        self.changelog_entry = changelog_entry
        self.buildinfo = buildinfo
        self.builddepends = builddepends
        self.builddependsindep = builddependsindep
        self.build_conflicts = build_conflicts
        self.build_conflicts_indep = build_conflicts_indep
        self.homepage = homepage
        self.source_package_recipe_build = source_package_recipe_build
        self.ci_build = ci_build
        self.source_package_git_build = source_package_git_build
        self.dsc_maintainer_rfc822 = dsc_maintainer_rfc822
        self.dsc_standards_version = dsc_standards_version
        self.dsc_format = dsc_format
        self.dsc_binaries = dsc_binaries
        if user_defined_fields is not None:
            self._user_defined_fields = json.dumps(user_defined_fields)
        if copyright is not None:
            # PostgreSQL text columns can't contain null characters, so
            # remove them as this is only used for display.
            self.copyright = copyright.replace("\0", "")

    def __repr__(self):
        """Returns an informative representation of a SourcePackageRelease."""
        return "<{cls} {pkg_name} (id: {id}, version: {version})>".format(
            cls=self.__class__.__name__,
            pkg_name=self.name,
            id=self.id,
            version=self.version,
        )

    @property
    def copyright(self):
        """See `ISourcePackageRelease`."""
        store = Store.of(self)
        store.flush()
        return store.execute(
            "SELECT copyright FROM sourcepackagerelease WHERE id=%s",
            (self.id,),
        ).get_one()[0]

    @copyright.setter
    def copyright(self, content):
        """See `ISourcePackageRelease`."""
        store = Store.of(self)
        store.flush()
        store.execute(
            "UPDATE sourcepackagerelease SET copyright=%s WHERE id=%s",
            (content, self.id),
        )

    @property
    def user_defined_fields(self):
        """See `IBinaryPackageRelease`."""
        if self._user_defined_fields is None:
            return []
        user_defined_fields = json.loads(self._user_defined_fields)
        if user_defined_fields is None:
            return []
        return user_defined_fields

    def getUserDefinedField(self, name):
        for k, v in self.user_defined_fields:
            if k.lower() == name.lower():
                return v

    @cachedproperty
    def package_diffs(self):
        return list(
            Store.of(self)
            .find(PackageDiff, to_source=self)
            .order_by(Desc(PackageDiff.date_requested))
        )

    @property
    def builds(self):
        """See `ISourcePackageRelease`."""
        # Circular import.
        from lp.soyuz.model.archive import Archive

        # Excluding PPA builds may seem like a strange thing to do, but,
        # since Archive.copyPackage can copy packages across archives, a
        # build may well have a different archive to the corresponding
        # sourcepackagerelease.
        return (
            IStore(BinaryPackageBuild)
            .find(
                BinaryPackageBuild,
                BinaryPackageBuild.source_package_release == self,
                BinaryPackageBuild.archive == Archive.id,
                Archive.purpose.is_in(MAIN_ARCHIVE_PURPOSES),
            )
            .order_by(
                Desc(BinaryPackageBuild.date_created), BinaryPackageBuild.id
            )
        )

    @property
    def age(self):
        """See ISourcePackageRelease."""
        now = datetime.now(timezone.utc)
        return now - self.dateuploaded

    def failed_builds(self):
        return [
            build
            for build in self._cached_builds
            if build.buildstate == BuildStatus.FAILEDTOBUILD
        ]

    @property
    def needs_building(self):
        for build in self._cached_builds:
            if build.status in [
                BuildStatus.NEEDSBUILD,
                BuildStatus.MANUALDEPWAIT,
                BuildStatus.CHROOTWAIT,
            ]:
                return True
        return False

    @cachedproperty
    def _cached_builds(self):
        # The reason we have this as a cachedproperty is that all the
        # *build* methods here need access to it; better not to
        # recalculate it multiple times.
        return list(self.builds)

    @property
    def name(self):
        return self.sourcepackagename.name

    @property
    def title(self):
        return "%s - %s" % (self.sourcepackagename.name, self.version)

    @property
    def publishings(self):
        # Circular import.
        from lp.soyuz.model.publishing import SourcePackagePublishingHistory

        return (
            IStore(self)
            .find(
                SourcePackagePublishingHistory,
                SourcePackagePublishingHistory.sourcepackagerelease == self,
            )
            .order_by(Desc(SourcePackagePublishingHistory.datecreated))
        )

    @cachedproperty
    def published_archives(self):
        # Circular imports.
        from lp.soyuz.model.archive import Archive
        from lp.soyuz.model.publishing import SourcePackagePublishingHistory

        return list(
            IStore(self)
            .find(
                Archive,
                SourcePackagePublishingHistory.sourcepackagerelease == self,
                SourcePackagePublishingHistory.archive == Archive.id,
            )
            .config(distinct=True)
            .order_by(Archive.id)
        )

    def addFile(self, file, filetype=None):
        """See ISourcePackageRelease."""
        if filetype is None:
            filetype = determine_source_file_type(file.filename)
        sprf = SourcePackageReleaseFile(
            sourcepackagerelease=self, filetype=filetype, libraryfile=file
        )
        del get_property_cache(self).files
        return sprf

    @cachedproperty
    def files(self):
        """See `ISourcePackageRelease`."""
        # Preload library files, since call sites will normally need them too.
        return [
            sprf
            for sprf, _, _ in Store.of(self)
            .using(
                SourcePackageReleaseFile,
                Join(
                    LibraryFileAlias,
                    SourcePackageReleaseFile.libraryfile
                    == LibraryFileAlias.id,
                ),
                LeftJoin(
                    LibraryFileContent,
                    LibraryFileAlias.content == LibraryFileContent.id,
                ),
            )
            .find(
                (
                    SourcePackageReleaseFile,
                    LibraryFileAlias,
                    LibraryFileContent,
                ),
                SourcePackageReleaseFile.sourcepackagerelease == self,
            )
            .order_by(SourcePackageReleaseFile.libraryfile_id)
        ]

    def getFileByName(self, filename):
        """See `ISourcePackageRelease`."""
        sprf = (
            Store.of(self)
            .find(
                SourcePackageReleaseFile,
                SourcePackageReleaseFile.sourcepackagerelease == self.id,
                LibraryFileAlias.id == SourcePackageReleaseFile.libraryfile_id,
                LibraryFileAlias.filename == filename,
            )
            .one()
        )
        if sprf:
            return sprf.libraryfile
        else:
            raise NotFoundError(filename)

    def getPackageSize(self):
        """See ISourcePackageRelease."""
        return float(
            Store.of(self)
            .using(
                SourcePackageRelease,
                Join(
                    SourcePackageReleaseFile,
                    SourcePackageReleaseFile.sourcepackagerelease
                    == SourcePackageRelease.id,
                ),
                Join(
                    LibraryFileAlias,
                    SourcePackageReleaseFile.libraryfile
                    == LibraryFileAlias.id,
                ),
                Join(
                    LibraryFileContent,
                    LibraryFileAlias.content == LibraryFileContent.id,
                ),
            )
            .find(
                Coalesce(Sum(LibraryFileContent.filesize) / 1024.0, 0.0),
                SourcePackageRelease.id == self.id,
            )
            .one()
        )

    def override(self, component=None, section=None, urgency=None):
        """See ISourcePackageRelease."""
        if component is not None:
            self.component = component
            # See if the new component requires a new archive:
            distribution = self.upload_distroseries.distribution
            new_archive = distribution.getArchiveByComponent(component.name)
            if new_archive is not None:
                self.upload_archive = new_archive
            else:
                raise QueueInconsistentStateError(
                    "New component '%s' requires a non-existent archive."
                )
        if section is not None:
            self.section = section
        if urgency is not None:
            self.urgency = urgency

    @property
    def upload_changesfile(self):
        """See `ISourcePackageRelease`."""
        package_upload = self.package_upload
        # Cope with `SourcePackageRelease`s imported by gina, they do not
        # have a corresponding `PackageUpload` record.
        if package_upload is None:
            return None
        return package_upload.changesfile

    @property
    def package_upload(self):
        """See `ISourcepackageRelease`."""
        store = Store.of(self)
        # The join on 'changesfile' is used for pre-fetching the
        # corresponding library file, so callsites don't have to issue an
        # extra query.
        origin = [
            PackageUploadSource,
            Join(
                PackageUpload,
                PackageUploadSource.packageupload == PackageUpload.id,
            ),
            Join(
                LibraryFileAlias,
                LibraryFileAlias.id == PackageUpload.changes_file_id,
            ),
            Join(
                LibraryFileContent,
                LibraryFileContent.id == LibraryFileAlias.content_id,
            ),
        ]
        results = store.using(*origin).find(
            (PackageUpload, LibraryFileAlias, LibraryFileContent),
            PackageUploadSource.sourcepackagerelease == self,
            PackageUpload.archive == self.upload_archive,
            PackageUpload.distroseries == self.upload_distroseries,
        )

        # Return the unique `PackageUpload` record that corresponds to the
        # upload of this `SourcePackageRelease`, load the `LibraryFileAlias`
        # and the `LibraryFileContent` in cache because it's most likely
        # they will be needed.
        return DecoratedResultSet(results, operator.itemgetter(0)).one()

    @property
    def uploader(self):
        """See `ISourcePackageRelease`"""
        if self.source_package_recipe_build is not None:
            return self.source_package_recipe_build.requester
        if self.signing_key_owner is not None:
            return self.signing_key_owner
        return None

    @property
    def change_summary(self):
        """See ISourcePackageRelease"""
        # this regex is copied from apt-listchanges.py courtesy of MDZ
        new_stanza_line = re.compile(
            r"^\S+ \((?P<version>.*)\) .*;.*urgency=(?P<urgency>\w+).*"
        )
        logfile = io.StringIO(self.changelog_entry)
        change = ""
        top_stanza = False
        for line in logfile.readlines():
            match = new_stanza_line.match(line)
            if match:
                if top_stanza:
                    break
                top_stanza = True
            change += line

        return change

    def getDiffTo(self, to_sourcepackagerelease):
        """See ISourcePackageRelease."""
        return (
            IStore(PackageDiff)
            .find(
                PackageDiff,
                from_source=self,
                to_source=to_sourcepackagerelease,
            )
            .one()
        )

    def requestDiffTo(self, requester, to_sourcepackagerelease):
        """See ISourcePackageRelease."""
        candidate = self.getDiffTo(to_sourcepackagerelease)

        if candidate is not None:
            raise PackageDiffAlreadyRequested(
                "%s has already been requested" % candidate.title
            )

        Store.of(to_sourcepackagerelease).flush()
        del get_property_cache(to_sourcepackagerelease).package_diffs
        packagediff = PackageDiff(
            from_source=self,
            to_source=to_sourcepackagerelease,
            requester=requester,
        )
        IStore(packagediff).flush()
        getUtility(IPackageDiffJobSource).create(packagediff)
        return packagediff

    def aggregate_changelog(self, since_version):
        """See `ISourcePackagePublishingHistory`."""
        if self.changelog is None:
            return None

        apt_pkg.init_system()
        chunks = []
        changelog = self.changelog
        # The python-debian API for parsing changelogs is pretty awful. The
        # only useful way of extracting info is to use the iterator on
        # Changelog and then compare versions.
        try:
            changelog_text = changelog.read().decode("UTF-8", "replace")
            for block in Changelog(changelog_text):
                version = block._raw_version
                if (
                    since_version
                    and apt_pkg.version_compare(version, since_version) <= 0
                ):
                    break
                # Poking in private attributes is not nice but again the
                # API is terrible.  We want to ensure that the name/date
                # line is omitted from these composite changelogs.
                block._no_trailer = True
                try:
                    # python-debian adds an extra blank line to the chunks
                    # so we'll have to sort this out.
                    chunks.append(str(block).rstrip())
                except ChangelogCreateError:
                    continue
                if not since_version:
                    # If a particular version was not requested we just
                    # return the most recent changelog entry.
                    break
        except ChangelogParseError:
            return None

        return "\n\n".join(chunks)
