# Copyright 2009-2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test the database garbage collector."""

__all__ = []

import hashlib
import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from textwrap import dedent

import transaction
from psycopg2 import IntegrityError
from storm.exceptions import LostObjectError
from storm.expr import SQL, In, Min, Not
from storm.locals import Int
from storm.store import Store
from testtools.content import text_content
from testtools.matchers import (
    AfterPreprocessing,
    ContainsDict,
    Equals,
    GreaterThan,
    Is,
    MatchesListwise,
    MatchesStructure,
)
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.answers.model.answercontact import AnswerContact
from lp.app.enums import InformationType
from lp.bugs.interfaces.bug import IBugSet
from lp.bugs.interfaces.cve import CveStatus, ICveSet
from lp.bugs.model.bugnotification import (
    BugNotification,
    BugNotificationRecipient,
)
from lp.buildmaster.enums import BuildStatus
from lp.code.bzr import BranchFormat, RepositoryFormat
from lp.code.enums import (
    CodeImportResultStatus,
    GitRepositoryStatus,
    RevisionStatusArtifactType,
)
from lp.code.interfaces.codeimportevent import ICodeImportEventSet
from lp.code.interfaces.gitrepository import IGitRepositorySet
from lp.code.interfaces.revisionstatus import (
    IRevisionStatusArtifactSet,
    IRevisionStatusReportSet,
)
from lp.code.model.branchjob import BranchJob, BranchUpgradeJob
from lp.code.model.branchmergeproposaljob import BranchMergeProposalJob
from lp.code.model.codeimportevent import CodeImportEvent
from lp.code.model.codeimportresult import CodeImportResult
from lp.code.model.diff import Diff
from lp.code.model.gitjob import GitJob, GitRefScanJob
from lp.code.model.gitrepository import GitRepository
from lp.code.tests.helpers import GitHostingFixture
from lp.oci.interfaces.ocirecipe import OCI_RECIPE_ALLOW_CREATE
from lp.oci.model.ocirecipebuild import OCIFile
from lp.registry.enums import BranchSharingPolicy, BugSharingPolicy, VCSType
from lp.registry.interfaces.accesspolicy import IAccessPolicySource
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.sourcepackage import SourcePackageType
from lp.registry.interfaces.teammembership import TeamMembershipStatus
from lp.registry.model.commercialsubscription import CommercialSubscription
from lp.registry.model.teammembership import TeamMembership
from lp.scripts.garbo import (
    AntiqueSessionPruner,
    ArchiveFileDatePopulator,
    BulkPruner,
    DailyDatabaseGarbageCollector,
    DuplicateSessionPruner,
    FrequentDatabaseGarbageCollector,
    HourlyDatabaseGarbageCollector,
    LoginTokenPruner,
    OpenIDConsumerAssociationPruner,
    PopulateCveSequence,
    ProductVCSPopulator,
    UnusedPOTMsgSetPruner,
    UnusedSessionPruner,
    UpdatePPASigningKeyFingerprintToRSA4096Key,
    load_garbo_job_state,
    save_garbo_job_state,
)
from lp.services.config import config
from lp.services.database import sqlbase
from lp.services.database.constants import (
    ONE_DAY_AGO,
    SEVEN_DAYS_AGO,
    THIRTY_DAYS_AGO,
    UTC_NOW,
)
from lp.services.database.interfaces import IPrimaryStore
from lp.services.database.stormbase import StormBase
from lp.services.features.model import FeatureFlag
from lp.services.features.testing import FeatureFixture
from lp.services.identity.interfaces.account import AccountStatus
from lp.services.identity.interfaces.emailaddress import EmailAddressStatus
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.model.job import Job
from lp.services.librarian.model import TimeLimitedToken
from lp.services.messages.interfaces.message import IMessageSet
from lp.services.messages.model.message import Message
from lp.services.openid.model.openidconsumer import OpenIDConsumerNonce
from lp.services.session.model import SessionData, SessionPkgData
from lp.services.signing.enums import SigningKeyType
from lp.services.verification.interfaces.authtoken import LoginTokenType
from lp.services.verification.model.logintoken import LoginToken
from lp.services.worlddata.interfaces.language import ILanguageSet
from lp.snappy.model.snapbuild import SnapFile
from lp.snappy.model.snapbuildjob import SnapBuildJob, SnapStoreUploadJob
from lp.soyuz.enums import (
    ArchivePublishingMethod,
    ArchiveRepositoryFormat,
    ArchiveSubscriberStatus,
    BinaryPackageFormat,
    PackagePublishingStatus,
)
from lp.soyuz.interfaces.archive import NAMED_AUTH_TOKEN_FEATURE_FLAG
from lp.soyuz.interfaces.archivefile import IArchiveFileSet
from lp.soyuz.interfaces.livefs import LIVEFS_FEATURE_FLAG
from lp.soyuz.interfaces.publishing import IPublishingSet
from lp.soyuz.model.distributionsourcepackagecache import (
    DistributionSourcePackageCache,
)
from lp.soyuz.model.livefsbuild import LiveFSFile
from lp.soyuz.model.reporting import LatestPersonSourcePackageReleaseCache
from lp.testing import (
    FakeAdapterMixin,
    TestCase,
    TestCaseWithFactory,
    admin_logged_in,
    person_logged_in,
)
from lp.testing.dbuser import switch_dbuser
from lp.testing.layers import (
    DatabaseLayer,
    LaunchpadScriptLayer,
    LaunchpadZopelessLayer,
    ZopelessDatabaseLayer,
)
from lp.testing.mail_helpers import pop_notifications
from lp.testing.script import run_script
from lp.translations.model.pofile import POFile
from lp.translations.model.potmsgset import POTMsgSet
from lp.translations.model.translationtemplateitem import (
    TranslationTemplateItem,
)

_default = object()


class TestGarboScript(TestCase):
    layer = LaunchpadScriptLayer

    def test_daily_script(self):
        """Ensure garbo-daily.py actually runs."""
        exit_code, out, err = run_script(
            "cronscripts/garbo-daily.py", args=["-q"]
        )
        self.assertEqual(0, exit_code)
        self.assertFalse(out.strip(), "Output to stdout: %s" % out)
        self.assertFalse(err.strip(), "Output to stderr: %s" % err)
        DatabaseLayer.force_dirty_database()

    def test_hourly_script(self):
        """Ensure garbo-hourly.py actually runs."""
        exit_code, out, err = run_script(
            "cronscripts/garbo-hourly.py", args=["-q"]
        )
        self.assertEqual(0, exit_code)
        self.assertFalse(out.strip(), "Output to stdout: %s" % out)
        self.assertFalse(err.strip(), "Output to stderr: %s" % err)
        DatabaseLayer.force_dirty_database()


class BulkFoo(StormBase):
    __storm_table__ = "bulkfoo"
    id = Int(primary=True)


class BulkFooPruner(BulkPruner):
    target_table_class = BulkFoo
    ids_to_prune_query = "SELECT id FROM BulkFoo WHERE id < 5"
    maximum_chunk_size = 2


class TestBulkPruner(TestCase):
    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()

        self.store = IPrimaryStore(CommercialSubscription)
        self.store.execute("CREATE TABLE BulkFoo (id serial PRIMARY KEY)")

        for _ in range(10):
            self.store.add(BulkFoo())

        self.log = logging.getLogger("garbo")

    def test_bulkpruner(self):
        pruner = BulkFooPruner(self.log)

        # The loop thinks there is stuff to do. Confirm the initial
        # state is sane.
        self.assertFalse(pruner.isDone())

        # An arbitrary chunk size.
        chunk_size = 2

        # Determine how many items to prune and to leave rather than
        # hardcode these numbers.
        num_to_prune = self.store.find(BulkFoo, BulkFoo.id < 5).count()
        num_to_leave = self.store.find(BulkFoo, BulkFoo.id >= 5).count()
        self.assertTrue(num_to_prune > chunk_size)
        self.assertTrue(num_to_leave > 0)

        # Run one loop. Make sure it committed by throwing away
        # uncommitted changes.
        pruner(chunk_size)
        transaction.abort()

        # Confirm 'chunk_size' items where removed; no more, no less.
        num_remaining = self.store.find(BulkFoo).count()
        expected_num_remaining = num_to_leave + num_to_prune - chunk_size
        self.assertEqual(num_remaining, expected_num_remaining)

        # The loop thinks there is more stuff to do.
        self.assertFalse(pruner.isDone())

        # Run the loop to completion, removing the remaining targeted rows.
        while not pruner.isDone():
            pruner(1000000)
        transaction.abort()

        # Confirm we have removed all targeted rows.
        self.assertEqual(self.store.find(BulkFoo, BulkFoo.id < 5).count(), 0)

        # Confirm we have the expected number of remaining rows.
        # With the previous check, this means no untargeted rows
        # where removed.
        self.assertEqual(
            self.store.find(BulkFoo, BulkFoo.id >= 5).count(), num_to_leave
        )

        # Cleanup clears up our resources.
        pruner.cleanUp()

        # We can run it again - temporary objects cleaned up.
        pruner = BulkFooPruner(self.log)
        while not pruner.isDone():
            pruner(chunk_size)


class TestSessionPruner(TestCase):
    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()

        # Session database isn't reset between tests. We need to do this
        # manually.
        nuke_all_sessions = IPrimaryStore(SessionData).find(SessionData).remove
        nuke_all_sessions()
        self.addCleanup(nuke_all_sessions)

        recent = datetime.now(timezone.utc)
        yesterday = recent - timedelta(days=1)
        ancient = recent - timedelta(days=61)

        self.make_session("recent_auth", recent, b"auth1")
        self.make_session("recent_unauth", recent, False)
        self.make_session("yesterday_auth", yesterday, b"auth2")
        self.make_session("yesterday_unauth", yesterday, False)
        self.make_session("ancient_auth", ancient, b"auth3")
        self.make_session("ancient_unauth", ancient, False)

        self.log = logging.getLogger("garbo")

    def make_session(self, client_id, accessed, authenticated=None):
        session_data = SessionData()
        session_data.client_id = client_id
        session_data.last_accessed = accessed
        IPrimaryStore(SessionData).add(session_data)

        if authenticated:
            # Add login time information.
            session_pkg_data = SessionPkgData()
            session_pkg_data.client_id = client_id
            session_pkg_data.product_id = "launchpad.authenticateduser"
            session_pkg_data.key = "logintime"
            session_pkg_data.pickle = b"value is ignored"
            IPrimaryStore(SessionPkgData).add(session_pkg_data)

            # Add authenticated as information.
            session_pkg_data = SessionPkgData()
            session_pkg_data.client_id = client_id
            session_pkg_data.product_id = "launchpad.authenticateduser"
            session_pkg_data.key = "accountid"
            # Normally Account.id, but the session pruning works
            # at the SQL level and doesn't unpickle anything.
            session_pkg_data.pickle = authenticated
            IPrimaryStore(SessionPkgData).add(session_pkg_data)

    def sessionExists(self, client_id):
        store = IPrimaryStore(SessionData)
        return not store.find(
            SessionData, SessionData.client_id == client_id
        ).is_empty()

    def test_antique_session_pruner(self):
        chunk_size = 2
        pruner = AntiqueSessionPruner(self.log)
        try:
            while not pruner.isDone():
                pruner(chunk_size)
        finally:
            pruner.cleanUp()

        expected_sessions = {
            "recent_auth",
            "recent_unauth",
            "yesterday_auth",
            "yesterday_unauth",
            # 'ancient_auth',
            # 'ancient_unauth',
        }

        found_sessions = set(
            IPrimaryStore(SessionData).find(SessionData.client_id)
        )

        self.assertEqual(expected_sessions, found_sessions)

    def test_unused_session_pruner(self):
        chunk_size = 2
        pruner = UnusedSessionPruner(self.log)
        try:
            while not pruner.isDone():
                pruner(chunk_size)
        finally:
            pruner.cleanUp()

        expected_sessions = {
            "recent_auth",
            "recent_unauth",
            "yesterday_auth",
            # 'yesterday_unauth',
            "ancient_auth",
            # 'ancient_unauth',
        }

        found_sessions = set(
            IPrimaryStore(SessionData).find(SessionData.client_id)
        )

        self.assertEqual(expected_sessions, found_sessions)

    def test_duplicate_session_pruner(self):
        # None of the sessions created in setUp() are duplicates, so
        # they will all survive the pruning.
        expected_sessions = {
            "recent_auth",
            "recent_unauth",
            "yesterday_auth",
            "yesterday_unauth",
            "ancient_auth",
            "ancient_unauth",
        }

        now = datetime.now(timezone.utc)

        # Make some duplicate logins from a few days ago.
        # Only the most recent 6 will be kept. Oldest is 'old dupe 9',
        # most recent 'old dupe 1'.
        for count in range(1, 10):
            self.make_session(
                "old dupe %d" % count,
                now - timedelta(days=2, seconds=count),
                b"old dupe",
            )
        for count in range(1, 7):
            expected_sessions.add("old dupe %d" % count)

        # Make some other duplicate logins less than an hour old.
        # All of these will be kept.
        for count in range(1, 10):
            self.make_session("new dupe %d" % count, now, b"new dupe")
            expected_sessions.add("new dupe %d" % count)

        chunk_size = 2
        pruner = DuplicateSessionPruner(self.log)
        try:
            while not pruner.isDone():
                pruner(chunk_size)
        finally:
            pruner.cleanUp()

        found_sessions = set(
            IPrimaryStore(SessionData).find(SessionData.client_id)
        )

        self.assertEqual(expected_sessions, found_sessions)


class TestGarbo(FakeAdapterMixin, TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()

        # Silence the root Logger by instructing the garbo logger to not
        # propagate messages.
        self.log = logging.getLogger("garbo")
        self.log.addHandler(logging.NullHandler())
        self.log.propagate = 0

        # Run the garbage collectors to remove any existing garbage,
        # starting us in a known state.
        self.runDaily()
        self.runHourly()
        self.runFrequently()

        # Capture garbo log output to tests can examine it.
        self.log_buffer = io.StringIO()
        handler = logging.StreamHandler(self.log_buffer)
        self.log.addHandler(handler)
        self.addCleanup(
            lambda: self.addDetail(
                "garbo-log", text_content(self.log_buffer.getvalue())
            )
        )

    def runFrequently(self, maximum_chunk_size=2, test_args=()):
        switch_dbuser("garbo_daily")
        collector = FrequentDatabaseGarbageCollector(test_args=list(test_args))
        collector._maximum_chunk_size = maximum_chunk_size
        collector.logger = self.log
        collector.main()
        return collector

    def runDaily(self, maximum_chunk_size=2, test_args=()):
        switch_dbuser("garbo_daily")
        collector = DailyDatabaseGarbageCollector(test_args=list(test_args))
        collector._maximum_chunk_size = maximum_chunk_size
        collector.logger = self.log
        collector.main()
        return collector

    def runHourly(self, maximum_chunk_size=2, test_args=()):
        switch_dbuser("garbo_hourly")
        collector = HourlyDatabaseGarbageCollector(test_args=list(test_args))
        collector._maximum_chunk_size = maximum_chunk_size
        collector.logger = self.log
        collector.main()
        return collector

    def test_persist_garbo_state(self):
        # Test that loading and saving garbo job state works.
        save_garbo_job_state("job", {"data": 1})
        data = load_garbo_job_state("job")
        self.assertEqual({"data": 1}, data)
        save_garbo_job_state("job", {"data": 2})
        data = load_garbo_job_state("job")
        self.assertEqual({"data": 2}, data)

    def test_OpenIDConsumerNoncePruner(self):
        now = int(time.mktime(time.gmtime()))
        MINUTES = 60
        HOURS = 60 * 60
        DAYS = 24 * HOURS
        timestamps = [
            now - 2 * DAYS,  # Garbage
            now - 1 * DAYS - 1 * MINUTES,  # Garbage
            now - 1 * DAYS + 1 * MINUTES,  # Not garbage
            now,  # Not garbage
        ]
        switch_dbuser("testadmin")

        store = IPrimaryStore(OpenIDConsumerNonce)

        # Make sure we start with 0 nonces.
        self.assertEqual(store.find(OpenIDConsumerNonce).count(), 0)

        for timestamp in timestamps:
            store.add(OpenIDConsumerNonce("http://server/", timestamp, "aa"))
        transaction.commit()

        # Make sure we have 4 nonces now.
        self.assertEqual(store.find(OpenIDConsumerNonce).count(), 4)

        # Run the garbage collector.
        self.runFrequently(maximum_chunk_size=60)  # 1 minute maximum chunks.

        store = IPrimaryStore(OpenIDConsumerNonce)

        # We should now have 2 nonces.
        self.assertEqual(store.find(OpenIDConsumerNonce).count(), 2)

        # And none of them are older than 1 day
        earliest = store.find(Min(OpenIDConsumerNonce.timestamp)).one()
        self.assertTrue(
            earliest >= now - 24 * 60 * 60, "Still have old nonces"
        )

    def test_CodeImportResultPruner(self):
        self.useFixture(GitHostingFixture())
        now = datetime.now(timezone.utc)
        store = IPrimaryStore(CodeImportResult)

        results_to_keep_count = config.codeimport.consecutive_failure_limit - 1

        switch_dbuser("testadmin")
        code_import = self.factory.makeCodeImport()
        machine = self.factory.makeCodeImportMachine()
        requester = self.factory.makePerson()
        transaction.commit()

        def new_code_import_result(timestamp):
            switch_dbuser("testadmin")
            CodeImportResult(
                date_created=timestamp,
                code_import=code_import,
                machine=machine,
                requesting_user=requester,
                status=CodeImportResultStatus.FAILURE,
                date_job_started=timestamp,
            )
            transaction.commit()

        new_code_import_result(now - timedelta(days=60))
        for i in range(results_to_keep_count - 1):
            new_code_import_result(now - timedelta(days=19 + i))

        # Run the garbage collector
        self.runDaily()

        # Nothing is removed, because we always keep the
        # ``results_to_keep_count`` latest.
        store = IPrimaryStore(CodeImportResult)
        self.assertEqual(
            results_to_keep_count, store.find(CodeImportResult).count()
        )

        new_code_import_result(now - timedelta(days=31))
        self.runDaily()
        store = IPrimaryStore(CodeImportResult)
        self.assertEqual(
            results_to_keep_count, store.find(CodeImportResult).count()
        )

        new_code_import_result(now - timedelta(days=29))
        self.runDaily()
        store = IPrimaryStore(CodeImportResult)
        self.assertEqual(
            results_to_keep_count, store.find(CodeImportResult).count()
        )

        # We now have no CodeImportResults older than 30 days
        self.assertTrue(
            store.find(Min(CodeImportResult.date_created))
            .one()
            .replace(tzinfo=timezone.utc)
            >= now - timedelta(days=30)
        )

    def test_CodeImportEventPruner(self):
        now = datetime.now(timezone.utc)
        store = IPrimaryStore(CodeImportResult)

        switch_dbuser("testadmin")
        machine = self.factory.makeCodeImportMachine()
        requester = self.factory.makePerson()
        # Create 6 code import events for this machine, 3 on each side of 30
        # days. Use the event set to the extra event data rows get created
        # too.
        event_set = getUtility(ICodeImportEventSet)
        for age in (35, 33, 31, 29, 27, 15):
            event_set.newOnline(
                machine,
                user=requester,
                message="Hello",
                _date_created=(now - timedelta(days=age)),
            )
        transaction.commit()

        # Run the garbage collector
        self.runDaily()

        # Only the three most recent results are left.
        events = list(machine.events)
        self.assertEqual(3, len(events))
        # We now have no CodeImportEvents older than 30 days
        self.assertTrue(
            store.find(Min(CodeImportEvent.date_created))
            .one()
            .replace(tzinfo=timezone.utc)
            >= now - timedelta(days=30)
        )

    def test_OpenIDConsumerAssociationPruner(self):
        pruner = OpenIDConsumerAssociationPruner
        table_name = pruner.table_name
        switch_dbuser("testadmin")
        store = IPrimaryStore(CommercialSubscription)
        now = time.time()
        # Create some associations in the past with lifetimes
        for delta in range(0, 20):
            store.execute(
                """
                INSERT INTO %s (server_url, handle, issued, lifetime)
                VALUES (%s, %s, %d, %d)
                """
                % (table_name, str(delta), str(delta), now - 10, delta)
            )
        transaction.commit()

        # Ensure that we created at least one expirable row (using the
        # test start time as 'now').
        num_expired = store.execute(
            """
            SELECT COUNT(*) FROM %s
            WHERE issued + lifetime < %f
            """
            % (table_name, now)
        ).get_one()[0]
        self.assertTrue(num_expired > 0)

        # Expire all those expirable rows, and possibly a few more if this
        # test is running slow.
        self.runFrequently()

        switch_dbuser("testadmin")
        store = IPrimaryStore(CommercialSubscription)
        # Confirm all the rows we know should have been expired have
        # been expired. These are the ones that would be expired using
        # the test start time as 'now'.
        num_expired = store.execute(
            """
            SELECT COUNT(*) FROM %s
            WHERE issued + lifetime < %f
            """
            % (table_name, now)
        ).get_one()[0]
        self.assertEqual(num_expired, 0)

        # Confirm that we haven't expired everything. This test will fail
        # if it has taken 10 seconds to get this far.
        num_unexpired = store.execute(
            "SELECT COUNT(*) FROM %s" % table_name
        ).get_one()[0]
        self.assertTrue(num_unexpired > 0)

    def test_PreviewDiffPruner(self):
        switch_dbuser("testadmin")
        mp1 = self.factory.makeBranchMergeProposal()
        now = datetime.now(timezone.utc)
        self.factory.makePreviewDiff(
            merge_proposal=mp1, date_created=now - timedelta(hours=2)
        )
        self.factory.makePreviewDiff(
            merge_proposal=mp1, date_created=now - timedelta(hours=1)
        )
        mp1_diff = self.factory.makePreviewDiff(merge_proposal=mp1)
        mp2 = self.factory.makeBranchMergeProposal()
        mp2_diff = self.factory.makePreviewDiff(merge_proposal=mp2)
        self.runDaily()
        mp1_diff_ids = [removeSecurityProxy(p).id for p in mp1.preview_diffs]
        mp2_diff_ids = [removeSecurityProxy(p).id for p in mp2.preview_diffs]
        self.assertEqual([mp1_diff.id], mp1_diff_ids)
        self.assertEqual([mp2_diff.id], mp2_diff_ids)

    def test_PreviewDiffPruner_with_inline_comments(self):
        # Old PreviewDiffs with published or draft inline comments
        # are not removed.
        switch_dbuser("testadmin")
        mp1 = self.factory.makeBranchMergeProposal()
        now = datetime.now(timezone.utc)
        mp1_diff_comment = self.factory.makePreviewDiff(
            merge_proposal=mp1, date_created=now - timedelta(hours=2)
        )
        mp1_diff_draft = self.factory.makePreviewDiff(
            merge_proposal=mp1, date_created=now - timedelta(hours=1)
        )
        mp1_diff = self.factory.makePreviewDiff(merge_proposal=mp1)
        # Attach comments on the old diffs, so they are kept in DB.
        mp1.createComment(
            owner=mp1.registrant,
            previewdiff_id=mp1_diff_comment.id,
            subject="Hold this diff!",
            inline_comments={"1": "!!!"},
        )
        mp1.saveDraftInlineComment(
            previewdiff_id=mp1_diff_draft.id,
            person=mp1.registrant,
            comments={"1": "???"},
        )
        # Run the 'garbo' and verify the old diffs were not removed.
        self.runDaily()
        self.assertEqual(
            [mp1_diff_comment.id, mp1_diff_draft.id, mp1_diff.id],
            [p.id for p in mp1.preview_diffs],
        )

    def test_DiffPruner(self):
        switch_dbuser("testadmin")
        diff_id = removeSecurityProxy(self.factory.makeDiff()).id
        self.runDaily()
        store = IPrimaryStore(Diff)
        self.assertContentEqual([], store.find(Diff, Diff.id == diff_id))

    def test_RevisionAuthorEmailLinker(self):
        switch_dbuser("testadmin")
        rev1 = self.factory.makeRevision("Author 1 <author-1@Example.Org>")
        rev2 = self.factory.makeRevision("Author 2 <author-2@Example.Org>")

        person1 = self.factory.makePerson(email="Author-1@example.org")
        person2 = self.factory.makePerson(
            email="Author-2@example.org",
            email_address_status=EmailAddressStatus.NEW,
        )

        self.assertEqual(rev1.revision_author.person, None)
        self.assertEqual(rev2.revision_author.person, None)

        self.runDaily()

        # Only the validated email address associated with a Person
        # causes a linkage.
        switch_dbuser("testadmin")
        self.assertEqual(rev1.revision_author.person, person1)
        self.assertEqual(rev2.revision_author.person, None)

        # Validating an email address creates a linkage.
        person2.validateAndEnsurePreferredEmail(person2.guessedemails[0])
        self.assertEqual(rev2.revision_author.person, None)

        self.runDaily()
        switch_dbuser("testadmin")
        self.assertEqual(rev2.revision_author.person, person2)

    def test_PersonPruner(self):
        personset = getUtility(IPersonSet)
        # Switch the DB user because the garbo_daily user isn't allowed to
        # create person entries.
        switch_dbuser("testadmin")

        # Create two new person entries, both not linked to anything. One of
        # them will have the present day as its date created, and so will not
        # be deleted, whereas the other will have a creation date far in the
        # past, so it will be deleted.
        self.factory.makePerson(name="test-unlinked-person-new")
        person_old = self.factory.makePerson(name="test-unlinked-person-old")
        removeSecurityProxy(person_old).datecreated = datetime(
            2008, 1, 1, tzinfo=timezone.utc
        )

        # Normally, the garbage collector will do nothing because the
        # PersonPruner is experimental
        self.runDaily()
        self.assertIsNot(personset.getByName("test-unlinked-person-new"), None)
        self.assertIsNot(personset.getByName("test-unlinked-person-old"), None)

        # When we run the garbage collector with experimental jobs turned
        # on, the old unlinked Person is removed.
        self.runDaily(test_args=["--experimental"])
        self.assertIsNot(personset.getByName("test-unlinked-person-new"), None)
        self.assertIs(personset.getByName("test-unlinked-person-old"), None)

    def test_TeamMembershipPruner(self):
        # Garbo should remove team memberships for merged users and teams.
        switch_dbuser("testadmin")
        merged_user = self.factory.makePerson()
        team = self.factory.makeTeam(members=[merged_user])
        merged_team = self.factory.makeTeam()
        team.addMember(
            merged_team, team.teamowner, status=TeamMembershipStatus.PROPOSED
        )
        # This is fast and dirty way to place the user and team in a
        # merged state to verify what the TeamMembershipPruner sees.
        removeSecurityProxy(merged_user).merged = self.factory.makePerson()
        removeSecurityProxy(merged_team).merged = self.factory.makeTeam()
        store = Store.of(team)
        store.flush()
        result = store.find(TeamMembership, team=team)
        self.assertEqual(3, result.count())
        self.runDaily()
        self.assertContentEqual([team.teamowner], [tm.person for tm in result])

    def test_BugNotificationPruner(self):
        # Create some sample data
        switch_dbuser("testadmin")
        bug = getUtility(IBugSet).get(1)
        message = getUtility(IMessageSet).get("foo@example.com-332342--1231")[
            0
        ]
        person = self.factory.makePerson()
        notification = BugNotification(
            message=message, bug=bug, is_comment=True, date_emailed=None
        )
        BugNotificationRecipient(
            bug_notification=notification,
            person=person,
            reason_header="Whatever",
            reason_body="Whatever",
        )
        # We don't create an entry exactly 30 days old to avoid
        # races in the test.
        for delta in range(-45, -14, 2):
            message = Message(rfc822msgid=str(delta))
            notification = BugNotification(
                message=message,
                bug=bug,
                is_comment=True,
                date_emailed=UTC_NOW + SQL("interval '%d days'" % delta),
            )
            BugNotificationRecipient(
                bug_notification=notification,
                person=person,
                reason_header="Whatever",
                reason_body="Whatever",
            )

        store = IPrimaryStore(BugNotification)

        # Ensure we are at a known starting point.
        num_unsent = store.find(
            BugNotification, BugNotification.date_emailed == None
        ).count()
        num_old = store.find(
            BugNotification, BugNotification.date_emailed < THIRTY_DAYS_AGO
        ).count()
        num_new = store.find(
            BugNotification, BugNotification.date_emailed > THIRTY_DAYS_AGO
        ).count()

        self.assertEqual(num_unsent, 1)
        self.assertEqual(num_old, 8)
        self.assertEqual(num_new, 8)

        # Run the garbage collector.
        self.runDaily()

        # We should have 9 BugNotifications left.
        self.assertEqual(
            store.find(
                BugNotification, BugNotification.date_emailed == None
            ).count(),
            num_unsent,
        )
        self.assertEqual(
            store.find(
                BugNotification, BugNotification.date_emailed > THIRTY_DAYS_AGO
            ).count(),
            num_new,
        )
        self.assertEqual(
            store.find(
                BugNotification, BugNotification.date_emailed < THIRTY_DAYS_AGO
            ).count(),
            0,
        )

    def _test_AnswerContactPruner(self, status, interval, expected_count=0):
        # Garbo should remove answer contacts for accounts with given 'status'
        # which was set more than 'interval' days ago.
        switch_dbuser("testadmin")
        store = IPrimaryStore(AnswerContact)

        person = self.factory.makePerson()
        person.addLanguage(getUtility(ILanguageSet)["en"])
        question = self.factory.makeQuestion()
        with person_logged_in(question.owner):
            question.target.addAnswerContact(person, person)
        Store.of(question).flush()
        self.assertEqual(
            store.find(
                AnswerContact, AnswerContact.person == person.id
            ).count(),
            1,
        )

        account = person.account
        account.setStatus(status, None, "Go away")
        # We flush because a trigger sets the date_status_set and we need to
        # modify it ourselves.
        Store.of(account).flush()
        if interval is not None:
            removeSecurityProxy(account).date_status_set = interval

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(
            store.find(
                AnswerContact, AnswerContact.person == person.id
            ).count(),
            expected_count,
        )

    def test_AnswerContactPruner_deactivated_accounts(self):
        # Answer contacts with an account deactivated at least one day ago
        # should be pruned.
        self._test_AnswerContactPruner(AccountStatus.DEACTIVATED, ONE_DAY_AGO)

    def test_AnswerContactPruner_suspended_accounts(self):
        # Answer contacts with an account suspended at least seven days ago
        # should be pruned.
        self._test_AnswerContactPruner(AccountStatus.SUSPENDED, SEVEN_DAYS_AGO)

    def test_AnswerContactPruner_deceased_accounts(self):
        # Answer contacts with an account marked as deceased at least seven
        # days ago should be pruned.
        self._test_AnswerContactPruner(AccountStatus.DECEASED, SEVEN_DAYS_AGO)

    def test_AnswerContactPruner_doesnt_prune_recently_changed_accounts(self):
        # Answer contacts which are deactivated, suspended, or deceased
        # inside the minimum time interval are not pruned.
        self._test_AnswerContactPruner(
            AccountStatus.DEACTIVATED, None, expected_count=1
        )
        self._test_AnswerContactPruner(
            AccountStatus.SUSPENDED, ONE_DAY_AGO, expected_count=1
        )
        self._test_AnswerContactPruner(
            AccountStatus.DECEASED, ONE_DAY_AGO, expected_count=1
        )

    def test_BranchJobPruner(self):
        # Garbo should remove jobs completed over 30 days ago.
        switch_dbuser("testadmin")
        store = IPrimaryStore(Job)

        db_branch = self.factory.makeAnyBranch()
        db_branch.branch_format = BranchFormat.BZR_BRANCH_5
        db_branch.repository_format = RepositoryFormat.BZR_KNIT_1
        Store.of(db_branch).flush()
        branch_job = BranchUpgradeJob.create(
            db_branch, self.factory.makePerson()
        )
        branch_job.job.date_finished = THIRTY_DAYS_AGO

        self.assertEqual(
            store.find(BranchJob, BranchJob.branch == db_branch.id).count(), 1
        )

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(
            store.find(BranchJob, BranchJob.branch == db_branch.id).count(), 0
        )

    def test_BranchJobPruner_doesnt_prune_recent_jobs(self):
        # Check to make sure the garbo doesn't remove jobs that aren't more
        # than thirty days old.
        switch_dbuser("testadmin")
        store = IPrimaryStore(Job)

        db_branch = self.factory.makeAnyBranch(
            branch_format=BranchFormat.BZR_BRANCH_5,
            repository_format=RepositoryFormat.BZR_KNIT_1,
        )

        branch_job = BranchUpgradeJob.create(
            db_branch, self.factory.makePerson()
        )
        branch_job.job.date_finished = THIRTY_DAYS_AGO

        db_branch2 = self.factory.makeAnyBranch(
            branch_format=BranchFormat.BZR_BRANCH_5,
            repository_format=RepositoryFormat.BZR_KNIT_1,
        )
        BranchUpgradeJob.create(db_branch2, self.factory.makePerson())

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(store.find(BranchJob).count(), 1)

    def test_BranchMergeProposalJobPruner(self):
        # Garbo should remove jobs completed over 30 days ago.
        switch_dbuser("testadmin")
        store = IPrimaryStore(Job)

        bmp = self.factory.makeBranchMergeProposal()
        bmp_job = removeSecurityProxy(bmp.next_preview_diff_job)
        bmp_job.job.date_finished = THIRTY_DAYS_AGO

        self.assertEqual(
            store.find(
                BranchMergeProposalJob,
                BranchMergeProposalJob.branch_merge_proposal == bmp.id,
            ).count(),
            1,
        )

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(
            store.find(
                BranchMergeProposalJob,
                BranchMergeProposalJob.branch_merge_proposal == bmp.id,
            ).count(),
            0,
        )

    def test_BranchMergeProposalJobPruner_doesnt_prune_recent_jobs(self):
        # Check to make sure the garbo doesn't remove jobs that aren't more
        # than thirty days old.
        switch_dbuser("testadmin")
        store = IPrimaryStore(Job)

        bmp = self.factory.makeBranchMergeProposal()
        bmp_job = removeSecurityProxy(bmp.next_preview_diff_job)
        bmp_job.job.date_finished = THIRTY_DAYS_AGO

        bmp2 = self.factory.makeBranchMergeProposal()
        self.assertIsNotNone(bmp2.next_preview_diff_job)

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(store.find(BranchMergeProposalJob).count(), 1)

    def test_GitJobPruner(self):
        # Garbo should remove jobs completed over 30 days ago.
        switch_dbuser("testadmin")
        store = IPrimaryStore(Job)

        db_repository = self.factory.makeGitRepository()
        Store.of(db_repository).flush()
        git_job = GitRefScanJob.create(db_repository)
        git_job.job.date_finished = THIRTY_DAYS_AGO

        self.assertEqual(
            1,
            store.find(GitJob, GitJob.repository == db_repository.id).count(),
        )

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(
            0,
            store.find(GitJob, GitJob.repository == db_repository.id).count(),
        )

    def test_GitJobPruner_doesnt_prune_recent_jobs(self):
        # Check to make sure the garbo doesn't remove jobs that aren't more
        # than thirty days old.
        switch_dbuser("testadmin")
        store = IPrimaryStore(Job)

        db_repository = self.factory.makeGitRepository()

        git_job = GitRefScanJob.create(db_repository)
        git_job.job.date_finished = THIRTY_DAYS_AGO

        db_repository2 = self.factory.makeGitRepository()
        GitRefScanJob.create(db_repository2)

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(1, store.find(GitJob).count())

    def test_SnapBuildJobPruner(self):
        # Garbo removes jobs completed over 30 days ago.
        switch_dbuser("testadmin")
        store = IPrimaryStore(Job)

        snapbuild = self.factory.makeSnapBuild()
        snapbuild_job = SnapStoreUploadJob.create(snapbuild)
        snapbuild_job.job.date_finished = THIRTY_DAYS_AGO
        SnapStoreUploadJob.create(snapbuild)

        self.assertEqual(2, store.find(SnapBuildJob).count())

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(1, store.find(SnapBuildJob).count())

    def test_SnapBuildJobPruner_doesnt_prune_recent_jobs(self):
        # Garbo doesn't remove jobs under thirty days old.
        switch_dbuser("testadmin")
        store = IPrimaryStore(Job)

        snapbuild = self.factory.makeSnapBuild()
        snapbuild_job = SnapStoreUploadJob.create(snapbuild)
        SnapStoreUploadJob.create(snapbuild)

        snapbuild2 = self.factory.makeSnapBuild()
        snapbuild_job2 = SnapStoreUploadJob.create(snapbuild2)
        snapbuild_job2.job.date_finished = THIRTY_DAYS_AGO
        SnapStoreUploadJob.create(snapbuild2)

        self.assertEqual(4, store.find(SnapBuildJob).count())

        self.runDaily()

        switch_dbuser("testadmin")
        snapbuild_jobs = set(store.find(SnapBuildJob))
        self.assertEqual(3, len(snapbuild_jobs))
        self.assertIn(snapbuild_job.context, snapbuild_jobs)

    def test_SnapBuildJobPruner_doesnt_prune_most_recent_job_for_build(self):
        # Garbo doesn't remove the most recent job for a build.
        switch_dbuser("testadmin")
        store = IPrimaryStore(Job)

        snapbuild = self.factory.makeSnapBuild()
        snapbuild_job = SnapStoreUploadJob.create(snapbuild)
        snapbuild_job.job.date_finished = THIRTY_DAYS_AGO

        self.assertEqual(1, store.find(SnapBuildJob).count())

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(1, store.find(SnapBuildJob).count())

    def test_OCIFileJobPruner(self):
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        # Garbo removes files that haven't been used in 7 days
        switch_dbuser("testadmin")
        store = IPrimaryStore(OCIFile)
        ocifile = self.factory.makeOCIFile()
        removeSecurityProxy(ocifile).date_last_used = THIRTY_DAYS_AGO
        self.assertEqual(1, store.find(OCIFile).count())

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(0, store.find(OCIFile).count())

    def test_OCIFileJobPruner_doesnt_prune_recent(self):
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        # Garbo removes files that haven't been used in 7 days
        switch_dbuser("testadmin")
        store = IPrimaryStore(OCIFile)
        self.factory.makeOCIFile()
        self.assertEqual(1, store.find(OCIFile).count())

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(1, store.find(OCIFile).count())

    def test_OCIFileJobPruner_mixed_dates(self):
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        # Garbo removes files that haven't been used in 7 days
        switch_dbuser("testadmin")
        store = IPrimaryStore(OCIFile)
        ocifile = self.factory.makeOCIFile()
        removeSecurityProxy(ocifile).date_last_used = THIRTY_DAYS_AGO
        self.factory.makeOCIFile()
        self.assertEqual(2, store.find(OCIFile).count())

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(1, store.find(OCIFile).count())

    def test_GitRepositoryPruner_removes_stale_creations(self):
        # Garbo removes GitRepository with status = CREATING for too long.
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        switch_dbuser("testadmin")
        store = IPrimaryStore(GitRepository)
        now = datetime.now(timezone.utc)
        recently = now - timedelta(minutes=2)
        long_ago = now - timedelta(minutes=65)

        # Creating a bunch of old stale repositories to be deleted,
        # to make sure the chunk size is being respected.
        for _ in range(5):
            repo = removeSecurityProxy(self.factory.makeGitRepository())
            [ref1, ref2] = self.factory.makeGitRefs(
                repository=repo, paths=["refs/heads/a-20.04", "b"]
            )
            self.factory.makeBranchMergeProposalForGit(
                target_ref=ref1, source_ref=ref2
            )
            self.factory.makeSourcePackageRecipe(branches=[ref1])
            self.factory.makeSnap(git_ref=ref2)
            self.factory.makeOCIRecipe(git_ref=ref1)
            repo.date_created = long_ago
            repo.status = GitRepositoryStatus.CREATING
            long_ago += timedelta(seconds=1)

        # Create an old stale private repository as well.
        repo = removeSecurityProxy(
            self.factory.makeGitRepository(
                information_type=InformationType.USERDATA
            )
        )
        repo.date_created = long_ago
        repo.status = GitRepositoryStatus.CREATING
        long_ago += timedelta(seconds=1)

        recent_creating, old_available, recent_available = (
            removeSecurityProxy(self.factory.makeGitRepository())
            for _ in range(3)
        )

        recent_creating.date_created = recently
        recent_creating.status = GitRepositoryStatus.CREATING

        old_available.date_created = long_ago
        old_available.status = GitRepositoryStatus.AVAILABLE

        recent_available.date_created = recently
        recent_available.status = GitRepositoryStatus.AVAILABLE

        self.assertEqual(9, store.find(GitRepository).count())

        self.runHourly(maximum_chunk_size=2)

        switch_dbuser("testadmin")
        remaining_repos = store.find(GitRepository)
        self.assertEqual(3, remaining_repos.count())
        self.assertEqual(
            {old_available, recent_available, recent_creating},
            set(remaining_repos),
        )

    def test_WebhookJobPruner(self):
        # Garbo should remove jobs completed over 30 days ago.
        switch_dbuser("testadmin")

        webhook = self.factory.makeWebhook()
        job1 = webhook.ping()
        removeSecurityProxy(job1).job.date_finished = THIRTY_DAYS_AGO
        job2 = webhook.ping()
        removeSecurityProxy(job2).job.date_finished = SEVEN_DAYS_AGO

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(webhook, job2.webhook)
        self.assertRaises(LostObjectError, getattr, job1, "webhook")

    def test_ObsoleteBugAttachmentPruner(self):
        # Bug attachments without a LibraryFileContent record are removed.

        switch_dbuser("testadmin")
        bug = self.factory.makeBug()
        attachment = self.factory.makeBugAttachment(bug=bug)
        # Attachments with URLs are never deleted.
        self.factory.makeBugAttachment(bug=bug, url="https://launchpad.net")
        transaction.commit()

        # Bug attachments that have a LibraryFileContent record are
        # not deleted.
        self.assertIsNot(attachment.libraryfile.content, None)
        self.runDaily()
        self.assertEqual(bug.attachments.count(), 2)

        # But once we delete the LfC record, the attachment is deleted
        # in the next daily garbo run.
        switch_dbuser("testadmin")
        removeSecurityProxy(attachment.libraryfile).content = None
        transaction.commit()
        self.runDaily()
        switch_dbuser("testadmin")
        self.assertEqual(bug.attachments.count(), 1)

    def test_TimeLimitedTokenPruner(self):
        # Ensure there are no tokens
        store = sqlbase.session_store()
        map(store.remove, store.find(TimeLimitedToken))
        store.flush()
        self.assertEqual(
            0, len(list(store.find(TimeLimitedToken, path="sample path")))
        )
        # One to clean and one to keep
        store.add(
            TimeLimitedToken(
                path="sample path",
                token=b"foo",
                created=datetime(2008, 1, 1, tzinfo=timezone.utc),
            )
        )
        store.add(TimeLimitedToken(path="sample path", token=b"bar")),
        store.commit()
        self.assertEqual(
            2, len(list(store.find(TimeLimitedToken, path="sample path")))
        )
        self.runDaily()
        self.assertEqual(
            0,
            len(
                list(
                    store.find(
                        TimeLimitedToken,
                        path="sample path",
                        token=hashlib.sha256(b"foo").hexdigest(),
                    )
                )
            ),
        )
        self.assertEqual(
            1,
            len(
                list(
                    store.find(
                        TimeLimitedToken,
                        path="sample path",
                        token=hashlib.sha256(b"bar").hexdigest(),
                    )
                )
            ),
        )

    def test_CacheSuggestivePOTemplates(self):
        switch_dbuser("testadmin")
        template = self.factory.makePOTemplate()
        self.runDaily()

        (count,) = (
            IPrimaryStore(CommercialSubscription)
            .execute(
                """
            SELECT count(*)
            FROM SuggestivePOTemplate
            WHERE potemplate = %s
            """
                % sqlbase.quote(template.id)
            )
            .get_one()
        )

        self.assertEqual(1, count)

    def test_BugSummaryJournalRollup(self):
        switch_dbuser("testadmin")
        store = IPrimaryStore(CommercialSubscription)

        # Generate a load of entries in BugSummaryJournal.
        store.execute("UPDATE BugTask SET status=42")

        # We only need a few to test.
        num_rows = store.execute(
            "SELECT COUNT(*) FROM BugSummaryJournal"
        ).get_one()[0]
        self.assertThat(num_rows, GreaterThan(10))

        self.runFrequently()

        # We just care that the rows have been removed. The bugsummary
        # tests confirm that the rollup stored method is working correctly.
        num_rows = store.execute(
            "SELECT COUNT(*) FROM BugSummaryJournal"
        ).get_one()[0]
        self.assertThat(num_rows, Equals(0))

    def test_UnusedPOTMsgSetPruner_removes_obsolete_message_sets(self):
        # UnusedPOTMsgSetPruner removes any POTMsgSet that are
        # participating in a POTemplate only as obsolete messages.
        switch_dbuser("testadmin")
        pofile = self.factory.makePOFile()
        translation_message = self.factory.makeCurrentTranslationMessage(
            pofile=pofile
        )
        translation_message.potmsgset.setSequence(pofile.potemplate, 0)
        transaction.commit()
        store = IPrimaryStore(POTMsgSet)
        obsolete_msgsets = store.find(
            POTMsgSet,
            TranslationTemplateItem.potmsgset == POTMsgSet.id,
            TranslationTemplateItem.sequence == 0,
        )
        self.assertNotEqual(0, obsolete_msgsets.count())
        self.runDaily()
        self.assertEqual(0, obsolete_msgsets.count())

    def test_UnusedPOTMsgSetPruner_preserves_used_potmsgsets(self):
        # UnusedPOTMsgSetPruner will not remove a potmsgset if it changes
        # between calls.
        switch_dbuser("testadmin")
        potmsgset_pofile = {}
        for _ in range(4):
            pofile = self.factory.makePOFile()
            translation_message = self.factory.makeCurrentTranslationMessage(
                pofile=pofile
            )
            translation_message.potmsgset.setSequence(pofile.potemplate, 0)
            potmsgset_pofile[translation_message.potmsgset.id] = pofile.id
        transaction.commit()
        store = IPrimaryStore(POTMsgSet)
        test_ids = list(potmsgset_pofile)
        obsolete_msgsets = store.find(
            POTMsgSet,
            In(TranslationTemplateItem.potmsgset_id, test_ids),
            TranslationTemplateItem.sequence == 0,
        )
        self.assertEqual(4, obsolete_msgsets.count())
        pruner = UnusedPOTMsgSetPruner(self.log)
        pruner(2)
        # A potmsgset is set to a sequence > 0 between batches/commits.
        last_id = pruner.msgset_ids_to_remove[-1]
        used_potmsgset = store.find(POTMsgSet, POTMsgSet.id == last_id).one()
        used_pofile = store.find(
            POFile, POFile.id == potmsgset_pofile[last_id]
        ).one()
        translation_message = self.factory.makeCurrentTranslationMessage(
            pofile=used_pofile, potmsgset=used_potmsgset
        )
        used_potmsgset.setSequence(used_pofile.potemplate, 1)
        transaction.commit()
        # Next batch.
        pruner(2)
        self.assertEqual(0, obsolete_msgsets.count())
        preserved_msgsets = store.find(
            POTMsgSet, In(TranslationTemplateItem.potmsgset_id, test_ids)
        )
        self.assertEqual(1, preserved_msgsets.count())

    def test_UnusedPOTMsgSetPruner_removes_unreferenced_message_sets(self):
        # If a POTMsgSet is not referenced by any templates the
        # UnusedPOTMsgSetPruner will remove it.
        switch_dbuser("testadmin")
        potmsgset = self.factory.makePOTMsgSet()
        # Cheekily drop any references to the POTMsgSet we just created.
        store = IPrimaryStore(POTMsgSet)
        store.execute(
            "DELETE FROM TranslationTemplateItem WHERE potmsgset = %s"
            % potmsgset.id
        )
        transaction.commit()
        unreferenced_msgsets = store.find(
            POTMsgSet,
            Not(
                In(
                    POTMsgSet.id,
                    SQL("SELECT potmsgset FROM TranslationTemplateItem"),
                )
            ),
        )
        self.assertNotEqual(0, unreferenced_msgsets.count())
        self.runDaily()
        self.assertEqual(0, unreferenced_msgsets.count())

    def test_BugHeatUpdater_sees_feature_flag(self):
        # BugHeatUpdater can see its feature flag even though it's
        # running in a thread. garbo sets up a feature controller for
        # each worker.
        switch_dbuser("testadmin")
        bug = self.factory.makeBug()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=1)
        old_update = now - timedelta(days=2)
        naked_bug = removeSecurityProxy(bug)
        naked_bug.heat_last_updated = old_update
        IPrimaryStore(FeatureFlag).add(
            FeatureFlag(
                "default", 0, "bugs.heat_updates.cutoff", cutoff.isoformat()
            )
        )
        transaction.commit()
        self.assertEqual(old_update, naked_bug.heat_last_updated)
        self.runHourly()
        self.assertNotEqual(old_update, naked_bug.heat_last_updated)

    def getAccessPolicyTypes(self, pillar):
        return [
            ap.type
            for ap in getUtility(IAccessPolicySource).findByPillar([pillar])
        ]

    def test_UnusedProductAccessPolicyPruner(self):
        # UnusedProductAccessPolicyPruner removes access policies that
        # aren't in use by artifacts or allowed by the project sharing
        # policy.
        switch_dbuser("testadmin")
        product = self.factory.makeProduct()
        self.factory.makeCommercialSubscription(pillar=product)
        self.factory.makeAccessPolicy(product, InformationType.PROPRIETARY)
        naked_product = removeSecurityProxy(product)
        naked_product.bug_sharing_policy = BugSharingPolicy.PROPRIETARY
        naked_product.branch_sharing_policy = BranchSharingPolicy.PROPRIETARY
        [ap] = getUtility(IAccessPolicySource).find(
            [(product, InformationType.PRIVATESECURITY)]
        )
        self.factory.makeAccessPolicyArtifact(policy=ap)

        # Private and Private Security were created with the project.
        # Proprietary was created when the branch sharing policy was set.
        self.assertContentEqual(
            [
                InformationType.PRIVATESECURITY,
                InformationType.USERDATA,
                InformationType.PROPRIETARY,
            ],
            self.getAccessPolicyTypes(product),
        )

        self.runDaily()

        # Proprietary is permitted by the sharing policy, and there's a
        # Private Security artifact. But Private isn't in use or allowed
        # by a sharing policy, so garbo deleted it.
        self.assertContentEqual(
            [InformationType.PRIVATESECURITY, InformationType.PROPRIETARY],
            self.getAccessPolicyTypes(product),
        )

    def test_UnusedDistributionAccessPolicyPruner(self):
        # UnusedDistributionAccessPolicyPruner removes access policies that
        # aren't in use by artifacts or allowed by the distribution sharing
        # policy.
        switch_dbuser("testadmin")
        distribution = self.factory.makeProduct()
        self.factory.makeCommercialSubscription(pillar=distribution)
        self.factory.makeAccessPolicy(
            distribution, InformationType.PROPRIETARY
        )
        naked_distribution = removeSecurityProxy(distribution)
        naked_distribution.bug_sharing_policy = BugSharingPolicy.PROPRIETARY
        naked_distribution.branch_sharing_policy = (
            BranchSharingPolicy.PROPRIETARY
        )
        [ap] = getUtility(IAccessPolicySource).find(
            [(distribution, InformationType.PRIVATESECURITY)]
        )
        self.factory.makeAccessPolicyArtifact(policy=ap)

        # Private and Private Security were created with the distribution.
        # Proprietary was created when the branch sharing policy was set.
        self.assertContentEqual(
            [
                InformationType.PRIVATESECURITY,
                InformationType.USERDATA,
                InformationType.PROPRIETARY,
            ],
            self.getAccessPolicyTypes(distribution),
        )

        self.runDaily()

        # Proprietary is permitted by the sharing policy, and there's a
        # Private Security artifact. But Private isn't in use or allowed
        # by a sharing policy, so garbo deleted it.
        self.assertContentEqual(
            [InformationType.PRIVATESECURITY, InformationType.PROPRIETARY],
            self.getAccessPolicyTypes(distribution),
        )

    def test_ProductVCSPopulator(self):
        switch_dbuser("testadmin")
        product = self.factory.makeProduct()
        self.assertIs(None, product.vcs)

        with admin_logged_in():
            repo = self.factory.makeGitRepository(target=product)
            getUtility(IGitRepositorySet).setDefaultRepository(
                target=product, repository=repo
            )

        self.runDaily()

        self.assertEqual(VCSType.GIT, product.vcs)

    def test_ProductVCSPopulator_findProducts_filters_correctly(self):
        switch_dbuser("testadmin")

        # Create 2 products: one with VCS set, and another one without.
        product = self.factory.makeProduct()
        self.assertIsNone(product.vcs)

        product_with_vcs = self.factory.makeProduct(vcs=VCSType.GIT)
        self.assertIsNotNone(product_with_vcs.vcs)

        populator = ProductVCSPopulator(None)
        # Consider only products created by this test.
        populator.start_at = product.id

        rs = populator.findProducts()
        self.assertEqual(1, rs.count())
        self.assertEqual(product, rs.one())

    def test_PopulateDistributionSourcePackageCache(self):
        switch_dbuser("testadmin")
        # Make some test data.  We create source publications for different
        # distributions and archives.
        distributions = []
        for _ in range(2):
            distribution = self.factory.makeDistribution()
            self.factory.makeDistroSeries(distribution=distribution)
            distributions.append(distribution)
        archives = []
        for distribution in distributions:
            archives.append(distribution.main_archive)
            archives.append(
                self.factory.makeArchive(distribution=distribution)
            )
        spns = [self.factory.makeSourcePackageName() for _ in range(2)]
        spphs = []
        for spn in spns:
            for archive in archives:
                spphs.append(
                    self.factory.makeSourcePackagePublishingHistory(
                        distroseries=archive.distribution.currentseries,
                        archive=archive,
                        status=PackagePublishingStatus.PUBLISHED,
                        sourcepackagename=spn,
                    )
                )
        transaction.commit()

        self.runFrequently()

        def _assert_cached_names(spns, archive):
            dspcs = DistributionSourcePackageCache._find(
                archive.distribution, archive
            )
            self.assertContentEqual(
                spns, [dspc.sourcepackagename for dspc in dspcs]
            )
            if archive.is_main:
                for spn in spns:
                    self.assertEqual(
                        1,
                        archive.distribution.searchSourcePackages(
                            spn.name
                        ).count(),
                    )

        def _assert_last_spph_id(spph_id):
            job_data = load_garbo_job_state(
                "PopulateDistributionSourcePackageCache"
            )
            self.assertEqual(spph_id, job_data["last_spph_id"])

        for archive in archives:
            _assert_cached_names(spns, archive)
        _assert_last_spph_id(spphs[-1].id)

        # Create some more publications.  Those with PENDING or PUBLISHED
        # status are inserted into the cache, while others are ignored.
        switch_dbuser("testadmin")
        spphs.append(
            self.factory.makeSourcePackagePublishingHistory(
                distroseries=archives[0].distribution.currentseries,
                archive=archives[0],
                status=PackagePublishingStatus.PENDING,
            )
        )
        spphs.append(
            self.factory.makeSourcePackagePublishingHistory(
                distroseries=archives[0].distribution.currentseries,
                archive=archives[0],
                status=PackagePublishingStatus.SUPERSEDED,
            )
        )
        transaction.commit()

        self.runFrequently()

        _assert_cached_names(spns + [spphs[-2].sourcepackagename], archives[0])
        for archive in archives[1:]:
            _assert_cached_names(spns, archive)
        _assert_last_spph_id(spphs[-2].id)

    def test_PopulateLatestPersonSourcePackageReleaseCache(self):
        switch_dbuser("testadmin")
        # Make some same test data - we create published source package
        # releases for three different creators and maintainers.
        creators = []
        for _ in range(3):
            creators.append(self.factory.makePerson())
        maintainers = []
        for _ in range(3):
            maintainers.append(self.factory.makePerson())

        spn = self.factory.makeSourcePackageName()
        distroseries = self.factory.makeDistroSeries()
        spr1 = self.factory.makeSourcePackageRelease(
            creator=creators[0],
            maintainer=maintainers[0],
            distroseries=distroseries,
            sourcepackagename=spn,
            date_uploaded=datetime(2010, 12, 1, tzinfo=timezone.utc),
        )
        self.factory.makeSourcePackagePublishingHistory(
            status=PackagePublishingStatus.PUBLISHED, sourcepackagerelease=spr1
        )
        spr2 = self.factory.makeSourcePackageRelease(
            creator=creators[0],
            maintainer=maintainers[1],
            distroseries=distroseries,
            sourcepackagename=spn,
            date_uploaded=datetime(2010, 12, 2, tzinfo=timezone.utc),
        )
        self.factory.makeSourcePackagePublishingHistory(
            status=PackagePublishingStatus.PUBLISHED, sourcepackagerelease=spr2
        )
        spr3 = self.factory.makeSourcePackageRelease(
            creator=creators[1],
            maintainer=maintainers[0],
            distroseries=distroseries,
            sourcepackagename=spn,
            date_uploaded=datetime(2010, 12, 3, tzinfo=timezone.utc),
        )
        self.factory.makeSourcePackagePublishingHistory(
            status=PackagePublishingStatus.PUBLISHED, sourcepackagerelease=spr3
        )
        spr4 = self.factory.makeSourcePackageRelease(
            creator=creators[1],
            maintainer=maintainers[1],
            distroseries=distroseries,
            sourcepackagename=spn,
            date_uploaded=datetime(2010, 12, 4, tzinfo=timezone.utc),
        )
        self.factory.makeSourcePackagePublishingHistory(
            status=PackagePublishingStatus.PUBLISHED, sourcepackagerelease=spr4
        )
        spr5 = self.factory.makeSourcePackageRelease(
            creator=creators[2],
            maintainer=maintainers[2],
            distroseries=distroseries,
            sourcepackagename=spn,
            date_uploaded=datetime(2010, 12, 5, tzinfo=timezone.utc),
        )
        spph_1 = self.factory.makeSourcePackagePublishingHistory(
            status=PackagePublishingStatus.PUBLISHED, sourcepackagerelease=spr5
        )

        # Some of the creators and maintainers have inactive accounts.
        removeSecurityProxy(creators[2].account).status = (
            AccountStatus.DEACTIVATED
        )
        removeSecurityProxy(maintainers[2].account).status = (
            AccountStatus.CLOSED
        )

        transaction.commit()
        self.runFrequently()

        store = IPrimaryStore(LatestPersonSourcePackageReleaseCache)
        # Check that the garbo state table has data.
        self.assertIsNotNone(
            store.execute(
                "SELECT * FROM GarboJobState WHERE name=?",
                params=["PopulateLatestPersonSourcePackageReleaseCache"],
            ).get_one()
        )

        def _assert_releases_by_creator(creator, sprs):
            release_records = store.find(
                LatestPersonSourcePackageReleaseCache,
                LatestPersonSourcePackageReleaseCache.creator_id == creator.id,
            )
            self.assertThat(
                list(release_records),
                MatchesListwise(
                    [
                        MatchesStructure(
                            creator=Equals(spr.creator),
                            maintainer_id=Is(None),
                            dateuploaded=Equals(spr.dateuploaded),
                        )
                        for spr in sprs
                    ]
                ),
            )

        def _assert_releases_by_maintainer(maintainer, sprs):
            release_records = store.find(
                LatestPersonSourcePackageReleaseCache,
                LatestPersonSourcePackageReleaseCache.maintainer_id
                == maintainer.id,
            )
            self.assertThat(
                list(release_records),
                MatchesListwise(
                    [
                        MatchesStructure(
                            maintainer=Equals(spr.maintainer),
                            creator_id=Is(None),
                            dateuploaded=Equals(spr.dateuploaded),
                        )
                        for spr in sprs
                    ]
                ),
            )

        _assert_releases_by_creator(creators[0], [spr2])
        _assert_releases_by_creator(creators[1], [spr4])
        _assert_releases_by_creator(creators[2], [])
        _assert_releases_by_maintainer(maintainers[0], [spr3])
        _assert_releases_by_maintainer(maintainers[1], [spr4])
        _assert_releases_by_maintainer(maintainers[2], [])

        job_data = load_garbo_job_state(
            "PopulateLatestPersonSourcePackageReleaseCache"
        )
        self.assertEqual(spph_1.id, job_data["last_spph_id"])

        # Create a newer published source package release and ensure the
        # release cache table is correctly updated.
        switch_dbuser("testadmin")
        spr6 = self.factory.makeSourcePackageRelease(
            creator=creators[1],
            maintainer=maintainers[1],
            distroseries=distroseries,
            sourcepackagename=spn,
            date_uploaded=datetime(2010, 12, 5, tzinfo=timezone.utc),
        )
        spph_2 = self.factory.makeSourcePackagePublishingHistory(
            status=PackagePublishingStatus.PUBLISHED, sourcepackagerelease=spr6
        )

        transaction.commit()
        self.runFrequently()

        _assert_releases_by_creator(creators[0], [spr2])
        _assert_releases_by_creator(creators[1], [spr6])
        _assert_releases_by_creator(creators[2], [])
        _assert_releases_by_maintainer(maintainers[0], [spr3])
        _assert_releases_by_maintainer(maintainers[1], [spr6])
        _assert_releases_by_maintainer(maintainers[2], [])

        job_data = load_garbo_job_state(
            "PopulateLatestPersonSourcePackageReleaseCache"
        )
        self.assertEqual(spph_2.id, job_data["last_spph_id"])

    def _test_LiveFSFilePruner(
        self,
        content_type,
        interval,
        keep_binary_files_days=_default,
        base_image=False,
        expected_count=0,
        **livefsbuild_kwargs,
    ):
        # Garbo should (or should not, if `expected_count=1`) remove LiveFS
        # files of MIME type `content_type` that finished more than
        # `interval` days ago.  If `keep_binary_files_days` is given, set
        # that on the test LiveFS.  If `base_image` is True, install the
        # test LiveFS file as a base image for its DAS.
        now = datetime.now(timezone.utc)
        switch_dbuser("testadmin")
        self.useFixture(FeatureFixture({LIVEFS_FEATURE_FLAG: "on"}))
        store = IPrimaryStore(LiveFSFile)
        initial_count = store.find(LiveFSFile).count()

        livefsbuild_kwargs = dict(livefsbuild_kwargs)
        if keep_binary_files_days is not _default:
            livefsbuild_kwargs["keep_binary_files_days"] = (
                keep_binary_files_days
            )
        db_build = self.factory.makeLiveFSBuild(
            date_created=now - timedelta(days=interval, minutes=15),
            status=BuildStatus.FULLYBUILT,
            duration=timedelta(minutes=10),
            **livefsbuild_kwargs,
        )
        db_lfa = self.factory.makeLibraryFileAlias(content_type=content_type)
        db_file = self.factory.makeLiveFSFile(
            livefsbuild=db_build, libraryfile=db_lfa
        )
        if base_image:
            db_build.distro_arch_series.setChrootFromBuild(
                db_build, db_file.libraryfile.filename
            )
        store.flush()

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(
            initial_count + expected_count, store.find(LiveFSFile).count()
        )

    def test_LiveFSFilePruner_old_binary_files(self):
        # By default, LiveFS binary files attached to builds over a day old
        # are pruned.
        self._test_LiveFSFilePruner("application/octet-stream", 1)

    def test_LiveFSFilePruner_old_text_files(self):
        # LiveFS text files attached to builds over a day old are retained.
        self._test_LiveFSFilePruner("text/plain", 1, expected_count=1)

    def test_LiveFSFilePruner_recent_binary_files(self):
        # LiveFS binary files attached to builds less than a day old are
        # retained.
        self._test_LiveFSFilePruner(
            "application/octet-stream", 0, expected_count=1
        )

    def test_LiveFSFilePruner_custom_interval_old_binary_files(self):
        # If a custom retention interval is set, LiveFS binary files
        # attached to builds over that interval old are pruned.
        self._test_LiveFSFilePruner(
            "application/octet-stream", 7, keep_binary_files_days=7
        )

    def test_LiveFSFilePruner_custom_interval_recent_binary_files(self):
        # If a custom retention interval is set, LiveFS binary files
        # attached to builds less than that interval old are pruned.
        self._test_LiveFSFilePruner(
            "application/octet-stream",
            6,
            keep_binary_files_days=7,
            expected_count=1,
        )

    def test_LiveFSFilePruner_null_interval_disables_pruning(self):
        # A null retention interval disables pruning.
        self._test_LiveFSFilePruner(
            "application/octet-stream",
            100,
            keep_binary_files_days=None,
            expected_count=1,
        )

    def test_LiveFSFilePruner_base_image(self):
        # An old LiveFS binary file is not pruned if it is a base image.
        self._test_LiveFSFilePruner(
            "application/octet-stream", 100, base_image=True, expected_count=1
        )

    def test_LiveFSFilePruner_other_base_image(self):
        # An old LiveFS binary file is pruned even if some other base image
        # exists.
        switch_dbuser("testadmin")
        self.useFixture(FeatureFixture({LIVEFS_FEATURE_FLAG: "on"}))
        store = IPrimaryStore(LiveFSFile)
        other_build = self.factory.makeLiveFSBuild(
            status=BuildStatus.FULLYBUILT, duration=timedelta(minutes=10)
        )
        other_lfa = self.factory.makeLibraryFileAlias(
            content_type="application/octet-stream"
        )
        other_file = self.factory.makeLiveFSFile(
            livefsbuild=other_build, libraryfile=other_lfa
        )
        other_build.distro_arch_series.setChrootFromBuild(
            other_build, other_file.libraryfile.filename
        )
        store.flush()
        self._test_LiveFSFilePruner(
            "application/octet-stream",
            100,
            distroarchseries=other_build.distro_arch_series,
        )
        self.assertContentEqual([other_file], store.find(LiveFSFile))

    def _test_SnapFilePruner(
        self, filenames, job_status, interval, expected_count=0
    ):
        # Garbo should (or should not, if `expected_count` is non-zero)
        # remove snap files named each of `filenames` with a store upload
        # job of status `job_status` that finished more than `interval` days
        # ago.
        now = datetime.now(timezone.utc)
        switch_dbuser("testadmin")
        store = IPrimaryStore(SnapFile)

        db_build = self.factory.makeSnapBuild(
            date_created=now - timedelta(days=interval, minutes=15),
            status=BuildStatus.FULLYBUILT,
            duration=timedelta(minutes=10),
        )
        for filename in filenames:
            db_lfa = self.factory.makeLibraryFileAlias(filename=filename)
            self.factory.makeSnapFile(snapbuild=db_build, libraryfile=db_lfa)
        if job_status is not None:
            db_build_job = SnapStoreUploadJob.create(db_build)
            db_build_job.job._status = job_status
            db_build_job.job.date_finished = now - timedelta(
                days=interval, minutes=5
            )
        Store.of(db_build).flush()
        self.assertEqual(len(filenames), store.find(SnapFile).count())

        self.runDaily()

        switch_dbuser("testadmin")
        self.assertEqual(expected_count, store.find(SnapFile).count())

    def test_SnapFilePruner_old_snap_files(self):
        # Snap files attached to builds over 7 days old that have been
        # uploaded to the store are pruned.
        self._test_SnapFilePruner(
            ["foo.snap", "foo.debug"], JobStatus.COMPLETED, 7
        )

    def test_SnapFilePruner_old_non_snap_files(self):
        # Non-snap files attached to builds over 7 days old that have been
        # uploaded to the store are retained.
        self._test_SnapFilePruner(
            ["foo.snap", "foo.tar.gz"],
            JobStatus.COMPLETED,
            7,
            expected_count=1,
        )

    def test_SnapFilePruner_recent_binary_files(self):
        # Snap binary files attached to builds less than 7 days old that
        # have been uploaded to the store are retained.
        self._test_SnapFilePruner(
            ["foo.snap", "foo.debug"], JobStatus.COMPLETED, 6, expected_count=2
        )

    def test_SnapFilePruner_binary_files_failed_to_upload(self):
        # Snap binary files attached to builds that failed to be uploaded to
        # the store are retained.
        self._test_SnapFilePruner(
            ["foo.snap", "foo.debug"], JobStatus.FAILED, 7, expected_count=2
        )

    def test_SnapFilePruner_binary_files_no_upload_job(self):
        # Snap binary files attached to builds with no store upload job are
        # retained.
        self._test_SnapFilePruner(
            ["foo.snap", "foo.debug"], None, 7, expected_count=2
        )

    def test_ArchiveSubscriptionExpirer_expires_subscriptions(self):
        # Archive subscriptions with expiry dates in the past have their
        # statuses set to EXPIRED.
        switch_dbuser("testadmin")
        ppa = self.factory.makeArchive(private=True)
        subs = [
            ppa.newSubscription(self.factory.makePerson(), ppa.owner)
            for _ in range(2)
        ]
        now = datetime.now(timezone.utc)
        subs[0].date_expires = now - timedelta(minutes=3)
        self.assertEqual(ArchiveSubscriberStatus.CURRENT, subs[0].status)
        subs[1].date_expires = now + timedelta(minutes=3)
        self.assertEqual(ArchiveSubscriberStatus.CURRENT, subs[1].status)
        Store.of(subs[0]).flush()

        self.runFrequently()

        switch_dbuser("testadmin")
        self.assertEqual(ArchiveSubscriberStatus.EXPIRED, subs[0].status)
        self.assertEqual(ArchiveSubscriberStatus.CURRENT, subs[1].status)

    def test_ArchiveAuthTokenDeactivator_ignores_named_tokens(self):
        switch_dbuser("testadmin")
        self.useFixture(FeatureFixture({NAMED_AUTH_TOKEN_FEATURE_FLAG: "on"}))
        ppa = self.factory.makeArchive(private=True)
        named_tokens = [
            ppa.newNamedAuthToken(self.factory.getUniqueUnicode())
            for _ in range(2)
        ]
        named_tokens[1].deactivate()

        self.runHourly()

        switch_dbuser("testadmin")
        self.assertIsNone(named_tokens[0].date_deactivated)

    def test_ArchiveAuthTokenDeactivator_leave_subscribed_team(self):
        # Somebody who leaves a subscribed team loses their token, but other
        # team members keep theirs.
        switch_dbuser("testadmin")
        ppa = self.factory.makeArchive(private=True)
        team = self.factory.makeTeam()
        people = []
        for _ in range(3):
            person = self.factory.makePerson()
            team.addMember(person, team.teamowner)
            people.append(person)
        ppa.newSubscription(team, ppa.owner)
        tokens = {person: ppa.newAuthToken(person) for person in people}

        self.runHourly()
        switch_dbuser("testadmin")
        for person in tokens:
            self.assertIsNone(tokens[person].date_deactivated)

        people[0].leave(team)
        # Clear out emails generated when leaving a team.
        pop_notifications()

        self.runHourly()
        switch_dbuser("testadmin")
        self.assertIsNotNone(tokens[people[0]].date_deactivated)
        del tokens[people[0]]
        for person in tokens:
            self.assertIsNone(tokens[person].date_deactivated)

        # A cancellation email was sent.
        self.assertEmailQueueLength(1)

    def test_ArchiveAuthTokenDeactivator_leave_only_one_subscribed_team(self):
        # Somebody who leaves a subscribed team retains their token if they
        # are still subscribed via another team.
        switch_dbuser("testadmin")
        ppa = self.factory.makeArchive(private=True)
        teams = [self.factory.makeTeam() for _ in range(2)]
        people = []
        for _ in range(3):
            for team in teams:
                person = self.factory.makePerson()
                team.addMember(person, team.teamowner)
                people.append(person)
        parent_team = self.factory.makeTeam()
        parent_team.addMember(
            teams[1], parent_team.teamowner, force_team_add=True
        )
        multiple_teams_person = self.factory.makePerson()
        for team in teams:
            team.addMember(multiple_teams_person, team.teamowner)
        people.append(multiple_teams_person)
        ppa.newSubscription(teams[0], ppa.owner)
        ppa.newSubscription(parent_team, ppa.owner)
        tokens = {person: ppa.newAuthToken(person) for person in people}

        self.runHourly()
        switch_dbuser("testadmin")
        for person in tokens:
            self.assertIsNone(tokens[person].date_deactivated)

        multiple_teams_person.leave(teams[0])
        # Clear out emails generated when leaving a team.
        pop_notifications()

        self.runHourly()
        switch_dbuser("testadmin")
        self.assertIsNone(tokens[multiple_teams_person].date_deactivated)
        for person in tokens:
            self.assertIsNone(tokens[person].date_deactivated)

        # A cancellation email was not sent.
        self.assertEmailQueueLength(0)

    def test_ArchiveAuthTokenDeactivator_leave_indirect_subscription(self):
        # Members of a team that leaves a subscribed parent team lose their
        # tokens.
        switch_dbuser("testadmin")
        ppa = self.factory.makeArchive(private=True)
        child_team = self.factory.makeTeam()
        people = []
        for _ in range(3):
            person = self.factory.makePerson()
            child_team.addMember(person, child_team.teamowner)
            people.append(person)
        parent_team = self.factory.makeTeam()
        parent_team.addMember(
            child_team, parent_team.teamowner, force_team_add=True
        )
        directly_subscribed_person = self.factory.makePerson()
        people.append(directly_subscribed_person)
        ppa.newSubscription(parent_team, ppa.owner)
        ppa.newSubscription(directly_subscribed_person, ppa.owner)
        tokens = {person: ppa.newAuthToken(person) for person in people}

        self.runHourly()
        switch_dbuser("testadmin")
        for person in tokens:
            self.assertIsNone(tokens[person].date_deactivated)

        # child_team now leaves parent_team, and all its members lose their
        # tokens.
        parent_team.setMembershipData(
            child_team, TeamMembershipStatus.APPROVED, parent_team.teamowner
        )
        parent_team.setMembershipData(
            child_team, TeamMembershipStatus.DEACTIVATED, parent_team.teamowner
        )
        self.assertFalse(child_team.inTeam(parent_team))
        self.runHourly()
        switch_dbuser("testadmin")
        for person in people[:3]:
            self.assertIsNotNone(tokens[person].date_deactivated)

        # directly_subscribed_person still has their token; they're not in
        # any teams.
        self.assertIsNone(tokens[directly_subscribed_person].date_deactivated)

    def test_ArchiveAuthTokenDeactivator_cancellation_email(self):
        # When a token is deactivated, its user gets an email, which
        # contains the correct headers and body.
        switch_dbuser("testadmin")
        # Avoid hyphens in owner and archive names; while these work fine,
        # MailWrapper may wrap at hyphens, which makes it inconvenient to
        # write a precise test for the email body text.
        owner = self.factory.makePerson(
            name="someperson", displayname="Some Person"
        )
        ppa = self.factory.makeArchive(
            owner=owner, name="someppa", private=True
        )
        subscriber = self.factory.makePerson()
        subscription = ppa.newSubscription(subscriber, owner)
        token = ppa.newAuthToken(subscriber)
        subscription.cancel(owner)
        pop_notifications()

        self.runHourly()

        switch_dbuser("testadmin")
        self.assertIsNotNone(token.date_deactivated)
        [email] = pop_notifications()
        self.assertThat(
            dict(email),
            ContainsDict(
                {
                    "From": AfterPreprocessing(
                        partial(re.sub, r"\n[\t ]", " "),
                        Equals("%s <noreply@launchpad.net>" % ppa.displayname),
                    ),
                    "To": Equals(subscriber.preferredemail.email),
                    "Subject": AfterPreprocessing(
                        partial(re.sub, r"\n[\t ]", " "),
                        Equals(
                            "PPA access cancelled for %s" % ppa.displayname
                        ),
                    ),
                    "Sender": Equals("bounces@canonical.com"),
                }
            ),
        )
        expected_body = dedent(
            """\
            Hello {subscriber},

            Launchpad: cancellation of archive access
            -----------------------------------------

            Your access to the private software archive "{archive}", which
            is hosted by Launchpad, has been cancelled.

            You will now no longer be able to download software from this
            archive. If you think this cancellation is in error, you should
            contact the owner of the archive to verify it.

            You can contact the archive owner by visiting their Launchpad
            page here:

            <http://launchpad\\.test/~{archive_owner_name}>

            If you have any concerns you can contact the Launchpad team by
            emailing feedback@launchpad\\.net


            Regards,
            The Launchpad team
            """
        ).format(
            subscriber=subscriber.display_name,
            archive=ppa.displayname,
            archive_owner_name=owner.name,
        )
        self.assertTextMatchesExpressionIgnoreWhitespace(
            expected_body, email.get_payload()
        )

    def test_ArchiveAuthTokenDeactivator_suppressed_archive(self):
        # When a token is deactivated for an archive with
        # suppress_subscription_notifications set, no email is sent.
        switch_dbuser("testadmin")
        ppa = self.factory.makeArchive(
            private=True, suppress_subscription_notifications=True
        )
        subscriber = self.factory.makePerson()
        subscription = ppa.newSubscription(subscriber, ppa.owner)
        token = ppa.newAuthToken(subscriber)
        subscription.cancel(ppa.owner)
        pop_notifications()

        self.runHourly()

        switch_dbuser("testadmin")
        self.assertIsNotNone(token.date_deactivated)
        self.assertEmailQueueLength(0)

    def test_RevisionStatusReportPruner(self):
        # Artifacts for report1 are older than 90 days
        # and we expect the garbo job to delete them.
        # Artifacts for report2 are newer than 90 days and
        # we expect them to survive the garbo job.
        switch_dbuser("testadmin")
        now = datetime.now(timezone.utc)
        report1 = removeSecurityProxy(self.factory.makeRevisionStatusReport())
        report2 = self.factory.makeRevisionStatusReport()
        report1.date_created = now - timedelta(days=120)
        repo1 = report1.git_repository
        repo2 = report2.git_repository
        artifact1_1 = self.factory.makeRevisionStatusArtifact(
            report=report1, artifact_type=RevisionStatusArtifactType.BINARY
        )
        artifact1_2 = self.factory.makeRevisionStatusArtifact(
            report=report1, artifact_type=RevisionStatusArtifactType.BINARY
        )
        artifact1_3 = self.factory.makeRevisionStatusArtifact(report=report1)
        for _ in range(0, 5):
            self.factory.makeRevisionStatusArtifact(
                report=report2, artifact_type=RevisionStatusArtifactType.BINARY
            )
        reports_in_db = list(
            getUtility(IRevisionStatusReportSet).findByRepository(repo1)
        )
        self.assertEqual(1, len(reports_in_db))
        artifacts_db = list(
            getUtility(IRevisionStatusArtifactSet).findByReport(report1)
        )
        self.assertEqual(3, len(artifacts_db))
        self.assertItemsEqual(
            [artifact1_1, artifact1_2, artifact1_3], artifacts_db
        )

        self.runDaily()

        log = self.log_buffer.getvalue()
        self.assertIn(
            "[RevisionStatusReportPruner] "
            "RevisionStatusReportPruner completed successfully.",
            log,
        )
        artifacts_db = list(
            getUtility(IRevisionStatusArtifactSet).findByReport(report1)
        )
        # artifact1_1 and artifact1_2 which were type Binary should be gone
        # only artifact1_3 of type Log should have survived the garbo job
        self.assertEqual(1, len(artifacts_db))
        self.assertEqual([artifact1_3], artifacts_db)
        # ensure the report is still there
        self.assertEqual(
            [report1],
            list(getUtility(IRevisionStatusReportSet).findByRepository(repo1)),
        )

        # assert report2 and its artifacts remained intact
        artifacts_db = list(
            getUtility(IRevisionStatusArtifactSet).findByReport(report2)
        )
        self.assertEqual(5, len(artifacts_db))
        self.assertEqual(
            [report2],
            list(getUtility(IRevisionStatusReportSet).findByRepository(repo2)),
        )

    def test_ArchiveArtifactoryColumnsPopulator(self):
        switch_dbuser("testadmin")
        old_archives = [self.factory.makeArchive() for _ in range(2)]
        for archive in old_archives:
            removeSecurityProxy(archive)._publishing_method = None
            removeSecurityProxy(archive)._repository_format = None
        try:
            Store.of(old_archives[0]).flush()
        except IntegrityError:
            # Now enforced by DB NOT NULL constraint; backfilling is no
            # longer necessary.
            return
        local_debian_archives = [
            self.factory.makeArchive(
                publishing_method=ArchivePublishingMethod.LOCAL,
                repository_format=ArchiveRepositoryFormat.DEBIAN,
            )
            for _ in range(2)
        ]
        artifactory_python_archives = [
            self.factory.makeArchive(
                publishing_method=ArchivePublishingMethod.ARTIFACTORY,
                repository_format=ArchiveRepositoryFormat.PYTHON,
            )
            for _ in range(2)
        ]
        transaction.commit()

        self.runDaily()

        # Old archives are backfilled.
        for archive in old_archives:
            self.assertThat(
                removeSecurityProxy(archive),
                MatchesStructure.byEquality(
                    _publishing_method=ArchivePublishingMethod.LOCAL,
                    _repository_format=ArchiveRepositoryFormat.DEBIAN,
                ),
            )
        # Other archives are left alone.
        for archive in local_debian_archives:
            self.assertThat(
                removeSecurityProxy(archive),
                MatchesStructure.byEquality(
                    _publishing_method=ArchivePublishingMethod.LOCAL,
                    _repository_format=ArchiveRepositoryFormat.DEBIAN,
                ),
            )
        for archive in artifactory_python_archives:
            self.assertThat(
                removeSecurityProxy(archive),
                MatchesStructure.byEquality(
                    _publishing_method=ArchivePublishingMethod.ARTIFACTORY,
                    _repository_format=ArchiveRepositoryFormat.PYTHON,
                ),
            )

    def test_SourcePackagePublishingHistoryFormatPopulator(self):
        switch_dbuser("testadmin")
        old_spphs = [
            self.factory.makeSourcePackagePublishingHistory(
                format=SourcePackageType.DPKG
            )
            for _ in range(2)
        ]
        for spph in old_spphs:
            removeSecurityProxy(spph)._format = None
        try:
            Store.of(old_spphs[0]).flush()
        except IntegrityError:
            # Now enforced by DB NOT NULL constraint; backfilling is no
            # longer necessary.
            return
        dpkg_spphs = [
            self.factory.makeSourcePackagePublishingHistory(
                format=SourcePackageType.DPKG
            )
            for _ in range(2)
        ]
        rpm_spphs = [
            self.factory.makeSourcePackagePublishingHistory(
                format=SourcePackageType.RPM
            )
            for _ in range(2)
        ]
        transaction.commit()

        self.runDaily()

        # Old publications are backfilled.
        for spph in old_spphs:
            self.assertEqual(
                SourcePackageType.DPKG, removeSecurityProxy(spph)._format
            )
        # Other publications are left alone.
        for spph in dpkg_spphs:
            self.assertEqual(
                SourcePackageType.DPKG, removeSecurityProxy(spph)._format
            )
        for spph in rpm_spphs:
            self.assertEqual(
                SourcePackageType.RPM, removeSecurityProxy(spph)._format
            )

    def test_SourcePackageReleaseHomepagePopulator(self):
        switch_dbuser("testadmin")
        homepage = "https://example.com/foo"
        # 4 cases:
        # 1. A spr without a homepage with a dsc with a homepage (backfill)
        # 2. A spr without a homepage without a dsc (skip)
        # 3. A spr without a homepage with a dsc without a homepage (skip)
        # 4. A spr with an existing homepage (skip)
        with_homepage = self.factory.makeSourcePackageRelease(homepage=None)
        removeSecurityProxy(with_homepage).dsc = (
            "Format: 3.0 (quilt)\nSource: foo\nHomepage: %s\n" % homepage
        )
        without_dsc = self.factory.makeSourcePackageRelease(homepage=None)
        without_homepage_field = self.factory.makeSourcePackageRelease(
            homepage=None
        )
        removeSecurityProxy(without_homepage_field).dsc = (
            "Format: 3.0 (quilt)\nSource: foo\n"
        )
        existing = self.factory.makeSourcePackageRelease(
            homepage="https://example.com/keep"
        )
        removeSecurityProxy(existing).dsc = (
            "Format: 3.0 (quilt)\nSource: foo\nHomepage: %s\n" % homepage
        )
        transaction.commit()

        self.runDaily()

        self.assertEqual(homepage, with_homepage.homepage)
        self.assertIsNone(without_dsc.homepage)
        self.assertIsNone(without_homepage_field.homepage)
        self.assertEqual("https://example.com/keep", existing.homepage)

    def test_BinaryPackagePublishingHistoryFormatPopulator(self):
        switch_dbuser("testadmin")
        old_bpphs = [
            self.factory.makeBinaryPackagePublishingHistory(
                binpackageformat=BinaryPackageFormat.DEB
            )
            for _ in range(2)
        ]
        for bpph in old_bpphs:
            removeSecurityProxy(bpph)._format = None
        try:
            Store.of(old_bpphs[0]).flush()
        except IntegrityError:
            # Now enforced by DB NOT NULL constraint; backfilling is no
            # longer necessary.
            return
        deb_bpphs = [
            self.factory.makeBinaryPackagePublishingHistory(
                binpackageformat=BinaryPackageFormat.DEB
            )
            for _ in range(2)
        ]
        rpm_bpphs = [
            self.factory.makeBinaryPackagePublishingHistory(
                binpackageformat=BinaryPackageFormat.RPM
            )
            for _ in range(2)
        ]
        transaction.commit()

        self.runDaily()

        # Old publications are backfilled.
        for bpph in old_bpphs:
            self.assertEqual(
                BinaryPackageFormat.DEB,
                removeSecurityProxy(bpph)._binarypackageformat,
            )
        # Other publications are left alone.
        for bpph in deb_bpphs:
            self.assertEqual(
                BinaryPackageFormat.DEB,
                removeSecurityProxy(bpph)._binarypackageformat,
            )
        for bpph in rpm_bpphs:
            self.assertEqual(
                BinaryPackageFormat.RPM,
                removeSecurityProxy(bpph)._binarypackageformat,
            )

    def test_BinaryPackagePublishingHistorySPNPopulator(self):
        switch_dbuser("testadmin")
        bpphs = [
            self.factory.makeBinaryPackagePublishingHistory() for _ in range(2)
        ]
        spns = [bpph.sourcepackagename for bpph in bpphs]
        for bpph in bpphs:
            removeSecurityProxy(bpph).sourcepackagename = None
        ci_build = self.factory.makeCIBuild()
        wheel_bpr = ci_build.createBinaryPackageRelease(
            self.factory.makeBinaryPackageName(),
            "1.0",
            "test summary",
            "test description",
            BinaryPackageFormat.WHL,
            False,
        )
        bpphs.append(
            getUtility(IPublishingSet).publishBinaries(
                bpphs[0].archive,
                bpphs[0].distroseries,
                bpphs[0].pocket,
                {wheel_bpr: (None, None, None, None)},
            )[0]
        )
        transaction.commit()
        self.assertIs(None, bpph.sourcepackagename)

        self.runDaily()

        # Publications with BinaryPackageBuilds are backfilled.
        for bpph, spn in zip(bpphs, spns):
            self.assertEqual(spn, bpph.sourcepackagename)
        # Other publications are left alone.
        self.assertIsNone(bpphs[2].sourcepackagename)

    def test_ArchiveFileDatePopulator(self):
        switch_dbuser("testadmin")
        now = datetime.now(timezone.utc)
        archive_files = [self.factory.makeArchiveFile() for _ in range(2)]
        removeSecurityProxy(archive_files[1]).scheduled_deletion_date = (
            now + timedelta(hours=6)
        )

        self.runDaily()

        self.assertThat(
            archive_files,
            MatchesListwise(
                [
                    MatchesStructure(date_superseded=Is(None)),
                    MatchesStructure.byEquality(
                        date_superseded=now - timedelta(hours=18)
                    ),
                ]
            ),
        )

    def test_ArchiveFileDatePopulator_findArchiveFiles_filters_correctly(self):
        switch_dbuser("testadmin")

        # Create three ArchiveFiles: one with date_superseded set, one with
        # date_superseded unset and scheduled_deletion_date set, and one
        # with both unset.
        archive_files = [self.factory.makeArchiveFile() for _ in range(3)]

        Store.of(archive_files[0]).flush()
        getUtility(IArchiveFileSet).scheduleDeletion(
            [archive_files[0]], timedelta(days=1)
        )
        self.assertIsNotNone(archive_files[0].date_superseded)

        removeSecurityProxy(archive_files[1]).scheduled_deletion_date = (
            datetime.now(timezone.utc) + timedelta(days=1)
        )
        self.assertIsNone(archive_files[1].date_superseded)

        self.assertIsNone(archive_files[2].date_superseded)
        self.assertIsNone(archive_files[2].scheduled_deletion_date)

        populator = ArchiveFileDatePopulator(None)
        # Consider only ArchiveFiles created by this test.
        populator.start_at = archive_files[0].id

        rs = populator.findArchiveFiles()
        self.assertEqual(1, rs.count())
        self.assertEqual(archive_files[1], rs.one())

    def test_UpdatePPASigningKeyFingerprintToRSA4096Key(self):
        switch_dbuser("testadmin")
        person = self.factory.makePerson()
        archive = self.factory.makeArchive(owner=person)
        rsa1024_gpg_key = self.factory.makeGPGKey(owner=person, keysize=1024)
        archive.signing_key_fingerprint = rsa1024_gpg_key.fingerprint
        rsa4096_gpg_key = self.factory.makeGPGKey(owner=person, keysize=4096)
        rsa4096_signing_key = self.factory.makeSigningKey(
            key_type=SigningKeyType.OPENPGP,
            fingerprint=rsa4096_gpg_key.fingerprint,
        )
        self.factory.makeArchiveSigningKey(archive, None, rsa4096_signing_key)

        archive_2 = self.factory.makeArchive()
        rsa1024_gpg_key_2 = self.factory.makeGPGKey(owner=person, keysize=1024)
        archive_2.signing_key_fingerprint = rsa1024_gpg_key_2.fingerprint
        rsa4096_gpg_key_2 = self.factory.makeGPGKey(owner=person, keysize=4096)
        rsa4096_signing_key_2 = self.factory.makeSigningKey(
            key_type=SigningKeyType.OPENPGP,
            fingerprint=rsa4096_gpg_key_2.fingerprint,
        )
        self.factory.makeArchiveSigningKey(
            archive_2, None, rsa4096_signing_key_2
        )
        updater = UpdatePPASigningKeyFingerprintToRSA4096Key(log=None)
        self.assertEqual(2, updater.findAffectedArchives().count())
        self.runHourly()
        self.assertTrue(updater.findAffectedArchives().is_empty())
        self.assertEqual(
            rsa4096_gpg_key.fingerprint, archive.signing_key_fingerprint
        )
        self.assertEqual(
            rsa4096_gpg_key_2.fingerprint, archive_2.signing_key_fingerprint
        )

    def test_PopulateCveSequence(self):
        # PopulateCveSequence migrates bare CVE sequences to prefixed format.
        switch_dbuser("testadmin")

        # Create CVEs with bare sequences (to be migrated)
        bare_cve_1 = getUtility(ICveSet).new(
            sequence="2024-1234",
            description="Test vulnerability 1",
            status=CveStatus.CANDIDATE,
        )
        bare_cve_2 = getUtility(ICveSet).new(
            sequence="2023-5678",
            description="Test vulnerability 2",
            status=CveStatus.CANDIDATE,
        )

        # Create CVEs with prefixed sequences (should be untouched)
        prefixed_cve = getUtility(ICveSet).new(
            sequence="CVE-2025-9999",
            description="Already prefixed",
            status=CveStatus.CANDIDATE,
        )

        transaction.commit()

        # Verify initial state
        populator = PopulateCveSequence(log=None)
        bare_sequences = list(populator.findBareSequences())
        self.assertEqual(2, len(bare_sequences))

        # Run the garbo job directly
        populator = PopulateCveSequence(self.log)
        while not populator.isDone():
            populator(chunk_size=10)

        self.assertEqual("CVE-2024-1234", bare_cve_1.sequence)
        self.assertEqual("CVE-2023-5678", bare_cve_2.sequence)
        # Already-prefixed CVE should be unchanged
        self.assertEqual("CVE-2025-9999", prefixed_cve.sequence)

        # Verify the job is now done
        populator = PopulateCveSequence(log=None)
        self.assertTrue(populator.isDone())

    def test_PopulateCveSequence_migrates_bug_links(self):
        # Bug links are keyed by the CVE sequence in the XRef table, so the
        # migration must update them too or the "Related bugs" section on
        # the CVE page would disappear for migrated CVEs.
        switch_dbuser("testadmin")

        cve = getUtility(ICveSet).new(
            sequence="2011-2699",
            description="Linked vulnerability",
            status=CveStatus.CANDIDATE,
        )
        bug = self.factory.makeBug()
        cve.linkBug(bug, user=bug.owner)
        transaction.commit()

        self.assertEqual([bug.id], [b.id for b in cve.bugs])

        populator = PopulateCveSequence(self.log)
        while not populator.isDone():
            populator(chunk_size=10)

        self.assertEqual("CVE-2011-2699", cve.sequence)
        # The bug link still resolves after the sequence was migrated.
        self.assertEqual([bug.id], [b.id for b in cve.bugs])

    def test_PopulateCveSequence_findBareSequences_filters_correctly(self):
        # PopulateCveSequence.findBareSequences should only return CVEs with
        # bare sequences, not prefixed ones.
        switch_dbuser("testadmin")

        # Create CVEs with different sequence formats
        bare_cve = getUtility(ICveSet).new(
            sequence="2024-1111",
            description="Bare sequence",
            status=CveStatus.CANDIDATE,
        )

        transaction.commit()

        prefixed_cve = getUtility(ICveSet).new(
            sequence="CVE-2024-2222",
            description="Prefixed sequence",
            status=CveStatus.CANDIDATE,
        )

        transaction.commit()

        populator = PopulateCveSequence(log=None)
        # Set start_at to only consider CVEs created by this test
        populator.start_at = bare_cve.id

        # Should only find the bare sequence, not the prefixed one
        bare_cves = list(populator.findBareSequences())
        self.assertEqual(bare_cve.id, bare_cves[0].id)
        self.assertNotIn(prefixed_cve.id, [c.id for c in bare_cves])
        self.assertEqual(1, len(bare_cves))


class TestGarboTasks(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def test_LoginTokenPruner(self):
        store = IPrimaryStore(LoginToken)
        now = datetime.now(timezone.utc)
        switch_dbuser("testadmin")

        # It is configured as a daily task.
        self.assertTrue(
            LoginTokenPruner in DailyDatabaseGarbageCollector.tunable_loops
        )

        # Create a token that will be pruned.
        old_token = LoginToken(
            email="whatever", tokentype=LoginTokenType.NEWACCOUNT
        )
        old_token.date_created = now - timedelta(days=666)
        store.add(old_token)
        store.flush()
        old_token_id = old_token.id

        # Create a token that will not be pruned.
        current_token = LoginToken(
            email="whatever", tokentype=LoginTokenType.NEWACCOUNT
        )
        store.add(current_token)
        store.flush()
        current_token_id = current_token.id

        # Run the pruner. Batching is tested by the BulkPruner tests so
        # no need to repeat here.
        switch_dbuser("garbo_daily")
        pruner = LoginTokenPruner(logging.getLogger("garbo"))
        while not pruner.isDone():
            pruner(10)
        pruner.cleanUp()

        # Only the old LoginToken is gone.
        self.assertEqual(store.find(LoginToken, id=old_token_id).count(), 0)
        self.assertEqual(
            store.find(LoginToken, id=current_token_id).count(), 1
        )
