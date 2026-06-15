# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""An SFTP server that backs on to a special kind of Bazaar Transport.

The Bazaar Transport is special in two ways:

 1. It implements two methods `writeChunk` and `local_realPath` (see the
    `FatLocalTransport` class for a description of these)
 2. Every transport method returns Deferreds and does not block.

We call such a transport a "Twisted Transport".
"""

__all__ = [
    "avatar_to_sftp_server",
    "TransportSFTPServer",
]


import os
import stat
from copy import copy

from breezy import errors as bzr_errors
from breezy import osutils, urlutils
from breezy.transport import NoSuchFile, FileExists
from breezy.transport.local import LocalTransport
from lazr.sshserver.sftp import FileIsADirectory
from twisted.conch.interfaces import ISFTPFile, ISFTPServer
from twisted.conch.ls import lsLine
from twisted.conch.ssh import filetransfer
from twisted.internet import defer
from twisted.python import util
from zope.interface import implementer

from lp.codehosting.vfs import AsyncLaunchpadTransport, LaunchpadServer
from lp.services.config import config
from lp.services.twistedsupport import gatherResults


class FatLocalTransport(LocalTransport):
    """A Bazaar transport that also implements writeChunk and local_realPath.

    We need these so that we can implement SFTP over a Bazaar transport.
    """

    def clone(self, offset=None):
        if offset is None:
            abspath = self.base
        else:
            abspath = self.abspath(offset)
        return FatLocalTransport(abspath)

    def writeChunk(self, name, offset, data):
        """Write a chunk of data to file `name` at `offset`."""
        abspath = self._abspath(name)
        osutils.check_legal_path(abspath)
        try:
            chunk_file = os.open(abspath, os.O_CREAT | os.O_WRONLY)
        except IsADirectoryError:
            raise FileIsADirectory(name)
        os.lseek(chunk_file, offset, 0)
        os.write(chunk_file, data)
        os.close(chunk_file)

    def local_realPath(self, path):
        """Return the absolute path to `path`."""
        abspath = self._abspath(path)
        return urlutils.escape(os.path.realpath(abspath))


def with_sftp_error(func):
    """Decorator used to translate Bazaar errors into SFTP errors.

    This assumes that the function being decorated returns a Deferred.

    See `TransportSFTPServer.translateError` for the details of the
    translation.
    """

    def decorator(*args, **kwargs):
        deferred = func(*args, **kwargs)
        return deferred.addErrback(
            TransportSFTPServer.translateError, func.__name__
        )

    return util.mergeFunctionMetadata(func, decorator)


class DirectoryListing:
    """Class to satisfy openDirectory return interface.

    openDirectory returns an iterator -- with a `close` method.  Hence
    this class.
    """

    def __init__(self, entries):
        self.iter = iter(entries)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.iter)

    def close(self):
        # I can't believe we had to implement a whole class just to
        # have this do-nothing method (abentley).
        pass


@implementer(ISFTPFile)
class TransportSFTPFile:
    """An implementation of `ISFTPFile` that backs onto a Bazaar transport.

    The transport must be a Twisted Transport.
    """

    def __init__(self, transport, name, flags, server):
        self._unescaped_relpath = name
        self._escaped_path = urlutils.escape(self._unescaped_relpath)
        self._flags = flags
        self.transport = transport
        self._written = False
        self._server = server

    def _shouldAppend(self):
        """Is this file opened append?"""
        return bool(self._flags & filetransfer.FXF_APPEND)

    def _shouldCreate(self):
        """Should we create a file?"""
        # The Twisted VFS adapter creates a file when any of these flags are
        # set. It's possible that we only need to check for FXF_CREAT.
        create_mask = (
            filetransfer.FXF_WRITE
            | filetransfer.FXF_APPEND
            | filetransfer.FXF_CREAT
        )
        return bool(self._flags & create_mask)

    def _shouldTruncate(self):
        """Should we truncate the file?"""
        return bool(self._flags & filetransfer.FXF_TRUNC) and not self._written

    def _shouldWrite(self):
        """Is this file opened writable?"""
        write_mask = filetransfer.FXF_WRITE | filetransfer.FXF_APPEND
        return bool(self._flags & write_mask)

    def _truncateFile(self):
        """Truncate this file."""
        self._written = True
        return self.transport.put_bytes(self._escaped_path, b"")

    @with_sftp_error
    @defer.inlineCallbacks
    def writeChunk(self, offset, data):
        """See `ISFTPFile`."""
        if not self._shouldWrite():
            raise filetransfer.SFTPError(
                filetransfer.FX_PERMISSION_DENIED,
                "%r was opened read-only." % self._unescaped_relpath,
            )
        if self._shouldTruncate():
            yield self._truncateFile()
        self._written = True
        if self._shouldAppend():
            yield self.transport.append_bytes(self._escaped_path, data)
        else:
            yield self.transport.writeChunk(self._escaped_path, offset, data)

    @with_sftp_error
    @defer.inlineCallbacks
    def readChunk(self, offset, length):
        """See `ISFTPFile`."""
        try:
            read_things = yield self.transport.readv(
                self._escaped_path, [(offset, length)]
            )
            chunk = next(read_things)[1]
        except bzr_errors.ShortReadvError as e:
            # Handle short reads by reading what was available.
            # Doing things this way around, by trying to read all the data
            # requested and then handling the short read error, might be a
            # bit inefficient, but the breezy sftp transport doesn't read
            # past the end of files, so we don't need to worry too much
            # about performance here.
            chunk = yield self.readChunk(e.offset, e.actual)
        return chunk

    def setAttrs(self, attrs):
        """See `ISFTPFile`.

        The Transport interface does not allow setting any attributes.
        """
        # XXX 2008-05-09 JonathanLange: This should at least raise an error,
        # not do nothing silently.
        return self._server.setAttrs(
            self._unescaped_relpath.encode("UTF-8"), attrs
        )

    @with_sftp_error
    def getAttrs(self):
        """See `ISFTPFile`."""
        return self._server.getAttrs(
            self._unescaped_relpath.encode("UTF-8"), False
        )

    @defer.inlineCallbacks
    def close(self):
        """See `ISFTPFile`."""
        if self._written or not self._shouldCreate():
            return

        if self._shouldTruncate():
            yield self._truncateFile()
            return

        already_exists = yield self.transport.has(self._escaped_path)
        if not already_exists:
            yield self._truncateFile()


def _get_transport_for_dir(directory):
    url = urlutils.local_path_to_url(directory)
    return FatLocalTransport(url)


def avatar_to_sftp_server(avatar):
    user_id = avatar.user_id
    branch_transport = _get_transport_for_dir(
        config.codehosting.mirrored_branches_root
    )
    server = LaunchpadServer(
        avatar.codehosting_proxy, user_id, branch_transport
    )
    server.start_server()
    transport = AsyncLaunchpadTransport(server, server.get_url())
    return TransportSFTPServer(transport)


@implementer(ISFTPServer)
class TransportSFTPServer:
    """An implementation of `ISFTPServer` that backs onto a Bazaar transport.

    The transport must be a Twisted Transport.
    """

    def __init__(self, transport):
        self.transport = transport

    def _decodePath(self, path):
        """Decode a path to UTF-8.

        Twisted sends paths over SFTP as bytes, so we must decode them.
        """
        try:
            return path.decode("UTF-8")
        except UnicodeDecodeError:
            raise filetransfer.SFTPError(filetransfer.FX_BAD_MESSAGE, path)

    def extendedRequest(self, extendedName, extendedData):
        """See `ISFTPServer`."""
        raise NotImplementedError

    def makeLink(self, src, dest):
        """See `ISFTPServer`."""
        raise NotImplementedError()

    def _stat_files_in_list(self, file_list, escaped_dir_path):
        """Stat the a list of files.

        :param file_list: The list of escaped file names.
        :param escaped_dir_path: The escaped path of the directory containing
            the files.
        :return: A Deferred which will be called back with the list of all the
            stat results.
        """
        deferreds = []
        for filename in file_list:
            escaped_file_path = os.path.join(escaped_dir_path, filename)
            deferreds.append(self.transport.stat(escaped_file_path))
        return gatherResults(deferreds)

    def _format_directory_entries(self, stat_results, filenames):
        """Produce entries suitable for returning from `openDirectory`.

        :param stat_results: A list of the results of calling `stat` on each
            file in filenames.
        :param filenames: The list of filenames to produce entries for.
        :return: An iterator of ``(shortname, longname, attributes)``.
        """
        for stat_result, filename in zip(stat_results, filenames):
            shortname = urlutils.unescape(filename).encode("utf-8")
            stat_result = copy(stat_result)
            for attribute in ["st_uid", "st_gid", "st_mtime", "st_nlink"]:
                if getattr(stat_result, attribute, None) is None:
                    setattr(stat_result, attribute, 0)
            longname = lsLine(shortname, stat_result)
            attr_dict = self._translate_stat(stat_result)
            yield (shortname, longname, attr_dict)

    @with_sftp_error
    @defer.inlineCallbacks
    def openDirectory(self, path):
        """See `ISFTPServer`."""
        path = self._decodePath(path)
        escaped_path = urlutils.escape(path)
        file_list = yield self.transport.list_dir(escaped_path)
        stat_results = yield self._stat_files_in_list(file_list, escaped_path)
        entries = self._format_directory_entries(stat_results, file_list)
        return DirectoryListing(entries)

    @with_sftp_error
    @defer.inlineCallbacks
    def openFile(self, path, flags, attrs):
        """See `ISFTPServer`."""
        path = self._decodePath(path)
        directory = os.path.dirname(path)
        stat_result = yield self.transport.stat(directory)
        if stat.S_ISDIR(stat_result.st_mode):
            return TransportSFTPFile(self.transport, path, flags, self)
        else:
            raise filetransfer.SFTPError(
                filetransfer.FX_NO_SUCH_FILE, directory
            )

    def readLink(self, path):
        """See `ISFTPServer`."""
        raise NotImplementedError()

    @defer.inlineCallbacks
    def realPath(self, relpath):
        """See `ISFTPServer`."""
        relpath = self._decodePath(relpath)
        path = yield self.transport.local_realPath(urlutils.escape(relpath))
        unescaped_path = urlutils.unescape(path)
        return unescaped_path.encode("utf-8")

    def setAttrs(self, path, attrs):
        """See `ISFTPServer`.

        This just delegates to TransportSFTPFile's implementation.
        """
        return defer.succeed(None)

    def _translate_stat(self, stat_val):
        """Translate the stat result `stat_val` into an attributes dict.

        This is very like conch.ssh.unix.SFTPServerForUnixConchUser._getAttrs,
        but (a) that is private and (b) we use getattr() to access the
        attributes as not all the Bazaar transports return full stat results.
        """
        return {
            "size": getattr(stat_val, "st_size", 0),
            "uid": getattr(stat_val, "st_uid", 0),
            "gid": getattr(stat_val, "st_gid", 0),
            "permissions": getattr(stat_val, "st_mode", 0),
            "atime": int(getattr(stat_val, "st_atime", 0)),
            "mtime": int(getattr(stat_val, "st_mtime", 0)),
        }

    @with_sftp_error
    @defer.inlineCallbacks
    def getAttrs(self, path, followLinks):
        """See `ISFTPServer`.

        This just delegates to TransportSFTPFile's implementation.
        """
        path = self._decodePath(path)
        stat_result = yield self.transport.stat(urlutils.escape(path))
        return self._translate_stat(stat_result)

    def gotVersion(self, otherVersion, extensionData):
        """See `ISFTPServer`."""
        return {}

    @with_sftp_error
    def makeDirectory(self, path, attrs):
        """See `ISFTPServer`."""
        path = self._decodePath(path)
        return self.transport.mkdir(
            urlutils.escape(path), attrs["permissions"]
        )

    @with_sftp_error
    def removeDirectory(self, path):
        """See `ISFTPServer`."""
        path = self._decodePath(path)
        return self.transport.rmdir(urlutils.escape(path))

    @with_sftp_error
    def removeFile(self, path):
        """See `ISFTPServer`."""
        path = self._decodePath(path)
        return self.transport.delete(urlutils.escape(path))

    @with_sftp_error
    def renameFile(self, oldpath, newpath):
        """See `ISFTPServer`."""
        oldpath = self._decodePath(oldpath)
        newpath = self._decodePath(newpath)
        return self.transport.rename(
            urlutils.escape(oldpath), urlutils.escape(newpath)
        )

    @staticmethod
    def translateError(failure, func_name):
        """Translate Bazaar errors to `filetransfer.SFTPError` instances."""
        types_to_codes = {
            bzr_errors.PermissionDenied: filetransfer.FX_PERMISSION_DENIED,
            bzr_errors.TransportNotPossible: filetransfer.FX_PERMISSION_DENIED,
            NoSuchFile: filetransfer.FX_NO_SUCH_FILE,
            FileExists: filetransfer.FX_FILE_ALREADY_EXISTS,
            bzr_errors.DirectoryNotEmpty: filetransfer.FX_FAILURE,
            bzr_errors.TransportError: filetransfer.FX_FAILURE,
            FileIsADirectory: filetransfer.FX_FILE_IS_A_DIRECTORY,
        }
        # Bazaar expects makeDirectory to fail with exactly the string "mkdir
        # failed".
        names_to_messages = {
            "makeDirectory": "mkdir failed",
        }
        try:
            sftp_code = types_to_codes[failure.type]
        except KeyError:
            failure.raiseException()
        message = names_to_messages.get(func_name, failure.getErrorMessage())
        raise filetransfer.SFTPError(sftp_code, message)
