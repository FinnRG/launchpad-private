# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for lp.bugs.model.Bug."""

import json
from datetime import timedelta

from lazr.lifecycle.snapshot import Snapshot
from storm.exceptions import NoneError
from testtools.matchers import MatchesStructure
from testtools.testcase import ExpectedException
from zope.component import getUtility
from zope.interface import providedBy
from zope.security.management import checkPermission
from zope.security.proxy import removeSecurityProxy

from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.bugs.enums import BugLockStatus, BugNotificationLevel
from lp.bugs.interfaces.bug import CreateBugParams, IBugSet
from lp.bugs.interfaces.bugtask import (
    BugTaskImportance,
    BugTaskStatus,
    IllegalTarget,
    UserCannotEditBugTaskAssignee,
    UserCannotEditBugTaskImportance,
    UserCannotEditBugTaskMilestone,
)
from lp.bugs.model.bug import CannotLockBug, CannotSetLockReason
from lp.registry.tests.test_person import KarmaTestMixin
from lp.services.webapp.interfaces import OAuthPermission
from lp.soyuz.enums import PackagePublishingStatus
from lp.testing import (
    ANONYMOUS,
    StormStatementRecorder,
    TestCaseWithFactory,
    admin_logged_in,
    api_url,
    celebrity_logged_in,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.pages import webservice_for_person


class TestBug(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_default_bugtask(self):
        product = self.factory.makeProduct()
        bug = self.factory.makeBug(target=product)
        first_task = bug.default_bugtask
        other_task = self.factory.makeBugTask(
            bug=bug, target=self.factory.makeProduct()
        )
        self.assertEqual(first_task, bug.default_bugtask)
        # default_bugtask avoids an inactive product if possible.
        with admin_logged_in():
            first_task.target.active = False
        self.assertEqual(other_task, bug.default_bugtask)
        # But it'll use the first inactive one if it has to.
        with admin_logged_in():
            other_task.target.active = False
        self.assertEqual(first_task, bug.default_bugtask)
        # An active distro task wins over an inactive product.
        distro_task = self.factory.makeBugTask(
            bug=bug, target=self.factory.makeDistribution()
        )
        self.assertEqual(distro_task, bug.default_bugtask)


class TestBugSubscriptionMethods(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.bug = self.factory.makeBug()
        self.person = self.factory.makePerson()

    def test_is_muted_returns_true_for_muted_users(self):
        # Bug.isMuted() will return True if the person passed to it is muted.
        with person_logged_in(self.person):
            self.bug.mute(self.person, self.person)
            self.assertEqual(True, self.bug.isMuted(self.person))

    def test_is_muted_returns_false_for_direct_subscribers(self):
        # Bug.isMuted() will return False if the user has a
        # regular subscription.
        with person_logged_in(self.person):
            self.bug.subscribe(
                self.person, self.person, level=BugNotificationLevel.METADATA
            )
            self.assertEqual(False, self.bug.isMuted(self.person))

    def test_is_muted_returns_false_for_non_subscribers(self):
        # Bug.isMuted() will return False if the user has no
        # subscription.
        with person_logged_in(self.person):
            self.assertEqual(False, self.bug.isMuted(self.person))

    def test_mute_team_fails(self):
        # Muting a subscription for an entire team doesn't work.
        with person_logged_in(self.person):
            team = self.factory.makeTeam(owner=self.person)
            self.assertRaises(AssertionError, self.bug.mute, team, team)

    def test_mute_mutes_user(self):
        # Bug.mute() adds a BugMute record for the person passed to it.
        with person_logged_in(self.person):
            self.bug.mute(self.person, self.person)
            naked_bug = removeSecurityProxy(self.bug)
            bug_mute = naked_bug._getMutes(self.person).one()
            self.assertEqual(self.bug, bug_mute.bug)
            self.assertEqual(self.person, bug_mute.person)

    def test_mute_mutes_muter(self):
        # When exposed in the web API, the mute method regards the
        # first, `person` argument as optional, and the second
        # `muted_by` argument is supplied from the request.  In this
        # case, the person should be the muter.
        with person_logged_in(self.person):
            self.bug.mute(None, self.person)
            self.assertTrue(self.bug.isMuted(self.person))

    def test_mute_mutes_user_with_existing_subscription(self):
        # Bug.mute() will not touch the existing subscription.
        with person_logged_in(self.person):
            subscription = self.bug.subscribe(
                self.person, self.person, level=BugNotificationLevel.METADATA
            )
            self.bug.mute(self.person, self.person)
            self.assertTrue(self.bug.isMuted(self.person))
            self.assertEqual(
                BugNotificationLevel.METADATA,
                subscription.bug_notification_level,
            )

    def test_unmute_unmutes_user(self):
        # Bug.unmute() will remove a muted subscription for the user
        # passed to it.
        with person_logged_in(self.person):
            self.bug.mute(self.person, self.person)
            self.assertTrue(self.bug.isMuted(self.person))
            self.bug.unmute(self.person, self.person)
            self.assertFalse(self.bug.isMuted(self.person))

    def test_unmute_returns_direct_subscription(self):
        # Bug.unmute() returns the previously muted direct subscription, if
        # any.
        with person_logged_in(self.person):
            self.bug.mute(self.person, self.person)
            self.assertEqual(True, self.bug.isMuted(self.person))
            self.assertEqual(None, self.bug.unmute(self.person, self.person))
            self.assertEqual(False, self.bug.isMuted(self.person))
            subscription = self.bug.subscribe(
                self.person, self.person, level=BugNotificationLevel.METADATA
            )
            self.bug.mute(self.person, self.person)
            self.assertEqual(True, self.bug.isMuted(self.person))
            self.assertEqual(
                subscription, self.bug.unmute(self.person, self.person)
            )

    def test_unmute_mutes_unmuter(self):
        # When exposed in the web API, the unmute method regards the
        # first, `person` argument as optional, and the second
        # `unmuted_by` argument is supplied from the request.  In this
        # case, the person should be the muter.
        with person_logged_in(self.person):
            self.bug.mute(self.person, self.person)
            self.bug.unmute(None, self.person)
            self.assertFalse(self.bug.isMuted(self.person))

    def test_double_unmute(self):
        # If unmute is called when not muted, it is a no-op.
        with person_logged_in(self.person):
            self.bug.mute(self.person, self.person)
            subscriptions = self.bug.unmute(self.person, self.person)
            sec_subscriptions = self.bug.unmute(self.person, self.person)
            self.assertEqual(sec_subscriptions, subscriptions)


class TestBugSnapshotting(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.bug = self.factory.makeBug()
        self.person = self.factory.makePerson()

    def test_bug_snapshot_does_not_include_messages(self):
        # A snapshot of a bug does not include its messages or
        # attachments (which get the messages from the database).  If it
        # does, the webservice can become unusable if changes are made
        # to bugs with many comments, such as bug 1. See, for instance,
        # bug 744888.  This test is primarily to keep the problem from
        # slipping in again.  To do so, we resort to somewhat
        # extraordinary measures.  In addition to verifying that the
        # snapshot does not have the attributes that currently trigger
        # the problem, we also actually look at the SQL that is
        # generated by creating the snapshot.  With this, we can verify
        # that the Message table is not included.  This is ugly, but
        # this has a chance of fighting against future eager loading
        # optimizations that might trigger the problem again.
        with person_logged_in(self.person):
            with StormStatementRecorder() as recorder:
                Snapshot(self.bug, providing=providedBy(self.bug))
            sql_statements = recorder.statements
        # This uses "self" as a marker to show that the attribute does not
        # exist.  We do not use hasattr because it eats exceptions.
        # self.assertTrue(getattr(snapshot, 'messages', self) is self)
        # self.assertTrue(getattr(snapshot, 'attachments', self) is self)
        for sql in sql_statements:
            # We are going to be aggressive about looking for the problem in
            # the SQL.  We'll split the SQL up by whitespace, and then look
            # for strings that start with "message".  If that is too
            # aggressive in the future from some reason, please do adjust the
            # test appropriately.
            sql_tokens = sql.lower().split()
            self.assertEqual(
                [token for token in sql_tokens if token.startswith("message")],
                [],
            )
            self.assertEqual(
                [
                    token
                    for token in sql_tokens
                    if token.startswith("bugactivity")
                ],
                [],
            )


class TestBugCreation(TestCaseWithFactory):
    """Tests for bug creation."""

    layer = DatabaseFunctionalLayer

    def createBug(
        self, owner=None, title="A bug", comment="Nothing important.", **kwargs
    ):
        with person_logged_in(owner):
            params = CreateBugParams(
                owner=owner, title=title, comment=comment, **kwargs
            )
            bug = getUtility(IBugSet).createBug(params)
        return bug

    def test_CreateBugParams_accepts_target(self):
        # The initial bug task's target can be set using
        # CreateBugParams.
        owner = self.factory.makePerson()
        target = self.factory.makeProduct(owner=owner)
        bug = self.createBug(owner=owner, target=target)
        self.assertEqual(bug.default_bugtask.target, target)

    def test_CreateBugParams_rejects_series_target(self):
        # createBug refuses attempts to create a bug with a series
        # target. A non-series task must be created first.
        owner = self.factory.makePerson()
        target = self.factory.makeProductSeries(owner=owner)
        self.assertRaises(
            IllegalTarget, self.createBug, owner=owner, target=target
        )

    def test_CreateBugParams_accepts_importance(self):
        # The importance of the initial bug task can be set using
        # CreateBugParams
        owner = self.factory.makePerson()
        target = self.factory.makeProduct(owner=owner)
        bug = self.createBug(
            owner=owner, target=target, importance=BugTaskImportance.HIGH
        )
        self.assertEqual(
            BugTaskImportance.HIGH, bug.default_bugtask.importance
        )

    def test_CreateBugParams_accepts_assignee(self):
        # The assignee of the initial bug task can be set using
        # CreateBugParams
        owner = self.factory.makePerson()
        target = self.factory.makeProduct(owner=owner)
        bug = self.createBug(owner=owner, target=target, assignee=owner)
        self.assertEqual(owner, bug.default_bugtask.assignee)

    def test_CreateBugParams_accepts_milestone(self):
        # The milestone of the initial bug task can be set using
        # CreateBugParams
        owner = self.factory.makePerson()
        target = self.factory.makeProduct(owner=owner)
        milestone = self.factory.makeMilestone(product=target)
        bug = self.createBug(owner=owner, target=target, milestone=milestone)
        self.assertEqual(milestone, bug.default_bugtask.milestone)

    def test_CreateBugParams_accepts_status(self):
        # The status of the initial bug task can be set using
        # CreateBugParams.
        owner = self.factory.makePerson()
        target = self.factory.makeProduct(owner=owner)
        bug = self.createBug(
            owner=owner, target=target, status=BugTaskStatus.TRIAGED
        )
        bugtask = bug.default_bugtask
        self.assertEqual(BugTaskStatus.TRIAGED, bugtask.status)
        self.assertEqual(bugtask.date_triaged, bugtask.datecreated)

    def test_CreateBugParams_rejects_not_allowed_importance_changes(self):
        # createBug() will reject any importance value passed by users
        # who don't have the right to set the importance.
        person = self.factory.makePerson()
        target = self.factory.makeProduct()
        self.assertRaises(
            UserCannotEditBugTaskImportance,
            self.createBug,
            owner=person,
            target=target,
            importance=BugTaskImportance.HIGH,
        )

    def test_CreateBugParams_rejects_not_allowed_assignee_changes(self):
        # createBug() will reject any importance value passed by users
        # who don't have the right to set the assignee.
        person = self.factory.makePerson()
        person_2 = self.factory.makePerson()
        target = self.factory.makeProduct()
        # Setting the target's bug supervisor means that
        # canTransitionToAssignee() will return False for `person` if
        # another Person is passed as `assignee`.
        with person_logged_in(target.owner):
            target.bug_supervisor = target.owner
        self.assertRaises(
            UserCannotEditBugTaskAssignee,
            self.createBug,
            owner=person,
            target=target,
            assignee=person_2,
        )

    def test_CreateBugParams_accepts_validate_asignee_changes(self):
        # createBug() will accept any assignee change if validate_assignee
        # equals false
        person = self.factory.makePerson()
        person_2 = self.factory.makePerson()
        target = self.factory.makeProduct()
        # Setting the target's bug supervisor means that
        # canTransitionToAssignee() will return False for `person` if
        # another Person is passed as `assignee`.
        with person_logged_in(target.owner):
            target.bug_supervisor = target.owner

        bug = self.createBug(
            owner=person,
            target=target,
            assignee=person_2,
            validate_assignee=False,
        )
        self.assertEqual(person_2, bug.default_bugtask.assignee)

    def test_CreateBugParams_rejects_not_allowed_milestone_changes(self):
        # createBug() will reject any importance value passed by users
        # who don't have the right to set the milestone.
        person = self.factory.makePerson()
        target = self.factory.makeProduct()
        self.assertRaises(
            UserCannotEditBugTaskMilestone,
            self.createBug,
            owner=person,
            target=target,
            milestone=self.factory.makeMilestone(product=target),
        )

    def test_createBug_cve(self):
        cve = self.factory.makeCVE("1999-1717")
        target = self.factory.makeProduct()
        person = self.factory.makePerson()
        bug = self.createBug(owner=person, target=target, cve=cve)
        self.assertContentEqual([cve], bug.cves)

    def test_createBug_subscribers(self):
        # Bugs normally start with just the reporter subscribed.
        person = self.factory.makePerson()
        target = self.factory.makeProduct()
        bug = self.createBug(owner=person, target=target)
        self.assertContentEqual([person], bug.getDirectSubscribers())

    def test_CreateBugParams_include_eol_dist_package_allows_eol_target(
        self,
    ):
        # createBug() accepts a package that is only published with
        # OBSOLETE status when include_eol_dist_package=True.
        ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        series = self.factory.makeDistroSeries(distribution=ubuntu)
        eol_pkg = self.factory.makeDistributionSourcePackage(
            distribution=ubuntu
        )
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=series,
            sourcepackagename=eol_pkg.sourcepackagename,
            archive=ubuntu.main_archive,
            status=PackagePublishingStatus.OBSOLETE,
        )
        owner = self.factory.makePerson()
        bug = self.createBug(
            owner=owner,
            target=eol_pkg,
            include_eol_dist_package=True,
        )
        self.assertEqual(eol_pkg, bug.default_bugtask.target)

    def test_CreateBugParams_default_rejects_eol_only_target(self):
        # createBug() raises IllegalTarget for a package whose only
        # publication is OBSOLETE when include_eol_dist_package is
        # not set (defaults to False).
        ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        series = self.factory.makeDistroSeries(distribution=ubuntu)
        eol_pkg = self.factory.makeDistributionSourcePackage(
            distribution=ubuntu
        )
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=series,
            sourcepackagename=eol_pkg.sourcepackagename,
            archive=ubuntu.main_archive,
            status=PackagePublishingStatus.OBSOLETE,
        )
        owner = self.factory.makePerson()
        self.assertRaises(
            IllegalTarget,
            self.createBug,
            owner=owner,
            target=eol_pkg,
        )


class TestBugPermissions(TestCaseWithFactory, KarmaTestMixin):
    """Test bug permissions."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.pushConfig(
            "launchpad", min_legitimate_karma=5, min_legitimate_account_age=5
        )
        self.bug = self.factory.makeBug()

    def test_unauthenticated_user_cannot_edit(self):
        self.assertFalse(checkPermission("launchpad.Edit", self.bug))

    def test_new_user_cannot_edit(self):
        with person_logged_in(self.factory.makePerson()):
            self.assertFalse(checkPermission("launchpad.Edit", self.bug))

    def test_private_bug_subscriber_can_edit(self):
        person = self.factory.makePerson()
        with admin_logged_in() as admin:
            self.bug.setPrivate(True, admin)
            self.bug.subscribe(person, admin)
        with person_logged_in(person):
            self.assertTrue(checkPermission("launchpad.Edit", self.bug))

    def test_private_bug_non_subscriber_cannot_edit(self):
        with admin_logged_in() as admin:
            self.bug.setPrivate(True, admin)
        with person_logged_in(self.factory.makePerson()):
            self.assertFalse(checkPermission("launchpad.Edit", self.bug))

    def test_user_with_karma_can_edit(self):
        person = self.factory.makePerson()
        self._makeKarmaTotalCache(person, 10)
        with person_logged_in(person):
            self.assertTrue(checkPermission("launchpad.Edit", self.bug))

    def test_user_with_account_age_can_edit(self):
        person = self.factory.makePerson()
        naked_account = removeSecurityProxy(person.account)
        naked_account.date_created = naked_account.date_created - timedelta(
            days=10
        )
        with person_logged_in(person):
            self.assertTrue(checkPermission("launchpad.Edit", self.bug))

    def test_bug_reporter_can_edit(self):
        with person_logged_in(self.bug.owner):
            self.assertTrue(checkPermission("launchpad.Edit", self.bug))

    def test_admin_can_edit(self):
        with admin_logged_in():
            self.assertTrue(checkPermission("launchpad.Edit", self.bug))

    def test_commercial_admin_can_edit(self):
        with celebrity_logged_in("commercial_admin"):
            self.assertTrue(checkPermission("launchpad.Edit", self.bug))

    def test_registry_expert_can_edit(self):
        with celebrity_logged_in("registry_experts"):
            self.assertTrue(checkPermission("launchpad.Edit", self.bug))

    def test_target_owner_can_edit(self):
        with person_logged_in(self.bug.default_bugtask.target.owner):
            self.assertTrue(checkPermission("launchpad.Edit", self.bug))

    def test_target_driver_can_edit(self):
        person = self.factory.makePerson()
        removeSecurityProxy(self.bug.default_bugtask.target).driver = person
        with person_logged_in(person):
            self.assertTrue(checkPermission("launchpad.Edit", self.bug))

    def test_target_bug_supervisor_can_edit(self):
        person = self.factory.makePerson()
        removeSecurityProxy(self.bug.default_bugtask.target).bug_supervisor = (
            person
        )
        with person_logged_in(person):
            self.assertTrue(checkPermission("launchpad.Edit", self.bug))


class TestBugLocking(TestCaseWithFactory):
    """
    Tests for the bug locking functionality.
    """

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()
        self.target = self.factory.makeProduct()

    def test_bug_lock_status_lock_reason_default_values(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        self.assertEqual(BugLockStatus.UNLOCKED, bug.lock_status)
        self.assertIsNone(bug.lock_reason)

    def test_bug_lock_status_cannot_be_none(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        self.assertEqual(BugLockStatus.UNLOCKED, bug.lock_status)
        with ExpectedException(NoneError):
            removeSecurityProxy(bug).lock_status = None

    def test_bug_locking_when_bug_already_locked(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        with person_logged_in(self.target.owner):
            bug.lock(who=self.target.owner, status=BugLockStatus.COMMENT_ONLY)
            self.assertEqual(BugLockStatus.COMMENT_ONLY, bug.lock_status)
            self.assertRaises(
                CannotLockBug,
                bug.lock,
                who=self.target.owner,
                status=BugLockStatus.COMMENT_ONLY,
            )
            self.assertEqual(BugLockStatus.COMMENT_ONLY, bug.lock_status)

    def test_bug_locking_with_a_reason_works(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        with person_logged_in(self.target.owner):
            bug.lock(
                who=self.person,
                status=BugLockStatus.COMMENT_ONLY,
                reason="too hot",
            )
            self.assertEqual("too hot", bug.lock_reason)

    def test_updating_bug_lock_reason_not_set_before(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        with person_logged_in(self.target.owner):
            bug.lock(who=self.person, status=BugLockStatus.COMMENT_ONLY)
            self.assertIsNone(bug.lock_reason)
            bug.setLockReason("too hot", who=self.target.owner)
            self.assertEqual("too hot", bug.lock_reason)

    def test_updating_existing_bug_lock_reason_to_none(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        with person_logged_in(self.target.owner):
            bug.lock(
                who=self.person,
                status=BugLockStatus.COMMENT_ONLY,
                reason="too hot",
            )
            self.assertEqual("too hot", bug.lock_reason)
            bug.setLockReason(None, who=self.target.owner)
            self.assertIsNone(bug.lock_reason)

    def test_bug_unlocking_clears_the_reason(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        with person_logged_in(self.target.owner):
            bug.lock(
                who=self.person,
                status=BugLockStatus.COMMENT_ONLY,
                reason="too hot",
            )
            bug.unlock(who=self.person)
            self.assertIsNone(bug.lock_reason)

    def test_bug_locking_unlocking_adds_bug_activity_entries(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        self.assertEqual(1, bug.activity.count())
        with person_logged_in(self.target.owner):
            bug.lock(
                who=self.target.owner,
                status=BugLockStatus.COMMENT_ONLY,
                reason="too hot",
            )
            self.assertEqual(2, bug.activity.count())
            self.assertThat(
                bug.activity[1],
                MatchesStructure.byEquality(
                    person=self.target.owner,
                    whatchanged="lock status",
                    oldvalue=str(BugLockStatus.UNLOCKED),
                    newvalue=str(BugLockStatus.COMMENT_ONLY),
                ),
            )
            bug.unlock(who=self.target.owner)
            self.assertEqual(3, bug.activity.count())
            self.assertThat(
                bug.activity[2],
                MatchesStructure.byEquality(
                    person=self.target.owner,
                    whatchanged="lock status",
                    oldvalue=str(BugLockStatus.COMMENT_ONLY),
                    newvalue=str(BugLockStatus.UNLOCKED),
                ),
            )

    def test_cannot_set_lock_reason_for_an_unlocked_bug(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        with person_logged_in(self.target.owner):
            self.assertRaises(
                CannotSetLockReason,
                bug.setLockReason,
                "too hot",
                who=self.target.owner,
            )

    def test_edit_permission_restrictions_when_a_bug_is_locked(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        another_person = self.factory.makePerson()

        with person_logged_in(self.target.owner):
            bug.lock(who=self.target.owner, status=BugLockStatus.COMMENT_ONLY)

        self.assertEqual(BugLockStatus.COMMENT_ONLY, bug.lock_status)

        # A user without the relevant role cannot edit a locked bug.
        with person_logged_in(another_person):
            self.assertFalse(checkPermission("launchpad.Edit", bug))
            self.assertFalse(
                checkPermission("launchpad.Edit", bug.default_bugtask)
            )

        # The bug reporter cannot edit a locked bug.
        with person_logged_in(self.person):
            self.assertFalse(checkPermission("launchpad.Edit", bug))
            self.assertFalse(
                checkPermission("launchpad.Edit", bug.default_bugtask)
            )

        # Target driver can edit a locked bug.
        new_person = self.factory.makePerson()
        removeSecurityProxy(bug.default_bugtask.target).driver = new_person
        with person_logged_in(new_person):
            self.assertTrue(checkPermission("launchpad.Edit", bug))
            self.assertTrue(
                checkPermission("launchpad.Edit", bug.default_bugtask)
            )

        # Admins can edit a locked bug.
        with admin_logged_in():
            self.assertTrue(checkPermission("launchpad.Edit", bug))
            self.assertTrue(
                checkPermission("launchpad.Edit", bug.default_bugtask)
            )

        # Commercial admins can edit a locked bug.
        with celebrity_logged_in("commercial_admin"):
            self.assertTrue(checkPermission("launchpad.Edit", bug))
            self.assertTrue(
                checkPermission("launchpad.Edit", bug.default_bugtask)
            )

        # Registry experts can edit a locked bug.
        with celebrity_logged_in("registry_experts"):
            self.assertTrue(checkPermission("launchpad.Edit", bug))
            self.assertTrue(
                checkPermission("launchpad.Edit", bug.default_bugtask)
            )

    def test_only_those_with_moderate_permission_can_lock_unlock_a_bug(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        another_person = self.factory.makePerson()

        # Unauthenticated person cannot moderate a bug.
        self.assertFalse(checkPermission("launchpad.Moderate", bug))

        # A user without the relevant role cannot moderate a bug.
        with person_logged_in(another_person):
            self.assertFalse(checkPermission("launchpad.Moderate", bug))

        # The bug reporter cannot moderate a bug.
        with person_logged_in(self.person):
            self.assertFalse(checkPermission("launchpad.Moderate", bug))

        # Admins can moderate a bug.
        with admin_logged_in():
            self.assertTrue(checkPermission("launchpad.Moderate", bug))

        # Commercial admins can moderate a bug.
        with celebrity_logged_in("commercial_admin"):
            self.assertTrue(checkPermission("launchpad.Moderate", bug))

        # Registry experts can moderate a bug.
        with celebrity_logged_in("registry_experts"):
            self.assertTrue(checkPermission("launchpad.Moderate", bug))

        # Target owner can moderate a bug.
        with person_logged_in(
            removeSecurityProxy(bug.default_bugtask.target).owner
        ):
            self.assertTrue(checkPermission("launchpad.Moderate", bug))

        # Target driver can moderate a bug.
        new_person = self.factory.makePerson()
        removeSecurityProxy(bug.default_bugtask.target).driver = new_person
        with person_logged_in(new_person):
            self.assertTrue(checkPermission("launchpad.Moderate", bug))

        yet_another_person = self.factory.makePerson()
        removeSecurityProxy(bug.default_bugtask.target).bug_supervisor = (
            yet_another_person
        )
        with person_logged_in(yet_another_person):
            self.assertTrue(checkPermission("launchpad.Moderate", bug))


class TestBugLockingWebService(TestCaseWithFactory):
    """Tests for the bug locking and unlocking web service methods."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()
        self.target = self.factory.makeProduct()

    def test_bug_lock_status_invalid_values(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        invalid_values_list = ["test", 1.23, 123, "Unlocked"]
        webservice = webservice_for_person(
            self.target.owner, permission=OAuthPermission.WRITE_PRIVATE
        )
        bug_url = api_url(bug)
        webservice.default_api_version = "devel"
        for invalid_value in invalid_values_list:
            response = webservice.named_post(
                bug_url, "lock", status=invalid_value
            )
            self.assertEqual(400, response.status)

    def test_who_value_for_lock_is_correctly_set(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        self.assertEqual(bug.activity.count(), 1)
        self.assertEqual(BugLockStatus.UNLOCKED, bug.lock_status)

        bug_url = api_url(bug)
        webservice = webservice_for_person(
            self.target.owner, permission=OAuthPermission.WRITE_PRIVATE
        )
        webservice.default_api_version = "devel"

        response = webservice.named_post(
            bug_url,
            "lock",
            status="Comment-only",
        )
        self.assertEqual(200, response.status)

        with person_logged_in(ANONYMOUS):
            self.assertEqual(2, bug.activity.count())
            self.assertThat(
                bug.activity[1],
                MatchesStructure.byEquality(
                    person=self.target.owner,
                    whatchanged="lock status",
                    oldvalue=str(BugLockStatus.UNLOCKED),
                    newvalue=str(BugLockStatus.COMMENT_ONLY),
                ),
            )

    def test_who_value_for_unlock_is_correctly_set(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        self.assertEqual(1, bug.activity.count())
        with person_logged_in(self.target.owner):
            bug.lock(who=self.target.owner, status=BugLockStatus.COMMENT_ONLY)
        self.assertEqual(2, bug.activity.count())
        bug_url = api_url(bug)
        webservice = webservice_for_person(
            self.target.owner, permission=OAuthPermission.WRITE_PRIVATE
        )
        webservice.default_api_version = "devel"

        response = webservice.named_post(bug_url, "unlock")
        self.assertEqual(200, response.status)
        with person_logged_in(ANONYMOUS):
            self.assertEqual(3, bug.activity.count())
            self.assertThat(
                bug.activity[2],
                MatchesStructure.byEquality(
                    person=self.target.owner,
                    whatchanged="lock status",
                    oldvalue=str(BugLockStatus.COMMENT_ONLY),
                    newvalue=str(BugLockStatus.UNLOCKED),
                ),
            )

    def test_lock_status_lock_reason_values_unlocked_bug(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        bug_url = api_url(bug)

        webservice = webservice_for_person(
            self.target.owner, permission=OAuthPermission.WRITE_PRIVATE
        )
        webservice.default_api_version = "devel"

        response = webservice.get(bug_url)
        self.assertEqual(200, response.status)
        response_json = response.jsonBody()
        self.assertEqual(
            str(BugLockStatus.UNLOCKED), response_json["lock_status"]
        )
        self.assertIsNone(response_json["lock_reason"])

    def test_lock_status_lock_reason_values_after_locking(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        bug_url = api_url(bug)

        with person_logged_in(self.target.owner):
            bug.lock(
                who=self.target.owner,
                status=BugLockStatus.COMMENT_ONLY,
                reason="too hot",
            )

        webservice = webservice_for_person(
            self.target.owner, permission=OAuthPermission.WRITE_PRIVATE
        )
        webservice.default_api_version = "devel"

        response = webservice.get(bug_url)
        self.assertEqual(200, response.status)
        response_json = response.jsonBody()
        self.assertEqual(
            str(BugLockStatus.COMMENT_ONLY), response_json["lock_status"]
        )
        self.assertEqual(response_json["lock_reason"], "too hot")

    def test_setting_lock_reason_for_an_unlocked_bug(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        bug_url = api_url(bug)
        webservice = webservice_for_person(
            self.target.owner, permission=OAuthPermission.WRITE_PRIVATE
        )
        webservice.default_api_version = "devel"

        response = webservice.patch(
            bug_url, "application/json", json.dumps({"lock_reason": "too hot"})
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=400,
                body=b"Lock reason cannot be set for an unlocked bug.",
            ),
        )

    def test_setting_lock_reason_for_a_locked_bug_without_a_reason(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        with person_logged_in(self.target.owner):
            bug.lock(who=self.target.owner, status=BugLockStatus.COMMENT_ONLY)

        self.assertEqual(BugLockStatus.COMMENT_ONLY, bug.lock_status)
        self.assertIsNone(bug.lock_reason)

        bug_url = api_url(bug)
        webservice = webservice_for_person(
            self.target.owner, permission=OAuthPermission.WRITE_PRIVATE
        )
        webservice.default_api_version = "devel"

        response = webservice.patch(
            bug_url, "application/json", json.dumps({"lock_reason": "too hot"})
        )
        self.assertEqual(209, response.status)
        self.assertEqual("too hot", response.jsonBody()["lock_reason"])

    def test_setting_lock_reason_for_a_locked_bug_with_a_reason(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        with person_logged_in(self.target.owner):
            bug.lock(
                who=self.target.owner,
                status=BugLockStatus.COMMENT_ONLY,
                reason="too hot",
            )

        self.assertEqual(BugLockStatus.COMMENT_ONLY, bug.lock_status)
        self.assertEqual("too hot", bug.lock_reason)

        bug_url = api_url(bug)
        webservice = webservice_for_person(
            self.target.owner, permission=OAuthPermission.WRITE_PRIVATE
        )
        webservice.default_api_version = "devel"

        response = webservice.patch(
            bug_url,
            "application/json",
            json.dumps({"lock_reason": "too hot!"}),
        )
        self.assertEqual(209, response.status)
        self.assertEqual("too hot!", response.jsonBody()["lock_reason"])

    def test_removing_lock_reason_for_a_locked_bug_with_a_reason(self):
        bug = self.factory.makeBug(owner=self.person, target=self.target)
        with person_logged_in(self.target.owner):
            bug.lock(
                who=self.target.owner,
                status=BugLockStatus.COMMENT_ONLY,
                reason="too hot",
            )

        self.assertEqual(BugLockStatus.COMMENT_ONLY, bug.lock_status)
        self.assertEqual("too hot", bug.lock_reason)

        bug_url = api_url(bug)
        webservice = webservice_for_person(
            self.target.owner, permission=OAuthPermission.WRITE_PRIVATE
        )
        webservice.default_api_version = "devel"

        response = webservice.patch(
            bug_url, "application/json", json.dumps({"lock_reason": None})
        )
        self.assertEqual(209, response.status)
        self.assertEqual(None, response.jsonBody()["lock_reason"])
