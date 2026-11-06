# Copyright 2016-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""ArchiveFile tests."""

import os
from datetime import datetime, timedelta, timezone

import transaction
from storm.store import Store
from testtools.matchers import AfterPreprocessing, Equals, Is, MatchesStructure
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.errors import IncompatibleArguments
from lp.services.database.constants import UTC_NOW
from lp.services.database.sqlbase import (
    flush_database_caches,
    get_transaction_timestamp,
)
from lp.services.osutils import open_for_writing
from lp.soyuz.interfaces.archivefile import IArchiveFileSet
from lp.testing import TestCaseWithFactory
from lp.testing.layers import LaunchpadZopelessLayer


def read_library_file(library_file):
    library_file.open()
    try:
        return library_file.read()
    finally:
        library_file.close()


class TestArchiveFile(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def test_getIndexDates(self):
        # Only dated package indexes in the requested suites set the cutoff.
        archive = self.factory.makeArchive()
        now = get_transaction_timestamp(Store.of(archive))
        old = now - timedelta(days=2)
        recent = now - timedelta(days=1)
        for container, path, date in (
            ("release:jammy", "dists/jammy/main/source/Sources.gz", old),
            ("release:jammy", "dists/jammy/main/source/Sources.gz", recent),
            (
                "release:jammy-updates",
                "dists/jammy-updates/main/binary-amd64/Packages.xz",
                old,
            ),
            ("release:jammy-updates", "dists/jammy-updates/Contents.gz", now),
            (
                "release:jammy-updates",
                "dists/jammy-updates/main/dep11/Components-amd64.yml.gz",
                now,
            ),
            ("release:focal", "dists/focal/main/source/Sources.gz", None),
            ("other:jammy", "dists/jammy/main/source/Other.gz", now),
            ("release:empty", "dists/empty/Release", now),
            ("release:jammy", "dists/jammy/Release", now),
            ("release:jammy", "dists/jammy/Release.gpg", now),
            ("release:jammy", "dists/jammy/InRelease", now),
            ("release:jammy", "dists/jammy/main/binary-amd64/Release", now),
        ):
            archive_file = self.factory.makeArchiveFile(
                archive=archive, container=container, path=path
            )
            removeSecurityProxy(archive_file).date_created = date
            if date == old and container == "release:jammy":
                removeSecurityProxy(archive_file).date_superseded = recent
        self.factory.makeArchiveFile(
            container="release:jammy", path="dists/jammy/Contents.gz"
        )
        archive_file_set = getUtility(IArchiveFileSet)
        suites = ["jammy", "jammy-updates", "focal", "empty", "unknown"]
        self.assertEqual(
            {"jammy": recent, "jammy-updates": old},
            archive_file_set.getIndexDates(archive, UTC_NOW, suites),
        )
        self.assertEqual(
            {"jammy": old, "jammy-updates": old},
            archive_file_set.getIndexDates(archive, old, suites),
        )
        self.assertEqual(
            {},
            archive_file_set.getIndexDates(
                archive, old - timedelta(days=1), suites
            ),
        )
        self.assertEqual(
            {"jammy": recent},
            archive_file_set.getIndexDates(archive, now, ["jammy"]),
        )
        self.assertEqual({}, archive_file_set.getIndexDates(archive, now, []))

    def test_new(self):
        archive = self.factory.makeArchive()
        library_file = self.factory.makeLibraryFileAlias()
        archive_file = getUtility(IArchiveFileSet).new(
            archive, "foo", "dists/foo", library_file
        )
        self.assertThat(
            archive_file,
            MatchesStructure(
                archive=Equals(archive),
                container=Equals("foo"),
                path=Equals("dists/foo"),
                library_file=Equals(library_file),
                date_created=Equals(
                    get_transaction_timestamp(Store.of(archive_file))
                ),
                date_superseded=Is(None),
                scheduled_deletion_date=Is(None),
            ),
        )

    def test_newFromFile(self):
        root = self.makeTemporaryDirectory()
        with open_for_writing(os.path.join(root, "dists/foo"), "w") as f:
            f.write("abc\n")
        archive = self.factory.makeArchive()
        with open(os.path.join(root, "dists/foo"), "rb") as f:
            archive_file = getUtility(IArchiveFileSet).newFromFile(
                archive, "foo", "dists/foo", f, 4, "text/plain"
            )
        now = get_transaction_timestamp(Store.of(archive_file))
        transaction.commit()
        self.assertThat(
            archive_file,
            MatchesStructure(
                archive=Equals(archive),
                container=Equals("foo"),
                path=Equals("dists/foo"),
                library_file=AfterPreprocessing(
                    read_library_file, Equals(b"abc\n")
                ),
                date_created=Equals(now),
                date_superseded=Is(None),
                scheduled_deletion_date=Is(None),
            ),
        )

    def test_getByArchive(self):
        archives = [self.factory.makeArchive(), self.factory.makeArchive()]
        archive_files = []
        for archive in archives:
            archive_files.append(self.factory.makeArchiveFile(archive=archive))
            archive_files.append(
                self.factory.makeArchiveFile(archive=archive, container="foo")
            )
        archive_file_set = getUtility(IArchiveFileSet)
        self.assertContentEqual(
            archive_files[:2], archive_file_set.getByArchive(archives[0])
        )
        self.assertContentEqual(
            [archive_files[1]],
            archive_file_set.getByArchive(archives[0], container="foo"),
        )
        self.assertContentEqual(
            [], archive_file_set.getByArchive(archives[0], container="bar")
        )
        self.assertContentEqual(
            [archive_files[1]],
            archive_file_set.getByArchive(
                archives[0], path=archive_files[1].path
            ),
        )
        self.assertContentEqual(
            [], archive_file_set.getByArchive(archives[0], path="other")
        )
        self.assertContentEqual(
            archive_files[2:], archive_file_set.getByArchive(archives[1])
        )
        self.assertContentEqual(
            [archive_files[3]],
            archive_file_set.getByArchive(archives[1], container="foo"),
        )
        self.assertContentEqual(
            [], archive_file_set.getByArchive(archives[1], container="bar")
        )
        self.assertContentEqual(
            [archive_files[3]],
            archive_file_set.getByArchive(
                archives[1], path=archive_files[3].path
            ),
        )
        self.assertContentEqual(
            [], archive_file_set.getByArchive(archives[1], path="other")
        )
        self.assertContentEqual(
            [archive_files[0]],
            archive_file_set.getByArchive(
                archives[0],
                sha256=archive_files[0].library_file.content.sha256,
            ),
        )
        self.assertContentEqual(
            [], archive_file_set.getByArchive(archives[0], sha256="nonsense")
        )

    def test_getByArchive_path_parent(self):
        archive = self.factory.makeArchive()
        archive_files = [
            self.factory.makeArchiveFile(archive=archive, path=path)
            for path in (
                "dists/jammy/InRelease",
                "dists/jammy/Release",
                "dists/jammy/main/binary-amd64/Release",
            )
        ]
        archive_file_set = getUtility(IArchiveFileSet)
        self.assertContentEqual(
            archive_files[:2],
            archive_file_set.getByArchive(archive, path_parent="dists/jammy"),
        )
        self.assertContentEqual(
            [archive_files[2]],
            archive_file_set.getByArchive(
                archive, path_parent="dists/jammy/main/binary-amd64"
            ),
        )
        self.assertContentEqual(
            [],
            archive_file_set.getByArchive(archive, path_parent="dists/xenial"),
        )

    def test_getByArchive_both_live_at_and_existed_at(self):
        now = datetime.now(timezone.utc)
        archive = self.factory.makeArchive()
        self.assertRaisesWithContent(
            IncompatibleArguments,
            "You cannot specify both 'live_at' and 'existed_at'.",
            getUtility(IArchiveFileSet).getByArchive,
            archive,
            live_at=now,
            existed_at=now,
        )

    def test_getByArchive_live_at(self):
        archive = self.factory.makeArchive()
        now = get_transaction_timestamp(Store.of(archive))
        archive_file_1 = self.factory.makeArchiveFile(
            archive=archive, path="dists/jammy/InRelease"
        )
        naked_archive_file_1 = removeSecurityProxy(archive_file_1)
        naked_archive_file_1.date_created = now - timedelta(days=3)
        naked_archive_file_1.date_superseded = now - timedelta(days=1)
        archive_file_2 = self.factory.makeArchiveFile(
            archive=archive, path="dists/jammy/InRelease"
        )
        naked_archive_file_2 = removeSecurityProxy(archive_file_2)
        naked_archive_file_2.date_created = now - timedelta(days=1)
        archive_file_set = getUtility(IArchiveFileSet)
        for days, expected_file in (
            (4, None),
            (3, archive_file_1),
            (2, archive_file_1),
            (1, archive_file_2),
            (0, archive_file_2),
        ):
            self.assertEqual(
                expected_file,
                archive_file_set.getByArchive(
                    archive,
                    path="dists/jammy/InRelease",
                    live_at=now - timedelta(days=days) if days else UTC_NOW,
                ).one(),
            )

    def test_getByArchive_live_at_without_date_created(self):
        archive = self.factory.makeArchive()
        now = get_transaction_timestamp(Store.of(archive))
        archive_file = self.factory.makeArchiveFile(
            archive=archive, path="dists/jammy/InRelease"
        )
        naked_archive_file = removeSecurityProxy(archive_file)
        naked_archive_file.date_created = None
        naked_archive_file.date_superseded = now
        archive_file_set = getUtility(IArchiveFileSet)
        for days, expected_file in ((1, archive_file), (0, None)):
            self.assertEqual(
                expected_file,
                archive_file_set.getByArchive(
                    archive,
                    path="dists/jammy/InRelease",
                    live_at=now - timedelta(days=days) if days else UTC_NOW,
                ).one(),
            )

    def test_getByArchive_existed_at(self):
        archive = self.factory.makeArchive()
        now = get_transaction_timestamp(Store.of(archive))
        archive_file_1 = self.factory.makeArchiveFile(
            archive=archive, path="dists/jammy/InRelease"
        )
        naked_archive_file_1 = removeSecurityProxy(archive_file_1)
        naked_archive_file_1.date_created = now - timedelta(days=3)
        naked_archive_file_1.date_superseded = now - timedelta(days=2)
        naked_archive_file_1.date_removed = now - timedelta(days=1)
        archive_file_2 = self.factory.makeArchiveFile(
            archive=archive, path="dists/jammy/InRelease"
        )
        naked_archive_file_2 = removeSecurityProxy(archive_file_2)
        naked_archive_file_2.date_created = now - timedelta(days=2)
        archive_file_set = getUtility(IArchiveFileSet)
        for days, existed in ((4, False), (3, True), (2, True), (1, False)):
            self.assertEqual(
                archive_file_1 if existed else None,
                archive_file_set.getByArchive(
                    archive,
                    path="dists/jammy/InRelease",
                    sha256=archive_file_1.library_file.content.sha256,
                    existed_at=now - timedelta(days=days),
                ).one(),
            )
        for days, existed in ((3, False), (2, True), (1, True), (0, True)):
            self.assertEqual(
                archive_file_2 if existed else None,
                archive_file_set.getByArchive(
                    archive,
                    path="dists/jammy/InRelease",
                    sha256=archive_file_2.library_file.content.sha256,
                    existed_at=now - timedelta(days=days) if days else UTC_NOW,
                ).one(),
            )

    def test_getByArchive_existed_at_without_date_created(self):
        archive = self.factory.makeArchive()
        now = get_transaction_timestamp(Store.of(archive))
        archive_file = self.factory.makeArchiveFile(
            archive=archive, path="dists/jammy/InRelease"
        )
        naked_archive_file = removeSecurityProxy(archive_file)
        naked_archive_file.date_created = None
        naked_archive_file.date_removed = now
        archive_file_set = getUtility(IArchiveFileSet)
        for days, expected_file in ((1, archive_file), (0, None)):
            self.assertEqual(
                expected_file,
                archive_file_set.getByArchive(
                    archive,
                    path="dists/jammy/InRelease",
                    existed_at=now - timedelta(days=days) if days else UTC_NOW,
                ).one(),
            )

    def test_scheduleDeletion(self):
        archive_files = [self.factory.makeArchiveFile() for _ in range(3)]
        getUtility(IArchiveFileSet).scheduleDeletion(
            archive_files[:2], timedelta(days=1)
        )
        flush_database_caches()
        now = get_transaction_timestamp(Store.of(archive_files[0]))
        tomorrow = now + timedelta(days=1)
        self.assertEqual(now, archive_files[0].date_superseded)
        self.assertEqual(tomorrow, archive_files[0].scheduled_deletion_date)
        self.assertEqual(now, archive_files[1].date_superseded)
        self.assertEqual(tomorrow, archive_files[1].scheduled_deletion_date)
        self.assertIsNone(archive_files[2].date_superseded)
        self.assertIsNone(archive_files[2].scheduled_deletion_date)

    def test_getContainersToReap(self):
        archive = self.factory.makeArchive()
        archive_files = []
        for container in ("release:foo", "other:bar", "baz"):
            for _ in range(2):
                archive_files.append(
                    self.factory.makeArchiveFile(
                        archive=archive, container=container
                    )
                )
        other_archive = self.factory.makeArchive()
        archive_files.append(
            self.factory.makeArchiveFile(
                archive=other_archive, container="baz"
            )
        )
        now = get_transaction_timestamp(Store.of(archive_files[0]))
        removeSecurityProxy(archive_files[0]).scheduled_deletion_date = (
            now - timedelta(days=1)
        )
        removeSecurityProxy(archive_files[1]).scheduled_deletion_date = (
            now - timedelta(days=1)
        )
        removeSecurityProxy(archive_files[2]).scheduled_deletion_date = (
            now + timedelta(days=1)
        )
        removeSecurityProxy(archive_files[6]).scheduled_deletion_date = (
            now - timedelta(days=1)
        )
        archive_file_set = getUtility(IArchiveFileSet)
        self.assertContentEqual(
            ["release:foo"], archive_file_set.getContainersToReap(archive)
        )
        self.assertContentEqual(
            ["baz"], archive_file_set.getContainersToReap(other_archive)
        )
        removeSecurityProxy(archive_files[3]).scheduled_deletion_date = (
            now - timedelta(days=1)
        )
        self.assertContentEqual(
            ["release:foo", "other:bar"],
            archive_file_set.getContainersToReap(archive),
        )
        self.assertContentEqual(
            ["release:foo"],
            archive_file_set.getContainersToReap(
                archive, container_prefix="release:"
            ),
        )
        archive_file_set.markDeleted([archive_files[3]])
        self.assertContentEqual(
            ["release:foo"], archive_file_set.getContainersToReap(archive)
        )

    def test_markDeleted(self):
        archive = self.factory.makeArchive()
        archive_files = [
            self.factory.makeArchiveFile(archive=archive) for _ in range(4)
        ]
        archive_file_set = getUtility(IArchiveFileSet)
        archive_file_set.markDeleted(archive_files[:2])
        flush_database_caches()
        self.assertIsNotNone(archive_files[0].date_removed)
        self.assertIsNotNone(archive_files[1].date_removed)
        self.assertIsNone(archive_files[2].date_removed)
        self.assertIsNone(archive_files[3].date_removed)
        self.assertContentEqual(
            archive_files[2:],
            archive_file_set.getByArchive(archive, only_published=True),
        )
