# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the transport-backed SFTP server implementation."""

import os
from contextlib import closing

from breezy import errors as bzr_errors
from breezy import urlutils
from breezy.tests import TestCaseInTempDir
from breezy.transport import get_transport, NoSuchFile as BzrNoSuchFile, FileExists
from breezy.transport.memory import MemoryTransport
from lazr.sshserver.sftp import FileIsADirectory
from testtools.matchers import MatchesStructure
from testtools.testcase import ExpectedException
from testtools.twistedsupport import AsynchronousDeferredRunTest
from twisted.conch.interfaces import ISFTPServer
from twisted.conch.ls import lsLine
from twisted.conch.ssh import filetransfer
from twisted.internet import defer
from twisted.python import failure
from twisted.python.util import mergeFunctionMetadata

from lp.codehosting.inmemory import InMemoryFrontend, XMLRPCWrapper
from lp.codehosting.sftp import FatLocalTransport, TransportSFTPServer
from lp.codehosting.sshserver.daemon import CodehostingAvatar
from lp.services.utils import file_exists
from lp.testing import TestCase
from lp.testing.factory import LaunchpadObjectFactory


class AsyncTransport:
    """Make a transport that returns Deferreds.

    While this could wrap any object and make its methods return Deferreds, we
    expect this to be wrapping FatLocalTransport (and so making a Twisted
    Transport, as defined in lp.codehosting.sftp's docstring).
    """

    def __init__(self, transport):
        self._transport = transport

    def __getattr__(self, name):
        maybe_method = getattr(self._transport, name)
        if not callable(maybe_method):
            return maybe_method

        def defer_it(*args, **kwargs):
            return defer.maybeDeferred(maybe_method, *args, **kwargs)

        return mergeFunctionMetadata(maybe_method, defer_it)


class TestFatLocalTransport(TestCaseInTempDir):
    def setUp(self):
        TestCaseInTempDir.setUp(self)
        self.transport = FatLocalTransport(urlutils.local_path_to_url("."))

    def test_writeChunk(self):
        # writeChunk writes a chunk of data to a file at a given offset.
        filename = "foo"
        self.transport.put_bytes(filename, b"content")
        self.transport.writeChunk(filename, 1, b"razy")
        self.assertEqual(b"crazynt", self.transport.get_bytes(filename))

    def test_localRealPath(self):
        # localRealPath takes a URL-encoded relpath and returns a URL-encoded
        # absolute path.
        filename = "foo bar"
        escaped_filename = urlutils.escape(filename)
        self.assertNotEqual(filename, escaped_filename)
        realpath = self.transport.local_realPath(escaped_filename)
        self.assertEqual(urlutils.escape(os.path.abspath(filename)), realpath)

    def test_clone_with_no_offset(self):
        # FatLocalTransport.clone with no arguments returns a new instance of
        # FatLocalTransport with the same base URL.
        transport = self.transport.clone()
        self.assertIsNot(self.transport, transport)
        self.assertEqual(self.transport.base, transport.base)
        self.assertIsInstance(transport, FatLocalTransport)

    def test_clone_with_relative_offset(self):
        # FatLocalTransport.clone with an offset path returns a new instance
        # of FatLocalTransport with a base URL equal to the offset path
        # relative to the old base.
        transport = self.transport.clone("foo")
        self.assertIsNot(self.transport, transport)
        self.assertEqual(
            urlutils.join(self.transport.base, "foo").rstrip("/"),
            transport.base.rstrip("/"),
        )
        self.assertIsInstance(transport, FatLocalTransport)

    def test_clone_with_absolute_offset(self):
        transport = self.transport.clone("/")
        self.assertIsNot(self.transport, transport)
        self.assertEqual("file:///", transport.base)
        self.assertIsInstance(transport, FatLocalTransport)


class TestSFTPAdapter(TestCase):
    run_tests_with = AsynchronousDeferredRunTest

    def setUp(self):
        TestCase.setUp(self)
        frontend = InMemoryFrontend()
        self.factory = frontend.getLaunchpadObjectFactory()
        self.codehosting_endpoint = XMLRPCWrapper(
            frontend.getCodehostingEndpoint()
        )

    def makeCodehostingAvatar(self):
        user = self.factory.makePerson()
        user_dict = dict(id=user.id, name=user.name)
        return CodehostingAvatar(user_dict, self.codehosting_endpoint)

    def test_canAdaptToSFTPServer(self):
        avatar = self.makeCodehostingAvatar()
        # The adapter logs the SFTPStarted event, which gets the id of the
        # transport attribute of 'avatar'. Here we set transport to an
        # arbitrary object that can have its id taken.
        avatar.transport = object()
        server = ISFTPServer(avatar)
        self.assertIsInstance(server, TransportSFTPServer)
        product = self.factory.makeProduct()
        branch_name = self.factory.getUniqueString()
        deferred = server.makeDirectory(
            (
                "~%s/%s/%s" % (avatar.username, product.name, branch_name)
            ).encode("UTF-8"),
            {"permissions": 0o777},
        )
        return deferred


class SFTPTestMixin:
    """Mixin used to check getAttrs."""

    def setUp(self):
        self._factory = LaunchpadObjectFactory()

    def checkAttrs(self, attrs, stat_value):
        """Check that an attrs dictionary matches a stat result."""
        self.assertEqual(stat_value.st_size, attrs["size"])
        self.assertEqual(os.getuid(), attrs["uid"])
        self.assertEqual(os.getgid(), attrs["gid"])
        self.assertEqual(stat_value.st_mode, attrs["permissions"])
        self.assertEqual(int(stat_value.st_mtime), attrs["mtime"])
        self.assertEqual(int(stat_value.st_atime), attrs["atime"])

    def getPathSegment(self):
        """Return a unique path segment for testing.

        This returns a path segment such that 'path != unescape(path)'. This
        exercises the interface between the sftp server and the Bazaar
        transport, which expects escaped URL segments.
        """
        return self._factory.getUniqueString("%41%42%43-")


class TestSFTPFile(TestCaseInTempDir, SFTPTestMixin):
    """Tests for `TransportSFTPServer` and `TransportSFTPFile`."""

    run_tests_with = AsynchronousDeferredRunTest

    # This works around a clash between the TrialTestCase and the BzrTestCase.
    skip = None

    def setUp(self):
        TestCaseInTempDir.setUp(self)
        SFTPTestMixin.setUp(self)
        transport = AsyncTransport(
            FatLocalTransport(urlutils.local_path_to_url("."))
        )
        self._sftp_server = TransportSFTPServer(transport)

    @defer.inlineCallbacks
    def assertSFTPError(self, sftp_code, function, *args, **kwargs):
        """Assert that calling functions fails with `sftp_code`."""
        with ExpectedException(
            filetransfer.SFTPError, MatchesStructure.byEquality(code=sftp_code)
        ):
            yield function(*args, **kwargs)

    def openFile(self, path, flags, attrs):
        return self._sftp_server.openFile(path.encode("UTF-8"), flags, attrs)

    def test_openFileInNonexistingDirectory(self):
        # openFile fails with a no such file error if we try to open a file in
        # a directory that doesn't exist. The flags passed to openFile() do
        # not have any effect.
        return self.assertSFTPError(
            filetransfer.FX_NO_SUCH_FILE,
            self.openFile,
            "%s/%s" % (self.getPathSegment(), self.getPathSegment()),
            0,
            {},
        )

    def test_openFileInNonDirectory(self):
        # openFile fails with a no such file error if we try to open a file
        # that has another file as one of its "parents". The flags passed to
        # openFile() do not have any effect.
        nondirectory = self.getPathSegment()
        self.build_tree_contents([(nondirectory, b"content")])
        return self.assertSFTPError(
            filetransfer.FX_NO_SUCH_FILE,
            self.openFile,
            "%s/%s" % (nondirectory, self.getPathSegment()),
            0,
            {},
        )

    @defer.inlineCallbacks
    def test_createEmptyFile(self):
        # Opening a file with create flags and then closing it will create a
        # new, empty file.
        filename = self.getPathSegment()
        handle = yield self.openFile(filename, filetransfer.FXF_CREAT, {})
        yield handle.close()
        self.assertFileEqual(b"", filename)

    @defer.inlineCallbacks
    def test_createFileWithData(self):
        # writeChunk writes data to the file.
        filename = self.getPathSegment()
        handle = yield self.openFile(
            filename, filetransfer.FXF_CREAT | filetransfer.FXF_WRITE, {}
        )
        yield handle.writeChunk(0, b"bar")
        yield handle.close()
        self.assertFileEqual(b"bar", filename)

    @defer.inlineCallbacks
    def test_writeChunkToFile(self):
        filename = self.getPathSegment()
        self.build_tree_contents([(filename, b"contents")])
        handle = yield self.openFile(filename, filetransfer.FXF_WRITE, {})
        yield handle.writeChunk(1, b"qux")
        yield handle.close()
        self.assertFileEqual(b"cquxents", filename)

    @defer.inlineCallbacks
    def test_writeTwoChunks(self):
        # We can write one chunk after another.
        filename = self.getPathSegment()
        handle = yield self.openFile(
            filename, filetransfer.FXF_WRITE | filetransfer.FXF_TRUNC, {}
        )
        yield handle.writeChunk(1, b"a")
        yield handle.writeChunk(2, b"a")
        yield handle.close()
        self.assertFileEqual(b"\0aa", filename)

    @defer.inlineCallbacks
    def test_writeChunkToNonexistentFile(self):
        # Writing a chunk of data to a non-existent file creates the file even
        # if the create flag is not set. NOTE: This behaviour is unspecified
        # in the SFTP drafts at
        # http://tools.ietf.org/wg/secsh/draft-ietf-secsh-filexfer/
        filename = self.getPathSegment()
        handle = yield self.openFile(filename, filetransfer.FXF_WRITE, {})
        yield handle.writeChunk(1, b"qux")
        yield handle.close()
        self.assertFileEqual(b"\0qux", filename)

    @defer.inlineCallbacks
    def test_writeToReadOpenedFile(self):
        # writeChunk raises an error if we try to write to a file that has
        # been opened only for reading.
        filename = self.getPathSegment()
        self.build_tree_contents([(filename, b"bar")])
        handle = yield self.openFile(filename, filetransfer.FXF_READ, {})
        yield self.assertSFTPError(
            filetransfer.FX_PERMISSION_DENIED,
            handle.writeChunk,
            0,
            b"new content",
        )

    @defer.inlineCallbacks
    def test_overwriteFile(self):
        # writeChunk overwrites a file if write, create and trunk flags are
        # set.
        self.build_tree_contents([("foo", b"contents")])
        handle = yield self.openFile(
            "foo",
            filetransfer.FXF_CREAT
            | filetransfer.FXF_TRUNC
            | filetransfer.FXF_WRITE,
            {},
        )
        yield handle.writeChunk(0, b"bar")
        self.assertFileEqual(b"bar", "foo")

    @defer.inlineCallbacks
    def test_writeToAppendingFileIgnoresOffset(self):
        # If a file is opened with the 'append' flag, writeChunk ignores its
        # offset parameter.
        filename = self.getPathSegment()
        self.build_tree_contents([(filename, b"bar")])
        handle = yield self.openFile(filename, filetransfer.FXF_APPEND, {})
        yield handle.writeChunk(0, b"baz")
        self.assertFileEqual(b"barbaz", filename)

    @defer.inlineCallbacks
    def test_openAndCloseExistingFileLeavesUnchanged(self):
        # If we open a file with the 'create' flag and without the 'truncate'
        # flag, the file remains unchanged.
        filename = self.getPathSegment()
        self.build_tree_contents([(filename, b"bar")])
        handle = yield self.openFile(filename, filetransfer.FXF_CREAT, {})
        yield handle.close()
        self.assertFileEqual(b"bar", filename)

    @defer.inlineCallbacks
    def test_openAndCloseExistingFileTruncation(self):
        # If we open a file with the 'create' flag and the 'truncate' flag,
        # the file is reset to empty.
        filename = self.getPathSegment()
        self.build_tree_contents([(filename, b"bar")])
        handle = yield self.openFile(
            filename, filetransfer.FXF_TRUNC | filetransfer.FXF_CREAT, {}
        )
        yield handle.close()
        self.assertFileEqual(b"", filename)

    @defer.inlineCallbacks
    def test_writeChunkOnDirectory(self):
        # Errors in writeChunk are translated to SFTPErrors.
        directory = self.getPathSegment()
        os.mkdir(directory)
        handle = yield self.openFile(directory, filetransfer.FXF_WRITE, {})
        with ExpectedException(filetransfer.SFTPError):
            yield handle.writeChunk(0, b"bar")

    @defer.inlineCallbacks
    def test_readChunk(self):
        # readChunk reads a chunk of data from the file.
        filename = self.getPathSegment()
        self.build_tree_contents([(filename, b"bar")])
        handle = yield self.openFile(filename, 0, {})
        chunk = yield handle.readChunk(1, 2)
        self.assertEqual(b"ar", chunk)

    @defer.inlineCallbacks
    def test_readChunkPastEndOfFile(self):
        # readChunk returns the rest of the file if it is asked to read past
        # the end of the file.
        filename = self.getPathSegment()
        self.build_tree_contents([(filename, b"bar")])
        handle = yield self.openFile(filename, 0, {})
        chunk = yield handle.readChunk(2, 10)
        self.assertEqual(b"r", chunk)

    @defer.inlineCallbacks
    def test_readChunkEOF(self):
        # readChunk returns the empty string if it encounters end-of-file
        # before reading any data.
        filename = self.getPathSegment()
        self.build_tree_contents([(filename, b"bar")])
        handle = yield self.openFile(filename, 0, {})
        chunk = yield handle.readChunk(3, 10)
        self.assertEqual(b"", chunk)

    @defer.inlineCallbacks
    def test_readChunkError(self):
        # Errors in readChunk are translated to SFTPErrors.
        filename = self.getPathSegment()
        handle = yield self.openFile(filename, 0, {})
        with ExpectedException(filetransfer.SFTPError):
            yield handle.readChunk(1, 2)

    @defer.inlineCallbacks
    def test_setAttrs(self):
        # setAttrs on TransportSFTPFile does nothing.
        filename = self.getPathSegment()
        self.build_tree_contents([(filename, b"bar")])
        handle = yield self.openFile(filename, 0, {})
        yield handle.setAttrs({})

    @defer.inlineCallbacks
    def test_getAttrs(self):
        # getAttrs on TransportSFTPFile returns a dictionary consistent
        # with the results of os.stat.
        filename = self.getPathSegment()
        self.build_tree_contents([(filename, b"bar")])
        stat_value = os.stat(filename)
        handle = yield self.openFile(filename, 0, {})
        attrs = yield handle.getAttrs()
        self.checkAttrs(attrs, stat_value)

    @defer.inlineCallbacks
    def test_getAttrsError(self):
        # Errors in getAttrs on TransportSFTPFile are translated into
        # SFTPErrors.
        filename = self.getPathSegment()
        handle = yield self.openFile(filename, 0, {})
        with ExpectedException(filetransfer.SFTPError):
            yield handle.getAttrs()


class TestSFTPServer(TestCaseInTempDir, SFTPTestMixin):
    """Tests for `TransportSFTPServer` and `TransportSFTPFile`."""

    run_tests_with = AsynchronousDeferredRunTest

    def setUp(self):
        TestCaseInTempDir.setUp(self)
        SFTPTestMixin.setUp(self)
        transport = AsyncTransport(
            FatLocalTransport(urlutils.local_path_to_url("."))
        )
        self.sftp_server = TransportSFTPServer(transport)

    @defer.inlineCallbacks
    def test_serverSetAttrs(self):
        # setAttrs on the TransportSFTPServer doesn't do anything either.
        filename = self.getPathSegment()
        self.build_tree_contents([(filename, b"bar")])
        yield self.sftp_server.setAttrs(filename.encode("UTF-8"), {})

    @defer.inlineCallbacks
    def test_serverGetAttrs(self):
        # getAttrs on the TransportSFTPServer also returns a dictionary
        # consistent with the results of os.stat.
        filename = self.getPathSegment()
        self.build_tree_contents([(filename, b"bar")])
        stat_value = os.stat(filename)
        attrs = yield self.sftp_server.getAttrs(
            filename.encode("UTF-8"), False
        )
        self.checkAttrs(attrs, stat_value)

    @defer.inlineCallbacks
    def test_serverGetAttrsError(self):
        # Errors in getAttrs on the TransportSFTPServer are translated into
        # SFTPErrors.
        nonexistent_file = self.getPathSegment()
        with ExpectedException(filetransfer.SFTPError):
            yield self.sftp_server.getAttrs(
                nonexistent_file.encode("UTF-8"), False
            )

    @defer.inlineCallbacks
    def test_removeFile(self):
        # removeFile removes the file.
        filename = self.getPathSegment()
        self.build_tree_contents([(filename, b"bar")])
        yield self.sftp_server.removeFile(filename.encode("UTF-8"))
        self.assertFalse(file_exists(filename))

    @defer.inlineCallbacks
    def test_removeFileError(self):
        # Errors in removeFile are translated into SFTPErrors.
        filename = self.getPathSegment()
        with ExpectedException(filetransfer.SFTPError):
            yield self.sftp_server.removeFile(filename.encode("UTF-8"))

    @defer.inlineCallbacks
    def test_removeFile_directory(self):
        # Errors in removeFile are translated into SFTPErrors.
        filename = self.getPathSegment()
        self.build_tree_contents([(filename + "/",)])
        with ExpectedException(filetransfer.SFTPError):
            yield self.sftp_server.removeFile(filename.encode("UTF-8"))

    @defer.inlineCallbacks
    def test_renameFile(self):
        # renameFile renames the file.
        orig_filename = self.getPathSegment()
        new_filename = self.getPathSegment()
        self.build_tree_contents([(orig_filename, b"bar")])
        yield self.sftp_server.renameFile(
            orig_filename.encode("UTF-8"), new_filename.encode("UTF-8")
        )
        self.assertFalse(file_exists(orig_filename))
        self.assertTrue(file_exists(new_filename))

    @defer.inlineCallbacks
    def test_renameFileError(self):
        # Errors in renameFile are translated into SFTPErrors.
        orig_filename = self.getPathSegment()
        new_filename = self.getPathSegment()
        with ExpectedException(filetransfer.SFTPError):
            yield self.sftp_server.renameFile(
                orig_filename.encode("UTF-8"), new_filename.encode("UTF-8")
            )

    @defer.inlineCallbacks
    def test_makeDirectory(self):
        # makeDirectory makes the directory.
        directory = self.getPathSegment()
        yield self.sftp_server.makeDirectory(
            directory.encode("UTF-8"), {"permissions": 0o777}
        )
        self.assertTrue(
            os.path.isdir(directory), "%r is not a directory" % directory
        )
        self.assertEqual(0o40777, os.stat(directory).st_mode)

    @defer.inlineCallbacks
    def test_makeDirectoryError(self):
        # Errors in makeDirectory are translated into SFTPErrors.
        nonexistent = self.getPathSegment()
        nonexistent_child = "%s/%s" % (nonexistent, self.getPathSegment())
        with ExpectedException(filetransfer.SFTPError):
            yield self.sftp_server.makeDirectory(
                nonexistent_child.encode("UTF-8"), {"permissions": 0o777}
            )

    @defer.inlineCallbacks
    def test_removeDirectory(self):
        # removeDirectory removes the directory.
        directory = self.getPathSegment()
        os.mkdir(directory)
        yield self.sftp_server.removeDirectory(directory.encode("UTF-8"))
        self.assertFalse(file_exists(directory))

    @defer.inlineCallbacks
    def test_removeDirectoryError(self):
        # Errors in removeDirectory are translated into SFTPErrors.
        directory = self.getPathSegment()
        with ExpectedException(filetransfer.SFTPError):
            yield self.sftp_server.removeDirectory(directory.encode("UTF-8"))

    def test_gotVersion(self):
        # gotVersion returns an empty dictionary.
        extended = self.sftp_server.gotVersion("version", {})
        self.assertEqual({}, extended)

    def test_extendedRequest(self):
        # We don't support any extensions.
        self.assertRaises(
            NotImplementedError,
            self.sftp_server.extendedRequest,
            b"foo",
            b"bar",
        )

    @defer.inlineCallbacks
    def test_realPath(self):
        # realPath returns the absolute path of the file.
        src, dst = self.getPathSegment(), self.getPathSegment()
        os.symlink(src, dst)
        path = yield self.sftp_server.realPath(dst.encode("UTF-8"))
        self.assertEqual(os.path.abspath(src).encode("UTF-8"), path)

    def test_makeLink(self):
        # makeLink is not supported.
        self.assertRaises(
            NotImplementedError,
            self.sftp_server.makeLink,
            self.getPathSegment().encode("UTF-8"),
            self.getPathSegment().encode("UTF-8"),
        )

    def test_readLink(self):
        # readLink is not supported.
        self.assertRaises(
            NotImplementedError,
            self.sftp_server.readLink,
            self.getPathSegment().encode("UTF-8"),
        )

    @defer.inlineCallbacks
    def test_openDirectory(self):
        # openDirectory returns an iterator that iterates over the contents of
        # the directory.
        parent_dir = self.getPathSegment()
        child_dir = self.getPathSegment()
        child_file = self.getPathSegment()
        self.build_tree(
            [
                parent_dir + "/",
                "%s/%s/" % (parent_dir, child_dir),
                "%s/%s" % (parent_dir, child_file),
            ]
        )
        directory = yield self.sftp_server.openDirectory(
            parent_dir.encode("UTF-8")
        )
        entries = list(directory)
        directory.close()
        names = [entry[0] for entry in entries]
        self.assertEqual(
            set(names), {child_dir.encode("UTF-8"), child_file.encode("UTF-8")}
        )

        def check_entry(entries, filename):
            t = get_transport(".")
            stat = t.stat(urlutils.escape("%s/%s" % (parent_dir, filename)))
            named_entries = [
                entry
                for entry in entries
                if entry[0] == filename.encode("UTF-8")
            ]
            self.assertEqual(1, len(named_entries))
            name, longname, attrs = named_entries[0]
            self.assertEqual(lsLine(name, stat), longname)
            self.assertEqual(self.sftp_server._translate_stat(stat), attrs)

        check_entry(entries, child_dir)
        check_entry(entries, child_file)

    @defer.inlineCallbacks
    def test_openDirectoryError(self):
        # Errors in openDirectory are translated into SFTPErrors.
        nonexistent = self.getPathSegment()
        with ExpectedException(filetransfer.SFTPError):
            yield self.sftp_server.openDirectory(nonexistent.encode("UTF-8"))

    @defer.inlineCallbacks
    def test_openDirectoryMemory(self):
        """openDirectory works on MemoryTransport."""
        transport = MemoryTransport()
        transport.put_bytes("hello", b"hello")
        sftp_server = TransportSFTPServer(AsyncTransport(transport))
        directory = yield sftp_server.openDirectory(b".")
        with closing(directory):
            names = [entry[0] for entry in directory]
        self.assertEqual([b"hello"], names)

    def test__format_directory_entries_with_MemoryStat(self):
        """format_directory_entries works with MemoryStat.

        MemoryStat lacks many fields, but format_directory_entries works
        around that.
        """
        t = MemoryTransport()
        stat_result = t.stat(".")
        entries = self.sftp_server._format_directory_entries(
            [stat_result], ["filename"]
        )
        self.assertEqual(
            list(entries),
            [
                (
                    b"filename",
                    "drwxr-xr-x    0 0        0               0 "
                    "Jan 01  1970 filename",
                    {
                        "atime": 0,
                        "gid": 0,
                        "mtime": 0,
                        "permissions": 16877,
                        "size": 0,
                        "uid": 0,
                    },
                )
            ],
        )
        self.assertIs(None, getattr(stat_result, "st_mtime", None))

    def do_translation_test(self, exception, sftp_code, method_name=None):
        """Test that `exception` is translated into the correct SFTPError."""
        result = self.assertRaises(
            filetransfer.SFTPError,
            self.sftp_server.translateError,
            failure.Failure(exception),
            method_name,
        )
        self.assertEqual(sftp_code, result.code)
        self.assertEqual(str(exception), result.message)

    def test_translatePermissionDenied(self):
        exception = bzr_errors.PermissionDenied(self.getPathSegment())
        self.do_translation_test(exception, filetransfer.FX_PERMISSION_DENIED)

    def test_translateTransportNotPossible(self):
        exception = bzr_errors.TransportNotPossible(self.getPathSegment())
        self.do_translation_test(exception, filetransfer.FX_PERMISSION_DENIED)

    def test_translateNoSuchFile(self):
        exception = BzrNoSuchFile(self.getPathSegment())
        self.do_translation_test(exception, filetransfer.FX_NO_SUCH_FILE)

    def test_translateFileExists(self):
        exception = FileExists(self.getPathSegment())
        self.do_translation_test(
            exception, filetransfer.FX_FILE_ALREADY_EXISTS
        )

    def test_translateFileIsADirectory(self):
        exception = FileIsADirectory(self.getPathSegment())
        self.do_translation_test(
            exception, filetransfer.FX_FILE_IS_A_DIRECTORY
        )

    def test_translateDirectoryNotEmpty(self):
        exception = bzr_errors.DirectoryNotEmpty(self.getPathSegment())
        self.do_translation_test(exception, filetransfer.FX_FAILURE)

    def test_translateRandomError(self):
        # translateError re-raises unrecognized errors.
        exception = KeyboardInterrupt()
        result = self.assertRaises(
            KeyboardInterrupt,
            self.sftp_server.translateError,
            failure.Failure(exception),
            "methodName",
        )
        self.assertIs(result, exception)
