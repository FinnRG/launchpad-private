# Copyright 2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Artifactory pool tests."""

from pathlib import PurePath

import transaction
from artifactory import ArtifactoryPath
from zope.component import getUtility

from lp.archivepublisher.artifactory import ArtifactoryPool
from lp.archivepublisher.tests.artifactory_fixture import (
    FakeArtifactoryFixture,
)
from lp.archivepublisher.tests.test_pool import (
    FakeArchive,
    FakePackageReleaseFile,
    FakeReleaseType,
    PoolTestingFile,
)
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.sourcepackage import (
    SourcePackageFileType,
    SourcePackageType,
)
from lp.services.log.logger import BufferLogger
from lp.soyuz.enums import (
    ArchivePurpose,
    ArchiveRepositoryFormat,
    BinaryPackageFileType,
    BinaryPackageFormat,
)
from lp.soyuz.interfaces.publishing import (
    IgnorableArtifactoryPoolFileOverwriteError,
    IPublishingSet,
    PoolFileOverwriteError,
)
from lp.testing import TestCase, TestCaseWithFactory
from lp.testing.layers import BaseLayer, LaunchpadZopelessLayer


class ArtifactoryPoolTestingFile(PoolTestingFile):
    """`PoolTestingFile` variant for Artifactory.

    Artifactory publishing doesn't use the component to form paths, and has
    some additional features.
    """

    def addToPool(self, component=None):
        return super().addToPool(None)

    def removeFromPool(self, component=None):
        return super().removeFromPool(None)

    def checkExists(self, component=None):
        return super().checkExists(None)

    def checkIsLink(self, component=None):
        return super().checkIsLink(None)

    def checkIsFile(self, component=None):
        return super().checkIsFile(None)

    def getProperties(self):
        path = self.pool.pathFor(
            None, self.source_name, self.source_version, self.pub_file
        )
        return path.properties


class TestArtifactoryPool(TestCase):
    layer = BaseLayer

    def setUp(self):
        super().setUp()
        self.base_url = "https://foo.example.com/artifactory"
        self.repository_name = "repository"
        self.artifactory = self.useFixture(
            FakeArtifactoryFixture(self.base_url, self.repository_name)
        )

    def makePool(self, repository_format=ArchiveRepositoryFormat.DEBIAN):
        # Matches behaviour of lp.archivepublisher.config.getPubConfig.
        root_url = "%s/%s" % (self.base_url, self.repository_name)
        if repository_format == ArchiveRepositoryFormat.DEBIAN:
            root_url += "/pool"
        return ArtifactoryPool(
            FakeArchive(repository_format), root_url, BufferLogger()
        )

    def test_pathFor_debian_with_file(self):
        pool = self.makePool()
        pub_file = FakePackageReleaseFile(b"foo", "foo-1.0.deb")
        self.assertEqual(
            ArtifactoryPath(
                "https://foo.example.com/artifactory/repository/pool/f/foo/"
                "foo-1.0.deb"
            ),
            pool.pathFor(None, "foo", "1.0", pub_file),
        )

    def test_pathFor_python_with_file(self):
        pool = self.makePool(ArchiveRepositoryFormat.PYTHON)
        pub_file = FakePackageReleaseFile(b"foo", "foo-1.0.whl")
        self.assertEqual(
            ArtifactoryPath(
                "https://foo.example.com/artifactory/repository/foo/1.0/"
                "foo-1.0.whl"
            ),
            pool.pathFor(None, "foo", "1.0", pub_file),
        )

    def test_pathFor_conda_with_file(self):
        pool = self.makePool(ArchiveRepositoryFormat.CONDA)
        pub_file = FakePackageReleaseFile(
            b"foo",
            "foo-1.0.tar.bz2",
            user_defined_fields=[("subdir", "linux-64")],
        )
        self.assertEqual(
            ArtifactoryPath(
                "https://foo.example.com/artifactory/repository/linux-64/"
                "foo-1.0.tar.bz2"
            ),
            pool.pathFor(None, "foo", "1.0", pub_file),
        )

    def test_pathFor_conda_with_file_no_subir(self):
        pool = self.makePool(ArchiveRepositoryFormat.CONDA)
        pub_file = FakePackageReleaseFile(
            b"foo",
            "foo-1.0.tar.bz2",
            user_defined_fields=[("bogus_filed", "instead_of_subdir")],
        )
        self.assertRaises(
            ValueError, pool.pathFor, None, "foo", "1.0", pub_file
        )

    def test_pathFor_go_proxy_with_file(self):
        pool = self.makePool(ArchiveRepositoryFormat.GO_PROXY)
        pub_file = FakePackageReleaseFile(
            b"go-module",
            "v1.0.zip",
            release_type=FakeReleaseType.SOURCE,
            user_defined_fields=[("module-path", "launchpad.net/go-module")],
        )
        self.assertEqual(
            ArtifactoryPath(
                "https://foo.example.com/artifactory/repository/"
                "launchpad.net/go-module/@v/v1.0.zip"
            ),
            pool.pathFor(None, "go-module", "v1.0", pub_file),
        )

    def test_pathFor_generic_with_file(self):
        pool = self.makePool(ArchiveRepositoryFormat.GENERIC)
        pub_file = FakePackageReleaseFile(
            b"source artifact",
            "foo-1.0.tar.gz",
            release_type=FakeReleaseType.SOURCE,
            user_defined_fields=[
                ("name", "foo"),
                ("version", "1.0"),
                ("source", True),
            ],
        )
        self.assertEqual(
            ArtifactoryPath(
                "https://foo.example.com/artifactory/repository/"
                "foo/1.0/foo-1.0.tar.gz"
            ),
            pool.pathFor(None, "foo-generic", "1.0", pub_file),
        )

    def test_addFile(self):
        pool = self.makePool()
        foo = ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="foo",
            source_version="1.0+1",
            filename="foo-1.0+1.deb",
            release_type=FakeReleaseType.BINARY,
            release_id=1,
        )
        self.assertFalse(foo.checkIsFile())
        result = foo.addToPool()
        self.assertEqual(pool.results.FILE_ADDED, result)
        self.assertTrue(foo.checkIsFile())
        self.assertEqual(
            {
                "launchpad.release-id": ["binary:1"],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0+1"],
                "soss.type": ["binary"],
            },
            foo.getProperties(),
        )

    def test_addFile_exists_identical(self):
        pool = self.makePool()
        foo = ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="foo",
            source_version="1.0",
            filename="foo-1.0.deb",
            release_type=FakeReleaseType.BINARY,
            release_id=1,
        )
        foo.addToPool()
        self.assertTrue(foo.checkIsFile())
        result = foo.addToPool()
        self.assertEqual(pool.results.NONE, result)
        self.assertTrue(foo.checkIsFile())

    def test_addFile_exists_overwrite_non_ignored_archive_types(self):
        # PoolFileOverwriteErrors are raised only for non-Python, non-Conda,
        # non-source archives' Artifactory files.
        pool = self.makePool()
        foo = ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="foo",
            source_version="1.0",
            filename="foo-1.0.deb",
            release_type=FakeReleaseType.BINARY,
            release_id=1,
        )
        foo.addToPool()
        self.assertTrue(foo.checkIsFile())
        foo.pub_file.libraryfile.contents = b"different"
        self.assertRaises(PoolFileOverwriteError, foo.addToPool)

    def test_addFile_exists_overwrite_ignored_for_python_archives(self):
        # IgnorableArtifactoryPoolFileOverwriteErrors are raised only
        # for Python, Conda, and source archive Artifactory files. See
        # XXX comments in the appropriate sources for more details.

        pool = self.makePool(repository_format=ArchiveRepositoryFormat.PYTHON)
        foo = ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="foo",
            source_version="1.0",
            filename="foo-1.0.whl",
            release_type=FakeReleaseType.BINARY,
            release_id=1,
        )
        foo.addToPool()
        self.assertTrue(foo.checkIsFile())
        foo.pub_file.libraryfile.contents = b"different"
        self.assertRaises(
            IgnorableArtifactoryPoolFileOverwriteError, foo.addToPool
        )

    def test_addFile_exists_overwrite_ignored_for_conda_archives(self):
        # IgnorableArtifactoryPoolFileOverwriteErrors are raised only
        # for Python, Conda, and source archive Artifactory files. See
        # XXX comments in the appropriate sources for more details.

        pool = self.makePool(repository_format=ArchiveRepositoryFormat.CONDA)
        foo = ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="foo",
            source_version="1.0",
            filename="foo-1.0.tar.bz2",
            release_type=FakeReleaseType.BINARY,
            release_id=1,
            user_defined_fields=[("subdir", "linux-64")],
        )
        foo.addToPool()
        self.assertTrue(foo.checkIsFile())
        foo.pub_file.libraryfile.contents = b"different"
        self.assertRaises(
            IgnorableArtifactoryPoolFileOverwriteError, foo.addToPool
        )

    def test_addFile_exists_overwrite_ignored_for_source_archives(self):
        # IgnorableArtifactoryPoolFileOverwriteErrors are raised only
        # for Python, Conda, and source archive Artifactory files. See
        # XXX comments in the appropriate sources for more details.

        pool = self.makePool(repository_format=ArchiveRepositoryFormat.GENERIC)
        foo = ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="foo",
            source_version="1.0",
            filename="foo-1.0.tar.gz",
            release_type=FakeReleaseType.SOURCE,
            release_id=1,
        )
        foo.addToPool()
        self.assertTrue(foo.checkIsFile())
        foo.pub_file.libraryfile.contents = b"different"
        self.assertRaises(
            IgnorableArtifactoryPoolFileOverwriteError, foo.addToPool
        )

    def test_removeFile(self):
        pool = self.makePool()
        foo = ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="foo",
            source_version="1.0",
            filename="foo-1.0.deb",
        )
        foo.addToPool()
        self.assertTrue(foo.checkIsFile())
        size = foo.removeFromPool()
        self.assertFalse(foo.checkExists())
        self.assertEqual(3, size)

    def test_getArtifactPatterns_debian(self):
        pool = self.makePool()
        self.assertEqual(
            [
                "*.ddeb",
                "*.deb",
                "*.diff.*",
                "*.dsc",
                "*.tar.*",
                "*.udeb",
            ],
            pool.getArtifactPatterns(ArchiveRepositoryFormat.DEBIAN),
        )

    def test_getArtifactPatterns_python(self):
        pool = self.makePool()
        self.assertEqual(
            [
                "*.tar.gz",
                "*.whl",
                "*.zip",
            ],
            pool.getArtifactPatterns(ArchiveRepositoryFormat.PYTHON),
        )

    def test_getArtifactPatterns_conda(self):
        pool = self.makePool()
        self.assertEqual(
            [
                "*.tar.bz2",
                "*.conda",
            ],
            pool.getArtifactPatterns(ArchiveRepositoryFormat.CONDA),
        )

    def test_getArtifactPatterns_go_proxy(self):
        pool = self.makePool()
        self.assertEqual(
            [
                "*.info",
                "*.mod",
                "*.zip",
            ],
            pool.getArtifactPatterns(ArchiveRepositoryFormat.GO_PROXY),
        )

    def test_getArtifactPatterns_rust(self):
        pool = self.makePool()
        self.assertEqual(
            ["*.crate"],
            pool.getArtifactPatterns(ArchiveRepositoryFormat.RUST),
        )

    def test_getArtifactPatterns_generic(self):
        pool = self.makePool()
        self.assertEqual(
            ["*"], pool.getArtifactPatterns(ArchiveRepositoryFormat.GENERIC)
        )

    def test_getAllArtifacts_debian(self):
        # getAllArtifacts mostly relies on constructing a correct AQL query,
        # which we can't meaningfully test without a real Artifactory
        # instance, although `FakeArtifactoryFixture` tries to do something
        # with it.  These tests mainly ensure that we transform the response
        # correctly.
        pool = self.makePool(ArchiveRepositoryFormat.DEBIAN)
        ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="foo",
            source_version="1.0",
            filename="foo-1.0.deb",
            release_type=FakeReleaseType.BINARY,
            release_id=1,
        ).addToPool()
        ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="foo",
            source_version="1.1",
            filename="foo-1.1.deb",
            release_type=FakeReleaseType.BINARY,
            release_id=2,
        ).addToPool()
        self.assertEqual(
            {
                PurePath("pool/f/foo/foo-1.0.deb"): {
                    "launchpad.release-id": ["binary:1"],
                    "launchpad.source-name": ["foo"],
                    "launchpad.source-version": ["1.0"],
                    "soss.type": ["binary"],
                },
                PurePath("pool/f/foo/foo-1.1.deb"): {
                    "launchpad.release-id": ["binary:2"],
                    "launchpad.source-name": ["foo"],
                    "launchpad.source-version": ["1.1"],
                    "soss.type": ["binary"],
                },
            },
            pool.getAllArtifacts(
                self.repository_name, ArchiveRepositoryFormat.DEBIAN
            ),
        )

    def test_getAllArtifacts_python(self):
        pool = self.makePool(ArchiveRepositoryFormat.PYTHON)
        ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="bar",
            source_version="1.0",
            filename="bar-1.0.tar.gz",
            release_type=FakeReleaseType.SOURCE,
            release_id=2,
        ).addToPool()
        ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="bar",
            source_version="1.0",
            filename="bar-1.0.whl",
            release_type=FakeReleaseType.BINARY,
            release_id=3,
        ).addToPool()
        self.assertEqual(
            {
                PurePath("bar/1.0/bar-1.0.tar.gz"): {
                    "launchpad.release-id": ["source:2"],
                    "launchpad.source-name": ["bar"],
                    "launchpad.source-version": ["1.0"],
                    "soss.type": ["source"],
                },
                PurePath("bar/1.0/bar-1.0.whl"): {
                    "launchpad.release-id": ["binary:3"],
                    "launchpad.source-name": ["bar"],
                    "launchpad.source-version": ["1.0"],
                    "soss.type": ["binary"],
                },
            },
            pool.getAllArtifacts(
                self.repository_name, ArchiveRepositoryFormat.PYTHON
            ),
        )

    def test_getAllArtifacts_conda(self):
        pool = self.makePool(ArchiveRepositoryFormat.CONDA)
        ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="qux",
            source_version="1.0",
            filename="qux-1.0.conda",
            release_type=FakeReleaseType.BINARY,
            release_id=4,
            user_defined_fields=[("subdir", "linux-64")],
        ).addToPool()
        self.assertEqual(
            {
                PurePath("linux-64/qux-1.0.conda"): {
                    "launchpad.release-id": ["binary:4"],
                    "launchpad.source-name": ["qux"],
                    "launchpad.source-version": ["1.0"],
                    "soss.type": ["binary"],
                },
            },
            pool.getAllArtifacts(
                self.repository_name, ArchiveRepositoryFormat.CONDA
            ),
        )

    def test_getAllArtifacts_go_proxy(self):
        pool = self.makePool(ArchiveRepositoryFormat.GO_PROXY)
        ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="go-module",
            source_version="v0.0.1",
            filename="v0.0.1.zip",
            release_type=FakeReleaseType.SOURCE,
            release_id=5,
            user_defined_fields=[("module-path", "launchpad.net/go-module")],
        ).addToPool()
        self.assertEqual(
            {
                PurePath("launchpad.net/go-module/@v/v0.0.1.zip"): {
                    "launchpad.release-id": ["source:5"],
                    "launchpad.source-name": ["go-module"],
                    "launchpad.source-version": ["v0.0.1"],
                    "soss.type": ["source"],
                },
            },
            pool.getAllArtifacts(
                self.repository_name, ArchiveRepositoryFormat.GO_PROXY
            ),
        )

    def test_getAllArtifacts_generic(self):
        pool = self.makePool(ArchiveRepositoryFormat.GENERIC)
        ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="bar",
            source_version="1.0",
            filename="bar-1.0.tar.gz",
            release_type=FakeReleaseType.SOURCE,
            release_id=1,
            filetype=SourcePackageFileType.GENERIC,
            user_defined_fields=[
                ("name", "bar"),
                ("version", "1.0"),
                ("source", True),
            ],
        ).addToPool()
        self.assertEqual(
            {
                PurePath("bar/1.0/bar-1.0.tar.gz"): {
                    "generic.name": ["bar"],
                    "generic.version": ["1.0"],
                    "launchpad.release-id": ["source:1"],
                    "launchpad.source-name": ["bar"],
                    "launchpad.source-version": ["1.0"],
                    "soss.type": ["source"],
                },
            },
            pool.getAllArtifacts(
                self.repository_name, ArchiveRepositoryFormat.GENERIC
            ),
        )

    def test_getAllArtifacts_handles_empty_properties(self):
        # AQL queries seem to return empty properties as something like
        # `{"key": "pypi.requires.python"}` rather than `{"key":
        # "pypi.requires.python", "value": ""}`.  getAllArtifacts handles
        # this.
        pool = self.makePool(ArchiveRepositoryFormat.PYTHON)
        ArtifactoryPoolTestingFile(
            pool=pool,
            source_name="bar",
            source_version="1.0",
            filename="bar-1.0.whl",
            release_type=FakeReleaseType.BINARY,
            release_id=1,
        ).addToPool()
        path = pool.rootpath / "bar" / "1.0" / "bar-1.0.whl"
        # FakeArtifactoryFixture._make_aql_item returns empty properties as
        # a property dictionary with no value.
        path.set_properties({"pypi.requires.python": [""]}, recursive=False)
        self.assertEqual(
            {
                PurePath("bar/1.0/bar-1.0.whl"): {
                    "launchpad.release-id": ["binary:1"],
                    "launchpad.source-name": ["bar"],
                    "launchpad.source-version": ["1.0"],
                    "pypi.requires.python": [""],
                    "soss.type": ["binary"],
                },
            },
            pool.getAllArtifacts(
                self.repository_name, ArchiveRepositoryFormat.PYTHON
            ),
        )


class TestArtifactoryPoolFromLibrarian(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.base_url = "https://foo.example.com/artifactory"
        self.repository_name = "repository"
        self.artifactory = self.useFixture(
            FakeArtifactoryFixture(self.base_url, self.repository_name)
        )

    def makePool(self, repository_format=ArchiveRepositoryFormat.DEBIAN):
        # Matches behaviour of lp.archivepublisher.config.getPubConfig.
        root_url = "%s/%s" % (self.base_url, self.repository_name)
        if repository_format == ArchiveRepositoryFormat.DEBIAN:
            root_url += "/pool"
        archive = self.factory.makeArchive(
            purpose=ArchivePurpose.PPA, repository_format=repository_format
        )
        return ArtifactoryPool(archive, root_url, BufferLogger())

    def test_updateProperties_debian_source(self):
        pool = self.makePool()
        dses = [
            self.factory.makeDistroSeries(
                distribution=pool.archive.distribution
            )
            for _ in range(2)
        ]
        spph = self.factory.makeSourcePackagePublishingHistory(
            archive=pool.archive,
            distroseries=dses[0],
            pocket=PackagePublishingPocket.RELEASE,
            component="main",
            sourcepackagename="foo",
            version="1.0",
        )
        spr = spph.sourcepackagerelease
        sprf = self.factory.makeSourcePackageReleaseFile(
            sourcepackagerelease=spr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo_1.0.dsc"
            ),
            filetype=SourcePackageFileType.DSC,
        )
        spphs = [spph]
        spphs.append(
            spph.copyTo(dses[1], PackagePublishingPocket.RELEASE, pool.archive)
        )
        transaction.commit()
        pool.addFile(None, spr.name, spr.version, sprf)
        path = pool.rootpath / "f" / "foo" / "foo_1.0.dsc"
        self.assertTrue(path.exists())
        self.assertFalse(path.is_symlink())
        self.assertEqual(
            {
                "launchpad.release-id": ["source:%d" % spr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "soss.type": ["source"],
            },
            path.properties,
        )
        pool.updateProperties(spr.name, spr.version, sprf, spphs)
        self.assertEqual(
            {
                "launchpad.release-id": ["source:%d" % spr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "deb.distribution": list(sorted(ds.name for ds in dses)),
                "deb.component": ["main"],
                "deb.name": [spr.name],
                "deb.version": [spr.version],
                "soss.license": ["debian/copyright"],
                "soss.type": ["source"],
            },
            path.properties,
        )

    def test_updateProperties_debian_binary_multiple_series(self):
        pool = self.makePool()
        dses = [
            self.factory.makeDistroSeries(
                distribution=pool.archive.distribution
            )
            for _ in range(2)
        ]
        processor = self.factory.makeProcessor()
        dases = [
            self.factory.makeDistroArchSeries(
                distroseries=ds, architecturetag=processor.name
            )
            for ds in dses
        ]
        spr = self.factory.makeSourcePackageRelease(
            archive=pool.archive, sourcepackagename="foo", version="1.0"
        )
        bpph = self.factory.makeBinaryPackagePublishingHistory(
            archive=pool.archive,
            distroarchseries=dases[0],
            pocket=PackagePublishingPocket.RELEASE,
            component="main",
            source_package_release=spr,
            binarypackagename="foo",
            architecturespecific=True,
        )
        bpr = bpph.binarypackagerelease
        bpf = self.factory.makeBinaryPackageFile(
            binarypackagerelease=bpr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo_1.0_%s.deb" % processor.name
            ),
            filetype=BinaryPackageFileType.DEB,
        )
        bpphs = [bpph]
        bpphs.append(
            bpph.copyTo(
                dses[1], PackagePublishingPocket.RELEASE, pool.archive
            )[0]
        )
        transaction.commit()
        pool.addFile(
            None, bpr.sourcepackagename, bpr.sourcepackageversion, bpf
        )
        path = (
            pool.rootpath / "f" / "foo" / ("foo_1.0_%s.deb" % processor.name)
        )
        self.assertTrue(path.exists())
        self.assertFalse(path.is_symlink())
        self.assertEqual(
            {
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "soss.type": ["binary"],
            },
            path.properties,
        )
        pool.updateProperties(
            bpr.sourcepackagename, bpr.sourcepackageversion, bpf, bpphs
        )
        self.assertEqual(
            {
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "deb.distribution": list(sorted(ds.name for ds in dses)),
                "deb.component": ["main"],
                "deb.architecture": [processor.name],
                "soss.license": ["/usr/share/doc/foo/copyright"],
                "soss.source_url": [
                    "https://foo.example.com/artifactory/repository/"
                    "pool/f/foo/foo_1.0.dsc"
                ],
                "soss.type": ["binary"],
            },
            path.properties,
        )

    def test_updateProperties_debian_binary_multiple_architectures(self):
        pool = self.makePool()
        ds = self.factory.makeDistroSeries(
            distribution=pool.archive.distribution
        )
        dases = [
            self.factory.makeDistroArchSeries(distroseries=ds)
            for _ in range(2)
        ]
        spr = self.factory.makeSourcePackageRelease(
            archive=pool.archive, sourcepackagename="foo", version="1.0"
        )
        bpb = self.factory.makeBinaryPackageBuild(
            archive=pool.archive,
            source_package_release=spr,
            distroarchseries=dases[0],
            pocket=PackagePublishingPocket.RELEASE,
        )
        bpr = self.factory.makeBinaryPackageRelease(
            binarypackagename="foo",
            build=bpb,
            component="main",
            architecturespecific=False,
        )
        bpf = self.factory.makeBinaryPackageFile(
            binarypackagerelease=bpr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo_1.0_all.deb"
            ),
            filetype=BinaryPackageFileType.DEB,
        )
        bpphs = getUtility(IPublishingSet).publishBinaries(
            pool.archive,
            ds,
            PackagePublishingPocket.RELEASE,
            {bpr: (bpr.component, bpr.section, bpr.priority, None)},
        )
        transaction.commit()
        pool.addFile(
            None, bpr.sourcepackagename, bpr.sourcepackageversion, bpf
        )
        path = pool.rootpath / "f" / "foo" / "foo_1.0_all.deb"
        self.assertTrue(path.exists())
        self.assertFalse(path.is_symlink())
        self.assertEqual(
            {
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "soss.type": ["binary"],
            },
            path.properties,
        )
        pool.updateProperties(
            bpr.sourcepackagename, bpr.sourcepackageversion, bpf, bpphs
        )
        self.assertEqual(
            {
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "deb.distribution": [ds.name],
                "deb.component": ["main"],
                "deb.architecture": list(
                    sorted(das.architecturetag for das in dases)
                ),
                "soss.license": ["/usr/share/doc/foo/copyright"],
                "soss.source_url": [
                    "https://foo.example.com/artifactory/repository/"
                    "pool/f/foo/foo_1.0.dsc"
                ],
                "soss.type": ["binary"],
            },
            path.properties,
        )

    def test_updateProperties_python_sdist(self):
        pool = self.makePool(ArchiveRepositoryFormat.PYTHON)
        dses = [
            self.factory.makeDistroSeries(
                distribution=pool.archive.distribution
            )
            for _ in range(2)
        ]
        das = self.factory.makeDistroArchSeries(distroseries=dses[0])
        ci_build = self.factory.makeCIBuild(distro_arch_series=das)
        spr = self.factory.makeSourcePackageRelease(
            archive=pool.archive,
            sourcepackagename="python-foo",
            version="1.0",
            format=SourcePackageType.CI_BUILD,
            ci_build=ci_build,
            user_defined_fields=[("package-name", "foo")],
        )
        spph = self.factory.makeSourcePackagePublishingHistory(
            archive=pool.archive,
            sourcepackagerelease=spr,
            distroseries=dses[0],
            pocket=PackagePublishingPocket.RELEASE,
            component="main",
            sourcepackagename="python-foo",
            version="1.0",
            channel="edge",
            format=SourcePackageType.CI_BUILD,
        )
        spr = spph.sourcepackagerelease
        sprf = self.factory.makeSourcePackageReleaseFile(
            sourcepackagerelease=spr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo-1.0.tar.gz"
            ),
            filetype=SourcePackageFileType.SDIST,
        )
        spphs = [spph]
        spphs.append(
            spph.copyTo(dses[1], PackagePublishingPocket.RELEASE, pool.archive)
        )
        transaction.commit()
        pool.addFile(None, spr.name, spr.version, sprf)
        path = pool.rootpath / "foo" / "1.0" / "foo-1.0.tar.gz"
        self.assertTrue(path.exists())
        self.assertFalse(path.is_symlink())
        self.assertEqual(
            {
                "launchpad.release-id": ["source:%d" % spr.id],
                "launchpad.source-name": ["python-foo"],
                "launchpad.source-version": ["1.0"],
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["source"],
            },
            path.properties,
        )
        pool.updateProperties(spr.name, spr.version, sprf, spphs)
        self.assertEqual(
            {
                "launchpad.release-id": ["source:%d" % spr.id],
                "launchpad.source-name": ["python-foo"],
                "launchpad.source-version": ["1.0"],
                "launchpad.channel": list(
                    sorted("%s:edge" % ds.name for ds in dses)
                ),
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["source"],
            },
            path.properties,
        )

    def test_updateProperties_python_sdist_no_package_name(self):
        # Older sdist publications don't have package-name in
        # user_defined_fields.
        pool = self.makePool(ArchiveRepositoryFormat.PYTHON)
        dses = [
            self.factory.makeDistroSeries(
                distribution=pool.archive.distribution
            )
            for _ in range(2)
        ]
        das = self.factory.makeDistroArchSeries(distroseries=dses[0])
        ci_build = self.factory.makeCIBuild(distro_arch_series=das)
        spr = self.factory.makeSourcePackageRelease(
            archive=pool.archive,
            sourcepackagename="foo",
            version="1.0",
            format=SourcePackageType.CI_BUILD,
            ci_build=ci_build,
        )
        spph = self.factory.makeSourcePackagePublishingHistory(
            archive=pool.archive,
            sourcepackagerelease=spr,
            distroseries=dses[0],
            pocket=PackagePublishingPocket.RELEASE,
            component="main",
            sourcepackagename="foo",
            version="1.0",
            channel="edge",
            format=SourcePackageType.CI_BUILD,
        )
        spr = spph.sourcepackagerelease
        sprf = self.factory.makeSourcePackageReleaseFile(
            sourcepackagerelease=spr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo-1.0.tar.gz"
            ),
            filetype=SourcePackageFileType.SDIST,
        )
        spphs = [spph]
        spphs.append(
            spph.copyTo(dses[1], PackagePublishingPocket.RELEASE, pool.archive)
        )
        transaction.commit()
        pool.addFile(None, spr.name, spr.version, sprf)
        path = pool.rootpath / "foo" / "1.0" / "foo-1.0.tar.gz"
        self.assertTrue(path.exists())
        self.assertFalse(path.is_symlink())
        self.assertEqual(
            {
                "launchpad.release-id": ["source:%d" % spr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["source"],
            },
            path.properties,
        )
        pool.updateProperties(spr.name, spr.version, sprf, spphs)
        self.assertEqual(
            {
                "launchpad.release-id": ["source:%d" % spr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "launchpad.channel": list(
                    sorted("%s:edge" % ds.name for ds in dses)
                ),
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["source"],
            },
            path.properties,
        )

    def test_updateProperties_python_sdist_canonicalizes_name(self):
        pool = self.makePool(ArchiveRepositoryFormat.PYTHON)
        dses = [
            self.factory.makeDistroSeries(
                distribution=pool.archive.distribution
            )
            for _ in range(2)
        ]
        das = self.factory.makeDistroArchSeries(distroseries=dses[0])
        ci_build = self.factory.makeCIBuild(distro_arch_series=das)
        spr = self.factory.makeSourcePackageRelease(
            archive=pool.archive,
            sourcepackagename="python-foo-bar",
            version="1.0",
            format=SourcePackageType.CI_BUILD,
            ci_build=ci_build,
            user_defined_fields=[("package-name", "foo_bar")],
        )
        spph = self.factory.makeSourcePackagePublishingHistory(
            archive=pool.archive,
            sourcepackagerelease=spr,
            distroseries=dses[0],
            pocket=PackagePublishingPocket.RELEASE,
            component="main",
            sourcepackagename="python-foo-bar",
            version="1.0",
            channel="edge",
            format=SourcePackageType.CI_BUILD,
        )
        spr = spph.sourcepackagerelease
        sprf = self.factory.makeSourcePackageReleaseFile(
            sourcepackagerelease=spr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo_bar-1.0.tar.gz"
            ),
            filetype=SourcePackageFileType.SDIST,
        )
        spphs = [spph]
        spphs.append(
            spph.copyTo(dses[1], PackagePublishingPocket.RELEASE, pool.archive)
        )
        transaction.commit()
        pool.addFile(None, spr.name, spr.version, sprf)
        path = pool.rootpath / "foo-bar" / "1.0" / "foo_bar-1.0.tar.gz"
        self.assertTrue(path.exists())
        self.assertFalse(path.is_symlink())
        self.assertEqual(
            {
                "launchpad.release-id": ["source:%d" % spr.id],
                "launchpad.source-name": ["python-foo-bar"],
                "launchpad.source-version": ["1.0"],
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["source"],
            },
            path.properties,
        )
        pool.updateProperties(spr.name, spr.version, sprf, spphs)
        self.assertEqual(
            {
                "launchpad.release-id": ["source:%d" % spr.id],
                "launchpad.source-name": ["python-foo-bar"],
                "launchpad.source-version": ["1.0"],
                "launchpad.channel": list(
                    sorted("%s:edge" % ds.name for ds in dses)
                ),
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["source"],
            },
            path.properties,
        )

    def test_updateProperties_python_wheel(self):
        pool = self.makePool(ArchiveRepositoryFormat.PYTHON)
        dses = [
            self.factory.makeDistroSeries(
                distribution=pool.archive.distribution
            )
            for _ in range(2)
        ]
        processor = self.factory.makeProcessor()
        dases = [
            self.factory.makeDistroArchSeries(
                distroseries=ds, architecturetag=processor.name
            )
            for ds in dses
        ]
        ci_build = self.factory.makeCIBuild(distro_arch_series=dases[0])
        spr = self.factory.makeSourcePackageRelease(
            archive=pool.archive,
            sourcepackagename="foo",
            version="1.0",
            format=SourcePackageType.CI_BUILD,
            ci_build=ci_build,
        )
        bpn = self.factory.makeBinaryPackageName(name="foo")
        bpr = self.factory.makeBinaryPackageRelease(
            binarypackagename=bpn,
            version="1.0",
            ci_build=ci_build,
            binpackageformat=BinaryPackageFormat.WHL,
        )
        bpf = self.factory.makeBinaryPackageFile(
            binarypackagerelease=bpr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo-1.0-py3-none-any.whl"
            ),
            filetype=BinaryPackageFileType.WHL,
        )
        bpph = self.factory.makeBinaryPackagePublishingHistory(
            archive=pool.archive,
            distroarchseries=dases[0],
            pocket=PackagePublishingPocket.RELEASE,
            component="main",
            source_package_release=spr,
            binarypackagename="foo",
            binpackageformat=BinaryPackageFormat.WHL,
            architecturespecific=False,
            channel="edge",
        )
        bpphs = [bpph]
        bpphs.append(
            getUtility(IPublishingSet).copyBinaries(
                pool.archive,
                dses[1],
                PackagePublishingPocket.RELEASE,
                [bpph],
                channel="edge",
            )[0]
        )
        transaction.commit()
        pool.addFile(None, bpph.pool_name, bpph.pool_version, bpf)
        path = pool.rootpath / "foo" / "1.0" / "foo-1.0-py3-none-any.whl"
        self.assertTrue(path.exists())
        self.assertFalse(path.is_symlink())
        self.assertEqual(
            {
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["binary"],
            },
            path.properties,
        )
        pool.updateProperties(bpph.pool_name, bpph.pool_version, bpf, bpphs)
        self.assertEqual(
            {
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "launchpad.channel": list(
                    sorted("%s:edge" % ds.name for ds in dses)
                ),
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["binary"],
            },
            path.properties,
        )

    def test_updateProperties_conda_v1(self):
        pool = self.makePool(ArchiveRepositoryFormat.CONDA)
        dses = [
            self.factory.makeDistroSeries(
                distribution=pool.archive.distribution
            )
            for _ in range(2)
        ]
        processor = self.factory.makeProcessor()
        dases = [
            self.factory.makeDistroArchSeries(
                distroseries=ds, architecturetag=processor.name
            )
            for ds in dses
        ]
        ci_build = self.factory.makeCIBuild(distro_arch_series=dases[0])
        bpn = self.factory.makeBinaryPackageName(name="foo")
        bpr = self.factory.makeBinaryPackageRelease(
            binarypackagename=bpn,
            version="1.0",
            ci_build=ci_build,
            binpackageformat=BinaryPackageFormat.CONDA_V1,
            user_defined_fields=[("subdir", "linux-64")],
        )
        bpf = self.factory.makeBinaryPackageFile(
            binarypackagerelease=bpr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo-1.0.tar.bz2"
            ),
            filetype=BinaryPackageFileType.CONDA_V1,
        )
        bpph = self.factory.makeBinaryPackagePublishingHistory(
            binarypackagerelease=bpr,
            archive=pool.archive,
            distroarchseries=dases[0],
            pocket=PackagePublishingPocket.RELEASE,
            architecturespecific=False,
            channel="edge",
        )
        bpphs = [bpph]
        bpphs.append(
            getUtility(IPublishingSet).copyBinaries(
                pool.archive,
                dses[1],
                PackagePublishingPocket.RELEASE,
                [bpph],
                channel="edge",
            )[0]
        )
        transaction.commit()
        pool.addFile(None, bpph.pool_name, bpph.pool_version, bpf)
        path = pool.rootpath / "linux-64" / "foo-1.0.tar.bz2"
        self.assertTrue(path.exists())
        self.assertFalse(path.is_symlink())
        self.assertEqual(
            {
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["binary"],
            },
            path.properties,
        )
        pool.updateProperties(bpph.pool_name, bpph.pool_version, bpf, bpphs)
        self.assertEqual(
            {
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "launchpad.channel": list(
                    sorted("%s:edge" % ds.name for ds in dses)
                ),
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["binary"],
            },
            path.properties,
        )

    def test_updateProperties_conda_v2(self):
        pool = self.makePool(ArchiveRepositoryFormat.CONDA)
        dses = [
            self.factory.makeDistroSeries(
                distribution=pool.archive.distribution
            )
            for _ in range(2)
        ]
        processor = self.factory.makeProcessor()
        dases = [
            self.factory.makeDistroArchSeries(
                distroseries=ds, architecturetag=processor.name
            )
            for ds in dses
        ]
        ci_build = self.factory.makeCIBuild(distro_arch_series=dases[0])
        bpn = self.factory.makeBinaryPackageName(name="foo")
        bpr = self.factory.makeBinaryPackageRelease(
            binarypackagename=bpn,
            version="1.0",
            ci_build=ci_build,
            binpackageformat=BinaryPackageFormat.CONDA_V2,
            user_defined_fields=[("subdir", "noarch")],
        )
        bpf = self.factory.makeBinaryPackageFile(
            binarypackagerelease=bpr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo-1.0.conda"
            ),
            filetype=BinaryPackageFileType.CONDA_V2,
        )
        bpph = self.factory.makeBinaryPackagePublishingHistory(
            binarypackagerelease=bpr,
            archive=pool.archive,
            distroarchseries=dases[0],
            pocket=PackagePublishingPocket.RELEASE,
            architecturespecific=True,
            channel="edge",
        )
        bpphs = [bpph]
        bpphs.append(
            getUtility(IPublishingSet).copyBinaries(
                pool.archive,
                dses[1],
                PackagePublishingPocket.RELEASE,
                [bpph],
                channel="edge",
            )[0]
        )
        transaction.commit()
        pool.addFile(None, bpph.pool_name, bpph.pool_version, bpf)
        path = pool.rootpath / "noarch" / "foo-1.0.conda"
        self.assertTrue(path.exists())
        self.assertFalse(path.is_symlink())
        self.assertEqual(
            {
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["binary"],
            },
            path.properties,
        )
        pool.updateProperties(bpph.pool_name, bpph.pool_version, bpf, bpphs)
        self.assertEqual(
            {
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "launchpad.channel": list(
                    sorted("%s:edge" % ds.name for ds in dses)
                ),
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["binary"],
            },
            path.properties,
        )

    def test_updateProperties_generic_source(self):
        pool = self.makePool(ArchiveRepositoryFormat.GENERIC)
        dses = [
            self.factory.makeDistroSeries(
                distribution=pool.archive.distribution
            )
            for _ in range(2)
        ]
        das = self.factory.makeDistroArchSeries(distroseries=dses[0])
        ci_build = self.factory.makeCIBuild(distro_arch_series=das)
        spr = self.factory.makeSourcePackageRelease(
            archive=pool.archive,
            sourcepackagename="foo-package",
            version="1.0",
            format=SourcePackageType.CI_BUILD,
            ci_build=ci_build,
            user_defined_fields=[
                ("name", "foo"),
                ("version", "1.0"),
                ("source", True),
            ],
        )
        spph = self.factory.makeSourcePackagePublishingHistory(
            archive=pool.archive,
            sourcepackagerelease=spr,
            distroseries=dses[0],
            pocket=PackagePublishingPocket.RELEASE,
            component="main",
            sourcepackagename="foo-package",
            version="1.0",
            channel="edge",
            format=SourcePackageType.CI_BUILD,
        )
        spr = spph.sourcepackagerelease
        sprf = self.factory.makeSourcePackageReleaseFile(
            sourcepackagerelease=spr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo-1.0.tar.gz"
            ),
            filetype=SourcePackageFileType.GENERIC,
        )
        spphs = [spph]
        spphs.append(
            spph.copyTo(dses[1], PackagePublishingPocket.RELEASE, pool.archive)
        )
        transaction.commit()
        pool.addFile(None, spr.name, spr.version, sprf)
        path = pool.rootpath / "foo" / "1.0" / "foo-1.0.tar.gz"
        self.assertTrue(path.exists())
        self.assertFalse(path.is_symlink())
        self.assertEqual(
            {
                "generic.name": ["foo"],
                "generic.version": ["1.0"],
                "launchpad.release-id": ["source:%d" % spr.id],
                "launchpad.source-name": ["foo-package"],
                "launchpad.source-version": ["1.0"],
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["source"],
            },
            path.properties,
        )
        pool.updateProperties(spr.name, spr.version, sprf, spphs)
        self.assertEqual(
            {
                "generic.name": ["foo"],
                "generic.version": ["1.0"],
                "launchpad.release-id": ["source:%d" % spr.id],
                "launchpad.source-name": ["foo-package"],
                "launchpad.source-version": ["1.0"],
                "launchpad.channel": list(
                    sorted("%s:edge" % ds.name for ds in dses)
                ),
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["source"],
            },
            path.properties,
        )

    def test_updateProperties_generic_binary(self):
        pool = self.makePool(ArchiveRepositoryFormat.GENERIC)
        dses = [
            self.factory.makeDistroSeries(
                distribution=pool.archive.distribution
            )
            for _ in range(2)
        ]
        processor = self.factory.makeProcessor()
        dases = [
            self.factory.makeDistroArchSeries(
                distroseries=ds, architecturetag=processor.name
            )
            for ds in dses
        ]
        ci_build = self.factory.makeCIBuild(distro_arch_series=dases[0])
        bpn = self.factory.makeBinaryPackageName(name="foo")
        bpr = self.factory.makeBinaryPackageRelease(
            binarypackagename=bpn,
            version="1.0",
            ci_build=ci_build,
            binpackageformat=BinaryPackageFormat.GENERIC,
            user_defined_fields=[("name", "foo"), ("version", "1.0")],
        )
        bpf = self.factory.makeBinaryPackageFile(
            binarypackagerelease=bpr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="test-binary"
            ),
            filetype=BinaryPackageFileType.GENERIC,
        )
        bpph = self.factory.makeBinaryPackagePublishingHistory(
            binarypackagerelease=bpr,
            archive=pool.archive,
            distroarchseries=dases[0],
            pocket=PackagePublishingPocket.RELEASE,
            architecturespecific=True,
            channel="edge",
        )
        bpphs = [bpph]
        bpphs.append(
            getUtility(IPublishingSet).copyBinaries(
                pool.archive,
                dses[1],
                PackagePublishingPocket.RELEASE,
                [bpph],
                channel="edge",
            )[0]
        )
        transaction.commit()
        pool.addFile(None, bpph.pool_name, bpph.pool_version, bpf)
        path = pool.rootpath / "foo" / "1.0" / "test-binary"
        self.assertTrue(path.exists())
        self.assertFalse(path.is_symlink())
        self.assertEqual(
            {
                "generic.name": ["foo"],
                "generic.version": ["1.0"],
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["binary"],
            },
            path.properties,
        )
        pool.updateProperties(bpph.pool_name, bpph.pool_version, bpf, bpphs)
        self.assertEqual(
            {
                "generic.name": ["foo"],
                "generic.version": ["1.0"],
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "launchpad.channel": list(
                    sorted("%s:edge" % ds.name for ds in dses)
                ),
                "soss.source_url": [
                    ci_build.git_repository.getCodebrowseUrl()
                ],
                "soss.commit_id": [ci_build.commit_sha1],
                "soss.type": ["binary"],
            },
            path.properties,
        )

    def test_updateProperties_preserves_externally_set_properties(self):
        # Artifactory sets some properties by itself as part of scanning
        # packages.  We leave those untouched.
        pool = self.makePool()
        ds = self.factory.makeDistroSeries(
            distribution=pool.archive.distribution
        )
        das = self.factory.makeDistroArchSeries(distroseries=ds)
        spr = self.factory.makeSourcePackageRelease(
            archive=pool.archive, sourcepackagename="foo", version="1.0"
        )
        bpb = self.factory.makeBinaryPackageBuild(
            archive=pool.archive,
            source_package_release=spr,
            distroarchseries=das,
            pocket=PackagePublishingPocket.RELEASE,
        )
        bpr = self.factory.makeBinaryPackageRelease(
            binarypackagename="foo",
            build=bpb,
            component="main",
            architecturespecific=False,
        )
        bpf = self.factory.makeBinaryPackageFile(
            binarypackagerelease=bpr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo_1.0_all.deb"
            ),
            filetype=BinaryPackageFileType.DEB,
        )
        bpphs = getUtility(IPublishingSet).publishBinaries(
            pool.archive,
            ds,
            PackagePublishingPocket.RELEASE,
            {bpr: (bpr.component, bpr.section, bpr.priority, None)},
        )
        transaction.commit()
        pool.addFile(
            None, bpr.sourcepackagename, bpr.sourcepackageversion, bpf
        )
        path = pool.rootpath / "f" / "foo" / "foo_1.0_all.deb"
        path.set_properties({"deb.version": ["1.0"]}, recursive=False)
        self.assertEqual(
            {
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "deb.version": ["1.0"],
                "soss.type": ["binary"],
            },
            path.properties,
        )
        pool.updateProperties(
            bpr.sourcepackagename, bpr.sourcepackageversion, bpf, bpphs
        )
        self.assertEqual(
            {
                "launchpad.release-id": ["binary:%d" % bpr.id],
                "launchpad.source-name": ["foo"],
                "launchpad.source-version": ["1.0"],
                "deb.distribution": [ds.name],
                "deb.component": ["main"],
                "deb.architecture": [das.architecturetag],
                "deb.version": ["1.0"],
                "soss.license": ["/usr/share/doc/foo/copyright"],
                "soss.source_url": [
                    "https://foo.example.com/artifactory/repository/"
                    "pool/f/foo/foo_1.0.dsc"
                ],
                "soss.type": ["binary"],
            },
            path.properties,
        )

    def test_updateProperties_encodes_special_characters(self):
        pool = self.makePool(ArchiveRepositoryFormat.PYTHON)
        ds = self.factory.makeDistroSeries(
            distribution=pool.archive.distribution
        )
        das = self.factory.makeDistroArchSeries(distroseries=ds)
        spr = self.factory.makeSourcePackageRelease(
            archive=pool.archive,
            sourcepackagename="foo",
            version="1.0",
            format=SourcePackageType.CI_BUILD,
        )
        bpph = self.factory.makeBinaryPackagePublishingHistory(
            archive=pool.archive,
            distroarchseries=das,
            pocket=PackagePublishingPocket.RELEASE,
            component="main",
            source_package_release=spr,
            binarypackagename="foo",
            binpackageformat=BinaryPackageFormat.WHL,
            architecturespecific=False,
            channel="edge",
        )
        bpr = bpph.binarypackagerelease
        bpf = self.factory.makeBinaryPackageFile(
            binarypackagerelease=bpr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo-1.0-py3-none-any.whl"
            ),
            filetype=BinaryPackageFileType.WHL,
        )
        bpphs = [bpph]
        transaction.commit()
        pool.addFile(
            None, bpr.sourcepackagename, bpr.sourcepackageversion, bpf
        )
        path = pool.rootpath / "foo" / "1.0" / "foo-1.0-py3-none-any.whl"
        self.assertTrue(path.exists())
        self.assertFalse(path.is_symlink())
        # Simulate Artifactory scanning the package.
        summaries = [
            "text with special characters: ;=|,\\",
            "another value: \\;=|,\\\\",
        ]
        self.artifactory._fs["/foo/1.0/foo-1.0-py3-none-any.whl"][
            "properties"
        ]["pypi.summary"] = summaries

        pool.updateProperties(
            bpr.sourcepackagename, bpr.sourcepackageversion, bpf, bpphs
        )

        self.assertEqual(summaries, path.properties["pypi.summary"])

        pool.updateProperties(
            bpr.sourcepackagename, bpr.sourcepackageversion, bpf, bpphs
        )
        self.assertEqual(summaries, path.properties["pypi.summary"])

    def test_updateProperties_sets_soss_license_source_spdx(self):
        # Set the `soss.license` field appropriately for a source package if
        # the originating job set the `license` property to an SPDX
        # expression.
        pool = self.makePool(ArchiveRepositoryFormat.PYTHON)
        spr = self.factory.makeSourcePackageRelease(
            archive=pool.archive,
            sourcepackagename="foo",
            version="1.0",
            format=SourcePackageType.CI_BUILD,
            user_defined_fields=[
                ("license", {"spdx": "MIT"}),
                ("package-name", "foo"),
            ],
        )
        sprf = self.factory.makeSourcePackageReleaseFile(
            sourcepackagerelease=spr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo-1.0.tar.gz"
            ),
            filetype=SourcePackageFileType.SDIST,
        )
        transaction.commit()
        pool.addFile(None, "foo", "1.0", sprf)
        path = pool.rootpath / "foo" / "1.0" / "foo-1.0.tar.gz"
        self.assertEqual(["spdx:MIT"], path.properties["soss.license"])

    def test_updateProperties_sets_soss_license_binary_spdx(self):
        # Set the `soss.license` field appropriately for a binary package if
        # the originating job set the `license` property to an SPDX
        # expression.
        pool = self.makePool(ArchiveRepositoryFormat.PYTHON)
        bpr = self.factory.makeBinaryPackageRelease(
            binarypackagename="foo",
            version="1.0",
            binpackageformat=BinaryPackageFormat.WHL,
            user_defined_fields=[("license", {"spdx": "MIT"})],
        )
        bpf = self.factory.makeBinaryPackageFile(
            binarypackagerelease=bpr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo-1.0-py3-none-any.whl"
            ),
            filetype=BinaryPackageFileType.WHL,
        )
        transaction.commit()
        pool.addFile(None, "foo", "1.0", bpf)
        path = pool.rootpath / "foo" / "1.0" / "foo-1.0-py3-none-any.whl"
        self.assertEqual(["spdx:MIT"], path.properties["soss.license"])

    def test_updateProperties_sets_soss_license_source_path(self):
        # Set the `soss.license` field appropriately for a source package if
        # the originating job set the `license` property to a path.
        pool = self.makePool(ArchiveRepositoryFormat.PYTHON)
        spr = self.factory.makeSourcePackageRelease(
            archive=pool.archive,
            sourcepackagename="foo",
            version="1.0",
            format=SourcePackageType.CI_BUILD,
            user_defined_fields=[
                ("license", {"path": "LICENSE"}),
                ("package-name", "foo"),
            ],
        )
        sprf = self.factory.makeSourcePackageReleaseFile(
            sourcepackagerelease=spr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo-1.0.tar.gz"
            ),
            filetype=SourcePackageFileType.SDIST,
        )
        transaction.commit()
        pool.addFile(None, "foo", "1.0", sprf)
        path = pool.rootpath / "foo" / "1.0" / "foo-1.0.tar.gz"
        self.assertEqual(["LICENSE"], path.properties["soss.license"])

    def test_updateProperties_sets_soss_license_binary_path(self):
        # Set the `soss.license` field appropriately for a binary package if
        # the originating job set the `license` property to a path.
        pool = self.makePool(ArchiveRepositoryFormat.PYTHON)
        bpr = self.factory.makeBinaryPackageRelease(
            binarypackagename="foo",
            version="1.0",
            binpackageformat=BinaryPackageFormat.WHL,
            user_defined_fields=[("license", {"path": "LICENSE"})],
        )
        bpf = self.factory.makeBinaryPackageFile(
            binarypackagerelease=bpr,
            library_file=self.factory.makeLibraryFileAlias(
                filename="foo-1.0-py3-none-any.whl"
            ),
            filetype=BinaryPackageFileType.WHL,
        )
        transaction.commit()
        pool.addFile(None, "foo", "1.0", bpf)
        path = pool.rootpath / "foo" / "1.0" / "foo-1.0-py3-none-any.whl"
        self.assertEqual(["LICENSE"], path.properties["soss.license"])
