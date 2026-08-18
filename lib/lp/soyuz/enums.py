# Copyright 2010-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Enumerations used in the lp/soyuz modules."""

__all__ = [
    "ArchiveJobType",
    "ArchivePermissionType",
    "ArchivePublishingMethod",
    "ArchivePublishingSort",
    "ArchivePurpose",
    "ArchiveRepositoryFormat",
    "ArchiveStatus",
    "ArchiveSubscriberStatus",
    "archive_suffixes",
    "BinaryPackageFileType",
    "BinaryPackageFormat",
    "BinarySourceReferenceType",
    "DistroArchSeriesFilterSense",
    "IndexCompressionType",
    "PackageCopyPolicy",
    "PackageCopyStatus",
    "PackageDiffStatus",
    "PackagePublishingPriority",
    "PackagePublishingStatus",
    "PackageUploadCustomFormat",
    "PackageUploadStatus",
    "re_bug_numbers",
    "re_closes",
    "re_lp_closes",
    "SourcePackageFormat",
]

import re

from lazr.enum import DBEnumeratedType, DBItem, EnumeratedType, Item

# Regexes that match bug numbers for closing in change logs.
re_closes = re.compile(
    r"closes:\s*(?:bug)?\#?\s?\d+(?:,\s*(?:bug)?\#?\s?\d+)*", re.I
)
re_lp_closes = re.compile(r"lp:\s+\#\d+(?:,\s*\#\d+)*", re.I)
re_bug_numbers = re.compile(r"\#?\s?(\d+)")


class ArchiveJobType(DBEnumeratedType):
    """Values that IArchiveJob.job_type can take."""

    # 0 was once used for COPY_ARCHIVE.

    PACKAGE_UPLOAD_NOTIFICATION = DBItem(
        1,
        """
        Package upload notification

        Send notifications about a package upload being accepted, rejected,
        or held for approval.
        """,
    )

    CI_BUILD_UPLOAD = DBItem(
        2,
        """
        CI build upload

        Upload a CI build to this archive.
        """,
    )


class ArchivePermissionType(DBEnumeratedType):
    """Archive Permission Type.

    The permission being granted, such as upload rights, or queue
    manipulation rights.
    """

    UPLOAD = DBItem(
        1,
        """
        Archive Upload Rights

        This permission allows a user to upload.
        """,
    )

    QUEUE_ADMIN = DBItem(
        2,
        """
        Queue Administration Rights

        This permission allows a user to administer the distroseries
        upload queue.
        """,
    )


class ArchivePurpose(DBEnumeratedType):
    """The purpose, or type, of an archive.

    A distribution can be associated with different archives and this
    schema item enumerates the different archive types and their purpose.

    For example, Partner/ISV software in ubuntu is stored in a separate
    archive. PPAs are separate archives and contain packages that 'overlay'
    the ubuntu PRIMARY archive.
    """

    PRIMARY = DBItem(
        1,
        """
        Primary Archive

        This is the primary Ubuntu archive.
        """,
    )

    PPA = DBItem(
        2,
        """
        PPA Archive

        This is a Personal Package Archive.
        """,
    )

    PARTNER = DBItem(
        4,
        """
        Partner Archive

        This is the archive for partner packages.
        """,
    )

    COPY = DBItem(
        6,
        """
        Generalized copy archive

        This kind of archive will be used for rebuilds, snapshots etc.
        """,
    )


archive_suffixes = {
    ArchivePurpose.PRIMARY: "",
    ArchivePurpose.PARTNER: "-partner",
}


class ArchiveStatus(DBEnumeratedType):
    """The status of an archive, e.g. active, disabled."""

    ACTIVE = DBItem(
        0,
        """
        Active

        This archive accepts uploads, copying and publishes packages.
        """,
    )

    DELETING = DBItem(
        1,
        """
        Deleting

        This archive is in the process of being deleted.  This is a user-
        requested and short-lived status.
        """,
    )

    DELETED = DBItem(
        2,
        """
        Deleted

        This archive has been deleted and removed from disk.
        """,
    )


class ArchiveSubscriberStatus(DBEnumeratedType):
    """The status of an `ArchiveSubscriber`."""

    CURRENT = DBItem(
        1,
        """
        Active

        The subscription is current.
        """,
    )

    EXPIRED = DBItem(
        2,
        """
        Expired

        The subscription has expired.
        """,
    )

    CANCELLED = DBItem(
        3,
        """
        Cancelled

        The subscription was cancelled.
        """,
    )


class BinaryPackageFileType(DBEnumeratedType):
    """Binary Package File Type

    Launchpad handles a variety of packaging systems and binary package
    formats. This schema documents the known binary package file types.
    """

    DEB = DBItem(
        1,
        """
        DEB Format

        This format is the standard package format used on Ubuntu and other
        similar operating systems.
        """,
    )

    RPM = DBItem(
        2,
        """
        RPM Format

        This format is used on mandrake, Red Hat, Suse and other similar
        distributions.
        """,
    )

    UDEB = DBItem(
        3,
        """
        UDEB Format

        This format is the standard package format used on Ubuntu and other
        similar operating systems for the installation system.
        """,
    )

    DDEB = DBItem(
        4,
        """
        DDEB Format

        This format is the standard package format used on Ubuntu and other
        similar operating systems for distributing debug symbols.
        """,
    )

    BUILDINFO = DBItem(
        5,
        """
        Build information

        A file used by Debian-based systems to record information about the
        build environment.
        """,
    )

    WHL = DBItem(
        6,
        """
        Python Wheel

        The "wheel" binary package format for Python, originally defined in
        U{https://peps.python.org/pep-0427/}.
        """,
    )

    CONDA_V1 = DBItem(
        7,
        """
        Conda Package v1

        Version 1 of the Conda package format, with the ".tar.bz2" extension.
        """,
    )

    CONDA_V2 = DBItem(
        8,
        """
        Conda Package v2

        Version 2 of the Conda package format, with the ".conda" extension;
        introduced in Conda 4.7.
        """,
    )

    # This is particularly confusing for Crate packages since they don't have
    # a formal package format specification - they're just tarballs of source
    # code. We still need entries in both enums to maintain database
    # consistency.
    CRATE = DBItem(
        10,
        """
        Crate Package

        The "crate" binary package format for Rust.
        """,
    )

    GENERIC = DBItem(
        9,
        """
        Generic binary file

        This file is a generic binary file without any particular known
        metadata of its own, produced by a CI build.
        """,
    )


class BinaryPackageFormat(DBEnumeratedType):
    """Binary Package Format

    Launchpad tracks a variety of binary package formats. This schema
    documents the list of binary package formats that are supported
    in Launchpad.
    """

    DEB = DBItem(
        1,
        """
        Ubuntu Package

        This is the binary package format used by Ubuntu and all similar
        distributions. It includes dependency information to allow the
        system to ensure it always has all the software installed to make
        any new package work correctly.  """,
    )

    UDEB = DBItem(
        2,
        """
        Ubuntu Installer Package

        This is the binary package format used by the installer in Ubuntu and
        similar distributions.  """,
    )

    EBUILD = DBItem(
        3,
        """
        Gentoo Ebuild Package

        This is the Gentoo binary package format. While Gentoo is primarily
        known for being a build-it-from-source-yourself kind of
        distribution, it is possible to exchange binary packages between
        Gentoo systems.  """,
    )

    RPM = DBItem(
        4,
        """
        RPM Package

        This is the format used by Mandrake and other similar distributions.
        It does not include dependency tracking information.  """,
    )

    DDEB = DBItem(
        5,
        """
        Ubuntu Debug Package

        This is the binary package format used for shipping debug symbols
        in Ubuntu and similar distributions.""",
    )

    WHL = DBItem(
        6,
        """
        Python Wheel

        The "wheel" binary package format for Python, originally defined in
        U{https://peps.python.org/pep-0427/}.
        """,
    )

    CONDA_V1 = DBItem(
        7,
        """
        Conda Package v1

        Version 1 of the Conda package format, with the ".tar.bz2" extension.
        """,
    )

    CONDA_V2 = DBItem(
        8,
        """
        Conda Package v2

        Version 2 of the Conda package format, with the ".conda" extension;
        introduced in Conda 4.7.
        """,
    )

    # As noted above, there's unclear distinction between BinaryPackageFormat
    # and BinaryPackageFileType. CRATE was added for Rust crate packages,
    # requiring entries in both enums.
    CRATE = DBItem(
        10,
        """
        Crate Package

        The "crate" binary package format for Rust.
        """,
    )

    GENERIC = DBItem(
        9,
        """
        Generic binary

        This is a generic binary format without any particular known
        metadata of its own, produced by a CI build.
        """,
    )


class PackageCopyPolicy(DBEnumeratedType):
    """Package copying policy.

    Each of these is associated with one `ICopyPolicy`.
    """

    INSECURE = DBItem(
        1,
        """
        Copy from insecure source.

        This is the default.
        """,
    )

    MASS_SYNC = DBItem(
        2,
        """
        Mass package sync.

        This policy applies when synchronizing packages en masse.
        """,
    )


class PackageCopyStatus(DBEnumeratedType):
    """Package copy status type.

    The status may be one of the following: new, in progress, complete,
    failed, canceling, cancelled.
    """

    NEW = DBItem(
        0,
        """
        New

        A new package copy operation was requested.
        """,
    )

    INPROGRESS = DBItem(
        1,
        """
        In progress

        The package copy operation is in progress.
        """,
    )

    COMPLETE = DBItem(
        2,
        """
        Complete

        The package copy operation has completed successfully.
        """,
    )

    FAILED = DBItem(
        3,
        """
        Failed

        The package copy operation has failed.
        """,
    )

    CANCELING = DBItem(
        4,
        """
        Canceling

        The package copy operation was cancelled by the user and the
        cancellation is in progress.
        """,
    )

    CANCELLED = DBItem(
        5,
        """
        Cancelled

        The package copy operation was cancelled by the user.
        """,
    )


class PackageDiffStatus(DBEnumeratedType):
    """The status of a PackageDiff request."""

    PENDING = DBItem(
        0,
        """
        Pending

        This diff request is pending processing.
        """,
    )

    COMPLETED = DBItem(
        1,
        """
        Completed

        This diff request was successfully completed.
        """,
    )

    FAILED = DBItem(
        2,
        """
        Failed

        This diff request has failed.
        """,
    )


class PackagePublishingPriority(DBEnumeratedType):
    """Package Publishing Priority

    Binary packages have a priority which is related to how important
    it is to have that package installed in a system. Common priorities
    range from required to optional and various others are available.
    """

    REQUIRED = DBItem(
        50,
        """
        Required

        This priority indicates that the package is required. This priority
        is likely to be hard-coded into various package tools. Without all
        the packages at this priority it may become impossible to use dpkg.
        """,
    )

    IMPORTANT = DBItem(
        40,
        """
        Important

        If foo is in a package; and "What is going on?! Where on earth is
        foo?!?!" would be the reaction of an experienced UNIX hacker were
        the package not installed, then the package is important.
        """,
    )

    STANDARD = DBItem(
        30,
        """
        Standard

        Packages at this priority are standard ones you can rely on to be in
        a distribution. They will be installed by default and provide a
        basic character-interface userland.
        """,
    )

    OPTIONAL = DBItem(
        20,
        """
        Optional

        This is the software you might reasonably want to install if you did
        not know what it was or what your requiredments were. Systems such
        as X or TeX will live here.
        """,
    )

    EXTRA = DBItem(
        10,
        """
        Extra

        This contains all the packages which conflict with those at the
        other priority levels; or packages which are only useful to people
        who have very specialised needs.
        """,
    )


class PackagePublishingStatus(DBEnumeratedType):
    """Package Publishing Status

    A package has various levels of being published within a DistroSeries.
    This is important because of how new source uploads dominate binary
    uploads bit-by-bit. Packages (source or binary) enter the publishing
    tables as 'Pending', progress through to 'Published' eventually become
    'Superseded' and then become 'PendingRemoval'. Once removed from the
    DistroSeries the publishing record is also removed.
    """

    PENDING = DBItem(
        1,
        """
        Pending

        This [source] package has been accepted into the DistroSeries and
        is now pending the addition of the files to the published disk area.
        In due course, this source package will be published.
        """,
    )

    PUBLISHED = DBItem(
        2,
        """
        Published

        This package is currently published as part of the archive for that
        distroseries. In general there will only ever be one version of any
        source/binary package published at any one time. Once a newer
        version becomes published the older version is marked as superseded.
        """,
    )

    SUPERSEDED = DBItem(
        3,
        """
        Superseded

        When a newer version of a [source] package is published the existing
        one is marked as "superseded".  """,
    )

    DELETED = DBItem(
        4,
        """
        Deleted

        When a publication was "deleted" from the archive by user request.
        Records in this state contain a reference to the Launchpad user
        responsible for the deletion and a text comment with the removal
        reason.
        """,
    )

    OBSOLETE = DBItem(
        5,
        """
        Obsolete

        When a distroseries becomes obsolete, its published packages
        are no longer required in the archive.  The publications for
        those packages are marked as "obsolete" and are subsequently
        removed during domination and death row processing.
        """,
    )


# If you change this (add items, change the meaning, whatever) search for
# the token ##CUSTOMFORMAT## e.g. queue.py or nascentupload.py and update
# the stuff marked with it.
class PackageUploadCustomFormat(DBEnumeratedType):
    """Custom formats valid for the upload queue

    An upload has various files potentially associated with it, from source
    package releases, through binary builds, to specialist upload forms such
    as a debian-installer tarball or a set of translations.
    """

    DEBIAN_INSTALLER = DBItem(
        0,
        """
        raw-installer

        A raw-installer file is a tarball. This is processed as a version
        of the debian-installer to be unpacked into the archive root.
        """,
    )

    ROSETTA_TRANSLATIONS = DBItem(
        1,
        """
        raw-translations

        A raw-translations file is a tarball. This is passed to the rosetta
        import queue to be incorporated into that package's translations.
        """,
    )

    DIST_UPGRADER = DBItem(
        2,
        """
        raw-dist-upgrader

        A raw-dist-upgrader file is a tarball. It is simply published into
        the archive.
        """,
    )

    DDTP_TARBALL = DBItem(
        3,
        """
        raw-ddtp-tarball

        A raw-ddtp-tarball contains all the translated package description
        indexes for a component.
        """,
    )

    STATIC_TRANSLATIONS = DBItem(
        4,
        """
        raw-translations-static

        A tarball containing raw (Gnome) help file translations.
        """,
    )

    META_DATA = DBItem(
        5,
        """
        meta-data

        A file containing meta-data about the package, mainly for use in
        the Software Center.
        """,
    )

    UEFI = DBItem(
        6,
        """
        uefi

        A tarball containing EFI images to be signed.
        """,
    )

    SIGNING = DBItem(
        7,
        """
        signing

        A tarball containing images to be signed.
        """,
    )


class PackageUploadStatus(DBEnumeratedType):
    """Distro Release Queue Status

    An upload has various stages it must pass through before becoming part
    of a DistroSeries. These are managed via the Upload table
    and related tables and eventually (assuming a successful upload into the
    DistroSeries) the effects are published via the PackagePublishing and
    SourcePackagePublishing tables.
    """

    NEW = DBItem(
        0,
        """
        New

        This upload is either a brand-new source package or contains a
        binary package with brand new debs or similar. The package must sit
        here until someone with the right role in the DistroSeries checks
        and either accepts or rejects the upload. If the upload is accepted
        then entries will be made in the overrides tables and further
        uploads will bypass this state. """,
    )

    UNAPPROVED = DBItem(
        1,
        """
        Unapproved

        If a DistroSeries is frozen or locked out of ordinary updates then
        this state is used to mean that while the package is correct from a
        technical point of view; it has yet to be approved for inclusion in
        this DistroSeries. One use of this state may be for security
        releases where you want the security team of a DistroSeries to
        approve uploads.""",
    )

    ACCEPTED = DBItem(
        2,
        """
        Accepted

        An upload in this state has passed all the checks required of it and
        is ready to have its publishing records created.""",
    )

    DONE = DBItem(
        3,
        """
        Done

        An upload in this state has had its publishing records created if it
        needs them and is fully processed into the DistroSeries. This state
        exists so that a logging and/or auditing tool can pick up accepted
        uploads and create entries in a journal or similar before removing
        the queue item.""",
    )

    REJECTED = DBItem(
        4,
        """
        Rejected

        An upload which reaches this state has, for some reason or another
        not passed the requirements (technical or human) for entry into the
        DistroSeries it was targeting. As for the 'done' state, this state
        is present to allow logging tools to record the rejection and then
        clean up any subsequently unnecessary records.""",
    )


class SourcePackageFormat(DBEnumeratedType):
    """Source package format

    There are currently three formats of Debian source packages. The Format
    field in the .dsc file must specify one of these formats.
    """

    FORMAT_1_0 = DBItem(
        0,
        """
        1.0

        Specifies either a native (having a single tar.gz) or non-native
        (having an orig.tar.gz and a diff.gz) package. Supports only gzip
        compression.
        """,
    )

    FORMAT_3_0_QUILT = DBItem(
        1,
        """
        3.0 (quilt)

        Specifies a non-native package, with an orig.tar.* and a debian.tar.*.
        Supports gzip, bzip2, and xz compression.
        """,
    )

    FORMAT_3_0_NATIVE = DBItem(
        2,
        """
        3.0 (native)

        Specifies a native package, with a single tar.*. Supports gzip,
        bzip2, and xz compression.
        """,
    )


class IndexCompressionType(DBEnumeratedType):
    """Index compression type

    Archive indexes such as Packages and Sources may be compressed using any
    of several different schemes.
    """

    UNCOMPRESSED = DBItem(0, "uncompressed")
    GZIP = DBItem(1, "gzip")
    BZIP2 = DBItem(2, "bzip2")
    XZ = DBItem(3, "xz")


class DistroArchSeriesFilterSense(DBEnumeratedType):
    INCLUDE = DBItem(
        1,
        """
        Include

        Packages in this package set are included in the distro arch series.
        """,
    )

    EXCLUDE = DBItem(
        2,
        """
        Exclude

        Packages in this package set are excluded from the distro arch series.
        """,
    )


class BinarySourceReferenceType(DBEnumeratedType):
    """The type of a reference from a binary package to a source package."""

    BUILT_USING = DBItem(
        1,
        """
        Built-Using

        The referencing binary package incorporates part of the referenced
        source package, and so the referenced source package needs to remain
        in the archive for as long as the referencing binary package does.
        """,
    )


class ArchivePublishingMethod(DBEnumeratedType):
    """The method used to publish an archive."""

    LOCAL = DBItem(
        0,
        """
        Local

        Publish locally, either using apt-ftparchive or writing indexes
        directly from the database depending on the archive purpose.
        """,
    )

    ARTIFACTORY = DBItem(
        1,
        """
        Artifactory

        Publish via JFrog Artifactory.
        """,
    )


class ArchiveRepositoryFormat(DBEnumeratedType):
    """The repository format used by an archive."""

    DEBIAN = DBItem(
        0,
        """
        Debian

        A Debian-format apt archive
        (https://wiki.debian.org/DebianRepository/Format).
        """,
    )

    PYTHON = DBItem(
        1,
        """
        Python

        A Python package index (https://www.python.org/dev/peps/pep-0301/).
        """,
    )

    CONDA = DBItem(
        2,
        """
        Conda

        A Conda channel
        (https://docs.conda.io/projects/conda/en/latest/user-guide/concepts/channels.html).
        """,
    )

    GO_PROXY = DBItem(
        3,
        """
        Go

        A Go registry, laid out as a module proxy
        (https://go.dev/ref/mod#module-proxy).
        """,
    )

    GENERIC = DBItem(
        4,
        """
        Generic

        A generic repository with a basic name/version layout and no indexing.
        """,
    )

    RUST = DBItem(
        5,
        """
        Rust

        A Rust package index
        """,
    )


class ArchivePublishingSort(EnumeratedType):
    """Choices for how to sort list of package publishings."""

    PUBLISHED_DATE_ASC = Item(
        """
        published_date_asc

        Sort by package publication date in ascending order (oldest first).
        """
    )

    PUBLISHED_DATE_DESC = Item(
        """
        published_date_desc

        Sort by package publication date in descending order (newest first).
        """
    )

    SOURCE_NAME_ASC = Item(
        """
        source_name_asc

        Sort by source package name in ascending order (A to Z).
        """
    )

    SOURCE_NAME_DESC = Item(
        """
        source_name_desc

        Sort by source package name in descending order (Z to A).
        """
    )

    SERIES_ASC = Item(
        """
        series_asc

        Sort by distro series in ascending order (oldest first).
        """
    )

    SERIES_DESC = Item(
        """
        series_desc

        Sort by distro series in descending order (newest first).
        """
    )

    POCKET_ASC = Item(
        """
        pocket_asc

        Sort by pocket in ascending order.
        """
    )

    POCKET_DESC = Item(
        """
        pocket_desc

        Sort by pocket in descending order.
        """
    )

    BINARY_COUNT_ASC = Item(
        """
        binary_count_asc

        Sort by the number of built binary packages in ascending order
        (fewest first).
        """
    )

    BINARY_COUNT_DESC = Item(
        """
        binary_count_desc

        Sort by the number of built binary packages in descending order
        (most first).
        """
    )

    STATUS_ASC = Item(
        """
        status_asc

        Sort by publishing status in ascending order.
        """
    )

    STATUS_DESC = Item(
        """
        status_desc

        Sort by publishing status in descending order.
        """
    )
