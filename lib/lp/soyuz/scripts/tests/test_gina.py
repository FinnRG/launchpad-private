# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import hashlib
import os
import shutil
import subprocess
import tempfile
from doctest import DocTestSuite
from textwrap import dedent
from unittest import TestLoader

import apt_pkg
import transaction
from fixtures import EnvironmentVariableFixture
from testtools.matchers import MatchesSetwise, MatchesStructure

import lp.soyuz.scripts.gina.handlers
from lp.archiveuploader.tagfiles import parse_tagfile
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.series import SeriesStatus
from lp.scripts.helpers import TransactionFreeOperation
from lp.services.database.constants import UTC_NOW
from lp.services.features.testing import FeatureFixture
from lp.services.log.logger import DevNullLogger
from lp.services.osutils import write_file
from lp.services.tarfile_helpers import LaunchpadWriteTarFile
from lp.soyuz.enums import BinarySourceReferenceType, PackagePublishingStatus
from lp.soyuz.scripts.gina import ExecutionError
from lp.soyuz.scripts.gina.archive import (
    ArchiveComponentItems,
    ArchiveFilesystemInfo,
    PackagesMap,
)
from lp.soyuz.scripts.gina.dominate import dominate_imported_source_packages
from lp.soyuz.scripts.gina.handlers import (
    BinaryPackageHandler,
    BinaryPackagePublisher,
    ImporterHandler,
    SourcePackageHandler,
    SourcePackagePublisher,
)
from lp.soyuz.scripts.gina.packages import (
    BinaryPackageData,
    MissingRequiredArguments,
    SourcePackageData,
)
from lp.soyuz.scripts.gina.runner import import_sourcepackages
from lp.testing import TestCase, TestCaseWithFactory
from lp.testing.faketransaction import FakeTransaction
from lp.testing.layers import LaunchpadZopelessLayer, ZopelessDatabaseLayer


class FakePackagesMap:
    def __init__(self, src_map):
        self.src_map = src_map


class TestGina(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def assertPublishingStates(self, spphs, states):
        self.assertEqual(states, [pub.status for pub in spphs])

    def test_dominate_imported_source_packages_dominates_imports(self):
        # dominate_imported_source_packages dominates the source
        # packages that Gina imports.
        logger = DevNullLogger()
        txn = FakeTransaction()
        series = self.factory.makeDistroSeries()
        pocket = PackagePublishingPocket.RELEASE
        package = self.factory.makeSourcePackageName()

        # Realistic situation: there's an older, superseded publication;
        # a series of active ones; and a newer, pending publication
        # that's not in the Sources lists yet.
        # Gina dominates the Published ones and leaves the rest alone.
        old_spph = self.factory.makeSourcePackagePublishingHistory(
            distroseries=series,
            archive=series.main_archive,
            pocket=pocket,
            status=PackagePublishingStatus.SUPERSEDED,
            sourcepackagerelease=self.factory.makeSourcePackageRelease(
                sourcepackagename=package, version="1.0"
            ),
        )

        active_spphs = [
            self.factory.makeSourcePackagePublishingHistory(
                distroseries=series,
                archive=series.main_archive,
                pocket=pocket,
                status=PackagePublishingStatus.PUBLISHED,
                sourcepackagerelease=self.factory.makeSourcePackageRelease(
                    sourcepackagename=package, version=version
                ),
            )
            for version in ["1.1", "1.1.1", "1.1.1.1"]
        ]

        new_spph = self.factory.makeSourcePackagePublishingHistory(
            distroseries=series,
            archive=series.main_archive,
            pocket=pocket,
            status=PackagePublishingStatus.PENDING,
            sourcepackagerelease=self.factory.makeSourcePackageRelease(
                sourcepackagename=package, version="1.2"
            ),
        )

        spphs = [old_spph] + active_spphs + [new_spph]

        # Of the active publications, in this scenario, only one version
        # matches what Gina finds in the Sources list.  It stays
        # published; older active publications are superseded, newer
        # ones deleted.
        dominate_imported_source_packages(
            txn,
            logger,
            series.distribution.name,
            series.name,
            pocket,
            FakePackagesMap({package.name: [{"Version": "1.1.1"}]}),
        )
        states = [
            PackagePublishingStatus.SUPERSEDED,
            PackagePublishingStatus.SUPERSEDED,
            PackagePublishingStatus.PUBLISHED,
            PackagePublishingStatus.DELETED,
            PackagePublishingStatus.PENDING,
        ]
        self.assertPublishingStates(spphs, states)

    def test_dominate_imported_source_packages_dominates_deletions(self):
        # dominate_imported_source_packages dominates the source
        # packages that have been deleted from the Sources lists that
        # Gina imports.
        series = self.factory.makeDistroSeries()
        pocket = PackagePublishingPocket.RELEASE
        package = self.factory.makeSourcePackageName()
        pubs = [
            self.factory.makeSourcePackagePublishingHistory(
                archive=series.main_archive,
                distroseries=series,
                pocket=pocket,
                status=PackagePublishingStatus.PUBLISHED,
                sourcepackagerelease=self.factory.makeSourcePackageRelease(
                    sourcepackagename=package, version=version
                ),
            )
            for version in ["1.0", "1.1", "1.1a"]
        ]

        # In this scenario, 1.0 is a superseded release.
        pubs[0].supersede()
        logger = DevNullLogger()
        txn = FakeTransaction()
        dominate_imported_source_packages(
            txn,
            logger,
            series.distribution.name,
            series.name,
            pocket,
            FakePackagesMap({}),
        )

        # The older, superseded release stays superseded; but the
        # releases that dropped out of the imported Sources list without
        # known successors are marked deleted.
        self.assertPublishingStates(
            pubs,
            [
                PackagePublishingStatus.SUPERSEDED,
                PackagePublishingStatus.DELETED,
                PackagePublishingStatus.DELETED,
            ],
        )

    def test_dominate_imported_sources_dominates_supported_series(self):
        series = self.factory.makeDistroSeries()
        pocket = PackagePublishingPocket.RELEASE
        package = self.factory.makeSourcePackageName()
        pubs = [
            self.factory.makeSourcePackagePublishingHistory(
                archive=series.main_archive,
                distroseries=series,
                pocket=pocket,
                status=PackagePublishingStatus.PUBLISHED,
                sourcepackagerelease=self.factory.makeSourcePackageRelease(
                    sourcepackagename=package, version=version
                ),
            )
            for version in ["1.0", "1.1", "1.1a"]
        ]

        # In this scenario, 1.0 is a superseded release.
        pubs[0].supersede()
        # Now set the series to SUPPORTED.
        series.status = SeriesStatus.SUPPORTED
        logger = DevNullLogger()
        txn = FakeTransaction()
        dominate_imported_source_packages(
            txn,
            logger,
            series.distribution.name,
            series.name,
            pocket,
            FakePackagesMap({}),
        )
        self.assertPublishingStates(
            pubs,
            [
                PackagePublishingStatus.SUPERSEDED,
                PackagePublishingStatus.DELETED,
                PackagePublishingStatus.DELETED,
            ],
        )


class TestArchiveFilesystemInfo(TestCase):
    def assertCompressionTypeWorks(self, compressor_func):
        archive_root = self.useTempDir()
        sampledata_root = os.path.join(
            os.path.dirname(__file__), "gina_test_archive"
        )
        sampledata_component_dir = os.path.join(
            sampledata_root, "dists", "breezy", "main"
        )
        component_dir = os.path.join(archive_root, "dists", "breezy", "main")
        os.makedirs(os.path.join(component_dir, "source"))
        shutil.copy(
            os.path.join(sampledata_component_dir, "source", "Sources"),
            os.path.join(component_dir, "source", "Sources"),
        )
        compressor_func(os.path.join(component_dir, "source", "Sources"))
        os.makedirs(os.path.join(component_dir, "binary-i386"))
        shutil.copy(
            os.path.join(sampledata_component_dir, "binary-i386", "Packages"),
            os.path.join(component_dir, "binary-i386", "Packages"),
        )
        compressor_func(os.path.join(component_dir, "binary-i386", "Packages"))

        archive_info = ArchiveFilesystemInfo(
            archive_root, "breezy", "main", "i386"
        )
        try:
            with apt_pkg.TagFile(archive_info.srcfile) as sources:
                self.assertEqual("archive-copier", next(sources)["Package"])
            with apt_pkg.TagFile(archive_info.binfile) as binaries:
                self.assertEqual("python-pam", next(binaries)["Package"])
        finally:
            archive_info.cleanup()

    def test_uncompressed(self):
        self.assertCompressionTypeWorks(lambda path: None)

    def test_gzip(self):
        self.assertCompressionTypeWorks(
            lambda path: subprocess.check_call(["gzip", path])
        )

    def test_bzip2(self):
        self.assertCompressionTypeWorks(
            lambda path: subprocess.check_call(["bzip2", path])
        )

    def test_xz(self):
        self.assertCompressionTypeWorks(
            lambda path: subprocess.check_call(["xz", path])
        )


class TestSourcePackageData(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def test_unpack_dsc_with_vendor(self):
        # Some source packages unpack differently depending on dpkg's idea
        # of the "vendor", and in extreme cases may even fail with some
        # vendors.  gina always sets the vendor to the target distribution
        # name to ensure that it unpacks packages as if unpacking on that
        # distribution.
        archive_root = self.useTempDir()
        pool_dir = os.path.join(archive_root, "pool/main/f/foo")
        os.makedirs(pool_dir)

        # Synthesise a package that can be unpacked with DEB_VENDOR=debian
        # but not with DEB_VENDOR=ubuntu.
        with open(
            os.path.join(pool_dir, "foo_1.0.orig.tar.gz"), "wb+"
        ) as buffer:
            orig_tar = LaunchpadWriteTarFile(buffer, encoding="ISO-8859-1")
            orig_tar.add_directory("foo-1.0")
            # Add a Unicode file name (which becomes non-UTF-8 due to
            # encoding="ISO-8859-1" above) to ensure that shutil.rmtree is
            # called in such a way as to cope with non-UTF-8 file names on
            # Python 2.  See
            # https://bugs.launchpad.net/launchpad/+bug/1917449.
            orig_tar.add_file("íslenska.alias", b"Non-UTF-8 file name")
            orig_tar.close()
            buffer.seek(0)
            orig_tar_contents = buffer.read()
        with open(
            os.path.join(pool_dir, "foo_1.0-1.debian.tar.gz"), "wb+"
        ) as buffer:
            debian_tar = LaunchpadWriteTarFile(buffer)
            debian_tar.add_file("debian/source/format", b"3.0 (quilt)\n")
            debian_tar.add_file(
                "debian/patches/ubuntu.series", b"--- corrupt patch\n"
            )
            debian_tar.add_file("debian/rules", b"")
            debian_tar.close()
            buffer.seek(0)
            debian_tar_contents = buffer.read()
        dsc_path = os.path.join(pool_dir, "foo_1.0-1.dsc")
        with open(dsc_path, "w") as dsc:
            dsc.write(
                dedent(
                    """\
                Format: 3.0 (quilt)
                Source: foo
                Binary: foo
                Architecture: all
                Version: 1.0-1
                Maintainer: Foo Bar <foo.bar@canonical.com>
                Files:
                 %s %s foo_1.0.orig.tar.gz
                 %s %s foo_1.0-1.debian.tar.gz
                """
                    % (
                        hashlib.md5(orig_tar_contents).hexdigest(),
                        len(orig_tar_contents),
                        hashlib.md5(debian_tar_contents).hexdigest(),
                        len(debian_tar_contents),
                    )
                )
            )

        dsc_contents = parse_tagfile(dsc_path)
        dsc_contents["Directory"] = pool_dir.encode()
        dsc_contents["Package"] = b"foo"
        dsc_contents["Component"] = b"main"
        dsc_contents["Section"] = b"misc"

        sp_data = SourcePackageData(**dsc_contents)
        # Unpacking this in an Ubuntu context fails.
        self.assertRaises(
            ExecutionError, sp_data.do_package, "ubuntu", archive_root
        )
        self.assertFalse(os.path.exists("foo-1.0"))
        # But all is well in a Debian context.
        sp_data.do_package("debian", archive_root)
        self.assertFalse(os.path.exists("foo-1.0"))

    def test_process_package_cleans_up_after_unpack_failure(self):
        archive_root = self.useTempDir()
        pool_dir = os.path.join(archive_root, "pool/main/f/foo")
        os.makedirs(pool_dir)

        with open(
            os.path.join(pool_dir, "foo_1.0.orig.tar.gz"), "wb+"
        ) as buffer:
            orig_tar = LaunchpadWriteTarFile(buffer, encoding="ISO-8859-1")
            orig_tar.add_directory("foo-1.0")
            # Add a Unicode file name (which becomes non-UTF-8 due to
            # encoding="ISO-8859-1" above) to ensure that shutil.rmtree is
            # called in such a way as to cope with non-UTF-8 file names on
            # Python 2.  See
            # https://bugs.launchpad.net/launchpad/+bug/1917449.
            orig_tar.add_file("íslenska.alias", b"Non-UTF-8 file name")
            orig_tar.close()
            buffer.seek(0)
            orig_tar_contents = buffer.read()
        with open(
            os.path.join(pool_dir, "foo_1.0-1.debian.tar.gz"), "wb+"
        ) as buffer:
            debian_tar = LaunchpadWriteTarFile(buffer)
            debian_tar.add_file("debian/source/format", b"3.0 (quilt)\n")
            debian_tar.add_file(
                "debian/patches/series", b"--- corrupt patch\n"
            )
            debian_tar.add_file("debian/rules", b"")
            debian_tar.close()
            buffer.seek(0)
            debian_tar_contents = buffer.read()
        dsc_path = os.path.join(pool_dir, "foo_1.0-1.dsc")
        with open(dsc_path, "w") as dsc:
            dsc.write(
                dedent(
                    """\
                Format: 3.0 (quilt)
                Source: foo
                Binary: foo
                Architecture: all
                Version: 1.0-1
                Maintainer: Foo Bar <foo.bar@canonical.com>
                Files:
                 %s %s foo_1.0.orig.tar.gz
                 %s %s foo_1.0-1.debian.tar.gz
                """
                    % (
                        hashlib.md5(orig_tar_contents).hexdigest(),
                        len(orig_tar_contents),
                        hashlib.md5(debian_tar_contents).hexdigest(),
                        len(debian_tar_contents),
                    )
                )
            )

        dsc_contents = parse_tagfile(dsc_path)
        dsc_contents["Directory"] = pool_dir.encode()
        dsc_contents["Package"] = b"foo"
        dsc_contents["Component"] = b"main"
        dsc_contents["Section"] = b"misc"

        sp_data = SourcePackageData(**dsc_contents)
        unpack_tmpdir = self.makeTemporaryDirectory()
        with EnvironmentVariableFixture("TMPDIR", unpack_tmpdir):
            # Force tempfile to recheck TMPDIR.
            tempfile.tempdir = None
            try:
                self.assertRaises(
                    ExecutionError,
                    sp_data.process_package,
                    "ubuntu",
                    archive_root,
                )
            finally:
                # Force tempfile to recheck TMPDIR for future tests.
                tempfile.tempdir = None
        self.assertEqual([], os.listdir(unpack_tmpdir))

    def test_checksum_fields(self):
        # We only need one of Files or Checksums-*.
        base_dsc_contents = {
            "Package": b"foo",
            "Binary": b"foo",
            "Version": b"1.0-1",
            "Maintainer": b"Foo Bar <foo@canonical.com>",
            "Section": b"misc",
            "Architecture": b"all",
            "Directory": b"pool/main/f/foo",
            "Component": b"main",
        }
        for field in (
            "Files",
            "Checksums-Sha1",
            "Checksums-Sha256",
            "Checksums-Sha512",
        ):
            dsc_contents = dict(base_dsc_contents)
            dsc_contents[field] = b"xxx 000 foo_1.0-1.dsc"
            sp_data = SourcePackageData(**dsc_contents)
            self.assertEqual(["foo_1.0-1.dsc"], sp_data.files)
        self.assertRaises(
            MissingRequiredArguments, SourcePackageData, **base_dsc_contents
        )


class TestSourcePackageHandler(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def test_user_defined_fields(self):
        series = self.factory.makeDistroSeries()
        archive_root = self.useTempDir()
        sphandler = SourcePackageHandler(
            series.distribution.name,
            archive_root,
            PackagePublishingPocket.RELEASE,
            None,
        )
        sp_data = self.makeSourcePackageData(
            archive_root, {"Build-Indep-Architecture": b"amd64"}
        )
        self.assertEqual(
            [["Build-Indep-Architecture", "amd64"]], sp_data._user_defined
        )
        spr = sphandler.createSourcePackageRelease(sp_data, series)
        self.assertIsNotNone(spr)
        self.assertEqual(
            [["Build-Indep-Architecture", "amd64"]], spr.user_defined_fields
        )

    def makeSourcePackageData(self, archive_root, extra_fields=None):
        dsc_contents = {
            "Format": b"3.0 (quilt)",
            "Source": b"foo",
            "Binary": b"foo",
            "Architecture": b"all",
            "Version": b"1.0-1",
            "Maintainer": b"Foo Bar <foo@canonical.com>",
            "Files": b"xxx 000 foo_1.0-1.dsc",
            "Directory": b"pool/main/f/foo",
            "Package": b"foo",
            "Component": b"main",
            "Section": b"misc",
        }
        if extra_fields is not None:
            dsc_contents.update(extra_fields)
        sp_data = SourcePackageData(**dsc_contents)
        sp_data.archive_root = archive_root
        sp_data.dsc = ""
        sp_data.copyright = ""
        sp_data.urgency = "low"
        sp_data.changelog = None
        sp_data.changelog_entry = None
        sp_data.date_uploaded = UTC_NOW
        # We don't need a real .dsc here.
        write_file(
            os.path.join(archive_root, "pool/main/f/foo/foo_1.0-1.dsc"), b"x"
        )
        return sp_data

    def test_createSourcePackageRelease_stores_homepage(self):
        # The Homepage field from the Sources index is stored on the
        # created SourcePackageRelease.
        series = self.factory.makeDistroSeries()
        archive_root = self.useTempDir()
        sphandler = SourcePackageHandler(
            series.distribution.name,
            archive_root,
            PackagePublishingPocket.RELEASE,
            None,
        )
        sp_data = self.makeSourcePackageData(
            archive_root, {"Homepage": b"https://example.com/foo"}
        )
        self.assertEqual("https://example.com/foo", sp_data.homepage)
        spr = sphandler.createSourcePackageRelease(sp_data, series)
        self.assertIsNotNone(spr)
        self.assertEqual("https://example.com/foo", spr.homepage)

    def test_createSourcePackageRelease_without_homepage(self):
        # A Sources entry without a Homepage field produces a
        # SourcePackageRelease with no homepage.
        series = self.factory.makeDistroSeries()
        archive_root = self.useTempDir()
        sphandler = SourcePackageHandler(
            series.distribution.name,
            archive_root,
            PackagePublishingPocket.RELEASE,
            None,
        )
        sp_data = self.makeSourcePackageData(archive_root)
        self.assertIsNone(sp_data.homepage)
        spr = sphandler.createSourcePackageRelease(sp_data, series)
        self.assertIsNotNone(spr)
        self.assertIsNone(spr.homepage)


class TestSourcePackagePublisher(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def test_publish_creates_published_publication(self):
        maintainer = self.factory.makePerson()
        series = self.factory.makeDistroSeries()
        section = self.factory.makeSection()
        pocket = PackagePublishingPocket.RELEASE
        spr = self.factory.makeSourcePackageRelease()

        publisher = SourcePackagePublisher(series, pocket, None)
        publisher.publish(
            spr,
            SourcePackageData(
                component=b"main",
                section=section.name.encode("ASCII"),
                version=b"1.0",
                maintainer=maintainer.preferredemail.email.encode("ASCII"),
                architecture=b"all",
                files=b"foo.py",
                binaries=b"foo.py",
            ),
        )

        [spph] = series.main_archive.getPublishedSources()
        self.assertEqual(PackagePublishingStatus.PUBLISHED, spph.status)


class TestBinaryPackageData(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def test_checksum_fields(self):
        # We only need one of MD5sum or SHA*.
        base_deb_contents = {
            "Package": b"foo",
            "Installed-Size": b"0",
            "Maintainer": b"Foo Bar <foo@canonical.com>",
            "Section": b"misc",
            "Architecture": b"all",
            "Version": b"1.0-1",
            "Filename": b"pool/main/f/foo/foo_1.0-1_all.deb",
            "Component": b"main",
            "Size": b"0",
            "Description": b"",
            "Priority": b"extra",
        }
        for field in ("MD5sum", "SHA1", "SHA256", "SHA512"):
            deb_contents = dict(base_deb_contents)
            deb_contents[field] = b"0"
            BinaryPackageData(**deb_contents)
        self.assertRaises(
            MissingRequiredArguments, BinaryPackageData, **base_deb_contents
        )


class TestBinaryPackageHandler(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def test_user_defined_fields(self):
        das = self.factory.makeDistroArchSeries()
        archive_root = self.useTempDir()
        sphandler = SourcePackageHandler(
            das.distroseries.distribution.name,
            archive_root,
            PackagePublishingPocket.RELEASE,
            None,
        )
        bphandler = BinaryPackageHandler(
            sphandler, archive_root, PackagePublishingPocket.RELEASE
        )
        spr = self.factory.makeSourcePackageRelease(
            distroseries=das.distroseries
        )
        deb_contents = {
            "Package": b"foo",
            "Installed-Size": b"0",
            "Maintainer": b"Foo Bar <foo@canonical.com>",
            "Section": b"misc",
            "Architecture": b"amd64",
            "Version": b"1.0-1",
            "Filename": b"pool/main/f/foo/foo_1.0-1_amd64.deb",
            "Component": b"main",
            "Size": b"0",
            "MD5sum": b"0" * 32,
            "Description": b"",
            "Summary": b"",
            "Priority": b"extra",
            "Python-Version": b"2.7",
            "Built-Using": b"nonexistent (= 0.1)",
        }
        bp_data = BinaryPackageData(**deb_contents)
        self.assertContentEqual(
            [
                ["Python-Version", "2.7"],
                ["Built-Using", "nonexistent (= 0.1)"],
            ],
            bp_data._user_defined,
        )
        bp_data.archive_root = archive_root
        # We don't need a real .deb here.
        write_file(
            os.path.join(archive_root, "pool/main/f/foo/foo_1.0-1_amd64.deb"),
            b"x",
        )
        bpr = bphandler.createBinaryPackage(bp_data, spr, das, "amd64")
        self.assertIsNotNone(bpr)
        self.assertEqual([], bpr.built_using_references)
        self.assertContentEqual(
            [
                ["Python-Version", "2.7"],
                ["Built-Using", "nonexistent (= 0.1)"],
            ],
            bpr.user_defined_fields,
        )

    def test_resolvable_built_using(self):
        das = self.factory.makeDistroArchSeries()
        archive_root = self.useTempDir()
        sphandler = SourcePackageHandler(
            das.distroseries.distribution.name,
            archive_root,
            PackagePublishingPocket.RELEASE,
            None,
        )
        bphandler = BinaryPackageHandler(
            sphandler, archive_root, PackagePublishingPocket.RELEASE
        )
        spr = self.factory.makeSourcePackagePublishingHistory(
            distroseries=das.distroseries, component="main"
        ).sourcepackagerelease
        built_using_spph = self.factory.makeSourcePackagePublishingHistory(
            archive=das.main_archive,
            distroseries=das.distroseries,
            pocket=PackagePublishingPocket.RELEASE,
        )
        built_using_spr = built_using_spph.sourcepackagerelease
        built_using_relationship = "%s (= %s)" % (
            built_using_spr.name,
            built_using_spr.version,
        )
        deb_contents = {
            "Package": b"foo",
            "Installed-Size": b"0",
            "Maintainer": b"Foo Bar <foo@canonical.com>",
            "Section": b"misc",
            "Architecture": b"amd64",
            "Version": b"1.0-1",
            "Filename": b"pool/main/f/foo/foo_1.0-1_amd64.deb",
            "Component": b"main",
            "Size": b"0",
            "MD5sum": b"0" * 32,
            "Description": b"",
            "Summary": b"",
            "Priority": b"extra",
            "Built-Using": built_using_relationship.encode("ASCII"),
        }
        bp_data = BinaryPackageData(**deb_contents)
        self.assertContentEqual(
            [["Built-Using", built_using_relationship]], bp_data._user_defined
        )
        bp_data.archive_root = archive_root
        # We don't need a real .deb here.
        write_file(
            os.path.join(archive_root, "pool/main/f/foo/foo_1.0-1_amd64.deb"),
            b"x",
        )
        bpr = bphandler.createBinaryPackage(bp_data, spr, das, "amd64")
        self.assertIsNotNone(bpr)
        self.assertThat(
            bpr.built_using_references,
            MatchesSetwise(
                MatchesStructure.byEquality(
                    binary_package_release=bpr,
                    source_package_release=built_using_spr,
                    reference_type=BinarySourceReferenceType.BUILT_USING,
                )
            ),
        )
        self.assertContentEqual(
            [["Built-Using", built_using_relationship]],
            bpr.user_defined_fields,
        )


class TestBinaryPackagePublisher(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def test_publish_creates_published_publication(self):
        maintainer = self.factory.makePerson()
        series = self.factory.makeDistroArchSeries()
        section = self.factory.makeSection()
        pocket = PackagePublishingPocket.RELEASE
        bpr = self.factory.makeBinaryPackageRelease()

        publisher = BinaryPackagePublisher(series, pocket, None)
        publisher.publish(
            bpr,
            BinaryPackageData(
                component=b"main",
                section=section.name.encode("ASCII"),
                version=b"1.0",
                maintainer=maintainer.preferredemail.email.encode("ASCII"),
                architecture=b"all",
                files=b"foo.py",
                binaries=b"foo.py",
                size=128,
                installed_size=1024,
                md5sum=b"e83b5dd68079d727a494a469d40dc8db",
                description=b"test",
                summary=b"Test!",
            ),
        )

        [bpph] = series.main_archive.getAllPublishedBinaries()
        self.assertEqual(PackagePublishingStatus.PUBLISHED, bpph.status)


class TestRunner(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def test_import_sourcepackages_skip(self):
        # gina can be told to skip particular source versions by setting
        # soyuz.gina.skip_source_versions to a space-separated list of
        # $DISTRO/$NAME/$VERSION.
        series = self.factory.makeDistroSeries()

        archive_root = os.path.join(
            os.path.dirname(__file__), "gina_test_archive"
        )
        arch_component_items = ArchiveComponentItems(
            archive_root, "lenny", ["main"], [], True
        )
        packages_map = PackagesMap(arch_component_items)
        importer_handler = ImporterHandler(
            transaction,
            series.distribution.name,
            series.name,
            archive_root,
            PackagePublishingPocket.RELEASE,
            None,
        )

        def import_and_get_versions():
            # Unpacking source packages can be slow, so ensure that we don't
            # hold a transaction while doing so.
            with TransactionFreeOperation.require():
                import_sourcepackages(
                    series.distribution.name,
                    packages_map,
                    archive_root,
                    importer_handler,
                )
            return [
                p.source_package_version
                for p in series.main_archive.getPublishedSources(
                    name="archive-copier",
                    distroseries=series,
                    exact_match=True,
                )
            ]

        # Our test archive has archive-copier 0.1.5 and 0.3.6 With
        # soyuz.gina.skip_source_versions set to
        # '$distro/archive-copier/0.1.5', an import will grab only
        # 0.3.6.
        skiplist = "%s/archive-copier/0.1.5" % series.distribution.name
        with FeatureFixture({"soyuz.gina.skip_source_versions": skiplist}):
            self.assertContentEqual(["0.3.6"], import_and_get_versions())

        # Importing again without the feature flag removed grabs both.
        self.assertContentEqual(["0.1.5", "0.3.6"], import_and_get_versions())


def test_suite():
    suite = TestLoader().loadTestsFromName(__name__)
    suite.addTest(DocTestSuite(lp.soyuz.scripts.gina.handlers))
    return suite
