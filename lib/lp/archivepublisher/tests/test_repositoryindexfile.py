# Copyright 2009-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `RepositoryIndexFile`."""

import bz2
import gzip
import lzma
import os
import shutil
import stat
import tempfile
import unittest
import warnings
from unittest import mock

from lp.archivepublisher.utils import RepositoryIndexFile
from lp.soyuz.enums import IndexCompressionType


class TestRepositoryArchiveIndex(unittest.TestCase):
    def setUp(self):
        """Create temporary directories to be used in tests.

        'root': final destination for repository files.
        'temp_root': temporary destination for reporsitory files.
        """
        self.root = tempfile.mkdtemp()
        self.temp_root = tempfile.mkdtemp()

    def tearDown(self):
        """Purge temporary files created on `setUp`."""
        for path in [self.root, self.temp_root]:
            shutil.rmtree(path)

    def getRepoFile(self, filename, compressors=None):
        """Return a `RepositoryIndexFile` for the given filename.

        The `RepositoryIndexFile` is created with the test 'root' and
        'temp_root'.
        """
        if compressors is None:
            compressors = [
                IndexCompressionType.GZIP,
                IndexCompressionType.BZIP2,
                IndexCompressionType.XZ,
            ]
        return RepositoryIndexFile(
            os.path.join(self.root, filename), self.temp_root, compressors
        )

    def testWorkflow(self):
        """`RepositoryIndexFile` workflow.

        On creation, 2 temporary files are atomically created in the
        'temp_root' location (mkstemp). One for storing the plain contents
        and other for the corresponding compressed contents. At this point,
        no files were created in the 'root' location yet.

        Once the `RepositoryIndexFile` is closed, the files in 'temp_root'
        are closed and moved to 'root' with their expected names.

        Additionally, the resulting files are made readable and writable by
        their group and readable by others.
        """
        repo_file = self.getRepoFile("boing")

        self.assertEqual(0, len(os.listdir(self.root)))
        self.assertEqual(3, len(os.listdir(self.temp_root)))

        repo_file.close()

        self.assertEqual(3, len(os.listdir(self.root)))
        self.assertEqual(0, len(os.listdir(self.temp_root)))

        resulting_files = sorted(os.listdir(self.root))
        self.assertEqual(
            ["boing.bz2", "boing.gz", "boing.xz"], resulting_files
        )

        for filename in resulting_files:
            file_path = os.path.join(self.root, filename)
            mode = stat.S_IMODE(os.stat(file_path).st_mode)
            self.assertTrue(
                (stat.S_IWGRP | stat.S_IRGRP | stat.S_IROTH) & mode
            )

    def testWrite(self):
        """`RepositoryIndexFile` writing.

        Writes to a `RepositoryIndexFile` happens simultaneously in both
        of its counter-parts (plain and gzipped contents). Once the file
        is closed both resulting files have the same contents, one plain and
        other compressed.
        """
        repo_file = self.getRepoFile("boing")
        repo_file.write(b"hello")
        repo_file.close()

        with gzip.open(os.path.join(self.root, "boing.gz")) as gzip_file:
            gzip_content = gzip_file.read()
        with bz2.BZ2File(os.path.join(self.root, "boing.bz2")) as bz2_file:
            bz2_content = bz2_file.read()
        with lzma.open(os.path.join(self.root, "boing.xz")) as xz_file:
            xz_content = xz_file.read()

        self.assertEqual(gzip_content, bz2_content)
        self.assertEqual(gzip_content, xz_content)
        self.assertEqual(b"hello", gzip_content)

        # gzip is compressed as if with "-n", ensuring that the hash
        # doesn't change just because we're compressing at a different
        # point in time. The filename is also blank, but Python's gzip
        # module discards it so it's hard to test.
        self.assertEqual(0, gzip_file.mtime)

    def testGzipClosesUnderlyingFile(self):
        repo_file = self.getRepoFile(
            "boing", compressors=[IndexCompressionType.GZIP]
        )
        raw_file = repo_file.index_files[0]._fd.fileobj
        self.addCleanup(raw_file.close)
        repo_file.write(b"hello")

        repo_file.close()

        self.assertTrue(raw_file.closed)
        path = os.path.join(self.root, "boing.gz")
        with gzip.open(path) as gzip_file:
            self.assertEqual(b"hello", gzip_file.read())
        with open(path, "rb") as compressed_file:
            header = compressed_file.read(10)
        self.assertEqual(0, header[3])
        self.assertEqual(b"\0\0\0\0", header[4:8])

    def testGzipClosesUnderlyingFileOnError(self):
        repo_file = self.getRepoFile(
            "boing", compressors=[IndexCompressionType.GZIP]
        )
        raw_file = repo_file.index_files[0]._fd.fileobj
        self.addCleanup(raw_file.close)
        repo_file.write(b"hello")

        with mock.patch.object(
            raw_file, "write", side_effect=OSError("Cannot write gzip file")
        ):
            self.assertRaisesRegex(
                OSError, "Cannot write gzip file", repo_file.close
            )

        self.assertTrue(raw_file.closed)
        self.assertFalse(os.path.exists(os.path.join(self.root, "boing.gz")))

    def testGzipUsesExplicitWriteMode(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            repo_file = self.getRepoFile(
                "boing", compressors=[IndexCompressionType.GZIP]
            )
        repo_file.close()

    def testCompressors(self):
        """`RepositoryIndexFile` honours the supplied list of compressors."""
        repo_file = self.getRepoFile(
            "boing",
            compressors=[
                IndexCompressionType.UNCOMPRESSED,
                IndexCompressionType.XZ,
            ],
        )
        repo_file.close()
        self.assertEqual(["boing", "boing.xz"], sorted(os.listdir(self.root)))

    def testUnreferencing(self):
        """`RepositoryIndexFile` unreferencing.

        When a `RepositoryIndexFile` is unreferenced it takes care of
        removing the created files in the 'temp_root'.
        """
        repo_file = self.getRepoFile("boing")

        self.assertEqual(0, len(os.listdir(self.root)))
        self.assertEqual(3, len(os.listdir(self.temp_root)))

        del repo_file

        self.assertEqual(0, len(os.listdir(self.root)))
        self.assertEqual(0, len(os.listdir(self.temp_root)))

    def testRootCreation(self):
        """`RepositoryIndexFile` creates given 'root' path if necessary."""
        missing_root = os.path.join(self.root, "donotexist")
        compressors = [
            IndexCompressionType.GZIP,
            IndexCompressionType.BZIP2,
            IndexCompressionType.XZ,
        ]
        repo_file = RepositoryIndexFile(
            os.path.join(missing_root, "boing"), self.temp_root, compressors
        )

        self.assertFalse(os.path.exists(missing_root))

        repo_file.close()

        self.assertEqual(
            ["boing.bz2", "boing.gz", "boing.xz"],
            sorted(os.listdir(missing_root)),
        )

    def testMissingTempRoot(self):
        """`RepositoryIndexFile` cannot be given a missing 'temp_root'."""
        missing_temp_root = os.path.join(self.temp_root, "donotexist")
        self.assertRaises(
            AssertionError,
            RepositoryIndexFile,
            os.path.join(self.root, "boing"),
            missing_temp_root,
            [IndexCompressionType.UNCOMPRESSED],
        )

    def testRemoveOld(self):
        """`RepositoryIndexFile` removes old index files."""
        old_path = os.path.join(self.root, "boing")
        with open(old_path, "w"):
            pass
        self.assertEqual(["boing"], sorted(os.listdir(self.root)))
        repo_file = self.getRepoFile("boing")
        repo_file.close()
        self.assertEqual(
            ["boing.bz2", "boing.gz", "boing.xz"],
            sorted(os.listdir(self.root)),
        )
