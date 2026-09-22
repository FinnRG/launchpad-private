# Copyright 2013-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Librarian disk to Swift storage tests."""

import hashlib
import io
import os.path
import time
from unittest.mock import patch

import transaction
from fixtures import MonkeyPatch
from swiftclient import client as swiftclient
from urllib3.exceptions import ReadTimeoutError

from lp.services.database import write_transaction
from lp.services.database.interfaces import IStore
from lp.services.features.testing import FeatureFixture
from lp.services.librarian.client import LibrarianClient
from lp.services.librarian.model import LibraryFileAlias
from lp.services.librarianserver import swift
from lp.services.librarianserver.storage import LibrarianStorage
from lp.services.log.logger import BufferLogger
from lp.testing import TestCase
from lp.testing.layers import BaseLayer, LaunchpadZopelessLayer, LibrarianLayer
from lp.testing.swift.fixture import SwiftFixture


def nop(*args, **kwargs):
    pass


class FakeSwiftCall:
    """A callable that raises the given errors before returning a result."""

    def __init__(self, errors, result):
        self._errors = list(errors)
        self._result = result
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._result


class TestFeedSwift(TestCase):
    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.swift_fixture = self.useFixture(SwiftFixture())
        self.useFixture(FeatureFixture({"librarian.swift.enabled": True}))
        transaction.commit()

        # Restart the Librarian so it picks up the OS_* environment
        # variables.
        LibrarianLayer.librarian_fixture.cleanUp()
        LibrarianLayer.librarian_fixture.setUp()

        # Add some files. These common sample files all have their
        # modification times set to the past so they will not be
        # considered potential in-progress uploads.
        the_past = time.time() - 25 * 60 * 60
        self.librarian_client = LibrarianClient()
        self.contents = [str(i).encode("ASCII") * i for i in range(1, 5)]
        self.lfa_ids = [
            self.add_file(f"file_{i}", content, when=the_past)
            for i, content in enumerate(self.contents)
        ]
        self.lfas = [
            IStore(LibraryFileAlias).get(LibraryFileAlias, lfa_id)
            for lfa_id in self.lfa_ids
        ]
        self.lfcs = [lfa.content for lfa in self.lfas]

    def tearDown(self):
        super().tearDown()
        # Restart the Librarian so it picks up the feature flag change.
        self.attachLibrarianLog(LibrarianLayer.librarian_fixture)
        LibrarianLayer.librarian_fixture.cleanUp()
        LibrarianLayer.librarian_fixture.setUp()

    @write_transaction
    def add_file(self, name, content, when=None, content_type="text/plain"):
        lfa_id = self.librarian_client.addFile(
            name=name,
            size=len(content),
            file=io.BytesIO(content),
            contentType=content_type,
        )
        if when is None:
            when = 0  # Very very old
        lfa = IStore(LibraryFileAlias).get(LibraryFileAlias, lfa_id)
        path = swift.filesystem_path(lfa.content.id)
        os.utime(path, (when, when))
        return lfa_id

    def test_copy_to_swift(self):
        log = BufferLogger()

        # Confirm that files exist on disk where we expect to find them.
        for lfc in self.lfcs:
            path = swift.filesystem_path(lfc.id)
            self.assertTrue(os.path.exists(path))

        # Copy all the files into Swift.
        swift.to_swift(log, remove_func=None)

        # Confirm that files exist on disk where we expect to find them.
        for lfc in self.lfcs:
            path = swift.filesystem_path(lfc.id)
            self.assertTrue(os.path.exists(path))

        # Confirm all the files are also in Swift.
        swift_client = self.swift_fixture.connect()
        for lfc, contents in zip(self.lfcs, self.contents):
            container, name = swift.swift_location(lfc.id)
            headers, obj = swift_client.get_object(container, name)
            self.assertEqual(contents, obj, "Did not round trip")

        # Running again does nothing, in particular does not reupload
        # the files to Swift.
        con_patch = patch.object(
            swift.swiftclient.Connection,
            "put_object",
            side_effect=AssertionError("do not call"),
        )
        with con_patch:
            swift.to_swift(log)  # remove_func == None

    def test_copy_to_swift_and_rename(self):
        log = BufferLogger()

        # Confirm that files exist on disk where we expect to find them.
        for lfc in self.lfcs:
            path = swift.filesystem_path(lfc.id)
            self.assertTrue(os.path.exists(path))

        # Copy all the files into Swift.
        swift.to_swift(log, remove_func=swift.rename)

        # Confirm that files exist on disk where we expect to find them.
        for lfc in self.lfcs:
            path = swift.filesystem_path(lfc.id) + ".migrated"
            self.assertTrue(os.path.exists(path))

        # Confirm all the files are also in Swift.
        swift_client = self.swift_fixture.connect()
        for lfc, contents in zip(self.lfcs, self.contents):
            container, name = swift.swift_location(lfc.id)
            headers, obj = swift_client.get_object(container, name)
            self.assertEqual(contents, obj, "Did not round trip")

        # Running again does nothing, in particular does not reupload
        # the files to Swift.
        con_patch = patch.object(
            swift.swiftclient.Connection,
            "put_object",
            side_effect=AssertionError("do not call"),
        )
        with con_patch:
            swift.to_swift(log, remove_func=swift.rename)  # remove == False

    def test_move_to_swift(self):
        log = BufferLogger()

        # Confirm that files exist on disk where we expect to find them.
        for lfc in self.lfcs:
            path = swift.filesystem_path(lfc.id)
            self.assertTrue(os.path.exists(path))

        # Migrate all the files into Swift.
        swift.to_swift(log, remove_func=os.unlink)

        # Confirm that all the files have gone from disk.
        for lfc in self.lfcs:
            self.assertFalse(os.path.exists(swift.filesystem_path(lfc.id)))

        # Confirm all the files are in Swift.
        swift_client = self.swift_fixture.connect()
        for lfc, contents in zip(self.lfcs, self.contents):
            container, name = swift.swift_location(lfc.id)
            headers, obj = swift_client.get_object(container, name)
            self.assertEqual(contents, obj, "Did not round trip")

    def test_move_to_swift_multiple_instances(self):
        # If multiple Swift instances are configured, we only migrate to the
        # newest one.
        old_swift_fixture = self.useFixture(SwiftFixture(old_instance=True))

        log = BufferLogger()

        # Confirm that files exist on disk where we expect to find them.
        for lfc in self.lfcs:
            path = swift.filesystem_path(lfc.id)
            self.assertTrue(os.path.exists(path))

        # Migrate all the files into Swift.
        swift.to_swift(log, remove_func=os.unlink)

        # Confirm that all the files have gone from disk.
        for lfc in self.lfcs:
            self.assertFalse(os.path.exists(swift.filesystem_path(lfc.id)))

        # Confirm all the files are in the correct Swift instance.
        swift_client = self.swift_fixture.connect()
        try:
            for lfc, contents in zip(self.lfcs, self.contents):
                container, name = swift.swift_location(lfc.id)
                headers, obj = swift_client.get_object(container, name)
                self.assertEqual(contents, obj, "Did not round trip")
        finally:
            swift_client.close()

        old_swift_client = old_swift_fixture.connect()
        try:
            for lfc, _ in zip(self.lfcs, self.contents):
                container, name = swift.swift_location(lfc.id)
                self.assertRaises(
                    swiftclient.ClientException,
                    swift.quiet_swiftclient,
                    old_swift_client.get_object,
                    container,
                    name,
                )
        finally:
            old_swift_client.close()

    def test_librarian_serves_from_swift(self):
        log = BufferLogger()

        # Move all the files into Swift and off the file system.
        swift.to_swift(log, remove_func=os.unlink)

        # Confirm we can still access the files from the Librarian.
        for lfa_id, content in zip(self.lfa_ids, self.contents):
            data = self.librarian_client.getFileByAlias(lfa_id).read()
            self.assertEqual(content, data)

    def test_librarian_serves_from_disk(self):
        # Ensure the Librarian falls back to serving files from disk
        # when they cannot be found in the Swift server. Note that other
        # Librarian tests do not have Swift active, so this test is not
        # redundant.
        for lfa_id, content in zip(self.lfa_ids, self.contents):
            data = self.librarian_client.getFileByAlias(lfa_id).read()
            self.assertEqual(content, data)

    def test_largish_binary_files_from_disk(self):
        # Generate a largish blob, including null bytes for kicks.
        # A largish file is large enough that the HTTP upload needs
        # to be done in multiple chunks, but small enough that it is
        # stored in Swift as a single object.
        size = 512 * 1024  # 512KB
        expected_content = b"".join(bytes((i % 256,)) for i in range(0, size))
        lfa_id = self.add_file("hello_bigboy.xls", expected_content)

        # Data round trips when served from disk.
        lfa = self.librarian_client.getFileByAlias(lfa_id)
        self.assertEqual(expected_content, lfa.read())

    def test_largish_binary_files_from_swift(self):
        # Generate large blob, multiple of the chunk size.
        # Including null bytes for kicks.
        # A largish file is large enough that the HTTP upload needs
        # to be done in multiple chunks, but small enough that it is
        # stored in Swift as a single object.
        size = LibrarianStorage.CHUNK_SIZE * 50
        self.assertTrue(size > 1024 * 1024)
        expected_content = b"".join(bytes((i % 256,)) for i in range(0, size))
        lfa_id = self.add_file("hello_bigboy.xls", expected_content)
        lfc = IStore(LibraryFileAlias).get(LibraryFileAlias, lfa_id).content

        # This data size is a multiple of our chunk size.
        self.assertEqual(
            0, len(expected_content) % LibrarianStorage.CHUNK_SIZE
        )

        # Data round trips when served from Swift.
        swift.to_swift(BufferLogger(), remove_func=os.unlink)
        self.assertFalse(os.path.exists(swift.filesystem_path(lfc.id)))
        lfa = self.librarian_client.getFileByAlias(lfa_id)
        self.assertEqual(expected_content, lfa.read())

    def test_largish_binary_files_from_swift_offset(self):
        # Generate large blob, but NOT a multiple of the chunk size.
        # Including null bytes for kicks.
        # A largish file is large enough that the HTTP upload needs
        # to be done in multiple chunks, but small enough that it is
        # stored in Swift as a single object.
        size = LibrarianStorage.CHUNK_SIZE * 50 + 1
        self.assertTrue(size > 1024 * 1024)
        expected_content = b"".join(bytes((i % 256,)) for i in range(0, size))
        lfa_id = self.add_file("hello_bigboy.xls", expected_content)
        lfc = IStore(LibraryFileAlias).get(LibraryFileAlias, lfa_id).content

        # This data size is NOT a multiple of our chunk size.
        self.assertNotEqual(
            0, len(expected_content) % LibrarianStorage.CHUNK_SIZE
        )

        # Data round trips when served from Swift.
        swift.to_swift(BufferLogger(), remove_func=os.unlink)
        lfa = self.librarian_client.getFileByAlias(lfa_id)
        self.assertFalse(os.path.exists(swift.filesystem_path(lfc.id)))
        self.assertEqual(expected_content, lfa.read())

    def test_large_file_to_swift(self):
        # Generate a blob large enough that Swift requires us to store
        # it as multiple objects plus a manifest.
        size = LibrarianStorage.CHUNK_SIZE * 50
        self.assertTrue(size > 1024 * 1024)
        expected_content = b"".join(bytes((i % 256,)) for i in range(0, size))
        lfa_id = self.add_file("hello_bigboy.xls", expected_content)
        lfa = IStore(LibraryFileAlias).get(LibraryFileAlias, lfa_id)
        lfc = lfa.content

        # We don't really want to upload a file >5GB to our mock Swift,
        # so change the constant instead. Set it so we need 3 segments.
        def _reset_max(val):
            swift.MAX_SWIFT_OBJECT_SIZE = val

        self.addCleanup(_reset_max, swift.MAX_SWIFT_OBJECT_SIZE)
        swift.MAX_SWIFT_OBJECT_SIZE = int(size / 2) - 1

        # Shove the file requiring multiple segments into Swift.
        swift.to_swift(BufferLogger(), remove_func=None)

        # As our mock Swift does not support multi-segment files,
        # instead we examine it directly in Swift as best we can.
        swift_client = self.swift_fixture.connect()

        # The manifest exists. Unfortunately, we can't test that the
        # magic manifest header is set correctly.
        container, name = swift.swift_location(lfc.id)
        headers, obj = swift_client.get_object(container, name)
        self.assertEqual(obj, b"")

        # The segments we expect are all in their expected locations.
        _, obj1 = swift_client.get_object(container, f"{name}/0000")
        _, obj2 = swift_client.get_object(container, f"{name}/0001")
        _, obj3 = swift_client.get_object(container, f"{name}/0002")
        self.assertRaises(
            swiftclient.ClientException,
            swift.quiet_swiftclient,
            swift_client.get_object,
            container,
            f"{name}/0003",
        )

        # Our object round tripped
        self.assertEqual(obj1 + obj2 + obj3, expected_content)

    def test_multiple_feed_instances(self):
        log = BufferLogger()

        # Confirm that files exist on disk where we expect to find them.
        for lfc in self.lfcs:
            path = swift.filesystem_path(lfc.id)
            self.assertTrue(os.path.exists(path))

        # Migrate all the files into Swift, using multiple instances.  For
        # each instance, only the matching files are processed.
        for instance_id in range(3):
            swift.to_swift(
                log,
                instance_id=instance_id,
                num_instances=3,
                remove_func=os.unlink,
            )

            # Only the matching files are gone from disk.  (This is
            # cumulative, so we test for all instances up to this point.)
            for lfc in self.lfcs:
                exists = os.path.exists(swift.filesystem_path(lfc.id))
                if (lfc.id % 3) <= instance_id:
                    self.assertFalse(exists)
                else:
                    self.assertTrue(exists)

            # Confirm all the files that have been migrated so far are in
            # Swift.
            swift_client = self.swift_fixture.connect()
            try:
                for lfc, contents in zip(self.lfcs, self.contents):
                    container, name = swift.swift_location(lfc.id)
                    if (lfc.id % 3) <= instance_id:
                        headers, obj = swift_client.get_object(container, name)
                        self.assertEqual(contents, obj, "Did not round trip")
                    else:
                        self.assertRaises(
                            swiftclient.ClientException,
                            swift.quiet_swiftclient,
                            swift_client.get_object,
                            container,
                            name,
                        )
            finally:
                swift_client.close()

    def test_multiple_feed_instances_race(self):
        # The feed process copes with one instance removing its files in
        # parallel with another instance running.  Simulate this with a
        # remove_func that removes other instances' files as well.
        def remove_this_and_others(path):
            os.unlink(path)
            for lfc in self.lfcs:
                if (lfc.id % 3) != 0:
                    other_path = swift.filesystem_path(lfc.id)
                    try:
                        os.unlink(other_path)
                    except FileNotFoundError:
                        pass

        log = BufferLogger()

        # Confirm that files exist on disk where we expect to find them.
        for lfc in self.lfcs:
            path = swift.filesystem_path(lfc.id)
            self.assertTrue(os.path.exists(path))

        # Migrate all the files for the first instance into Swift,
        # pretending that other instances are running in parallel.
        swift.to_swift(
            log,
            instance_id=0,
            num_instances=3,
            remove_func=remove_this_and_others,
        )

        # All the matching files are gone from disk (some removed by the
        # first instance, others by our remove_func).
        for lfc in self.lfcs:
            self.assertFalse(os.path.exists(swift.filesystem_path(lfc.id)))

        # All the files migrated by this instance are in Swift.
        swift_client = self.swift_fixture.connect()
        try:
            for lfc, contents in zip(self.lfcs, self.contents):
                container, name = swift.swift_location(lfc.id)
                if (lfc.id % 3) == 0:
                    headers, obj = swift_client.get_object(container, name)
                    self.assertEqual(contents, obj, "Did not round trip")
                else:
                    self.assertRaises(
                        swiftclient.ClientException,
                        swift.quiet_swiftclient,
                        swift_client.get_object,
                        container,
                        name,
                    )
        finally:
            swift_client.close()

    def test_swift_timeout(self):
        # The librarian's Swift connections honour the configured timeout.
        self.pushConfig(
            "librarian_server", os_username="timeout", swift_timeout=0.1
        )
        swift_client = self.swift_fixture.connect()
        self.assertRaises(
            swiftclient.ClientException,
            swift_client.get_object,
            "container",
            "name",
        )


class TestRobustSwiftClient(TestCase):
    layer = BaseLayer

    def setUp(self):
        super().setUp()
        # Retry instantly so the tests do not wait for the real delay.
        self.useFixture(
            MonkeyPatch("lp.services.librarianserver.swift.time.sleep", nop)
        )

    def test_returns_result_without_retrying(self):
        """A call that succeeds first time is made exactly once."""
        func = FakeSwiftCall([], "done")
        self.assertEqual("done", swift.robust_swiftclient(func))
        self.assertEqual(1, func.calls)

    def test_retries_transient_network_error(self):
        """A transient network failure is retried until the call succeeds."""
        error = ReadTimeoutError(None, "url", "Read timed out.")
        func = FakeSwiftCall([error, error], "done")
        self.assertEqual("done", swift.robust_swiftclient(func))
        self.assertEqual(3, func.calls)

    def test_retries_server_error(self):
        """A server-side Swift error (5xx) is retried, as it may be
        transient."""
        error = swiftclient.ClientException("boom", http_status=503)
        func = FakeSwiftCall([error], "done")
        self.assertEqual("done", swift.robust_swiftclient(func))
        self.assertEqual(2, func.calls)

    def test_gives_up_after_max_retries(self):
        """If every attempt fails, the last error is raised after the
        configured number of attempts."""
        error = ReadTimeoutError(None, "url", "Read timed out.")
        func = FakeSwiftCall([error, error, error], "done")
        self.assertRaises(ReadTimeoutError, swift.robust_swiftclient, func)
        self.assertEqual(swift.SWIFT_MAX_RETRIES, func.calls)

    def test_does_not_retry_client_error(self):
        """A client error such as a 404 is raised immediately, without
        retrying, so callers can handle it."""
        error = swiftclient.ClientException("missing", http_status=404)
        func = FakeSwiftCall([error], "done")
        self.assertRaises(
            swiftclient.ClientException, swift.robust_swiftclient, func
        )
        self.assertEqual(1, func.calls)


class TestHashStream(TestCase):
    layer = BaseLayer

    def test_read(self):
        empty_md5 = "d41d8cd98f00b204e9800998ecf8427e"
        s = swift.HashStream(io.BytesIO(b"make me a coffee"))
        self.assertEqual(s.hash.hexdigest(), empty_md5)
        data = s.read()
        self.assertEqual(data, b"make me a coffee")
        self.assertEqual(
            s.hash.hexdigest(), "17dfd3e9f99a2260552e898406c696e9"
        )

    def test_partial_read(self):
        empty_sha1 = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
        s = swift.HashStream(
            io.BytesIO(b"make me another coffee"), hash_factory=hashlib.sha1
        )
        self.assertEqual(s.hash.hexdigest(), empty_sha1)
        chunk = s.read(4)
        self.assertEqual(chunk, b"make")
        self.assertEqual(
            s.hash.hexdigest(), "5821eb27d7b71c9078000da31a5a654c97e401b9"
        )
        chunk = s.read()
        self.assertEqual(chunk, b" me another coffee")
        self.assertEqual(
            s.hash.hexdigest(), "8c826e573016ce05f3968044f82507b46fd2aa93"
        )

    def test_limited_length(self):
        base_stream = io.BytesIO(b"make me a coffee")
        s = swift.HashStream(base_stream, length=8)
        chunk = s.read(4)
        self.assertEqual(chunk, b"make")
        self.assertEqual(
            s.hash.hexdigest(), "099dafc678df7d266c25f95ccf6cde22"
        )
        chunk = s.read(8)
        self.assertEqual(chunk, b" me ")
        self.assertEqual(
            s.hash.hexdigest(), "10a0334e435b75f35b1923842bd87f81"
        )
        self.assertEqual(s.read(), b"")
        self.assertEqual(s.tell(), 8)
        self.assertEqual(base_stream.tell(), 8)

    def test_tell(self):
        s = swift.HashStream(io.BytesIO(b"hurry up with that coffee"))
        self.assertEqual(s.tell(), 0)
        s.read(4)
        self.assertEqual(s.tell(), 4)

    def test_seek(self):
        s = swift.HashStream(io.BytesIO(b"hurry up with that coffee"))
        s.seek(0)
        self.assertEqual(s.tell(), 0)
        s.seek(6)
        self.assertEqual(s.tell(), 6)
        chunk = s.read()
        self.assertEqual(chunk, b"up with that coffee")
        self.assertEqual(
            s.hash.hexdigest(), "0687b12af46824e3584530c5262fed36"
        )

        # Seek also must reset the hash.
        s.seek(2)
        chunk = s.read(3)
        self.assertEqual(chunk, b"rry")
        self.assertEqual(
            s.hash.hexdigest(), "35cd51ccd493b67542201d20b6ed7db9"
        )
