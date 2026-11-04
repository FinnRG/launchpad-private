# Copyright 2012-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for jobs to close bugs for accepted package uploads."""

import io
from itertools import product
from textwrap import dedent
from unittest.mock import patch

import transaction
from debian.deb822 import Changes
from psycopg2.errors import LockNotAvailable
from testtools.content import text_content
from zope.component import getUtility
from zope.security.interfaces import ForbiddenAttribute
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.bugs.interfaces.bugtask import BugTaskStatus
from lp.bugs.model.bug import Bug
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.series import SeriesStatus
from lp.services.config import config
from lp.services.database.sqlbase import connect
from lp.services.features.testing import FeatureFixture
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.runner import JobRunner
from lp.services.job.tests import block_on_job
from lp.soyuz.interfaces.processacceptedbugsjob import (
    IProcessAcceptedBugsJob,
    IProcessAcceptedBugsJobSource,
)
from lp.soyuz.model.processacceptedbugsjob import (
    close_bug_ids_for_sourcepackagerelease,
    close_bugs_for_sourcepackagerelease,
    close_bugs_for_sourcepublication,
    get_bug_ids_from_changes_file,
)
from lp.soyuz.tests.test_publishing import SoyuzTestPublisher
from lp.testing import (
    TestCaseWithFactory,
    celebrity_logged_in,
    person_logged_in,
    verifyObject,
)
from lp.testing.fakemethod import FakeMethod
from lp.testing.layers import (
    CeleryJobLayer,
    DatabaseFunctionalLayer,
    LaunchpadZopelessLayer,
)
from lp.testing.script import run_script


class TestBugIDsFromChangesFile(TestCaseWithFactory):
    """Test get_bug_ids_from_changes_file."""

    layer = LaunchpadZopelessLayer
    dbuser = config.uploadqueue.dbuser

    def setUp(self):
        super().setUp()
        self.changes = Changes(
            {
                "Format": "1.8",
                "Source": "swat",
            }
        )

    def getBugIDs(self):
        """Serialize self.changes and use get_bug_ids_from_changes_file to
        extract bug IDs from it.
        """
        stream = io.BytesIO()
        self.changes.dump(stream)
        stream.seek(0)
        return get_bug_ids_from_changes_file(stream)

    def test_no_bugs(self):
        # An empty list is returned if there are no bugs
        # mentioned.
        self.assertEqual([], self.getBugIDs())

    def test_invalid_bug_id(self):
        # Invalid bug ids (i.e. containing non-digit characters) are ignored.
        self.changes["Launchpad-Bugs-Fixed"] = "bla"
        self.assertEqual([], self.getBugIDs())

    def test_unknown_bug_id(self):
        # Unknown bug ids are passed through; they will be ignored later, by
        # close_bug_ids_for_sourcepackagerelease.
        self.changes["Launchpad-Bugs-Fixed"] = "45120"
        self.assertEqual([45120], self.getBugIDs())

    def test_valid_bug(self):
        # For valid bug ids the bug object is returned.
        bug = self.factory.makeBug()
        self.changes["Launchpad-Bugs-Fixed"] = "%d" % bug.id
        self.assertEqual([bug.id], self.getBugIDs())

    def test_case_sensitivity(self):
        # The spelling of Launchpad-Bugs-Fixed is case-insensitive.
        bug = self.factory.makeBug()
        self.changes["LaUnchpad-Bugs-fixed"] = "%d" % bug.id
        self.assertEqual([bug.id], self.getBugIDs())

    def test_multiple_bugs(self):
        # Multiple bug ids can be specified, separated by spaces.
        bug1 = self.factory.makeBug()
        bug2 = self.factory.makeBug()
        self.changes["Launchpad-Bugs-Fixed"] = "%d invalid %d" % (
            bug1.id,
            bug2.id,
        )
        self.assertEqual([bug1.id, bug2.id], self.getBugIDs())


class TestClosingBugs(TestCaseWithFactory):
    """Test the various bug closing methods in processaccepted.py.

    Tests are currently spread around the codebase; this is an attempt to
    start a unification in a single file and those other tests need
    migrating here.
    See also:
        * lib/lp/soyuz/doc/closing-bugs-from-changelogs.rst
        * lib/lp/archiveuploader/tests/nascentupload-closing-bugs.rst
    """

    layer = LaunchpadZopelessLayer

    def makeChangelogWithBugs(self, spr, target_series=None):
        """Create a changelog for the passed sourcepackagerelease that has
        6 bugs referenced.

        :param spr: The sourcepackagerelease that needs a changelog.
        :param target_distro: the distribution context for the source package
            bug target.  If None, default to its uploaded distribution.

        :return: A tuple which is a list of (bug, bugtask)
        """
        # Make 4 bugs and corresponding bugtasks and put them in an array
        # as tuples.
        bugs = []
        for _ in range(6):
            if target_series is None:
                target_series = spr.upload_distroseries
            target = target_series.getSourcePackage(spr.sourcepackagename)
            bug = self.factory.makeBug()
            bugtask = self.factory.makeBugTask(target=target, bug=bug)
            bugs.append((bug, bugtask))
        # Make a changelog entry for a package which contains the IDs of
        # the 6 bugs separated across 3 releases.
        changelog = dedent(
            """
            foo (1.0-3) unstable; urgency=low

              * closes: %s, %s
              * lp: #%s, #%s

             -- Foo Bar <foo@example.com>  Tue, 01 Jan 1970 01:50:41 +0000

            foo (1.0-2) unstable; urgency=low

              * closes: %s

             -- Foo Bar <foo@example.com>  Tue, 01 Jan 1970 01:50:41 +0000

            foo (1.0-1) unstable; urgency=low

              * closes: %s

             -- Foo Bar <foo@example.com>  Tue, 01 Jan 1970 01:50:41 +0000

            """
            % (
                bugs[0][0].id,
                bugs[1][0].id,
                bugs[2][0].id,
                bugs[3][0].id,
                bugs[4][0].id,
                bugs[5][0].id,
            )
        ).encode("UTF-8")
        lfa = self.factory.makeLibraryFileAlias(content=changelog)
        removeSecurityProxy(spr).changelog = lfa
        self.layer.txn.commit()
        return bugs

    def test_close_bugs_for_sourcepackagerelease_with_no_changes_file(self):
        # If there's no changes file it should read the changelog_entry on
        # the sourcepackagerelease.

        spr = self.factory.makeSourcePackageRelease(changelog_entry="blah")
        bugs = self.makeChangelogWithBugs(spr)

        # Call the method and test it's closed the bugs.
        close_bugs_for_sourcepackagerelease(
            spr.upload_distroseries, spr, None, since_version="1.0-1"
        )
        for bug, bugtask in bugs:
            if bug.id != bugs[5][0].id:
                self.assertEqual(BugTaskStatus.FIXRELEASED, bugtask.status)
            else:
                self.assertEqual(BugTaskStatus.NEW, bugtask.status)

    def test__close_bugs_for_sourcepublication__uses_right_distro(self):
        # If a source was originally uploaded to a different distro,
        # closing bugs based on a publication of the same source in a new
        # distro should work.

        # Create a source package that was originally uploaded to one
        # distro and publish it in a second distro.
        spr = self.factory.makeSourcePackageRelease(changelog_entry="blah")
        target_distro = self.factory.makeDistribution()
        target_distroseries = self.factory.makeDistroSeries(target_distro)
        bugs = self.makeChangelogWithBugs(
            spr, target_series=target_distroseries
        )
        target_spph = self.factory.makeSourcePackagePublishingHistory(
            sourcepackagerelease=spr,
            distroseries=target_distroseries,
            archive=target_distro.main_archive,
            pocket=PackagePublishingPocket.RELEASE,
        )

        # The test depends on this pre-condition.
        self.assertNotEqual(
            spr.upload_distroseries.distribution,
            target_distroseries.distribution,
        )

        close_bugs_for_sourcepublication(target_spph, since_version="1.0")

        for _, bugtask in bugs:
            self.assertEqual(BugTaskStatus.FIXRELEASED, bugtask.status)


class TestClosingPrivateBugs(TestCaseWithFactory):
    # The distroseries +queue page can close private bugs when accepting
    # packages.

    layer = DatabaseFunctionalLayer

    def test_close_bugs_for_sourcepackagerelease_with_private_bug(self):
        """close_bugs_for_sourcepackagerelease works with private bugs."""
        changes_file_template = "Format: 1.7\nLaunchpad-bugs-fixed: %s\n"
        # changelog_entry is required for an assertion inside the function
        # we're testing.
        spr = self.factory.makeSourcePackageRelease(changelog_entry="blah")
        archive_admin = self.factory.makePerson()
        series = spr.upload_distroseries
        dsp = series.distribution.getSourcePackage(spr.sourcepackagename)
        bug = self.factory.makeBug(
            target=dsp, information_type=InformationType.USERDATA
        )
        changes = io.BytesIO((changes_file_template % bug.id).encode("UTF-8"))

        with person_logged_in(archive_admin):
            # The archive admin user can't normally see this bug.
            self.assertRaises(ForbiddenAttribute, bug, "status")
            # But the bug closure should work.
            close_bugs_for_sourcepackagerelease(series, spr, changes)

        # Rather than closing the bugs immediately, this creates a
        # ProcessAcceptedBugsJob.
        with celebrity_logged_in("admin"):
            self.assertEqual(BugTaskStatus.NEW, bug.default_bugtask.status)
        job_source = getUtility(IProcessAcceptedBugsJobSource)
        [job] = list(job_source.iterReady())
        self.assertEqual(series, job.distroseries)
        self.assertEqual(spr, job.sourcepackagerelease)
        self.assertEqual([bug.id], job.bug_ids)


class TestCloseBugIDsForSourcePackageRelease(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer
    dbuser = config.IProcessAcceptedBugsJobSource.dbuser

    def setUp(self):
        super().setUp()
        # Create a distribution with two series, two source package names,
        # and an SPR and a bug task for all combinations of those.
        self.distro = self.factory.makeDistribution()
        self.series = [
            self.factory.makeDistroSeries(
                distribution=self.distro, status=status
            )
            for status in (SeriesStatus.CURRENT, SeriesStatus.DEVELOPMENT)
        ]
        self.spns = [self.factory.makeSourcePackageName() for _ in range(2)]
        self.bug = self.factory.makeBug()
        self.sprs = [
            self.factory.makeSourcePackageRelease(
                sourcepackagename=spn,
                distroseries=series,
                changelog_entry="changelog",
            )
            for spn, series in product(self.spns, self.series)
        ]
        self.bugtasks = [
            self.factory.makeBugTask(
                target=spr.upload_distroseries.getSourcePackage(
                    spr.sourcepackagename
                ),
                bug=self.bug,
            )
            for spr in self.sprs
        ]

    def test_correct_tasks_with_distroseries(self):
        # Close the task for the correct source package name and the given
        # series.
        close_bug_ids_for_sourcepackagerelease(
            self.series[0], self.sprs[0], [self.bug.id]
        )
        self.assertEqual(BugTaskStatus.FIXRELEASED, self.bugtasks[0].status)
        for i in (1, 2, 3):
            self.assertEqual(BugTaskStatus.NEW, self.bugtasks[i].status)

    def test_correct_message(self):
        # When closing a bug, a reasonable message is added.
        close_bug_ids_for_sourcepackagerelease(
            self.series[0], self.sprs[0], [self.bug.id]
        )
        self.assertEqual(2, self.bug.messages.count())
        self.assertEqual(
            "This bug was fixed in the package %s"
            "\n\n---------------\nchangelog" % self.sprs[0].title,
            self.bug.messages[1].text_contents,
        )

    def test_locks_parent_bugs_before_changing_tasks(self):
        # Every affected bug is locked before any of their tasks change,
        # so a concurrent update that locks a bug before its tasks cannot
        # deadlock with this transaction.
        other_bug = self.factory.makeBug()
        other_task = self.factory.makeBugTask(
            target=self.bugtasks[0].target, bug=other_bug
        )
        bug_ids = [other_bug.id, self.bug.id, other_bug.id]
        transaction.commit()
        connection = connect(user=self.dbuser)
        self.addCleanup(connection.close)
        original_set_status = Bug.setStatus
        checked_bugs = []

        def assert_bug_locked(bug_id):
            # Run in a separate transaction; NOWAIT fails at once if this
            # test's transaction holds a conflicting lock on the row.
            try:
                self.assertRaises(
                    LockNotAvailable,
                    connection.cursor().execute,
                    "SELECT id FROM Bug WHERE id = %s "
                    "FOR NO KEY UPDATE NOWAIT",
                    (bug_id,),
                )
            finally:
                connection.rollback()

        def set_status(bug, *args, **kwargs):
            for bug_id in set(bug_ids):
                assert_bug_locked(bug_id)
            checked_bugs.append(bug.id)
            return original_set_status(bug, *args, **kwargs)

        with patch.object(Bug, "setStatus", set_status):
            close_bug_ids_for_sourcepackagerelease(
                self.series[0], self.sprs[0], bug_ids
            )
        self.assertEqual(sorted({self.bug.id, other_bug.id}), checked_bugs)
        self.assertEqual(BugTaskStatus.FIXRELEASED, self.bugtasks[0].status)
        self.assertEqual(BugTaskStatus.FIXRELEASED, other_task.status)

    def test_ignore_unknown_bug_ids(self):
        # Unknown bug IDs are ignored, and no message is added.
        close_bug_ids_for_sourcepackagerelease(
            self.series[0], self.sprs[0], [self.bug.id + 1]
        )
        for bugtask in self.bugtasks:
            self.assertEqual(BugTaskStatus.NEW, bugtask.status)
        self.assertEqual(1, self.bug.messages.count())

    def test_private_bug(self):
        # Closing private bugs is not a problem.
        self.bug.transitionToInformationType(
            InformationType.USERDATA, self.distro.owner
        )
        close_bug_ids_for_sourcepackagerelease(
            self.series[0], self.sprs[0], [self.bug.id]
        )
        self.assertEqual(BugTaskStatus.FIXRELEASED, self.bugtasks[0].status)


class TestProcessAcceptedBugsJob(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer
    dbuser = config.IProcessAcceptedBugsJobSource.dbuser

    def setUp(self):
        super().setUp()
        self.publisher = SoyuzTestPublisher()
        self.publisher.prepareBreezyAutotest()
        self.distroseries = self.publisher.breezy_autotest

    def makeJob(self, distroseries=None, spr=None, bug_ids=[1]):
        """Create a `ProcessAcceptedBugsJob`."""
        if distroseries is None:
            distroseries = self.distroseries
        if spr is None:
            spr = self.factory.makeSourcePackageRelease(
                distroseries=distroseries, changelog_entry="changelog"
            )
        return getUtility(IProcessAcceptedBugsJobSource).create(
            distroseries, spr, bug_ids
        )

    def test_job_implements_IProcessAcceptedBugsJob(self):
        job = self.makeJob()
        self.assertTrue(verifyObject(IProcessAcceptedBugsJob, job))

    def test_job_source_implements_IProcessAcceptedBugsJobSource(self):
        job_source = getUtility(IProcessAcceptedBugsJobSource)
        self.assertTrue(
            verifyObject(IProcessAcceptedBugsJobSource, job_source)
        )

    def test_create(self):
        # A ProcessAcceptedBugsJob can be created and stores its arguments.
        spr = self.factory.makeSourcePackageRelease(
            distroseries=self.distroseries, changelog_entry="changelog"
        )
        bug_ids = [1, 2]
        job = self.makeJob(spr=spr, bug_ids=bug_ids)
        self.assertProvides(job, IProcessAcceptedBugsJob)
        self.assertEqual(self.distroseries, job.distroseries)
        self.assertEqual(spr, job.sourcepackagerelease)
        self.assertEqual(bug_ids, job.bug_ids)

    def test_run_raises_errors(self):
        # A job reports unexpected errors as exceptions.
        class Boom(Exception):
            pass

        distroseries = self.factory.makeDistroSeries()
        removeSecurityProxy(distroseries).getSourcePackage = FakeMethod(
            failure=Boom()
        )
        job = self.makeJob(distroseries=distroseries)
        self.assertRaises(Boom, job.run)

    def test___repr__(self):
        spr = self.factory.makeSourcePackageRelease(
            distroseries=self.distroseries, changelog_entry="changelog"
        )
        bug_ids = [1, 2]
        job = self.makeJob(spr=spr, bug_ids=bug_ids)
        self.assertEqual(
            (
                "<ProcessAcceptedBugsJob to close bugs [1, 2] for "
                "{spr.name}/{spr.version} ({distroseries.distribution.name} "
                "{distroseries.name})>"
            ).format(distroseries=self.distroseries, spr=spr),
            repr(job),
        )

    def test_run(self):
        # A proper test run closes bugs.
        spr = self.factory.makeSourcePackageRelease(
            distroseries=self.distroseries, changelog_entry="changelog"
        )
        bug = self.factory.makeBug()
        bugtask = self.factory.makeBugTask(
            target=self.distroseries.getSourcePackage(spr.sourcepackagename),
            bug=bug,
        )
        self.assertEqual(BugTaskStatus.NEW, bugtask.status)
        job = self.makeJob(spr=spr, bug_ids=[bug.id])
        JobRunner([job]).runAll()
        self.assertEqual(BugTaskStatus.FIXRELEASED, bugtask.status)

    def test_smoke(self):
        spr = self.factory.makeSourcePackageRelease(
            distroseries=self.distroseries, changelog_entry="changelog"
        )
        bug = self.factory.makeBug()
        bugtask = self.factory.makeBugTask(
            target=self.distroseries.getSourcePackage(spr.sourcepackagename),
            bug=bug,
        )
        self.assertEqual(BugTaskStatus.NEW, bugtask.status)
        self.makeJob(spr=spr, bug_ids=[bug.id])
        transaction.commit()

        exit_code, out, err = run_script(
            "cronscripts/process-job-source.py",
            args=["-vv", IProcessAcceptedBugsJobSource.getName()],
            extra_env={"LP_DEBUG_SQL": "1"},
        )

        self.addDetail("stdout", text_content(out))
        self.addDetail("stderr", text_content(err))

        self.assertEqual(0, exit_code)
        self.assertEqual(BugTaskStatus.FIXRELEASED, bugtask.status)


class TestViaCelery(TestCaseWithFactory):
    """ProcessAcceptedBugsJob runs under Celery."""

    layer = CeleryJobLayer

    def test_run(self):
        # A proper test run closes bugs.
        self.useFixture(
            FeatureFixture(
                {
                    "jobs.celery.enabled_classes": "ProcessAcceptedBugsJob",
                }
            )
        )

        distroseries = self.factory.makeDistroSeries()
        spr = self.factory.makeSourcePackageRelease(
            distroseries=distroseries, changelog_entry="changelog"
        )
        bug = self.factory.makeBug()
        bugtask = self.factory.makeBugTask(
            target=distroseries.getSourcePackage(spr.sourcepackagename),
            bug=bug,
        )
        self.assertEqual(BugTaskStatus.NEW, bugtask.status)
        job = getUtility(IProcessAcceptedBugsJobSource).create(
            distroseries, spr, [bug.id]
        )
        self.assertEqual(distroseries, job.distroseries)
        self.assertEqual(spr, job.sourcepackagerelease)
        self.assertEqual([bug.id], job.bug_ids)

        with block_on_job(self):
            transaction.commit()

        self.assertEqual(JobStatus.COMPLETED, job.status)
        self.assertEqual(BugTaskStatus.FIXRELEASED, bugtask.status)
