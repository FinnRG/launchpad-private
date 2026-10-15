# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Database tests for external revision status watches."""

from datetime import datetime, timedelta, timezone

from storm.store import Store
from zope.component import getUtility
from zope.interface.verify import verifyObject
from zope.security.interfaces import ForbiddenAttribute, Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.code.interfaces.revisionstatus import IRevisionStatusReportSet
from lp.code.interfaces.revisionstatuswatch import (
    IRevisionStatusWatch,
    IRevisionStatusWatchSet,
)
from lp.services.database.constants import DEFAULT
from lp.testing import (
    TestCaseWithFactory,
    admin_logged_in,
    anonymous_logged_in,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer


class TestRevisionStatusWatch(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.watch_set = removeSecurityProxy(
            getUtility(IRevisionStatusWatchSet)
        )
        self.now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)

    def makeWatch(
        self,
        report=None,
        remote_system="autopkgtest",
        remote_ref=None,
        next_check=DEFAULT,
    ):
        if report is None:
            report = self.factory.makeRevisionStatusReport()
        if remote_ref is None:
            remote_ref = {"request_id": "test-request"}
        return self.watch_set.new(
            report, remote_system, remote_ref, next_check=next_check
        )

    def reloadWatch(self, watch):
        store = Store.of(watch)
        store.flush()
        watch_id = watch.id
        store.invalidate(watch)
        return self.watch_set.getByID(watch_id)

    def test_interfaces(self):
        self.assertTrue(verifyObject(IRevisionStatusWatchSet, self.watch_set))
        self.assertTrue(verifyObject(IRevisionStatusWatch, self.makeWatch()))

    def test_defaults(self):
        watch = self.reloadWatch(self.makeWatch())
        self.assertEqual("autopkgtest", watch.remote_system)
        self.assertEqual({"request_id": "test-request"}, watch.remote_ref)
        self.assertIsNone(watch.lastchecked)
        self.assertEqual(0, watch.failure_count)
        self.assertEqual(timezone.utc, watch.date_created.tzinfo)
        self.assertEqual(watch.date_created, watch.next_check)
        self.assertEqual(
            [watch], list(self.watch_set.findDue(now=watch.next_check))
        )

    def test_persistence(self):
        report = self.factory.makeRevisionStatusReport()
        remote_ref = {
            "request_id": "receipt-123",
            "package": {"name": "example", "version": "1.0~ppa1"},
            "architectures": ["amd64", "arm64"],
            "all_proposed": False,
            "result": None,
        }
        watch = self.reloadWatch(
            self.makeWatch(
                report=report,
                remote_system="another-service",
                remote_ref=remote_ref,
                next_check=self.now,
            )
        )
        self.assertEqual(report.id, watch.report_id)
        self.assertEqual(report, watch.report)
        self.assertEqual("another-service", watch.remote_system)
        self.assertEqual(remote_ref, watch.remote_ref)
        self.assertEqual(self.now, watch.next_check)

    def test_update_polling_state(self):
        watch = self.makeWatch()
        watch.lastchecked = self.now
        watch.next_check = self.now + timedelta(minutes=10)
        watch.failure_count = 2
        watch.remote_ref = {"request_id": "receipt-123", "cursor": "next"}
        watch = self.reloadWatch(watch)
        self.assertEqual(self.now, watch.lastchecked)
        self.assertEqual(self.now + timedelta(minutes=10), watch.next_check)
        self.assertEqual(2, watch.failure_count)
        self.assertEqual(
            {"request_id": "receipt-123", "cursor": "next"}, watch.remote_ref
        )

    def test_getByID_missing(self):
        self.assertIsNone(self.watch_set.getByID(-1))

    def test_findByReport(self):
        report = self.factory.makeRevisionStatusReport()
        watch = self.makeWatch(report=report)
        self.makeWatch()
        self.assertEqual(watch, self.watch_set.findByReport(report))

    def test_findByReport_missing(self):
        report = self.factory.makeRevisionStatusReport()
        self.assertIsNone(self.watch_set.findByReport(report))

    def test_new_rejects_existing_report_watch(self):
        report = self.factory.makeRevisionStatusReport()
        watch = self.makeWatch(report=report)
        self.assertRaises(ValueError, self.makeWatch, report=report)
        self.assertEqual(watch, self.watch_set.findByReport(report))

    def test_new_rejects_another_remote_system_for_same_report(self):
        report = self.factory.makeRevisionStatusReport()
        watch = self.makeWatch(report=report, next_check=self.now)
        self.assertRaises(
            ValueError,
            self.makeWatch,
            report=report,
            remote_system="another-service",
            remote_ref={"request_id": "another-request"},
            next_check=None,
        )
        self.assertEqual(watch, self.watch_set.findByReport(report))
        self.assertEqual("autopkgtest", watch.remote_system)
        self.assertEqual({"request_id": "test-request"}, watch.remote_ref)
        self.assertEqual(self.now, watch.next_check)

    def test_findDue_time_boundary(self):
        overdue = self.makeWatch(next_check=self.now - timedelta(seconds=1))
        due = self.makeWatch(next_check=self.now)
        self.makeWatch(next_check=self.now + timedelta(seconds=1))
        self.makeWatch(next_check=None)
        self.assertEqual(
            [overdue, due], list(self.watch_set.findDue(now=self.now))
        )

    def test_findDue_current_time(self):
        overdue = self.makeWatch(
            next_check=datetime(2000, 1, 1, tzinfo=timezone.utc)
        )
        self.makeWatch(next_check=datetime(3000, 1, 1, tzinfo=timezone.utc))
        self.assertEqual([overdue], list(self.watch_set.findDue()))

    def test_findDue_remote_system(self):
        watch = self.makeWatch(next_check=self.now)
        self.makeWatch(remote_system="another-service", next_check=self.now)
        self.assertEqual(
            [watch],
            list(
                self.watch_set.findDue(
                    now=self.now, remote_system="autopkgtest"
                )
            ),
        )

    def test_findDue_order_and_limit(self):
        later = self.makeWatch(next_check=self.now)
        earlier = self.makeWatch(next_check=self.now - timedelta(minutes=1))
        tied = self.makeWatch(next_check=self.now)
        self.assertEqual(
            [earlier, later],
            list(self.watch_set.findDue(now=self.now, limit=2)),
        )
        self.assertEqual(
            [earlier, later, tied], list(self.watch_set.findDue(now=self.now))
        )

    def test_findDue_empty(self):
        self.assertEqual([], list(self.watch_set.findDue(now=self.now)))

    def test_findDue_invalid_limit(self):
        for limit in (0, -1, True, 1.5, None):
            self.assertRaises(ValueError, self.watch_set.findDue, limit=limit)

    def test_destroySelf_keeps_report(self):
        report = self.factory.makeRevisionStatusReport()
        watch = self.reloadWatch(self.makeWatch(report=report))
        watch_id = watch.id
        watch.destroySelf()
        Store.of(report).flush()
        self.assertIsNone(self.watch_set.getByID(watch_id))
        self.assertIsNone(self.watch_set.findByReport(report))
        self.assertEqual(
            report, getUtility(IRevisionStatusReportSet).getByID(report.id)
        )

    def test_deleted_watch_can_be_replaced(self):
        report = self.factory.makeRevisionStatusReport()
        watch = self.reloadWatch(self.makeWatch(report=report))
        old_id = watch.id
        watch.destroySelf()
        replacement = self.reloadWatch(self.makeWatch(report=report))
        self.assertNotEqual(old_id, replacement.id)
        self.assertEqual(replacement, self.watch_set.findByReport(report))

    def test_report_deletion_cascades(self):
        report = self.factory.makeRevisionStatusReport()
        watch = self.reloadWatch(self.makeWatch(report=report))
        other = self.reloadWatch(self.makeWatch())
        watch_id = watch.id
        other_id = other.id
        store = Store.of(report)
        store.remove(report)
        store.flush()
        store.invalidate(watch)
        self.assertIsNone(self.watch_set.getByID(watch_id))
        self.assertEqual(other, self.watch_set.getByID(other_id))


class TestRevisionStatusWatchSecurity(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.watch_set = getUtility(IRevisionStatusWatchSet)
        self.now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
        with admin_logged_in():
            self.report = self.factory.makeRevisionStatusReport()
            self.watch = self.watch_set.new(
                self.report,
                "autopkgtest",
                {"request_id": "test-request"},
                next_check=self.now,
            )
            self.watch_id = self.watch.id

    def assertWatchSetAccessDenied(self):
        for name in ("new", "getByID", "findByReport", "findDue"):
            self.assertRaises(Unauthorized, getattr, self.watch_set, name)

    def assertWatchAccessDenied(self):
        for name in IRevisionStatusWatch:
            self.assertRaises(Unauthorized, getattr, self.watch, name)

    def test_admin_can_create_find_and_read_watch(self):
        with admin_logged_in():
            self.assertEqual(self.watch_id, self.watch.id)
            self.assertEqual(self.report, self.watch.report)
            self.assertEqual("autopkgtest", self.watch.remote_system)
            self.assertEqual(
                {"request_id": "test-request"}, self.watch.remote_ref
            )
            self.assertEqual(self.now, self.watch.next_check)
            self.assertIsNone(self.watch.lastchecked)
            self.assertEqual(0, self.watch.failure_count)
            self.assertIsNotNone(self.watch.date_created)
            self.assertEqual(self.watch, self.watch_set.getByID(self.watch_id))
            self.assertEqual(
                self.watch, self.watch_set.findByReport(self.report)
            )
            self.assertEqual(
                [self.watch], list(self.watch_set.findDue(now=self.now))
            )

    def test_anonymous_cannot_access_watch_or_set(self):
        with anonymous_logged_in():
            self.assertWatchSetAccessDenied()
            self.assertWatchAccessDenied()

    def test_ordinary_user_cannot_access_watch_or_set(self):
        with person_logged_in(self.factory.makePerson()):
            self.assertWatchSetAccessDenied()
            self.assertWatchAccessDenied()

    def test_report_owner_cannot_access_watch_or_set(self):
        with person_logged_in(self.report.git_repository.owner):
            self.assertWatchSetAccessDenied()
            self.assertWatchAccessDenied()

    def test_admin_cannot_write_watch_attributes_directly(self):
        with admin_logged_in():
            for name, value in (
                ("report", self.report),
                ("remote_system", "another-service"),
                ("remote_ref", {"request_id": "replacement"}),
                ("next_check", None),
                ("lastchecked", self.now),
                ("failure_count", 1),
            ):
                self.assertRaises(
                    ForbiddenAttribute, setattr, self.watch, name, value
                )

    def test_admin_cannot_access_undeclared_watch_attributes(self):
        with admin_logged_in():
            self.assertRaises(
                ForbiddenAttribute, getattr, self.watch, "report_id"
            )

    def test_admin_can_destroy_watch_without_deleting_report(self):
        with admin_logged_in():
            self.watch.destroySelf()
            self.assertIsNone(self.watch_set.getByID(self.watch_id))
            self.assertIsNone(self.watch_set.findByReport(self.report))
            self.assertEqual(
                self.report,
                getUtility(IRevisionStatusReportSet).getByID(self.report.id),
            )
