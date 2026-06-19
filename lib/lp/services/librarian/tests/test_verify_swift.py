# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the librarian Swift verification script."""

import os

import transaction
from swiftclient import client as swiftclient
from zope.security.proxy import removeSecurityProxy

from lp.services.features.testing import FeatureFixture
from lp.services.librarian.scripts.verify_swift import VerifyLibrarianSwift
from lp.services.librarianserver import swift
from lp.services.log.logger import BufferLogger
from lp.testing import TestCaseWithFactory
from lp.testing.dbuser import switch_dbuser
from lp.testing.layers import LaunchpadZopelessLayer
from lp.testing.swift.fixture import SwiftFixture


class TestVerifyLibrarianSwift(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.swift_fixture = self.useFixture(SwiftFixture())
        self.useFixture(FeatureFixture({"librarian.swift.enabled": True}))
        transaction.commit()
        self.checkpoint_file = os.path.join(
            self.makeTemporaryDirectory(), "checkpoint"
        )

    def _runScript(self, *args):
        switch_dbuser("testadmin")
        # Default to a zero grace period so freshly created files are
        # verified; individual tests may override it by passing their own.
        script = VerifyLibrarianSwift(
            "test-verify-swift",
            test_args=[
                "--grace-period-days",
                "0",
                "--checkpoint-file",
                self.checkpoint_file,
            ]
            + list(args),
        )
        script.logger = BufferLogger()
        script.txn = transaction
        script.main()
        switch_dbuser("testadmin")
        return script.logger.getLogBuffer()

    def _makeContent(self, data=b"some librarian content"):
        """Create a LibraryFileContent row and return its id."""
        switch_dbuser("testadmin")
        lfa = self.factory.makeLibraryFileAlias(content=data)
        lfc_id = removeSecurityProxy(lfa).content_id
        transaction.commit()
        return lfc_id

    def _putInSwift(self, lfc_id, data=b"some librarian content"):
        """Place an object for lfc_id into the mock Swift."""
        container, name = swift.swift_location(lfc_id)
        swift_client = self.swift_fixture.connect()
        try:
            try:
                swift_client.head_container(container)
            except swiftclient.ClientException as x:
                if x.http_status != 404:
                    raise
                swift_client.put_container(container)
            swift_client.put_object(container, name, data, len(data))
        finally:
            swift_client.close()

    def test_verifies_present_file(self):
        data = b"present file content"
        lfc_id = self._makeContent(data)
        self._putInSwift(lfc_id, data)

        log = self._runScript("--start-id", str(lfc_id))

        self.assertIn("verified=1", log)
        self.assertNotIn("MISSING", log)

    def test_reports_missing_file(self):
        # The content exists in the database but was never put into Swift.
        lfc_id = self._makeContent()

        log = self._runScript("--start-id", str(lfc_id))

        self.assertIn("MISSING: LibraryFileContent %d" % lfc_id, log)
        self.assertIn("missing=1", log)

    def test_treats_pruned_file_as_not_missing(self):
        # A file that is absent from Swift but whose database row has been
        # pruned (e.g. by librarian-gc) is not reported as missing.
        lfc_id = self._makeContent()

        switch_dbuser("testadmin")
        script = VerifyLibrarianSwift(
            "test-verify-swift",
            test_args=[
                "--grace-period-days",
                "0",
                "--checkpoint-file",
                self.checkpoint_file,
                "--start-id",
                str(lfc_id),
            ],
        )
        script.logger = BufferLogger()
        script.txn = transaction
        # Simulate the row having been pruned from the database between the
        # batch read and the missing-file recheck.
        script._still_in_db = lambda content_id: False
        script.main()
        log = script.logger.getLogBuffer()

        self.assertNotIn("MISSING", log)
        self.assertIn("pruned=1", log)

    def test_skips_recently_created_files(self):
        data = b"recent content"
        lfc_id = self._makeContent(data)
        self._putInSwift(lfc_id, data)

        log = self._runScript(
            "--grace-period-days", "1", "--start-id", str(lfc_id)
        )

        self.assertIn("skipped_recent=1", log)
        self.assertIn("verified=0", log)

    def test_resumes_from_checkpoint(self):
        # A file missing from Swift but below the checkpoint is not checked.
        missing_id = self._makeContent(b"below checkpoint")
        present_data = b"above checkpoint"
        present_id = self._makeContent(present_data)
        self._putInSwift(present_id, present_data)

        with open(self.checkpoint_file, "w") as f:
            f.write("%d\n" % missing_id)

        log = self._runScript()

        self.assertNotIn("MISSING", log)
        self.assertIn("verified=1", log)
        self.assertFalse(os.path.exists(self.checkpoint_file))

    def test_does_nothing_when_swift_disabled(self):
        self.useFixture(FeatureFixture({"librarian.swift.enabled": ""}))
        lfc_id = self._makeContent()

        log = self._runScript("--start-id", str(lfc_id))

        self.assertIn("not set; nothing to verify", log)
        self.assertNotIn("MISSING", log)
