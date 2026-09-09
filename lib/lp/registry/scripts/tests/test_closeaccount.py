# Copyright 2018-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test the close-account script."""
from datetime import datetime, timezone

import transaction
from storm.store import Store
from testtools.matchers import (
    MatchesSetwise,
    MatchesStructure,
    Not,
    StartsWith,
)
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.answers.enums import QuestionStatus
from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.archivepublisher.config import getPubConfig
from lp.archivepublisher.publishing import Publisher
from lp.bugs.model.bugsummary import BugSummary
from lp.code.enums import (
    CodeImportResultStatus,
    CodeReviewVote,
    TargetRevisionControlSystems,
)
from lp.code.interfaces.codeimportjob import ICodeImportJobWorkflow
from lp.code.interfaces.codereviewinlinecomment import (
    ICodeReviewInlineCommentSet,
)
from lp.code.tests.helpers import GitHostingFixture
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.socialaccount import (
    ISocialAccountSet,
    SocialPlatformType,
)
from lp.registry.interfaces.teammembership import ITeamMembershipSet
from lp.registry.model.productrelease import ProductRelease
from lp.registry.model.teammembership import TeamParticipation
from lp.registry.personmerge import merge_people
from lp.registry.scripts.closeaccount import CloseAccountScript
from lp.scripts.garbo import PopulateLatestPersonSourcePackageReleaseCache
from lp.services.config import config
from lp.services.database.interfaces import IStore
from lp.services.database.sqlbase import (
    flush_database_caches,
    get_transaction_timestamp,
)
from lp.services.identity.interfaces.account import AccountStatus, IAccountSet
from lp.services.identity.interfaces.emailaddress import IEmailAddressSet
from lp.services.job.interfaces.job import JobType
from lp.services.job.model.job import Job
from lp.services.log.logger import BufferLogger, DevNullLogger
from lp.services.scripts.base import LaunchpadScriptFailure
from lp.services.verification.interfaces.authtoken import LoginTokenType
from lp.services.verification.interfaces.logintoken import ILoginTokenSet
from lp.services.webauthn.interfaces import IRegisteredAuthenticatorSet
from lp.soyuz.enums import ArchiveSubscriberStatus, PackagePublishingStatus
from lp.soyuz.model.archive import Archive
from lp.soyuz.model.archiveauthtoken import ArchiveAuthToken
from lp.soyuz.tests.test_publishing import SoyuzTestPublisher
from lp.testing import TestCaseWithFactory, login_celebrity
from lp.testing.dbuser import dbuser
from lp.testing.layers import LaunchpadZopelessLayer
from lp.translations.interfaces.pofiletranslator import IPOFileTranslatorSet
from lp.translations.interfaces.translationsperson import ITranslationsPerson


class TestCloseAccount(TestCaseWithFactory):
    """Test the close-account script.

    Unfortunately, we have no way of detecting schema updates containing new
    information that needs to be removed or sanitized on account closure
    apart from reviewers noticing and prompting developers to update this
    script.  See Bug #120506 for more details.
    """

    layer = LaunchpadZopelessLayer

    def makeScript(self, test_args):
        script = CloseAccountScript(test_args=test_args)
        script.logger = BufferLogger()
        script.txn = transaction
        return script

    def runScript(self, script):
        try:
            script.main()
        finally:
            self.addDetail("log", script.logger.content)
            flush_database_caches()

    def makePopulatedUser(self, name=None):
        """Return a person and account linked to some personal information."""
        person = self.factory.makePerson(name=name, karma=10)
        self.assertEqual(AccountStatus.ACTIVE, person.account.status)
        self.assertNotEqual([], list(person.account.openid_identifiers))
        self.factory.makeBugTask().transitionToAssignee(person, validate=False)
        self.factory.makeQuestion().assignee = person
        self.factory.makeQuestion(owner=person)
        self.factory.makeGPGKey(owner=person)
        self.factory.makeBranchSubscription(person=person)
        self.factory.makeBug().subscribe(person, person)
        self.factory.makeGitSubscription(person=person)
        team = self.factory.makeTeam()
        self.factory.makeMailingList(team, team.teamowner).subscribe(person)
        self.factory.makeQuestionSubscription(person=person)
        self.factory.makeSpecification().subscribe(person)
        person.addLanguage(self.factory.makeLanguage())
        self.factory.makeSSHKey(person=person)
        self.factory.makeRegisteredAuthenticator(owner=person)
        self.factory.makeAccessArtifactGrant(grantee=person)
        self.factory.makeAccessPolicyGrant(grantee=person)
        self.factory.makeGitRuleGrant(grantee=person)
        person.selfgenerated_bugnotifications = True
        person.expanded_notification_footers = True
        person.require_strong_email_authentication = True
        return person, person.id, person.account.id

    def assertRemoved(self, account_id, person_id):
        # The Account row still exists, but has been anonymised, leaving
        # only a minimal audit trail.
        account = getUtility(IAccountSet).get(account_id)
        self.assertEqual("Removed by request", account.displayname)
        self.assertEqual(AccountStatus.CLOSED, account.status)
        self.assertIn("Closed using close-account.", account.status_history)

        # The Person row still exists to maintain links with information
        # that won't be removed, such as bug comments, but has been
        # anonymised.
        person = getUtility(IPersonSet).get(person_id)
        self.assertThat(person.name, StartsWith("removed"))
        self.assertEqual("Removed by request", person.display_name)
        self.assertEqual(account, person.account)

        # The corresponding PersonSettings row has been reset to the
        # defaults.
        self.assertFalse(person.selfgenerated_bugnotifications)
        self.assertFalse(person.expanded_notification_footers)
        self.assertFalse(person.require_strong_email_authentication)

        # EmailAddress and OpenIdIdentifier rows have been removed.
        self.assertEqual(
            [], list(getUtility(IEmailAddressSet).getByPerson(person))
        )
        self.assertEqual([], list(account.openid_identifiers))

    def assertNotRemoved(self, account_id, person_id):
        account = getUtility(IAccountSet).get(account_id)
        self.assertNotEqual("Removed by request", account.displayname)
        self.assertEqual(AccountStatus.ACTIVE, account.status)
        person = getUtility(IPersonSet).get(person_id)
        self.assertEqual(account, person.account)
        self.assertNotEqual("Removed by request", person.display_name)
        self.assertThat(person.name, Not(StartsWith("removed")))
        self.assertNotEqual(
            [], list(getUtility(IEmailAddressSet).getByPerson(person))
        )
        self.assertNotEqual([], list(account.openid_identifiers))

    def test_nonexistent(self):
        script = self.makeScript(["nonexistent-person"])
        with dbuser("launchpad"):
            self.assertRaisesWithContent(
                LaunchpadScriptFailure,
                "User nonexistent-person does not exist",
                self.runScript,
                script,
            )

    def test_team(self):
        team = self.factory.makeTeam()
        script = self.makeScript([team.name])
        with dbuser("launchpad"):
            self.assertRaisesWithContent(
                LaunchpadScriptFailure,
                "%s is a team" % team.name,
                self.runScript,
                script,
            )

    def test_unhandled_reference(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id
        self.factory.makeProduct(owner=person)
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.assertRaisesWithContent(
                LaunchpadScriptFailure,
                "User %s is still referenced" % person.name,
                self.runScript,
                script,
            )
        self.assertIn(
            "ERROR User %s is still referenced by 1 product.owner values"
            % (person.name),
            script.logger.getLogBuffer(),
        )
        self.assertNotRemoved(account_id, person_id)

    def test_dry_run(self):
        person, person_id, account_id = self.makePopulatedUser()
        script = self.makeScript(["--dry-run", person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertIn(
            "Dry run, so not committing changes", script.logger.getLogBuffer()
        )
        self.assertNotRemoved(account_id, person_id)

    def test_close_account_logs(self):
        person, person_id, account_id = self.makePopulatedUser(
            name="test-account-to-close"
        )
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertIn(
            "Closing test-account-to-close's account",
            script.logger.getLogBuffer(),
        )
        self.assertIn(
            "test-account-to-close's account closed",
            script.logger.getLogBuffer(),
        )

    def test_single_by_name(self):
        person, person_id, account_id = self.makePopulatedUser()
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)

    def test_single_by_email(self):
        person, person_id, account_id = self.makePopulatedUser()
        script = self.makeScript([person.preferredemail.email])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)

    def test_multiple(self):
        persons = [self.factory.makePerson() for _ in range(3)]
        person_ids = [person.id for person in persons]
        account_ids = [person.account.id for person in persons]
        script = self.makeScript([persons[0].name, persons[1].name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_ids[0], person_ids[0])
        self.assertRemoved(account_ids[1], person_ids[1])
        self.assertNotRemoved(account_ids[2], person_ids[2])

    def test_multiple_email_addresses(self):
        person, person_id, account_id = self.makePopulatedUser()
        self.factory.makeEmail("%s@another-domain.test" % person.name, person)
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)

    def test_unactivated(self):
        person = self.factory.makePerson(
            account_status=AccountStatus.NOACCOUNT
        )
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.guessedemails[0].email])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)

    def test_retains_audit_trail(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id
        branch_subscription = self.factory.makeBranchSubscription(
            subscribed_by=person
        )
        snap = self.factory.makeSnap()
        snap_build = self.factory.makeSnapBuild(requester=person, snap=snap)
        specification = self.factory.makeSpecification(drafter=person)
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertEqual(person, branch_subscription.subscribed_by)
        self.assertEqual(person, snap_build.requester)
        self.assertEqual(person, specification.drafter)

    def test_solves_questions_in_non_final_states(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id
        questions = []
        for status in (
            QuestionStatus.OPEN,
            QuestionStatus.NEEDSINFO,
            QuestionStatus.ANSWERED,
        ):
            question = self.factory.makeQuestion(owner=person)
            question.addComment(person, "comment")
            removeSecurityProxy(question).status = status
            questions.append(question)
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        for question in questions:
            self.assertEqual(QuestionStatus.SOLVED, question.status)
            self.assertEqual(
                "Closed by Launchpad due to owner requesting account removal",
                question.whiteboard,
            )

    def test_skips_questions_in_final_states(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id
        questions = {}
        for status in (
            QuestionStatus.SOLVED,
            QuestionStatus.EXPIRED,
            QuestionStatus.INVALID,
        ):
            question = self.factory.makeQuestion(owner=person)
            question.addComment(person, "comment")
            removeSecurityProxy(question).status = status
            questions[status] = question
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        for question_status, question in questions.items():
            self.assertEqual(question_status, question.status)
            self.assertIsNone(question.whiteboard)

    def test_skips_specification_branch_registrant(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id
        spec = self.factory.makeSpecification()
        branch = self.factory.makeAnyBranch()
        spec.linkBranch(branch, person)
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)

    def test_skips_bugbranch_registrant(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id
        bug = self.factory.makeBug()
        branch = self.factory.makeBranch()
        bug.linkBranch(branch, person)
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)

    def test_handles_packaging_references(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id
        self.factory.makeGPGKey(person)
        publisher = SoyuzTestPublisher()
        publisher.person = person
        ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        spph = publisher.getPubSource(
            status=PackagePublishingStatus.PUBLISHED,
            distroseries=ubuntu.currentseries,
            maintainer=person,
            creator=person,
        )
        with dbuser("garbo_frequently"):
            job = PopulateLatestPersonSourcePackageReleaseCache(
                DevNullLogger()
            )
            while not job.isDone():
                job(chunk_size=100)
        self.assertTrue(person.hasMaintainedPackages())
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertEqual(person, spph.package_maintainer)
        self.assertEqual(person, spph.package_creator)
        self.assertFalse(person.hasMaintainedPackages())

    def test_skips_reported_bugs(self):
        person = self.factory.makePerson()
        bug = self.factory.makeBug(owner=person)
        bugtask = self.factory.makeBugTask(bug=bug, owner=person)
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertEqual(person, bug.owner)
        self.assertEqual(person, bugtask.owner)

    def test_skips_bug_watch_owner(self):
        person = self.factory.makePerson()
        self.factory.makeBugWatch(owner=person)
        self.factory.makeBugWatch(owner=person)
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)

    def test_handles_bug_affects_person(self):
        person = self.factory.makePerson()
        bug = self.factory.makeBug()
        bug.markUserAffected(person)
        self.assertTrue(bug.isUserAffected(person))
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertFalse(bug.isUserAffected(person))

    def test_skips_translation_relicensing_agreements(self):
        person = self.factory.makePerson()
        translations_person = ITranslationsPerson(person)
        translations_person.translations_relicensing_agreement = True
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertTrue(translations_person.translations_relicensing_agreement)

    def test_skips_po_file_translators(self):
        person = self.factory.makePerson()
        pofile = self.factory.makePOFile()
        potmsgset = self.factory.makePOTMsgSet(pofile.potemplate)
        self.factory.makeCurrentTranslationMessage(
            potmsgset=potmsgset, translator=person, language=pofile.language
        )
        self.assertIsNotNone(
            getUtility(IPOFileTranslatorSet).getForPersonPOFile(person, pofile)
        )
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertIsNotNone(
            getUtility(IPOFileTranslatorSet).getForPersonPOFile(person, pofile)
        )

    def test_skips_po_file_owners(self):
        person = self.factory.makePerson()
        pofile = self.factory.makePOFile()
        pofile.owner = person
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)

    def test_skips_po_template_owners(self):
        person = self.factory.makePerson()
        productseries = self.factory.makeProductSeries()
        potemplate = self.factory.makePOTemplate(
            owner=person, productseries=productseries
        )
        potemplate.owner = person
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)

    def test_handles_archive_subscriptions_and_tokens(self):
        person = self.factory.makePerson()
        ppa = self.factory.makeArchive(private=True)
        subscription = ppa.newSubscription(person, ppa.owner)
        other_subscription = ppa.newSubscription(
            self.factory.makePerson(), ppa.owner
        )
        ppa.newAuthToken(person)
        self.assertEqual(ArchiveSubscriberStatus.CURRENT, subscription.status)
        self.assertIsNotNone(ppa.getAuthToken(person))
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            now = get_transaction_timestamp(Store.of(person))
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertEqual(
            ArchiveSubscriberStatus.CANCELLED, subscription.status
        )
        self.assertEqual(now, subscription.date_cancelled)
        self.assertEqual(
            ArchiveSubscriberStatus.CURRENT, other_subscription.status
        )
        # The token remains, but is no longer valid since the subscription
        # has been cancelled.
        self.assertIsNotNone(
            IStore(ArchiveAuthToken)
            .find(
                ArchiveAuthToken,
                ArchiveAuthToken.archive == ppa,
                ArchiveAuthToken.date_deactivated == None,
                ArchiveAuthToken.person == person,
            )
            .one()
        )
        self.assertIsNone(ppa.getAuthToken(person))

    def test_handles_hardware_submissions(self):
        # Launchpad used to support hardware submissions.  This is in the
        # process of being removed after a long period of relative disuse,
        # but close-account still needs to cope with old accounts that have
        # them, so we resort to raw SQL to set things up.
        person = self.factory.makePerson()
        store = Store.of(person)
        date_created = get_transaction_timestamp(store)
        bug = self.factory.makeBug(information_type=InformationType.USERDATA)
        keys = [
            self.factory.getUniqueUnicode("submission-key") for _ in range(2)
        ]
        raw_submissions = [
            self.factory.makeLibraryFileAlias(db_only=True) for _ in range(2)
        ]
        systems = [
            self.factory.getUniqueUnicode("system-fingerprint")
            for _ in range(2)
        ]
        system_fingerprint_ids = [
            row[0]
            for row in store.execute(
                """
                INSERT INTO HWSystemFingerprint (fingerprint)
                VALUES (?), (?)
                RETURNING id
                """,
                systems,
            )
        ]
        submission_ids = [
            row[0]
            for row in store.execute(
                """
                INSERT INTO HWSubmission
                    (date_created, format, private, contactable,
                     submission_key, owner, raw_submission,
                     system_fingerprint)
                VALUES
                    (?, 1, FALSE, FALSE, ?, ?, ?, ?),
                    (?, 1, FALSE, FALSE, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    date_created,
                    keys[0],
                    person.id,
                    raw_submissions[0].id,
                    system_fingerprint_ids[0],
                    date_created,
                    keys[1],
                    self.factory.makePerson().id,
                    raw_submissions[1].id,
                    system_fingerprint_ids[1],
                ),
            )
        ]
        with dbuser("hwdb-submission-processor"):
            vendor_name_id = store.execute(
                """
                INSERT INTO HWVendorName (name) VALUES (?) RETURNING id
                """,
                (self.factory.getUniqueUnicode(),),
            ).get_one()[0]
            vendor_id = store.execute(
                """
                INSERT INTO HWVendorID (bus, vendor_id_for_bus, vendor_name)
                VALUES (1, '0x0001', ?)
                RETURNING id
                """,
                (vendor_name_id,),
            ).get_one()[0]
            device_id = store.execute(
                """
                INSERT INTO HWDevice
                    (bus_vendor_id, bus_product_id, variant, name, submissions)
                VALUES (?, '0x0002', NULL, ?, 1)
                RETURNING id
                """,
                (vendor_id, self.factory.getUniqueUnicode()),
            ).get_one()[0]
            device_driver_link_id = store.execute(
                """
                INSERT INTO HWDeviceDriverLink (device, driver)
                VALUES (?, NULL)
                RETURNING id
                """,
                (device_id,),
            ).get_one()[0]
            parent_submission_device_id = store.execute(
                """
                INSERT INTO HWSubmissionDevice
                    (device_driver_link, submission, parent, hal_device_id)
                VALUES (?, ?, NULL, 1)
                RETURNING id
                """,
                (device_driver_link_id, submission_ids[0]),
            ).get_one()[0]
            store.execute(
                """
                INSERT INTO HWSubmissionBug
                    (submission, bug)
                VALUES (?, ?)
                """,
                (
                    submission_ids[0],
                    bug.id,
                ),
            )
            store.execute(
                """
                INSERT INTO HWSubmissionDevice
                    (device_driver_link, submission, parent, hal_device_id)
                VALUES (?, ?, ?, 2)
                """,
                (
                    device_driver_link_id,
                    submission_ids[0],
                    parent_submission_device_id,
                ),
            )
            other_submission_device_id = store.execute(
                """
                INSERT INTO HWSubmissionDevice
                    (device_driver_link, submission, hal_device_id)
                VALUES (?, ?, 1)
                RETURNING id
                """,
                (device_driver_link_id, submission_ids[1]),
            ).get_one()[0]

        def get_submissions_by_owner(person):
            return [
                row[0]
                for row in store.execute(
                    """
                    SELECT HWSubmission.id
                    FROM HWSubmission, HWSystemFingerprint
                    WHERE
                        HWSubmission.owner = ?
                        AND HWSystemFingerprint.id =
                            HWSubmission.system_fingerprint
                    """,
                    (person.id,),
                )
            ]

        def get_submission_by_submission_key(submission_key):
            result = store.execute(
                """
                SELECT id FROM HWSubmission WHERE submission_key = ?
                """,
                (submission_key,),
            )
            row = result.get_one()
            self.assertIsNone(result.get_one())
            return row[0] if row else None

        def get_devices_by_submission(submission_id):
            return [
                row[0]
                for row in store.execute(
                    """
                    SELECT id FROM HWSubmissionDevice WHERE submission = ?
                    """,
                    (submission_id,),
                )
            ]

        self.assertNotEqual([], get_submissions_by_owner(person))
        self.assertEqual(
            submission_ids[0], get_submission_by_submission_key(keys[0])
        )
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertEqual([], get_submissions_by_owner(person))
        self.assertIsNone(get_submission_by_submission_key(keys[0]))
        self.assertEqual(
            submission_ids[1], get_submission_by_submission_key(keys[1])
        )
        self.assertEqual(
            [other_submission_device_id],
            get_devices_by_submission(submission_ids[1]),
        )

    def test_skips_bug_summary(self):
        person = self.factory.makePerson()
        other_person = self.factory.makePerson()
        bug = self.factory.makeBug(information_type=InformationType.USERDATA)
        bug.subscribe(person, bug.owner)
        bug.subscribe(other_person, bug.owner)
        store = Store.of(bug)
        summaries = list(
            store.find(
                BugSummary,
                BugSummary.viewed_by_id.is_in([person.id, other_person.id]),
            )
        )
        self.assertThat(
            summaries,
            MatchesSetwise(
                MatchesStructure.byEquality(count=1, viewed_by=person),
                MatchesStructure.byEquality(count=1, viewed_by=other_person),
            ),
        )
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        # BugSummaryJournal has been updated, but BugSummary hasn't yet.
        summaries = list(
            store.find(
                BugSummary,
                BugSummary.viewed_by_id.is_in([person.id, other_person.id]),
            )
        )
        self.assertThat(
            summaries,
            MatchesSetwise(
                MatchesStructure.byEquality(count=1, viewed_by=person),
                MatchesStructure.byEquality(count=1, viewed_by=other_person),
                MatchesStructure.byEquality(count=-1, viewed_by=person),
            ),
        )
        # If we force an update (the equivalent of the
        # BugSummaryJournalRollup garbo job), that's enough to get rid of
        # the reference.
        store.execute("SELECT bugsummary_rollup_journal()")
        summaries = list(
            store.find(
                BugSummary,
                BugSummary.viewed_by_id.is_in([person.id, other_person.id]),
            )
        )
        self.assertThat(
            summaries,
            MatchesSetwise(
                MatchesStructure.byEquality(viewed_by=other_person)
            ),
        )

    def test_skips_bug_nomination(self):
        person = self.factory.makePerson()
        other_person = self.factory.makePerson()
        bug = self.factory.makeBug()
        targets = [self.factory.makeProductSeries() for _ in range(2)]
        self.factory.makeBugTask(bug=bug, target=targets[0].parent)
        bug.addNomination(person, targets[0])
        self.factory.makeBugTask(bug=bug, target=targets[1].parent)
        bug.addNomination(other_person, targets[1])
        self.assertThat(
            bug.getNominations(),
            MatchesSetwise(
                MatchesStructure.byEquality(owner=person),
                MatchesStructure.byEquality(owner=other_person),
            ),
        )
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertThat(
            bug.getNominations(),
            MatchesSetwise(
                MatchesStructure.byEquality(owner=person),
                MatchesStructure.byEquality(owner=other_person),
            ),
        )

    def test_skips_inactive_product_owner(self):
        person = self.factory.makePerson()
        product = self.factory.makeProduct(owner=person)
        product.active = False
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertEqual(person, product.owner)

    def test_skips_codereviewvote_reviewer(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id
        merge_proposal = self.factory.makeBranchMergeProposal(
            reviewer=person, registrant=self.factory.makePerson()
        )
        merge_proposal.createComment(
            person,
            vote=CodeReviewVote.APPROVE,
        )
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)

    def test_skips_code_import(self):
        self.useFixture(GitHostingFixture())
        person = self.factory.makePerson()
        team = self.factory.makeTeam(members=[person])
        code_import = self.factory.makeCodeImport(
            registrant=person,
            target_rcs_type=TargetRevisionControlSystems.GIT,
            owner=team,
        )
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertEqual(person, code_import.registrant)

    def test_skips_import_job_requester(self):
        self.useFixture(GitHostingFixture())
        person = self.factory.makePerson()
        team = self.factory.makeTeam(members=[person])
        code_import = self.factory.makeCodeImport(
            registrant=person,
            target_rcs_type=TargetRevisionControlSystems.GIT,
            owner=team,
        )

        getUtility(ICodeImportJobWorkflow).requestJob(
            code_import.import_job, person
        )
        self.assertEqual(person, code_import.import_job.requesting_user)
        result = self.factory.makeCodeImportResult(
            code_import=code_import,
            requesting_user=person,
            result_status=CodeImportResultStatus.SUCCESS,
        )
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertEqual(person, code_import.registrant)
        self.assertEqual(person, result.requesting_user)
        self.assertEqual(person, code_import.import_job.requesting_user)

    def test_skip_requester_package_diff_job(self):
        person = self.factory.makePerson()
        ppa = self.factory.makeArchive(owner=person)
        other_person = self.factory.makePerson()
        from_spr = self.factory.makeSourcePackageRelease(archive=ppa)
        to_spr = self.factory.makeSourcePackageRelease(archive=ppa)
        from_spr.requestDiffTo(ppa.owner, to_spr)
        job = (
            IStore(Job)
            .find(Job, Job.base_job_type == JobType.GENERATE_PACKAGE_DIFF)
            .order_by(Job.id)
            .last()
        )
        # XXX ilasc 2021-02-23: deleting the ppa in this test by running the
        # Publisher here results in the "Can't delete non-trivial PPAs
        # for user" exception. So we just point to another owner here
        # to simulate ppa owner removal during deletion. The deletion was
        # successfully performed on the account removal that triggered addition
        # of skipping the requester on job in dogfood so we need to come back
        # and fix this test setup as deletion at this point should work.
        removeSecurityProxy(ppa).owner = other_person
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertEqual(person, job.requester)

    def test_skips_specification_owner(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id
        specification = self.factory.makeSpecification(owner=person)
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertEqual(person, specification.owner)

    def test_skips_teammembership_last_changed_by(self):
        targetteam = self.factory.makeTeam(name="target")
        member = self.factory.makePerson()
        login_celebrity("admin")
        targetteam.addMember(member, targetteam.teamowner)
        membershipset = getUtility(ITeamMembershipSet)
        membershipset.deactivateActiveMemberships(
            targetteam, comment="test", reviewer=member
        )
        membership = membershipset.getByPersonAndTeam(member, targetteam)
        self.assertEqual(member, membership.last_changed_by)

        person_id = member.id
        account_id = member.account.id
        script = self.makeScript([member.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)

    def test_handles_teamparticipation_except_self(self):
        person = self.factory.makePerson()
        team = self.factory.makeTeam(members=[person])
        store = Store.of(person)
        person_id = person.id
        account_id = person.account.id
        self.assertContentEqual(
            [person_id, team.id],
            store.find(
                TeamParticipation.team_id,
                TeamParticipation.person_id == person_id,
            ),
        )
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertContentEqual(
            [person_id],
            store.find(
                TeamParticipation.team_id,
                TeamParticipation.person_id == person_id,
            ),
        )

    def test_skips_teamowner_merged(self):
        person = self.factory.makePerson()
        merged_person = self.factory.makePerson()
        owned_team1 = self.factory.makeTeam(name="target", owner=person)
        removeSecurityProxy(owned_team1).merged = merged_person
        owned_team2 = self.factory.makeTeam(name="target2", owner=person)
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])

        # Closing account fails as the user still owns team2
        with dbuser("launchpad"):
            self.assertRaises(LaunchpadScriptFailure, self.runScript, script)

        # Account will now close as the user doesn't own
        # any other teams at this point
        removeSecurityProxy(owned_team2).merged = merged_person
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)

    def test_handles_login_token(self):
        person = self.factory.makePerson()
        email = "%s@another-domain.test" % person.name
        login_token_set = getUtility(ILoginTokenSet)
        token = login_token_set.new(
            person,
            person.preferredemail.email,
            email,
            LoginTokenType.VALIDATEEMAIL,
        )
        plaintext_token = token.token
        self.assertEqual(token, login_token_set[plaintext_token])
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertRaises(
            KeyError, login_token_set.__getitem__, plaintext_token
        )

    def test_handles_oauth_request_token(self):
        person = self.factory.makePerson()
        other_person = self.factory.makePerson()
        request_token = self.factory.makeOAuthRequestToken(reviewed_by=person)
        other_request_token = self.factory.makeOAuthRequestToken(
            reviewed_by=other_person
        )
        self.assertContentEqual([request_token], person.oauth_request_tokens)
        self.assertContentEqual(
            [other_request_token], other_person.oauth_request_tokens
        )
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertContentEqual([], person.oauth_request_tokens)
        self.assertContentEqual(
            [other_request_token], other_person.oauth_request_tokens
        )

    def test_handles_oauth_access_token(self):
        person = self.factory.makePerson()
        other_person = self.factory.makePerson()
        access_token, _ = self.factory.makeOAuthAccessToken(owner=person)
        other_access_token, _ = self.factory.makeOAuthAccessToken(
            owner=other_person
        )
        self.assertContentEqual([access_token], person.oauth_access_tokens)
        self.assertContentEqual(
            [other_access_token], other_person.oauth_access_tokens
        )
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertContentEqual([], person.oauth_access_tokens)
        self.assertContentEqual(
            [other_access_token], other_person.oauth_access_tokens
        )

    def test_handles_registered_authenticators(self):
        person = self.factory.makePerson()
        other_person = self.factory.makePerson()
        authenticator = self.factory.makeRegisteredAuthenticator(owner=person)
        other_authenticator = self.factory.makeRegisteredAuthenticator(
            owner=other_person
        )
        # Revoking someone else's authenticator leaves an audit trail that
        # survives closing the revoker's account.
        removeSecurityProxy(other_authenticator).revoke(person)
        authenticator_set = getUtility(IRegisteredAuthenticatorSet)
        self.assertContentEqual(
            [authenticator], authenticator_set.findByOwner(person)
        )
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertContentEqual([], authenticator_set.findByOwner(person))
        self.assertContentEqual(
            [other_authenticator],
            authenticator_set.findByOwner(other_person),
        )
        self.assertEqual(
            person_id,
            removeSecurityProxy(other_authenticator).revoked_by_id,
        )

    def test_fails_on_undeleted_ppa(self):
        person = self.factory.makePerson()
        ppa = self.factory.makeArchive(owner=person)
        procs = [self.factory.makeProcessor() for _ in range(2)]
        ppa.setProcessors(procs)
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.assertRaisesWithContent(
                LaunchpadScriptFailure,
                "User %s is still referenced" % person.name,
                self.runScript,
                script,
            )
        self.assertIn(
            "ERROR User %s is still referenced by 1 archive.owner values"
            % (person.name),
            script.logger.getLogBuffer(),
        )
        self.assertNotRemoved(account_id, person_id)

    def test_ignores_deleted_ppa_with_builds(self):
        person = self.factory.makePerson()
        ppa = self.factory.makeArchive(owner=person)
        ppa_id = ppa.id
        self.factory.makeBinaryPackageBuild(archive=ppa)
        ppa.delete(person)
        Publisher(
            DevNullLogger(), getPubConfig(ppa), None, ppa
        ).deleteArchive()
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertEqual(ppa, Store.of(ppa).get(Archive, ppa_id))

    def test_purges_empty_deleted_ppa(self):
        person = self.factory.makePerson()
        ppa = self.factory.makeArchive(owner=person)
        ppa_id = ppa.id
        other_ppa = self.factory.makeArchive()
        other_ppa_id = other_ppa.id
        procs = [self.factory.makeProcessor() for _ in range(2)]
        ppa.setProcessors(procs)
        ppa.delete(person)
        Publisher(
            DevNullLogger(), getPubConfig(ppa), None, ppa
        ).deleteArchive()
        store = Store.of(ppa)
        person_id = person.id
        account_id = person.account.id
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertIsNone(store.get(Archive, ppa_id))
        self.assertEqual(other_ppa, store.get(Archive, other_ppa_id))

    def test_skips_announcements_in_deactivated_products(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id

        product = self.factory.makeProduct()
        product.announce(person, "announcement")

        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.assertRaisesWithContent(
                LaunchpadScriptFailure,
                "User %s is still referenced" % person.name,
                self.runScript,
                script,
            )
        self.assertNotRemoved(account_id, person_id)

        product.active = False
        with dbuser("launchpad"):
            self.runScript(script)

        self.assertRemoved(account_id, person_id)

    def test_non_product_announcements_are_not_skipped(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id

        distro = self.factory.makeDistribution()
        distro.announce(person, "announcement")

        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.assertRaisesWithContent(
                LaunchpadScriptFailure,
                "User %s is still referenced" % person.name,
                self.runScript,
                script,
            )
        self.assertIn(
            "ERROR User %s is still referenced by 1 announcement.registrant "
            "values" % person.name,
            script.logger.getLogBuffer(),
        )
        self.assertNotRemoved(account_id, person_id)

    def test_skip_milestone_tags_from_inactive_products(self):
        active_product = self.factory.makeProduct()
        inactive_product = self.factory.makeProduct()
        inactive_product.active = False

        active_product_series = self.factory.makeProductSeries(
            product=active_product
        )
        inactive_product_series = self.factory.makeProductSeries(
            product=inactive_product
        )

        for milestone_target, expected_to_be_removed in (
            ({"product": active_product}, False),
            ({"product": inactive_product}, True),
            ({"productseries": active_product_series}, False),
            ({"productseries": inactive_product_series}, True),
            ({"distribution": self.factory.makeDistribution()}, False),
            ({"distroseries": self.factory.makeDistroSeries()}, False),
        ):
            person = self.factory.makePerson()
            person_id = person.id
            account_id = person.account.id

            milestone = self.factory.makeMilestone(**milestone_target)
            milestone.setTags(["tag"], person)
            script = self.makeScript([person.name])
            with dbuser("launchpad"):
                if not expected_to_be_removed:
                    self.assertRaisesWithContent(
                        LaunchpadScriptFailure,
                        "User %s is still referenced" % person.name,
                        self.runScript,
                        script,
                    )
                    self.assertIn(
                        "ERROR User %s is still referenced by 1 "
                        "milestonetag.created_by values" % person.name,
                        script.logger.getLogBuffer(),
                    )
                    self.assertNotRemoved(account_id, person_id)
                else:
                    self.runScript(script)
                    self.assertRemoved(account_id, person_id)

    def test_skip_product_releases_from_inactive_products(self):
        active_product = self.factory.makeProduct()
        inactive_product = self.factory.makeProduct()
        inactive_product.active = False

        active_product_series = self.factory.makeProductSeries(
            product=active_product
        )
        inactive_product_series = self.factory.makeProductSeries(
            product=inactive_product
        )

        for milestone_target, expected_to_be_removed in (
            ({"product": active_product}, False),
            ({"product": inactive_product}, True),
            ({"productseries": active_product_series}, False),
            ({"productseries": inactive_product_series}, True),
        ):
            person = self.factory.makePerson()
            person_id = person.id
            account_id = person.account.id

            milestone = self.factory.makeMilestone(**milestone_target)
            milestone.createProductRelease(person, datetime.now(timezone.utc))
            script = self.makeScript([person.name])
            with dbuser("launchpad"):
                if not expected_to_be_removed:
                    self.assertRaisesWithContent(
                        LaunchpadScriptFailure,
                        "User %s is still referenced" % person.name,
                        self.runScript,
                        script,
                    )
                    self.assertNotRemoved(account_id, person_id)
                else:
                    self.runScript(script)
                    self.assertRemoved(account_id, person_id)

    def test_skip_product_releases_files_from_inactive_products(self):
        active_product = self.factory.makeProduct()
        inactive_product = self.factory.makeProduct()
        inactive_product.active = False

        active_product_series = self.factory.makeProductSeries(
            product=active_product
        )
        inactive_product_series = self.factory.makeProductSeries(
            product=inactive_product
        )

        for milestone_target, expected_to_be_removed in (
            ({"product": active_product}, False),
            ({"product": inactive_product}, True),
            ({"productseries": active_product_series}, False),
            ({"productseries": inactive_product_series}, True),
        ):
            person = self.factory.makePerson()
            person_id = person.id
            account_id = person.account.id

            milestone = self.factory.makeMilestone(**milestone_target)
            product_release: ProductRelease = milestone.createProductRelease(
                milestone.product.owner, datetime.now(timezone.utc)
            )
            product_release.addReleaseFile(
                "test.txt", b"test", "text/plain", person
            )
            script = self.makeScript([person.name])
            with dbuser("launchpad"):
                if not expected_to_be_removed:
                    self.assertRaisesWithContent(
                        LaunchpadScriptFailure,
                        "User %s is still referenced" % person.name,
                        self.runScript,
                        script,
                    )
                    self.assertNotRemoved(account_id, person_id)
                else:
                    self.runScript(script)
                    self.assertRemoved(account_id, person_id)

    def test_skip_branches_from_inactive_products(self):
        active_product = self.factory.makeProduct()
        inactive_product = self.factory.makeProduct()
        inactive_product.active = False

        for branch_target, expected_to_be_removed in (
            (active_product, False),
            (inactive_product, True),
            (self.factory.makeSourcePackage(), False),
        ):
            for col_name in "owner", "reviewer":
                person = self.factory.makePerson()
                self.factory.makeBranch(
                    target=branch_target, **{col_name: person}
                )
                person_id = person.id
                account_id = person.account.id
                script = self.makeScript([person.name])
                with dbuser("launchpad"):
                    if not expected_to_be_removed:
                        self.assertRaisesWithContent(
                            LaunchpadScriptFailure,
                            "User %s is still referenced" % person.name,
                            self.runScript,
                            script,
                        )
                        self.assertIn(
                            "ERROR User %s is still referenced by 1 "
                            "branch.%s values" % (person.name, col_name),
                            script.logger.getLogBuffer(),
                        )
                        self.assertNotRemoved(account_id, person_id)
                    else:
                        self.runScript(script)
                        self.assertRemoved(account_id, person_id)

    def test_skip_specifications_from_inactive_products(self):
        active_product = self.factory.makeProduct()
        inactive_product = self.factory.makeProduct()
        inactive_product.active = False

        for specification_target, expected_to_be_removed in (
            ({"product": active_product}, False),
            ({"product": inactive_product}, True),
            ({"distribution": self.factory.makeDistribution()}, False),
        ):
            person = self.factory.makePerson()
            person_id = person.id
            account_id = person.account.id

            self.factory.makeSpecification(
                assignee=person, **specification_target
            )
            script = self.makeScript([person.name])
            with dbuser("launchpad"):
                if not expected_to_be_removed:
                    self.assertRaisesWithContent(
                        LaunchpadScriptFailure,
                        "User %s is still referenced" % person.name,
                        self.runScript,
                        script,
                    )
                    self.assertIn(
                        "ERROR User %s is still referenced by 1 "
                        "specification.assignee values" % person.name,
                        script.logger.getLogBuffer(),
                    )
                    self.assertNotRemoved(account_id, person_id)
                else:
                    self.runScript(script)
                    self.assertRemoved(account_id, person_id)

    def test_skips_merged_and_personnotification_references(self):
        from_person = self.factory.makePerson(
            account_status=AccountStatus.PLACEHOLDER
        )
        to_person = self.factory.makePerson()

        # See TestMergePeople._do_merge()
        with dbuser(config.IPersonMergeJobSource.dbuser):
            merge_people(from_person, to_person, None)

        account_id = to_person.account.id
        person_id = to_person.id
        script = self.makeScript([to_person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)

    def test_skips_codereviewvote_registrant(self):
        vote = self.factory.makeCodeReviewVoteReference()
        registrant = vote.registrant
        registrant_account_id = registrant.account.id
        registrant_id = registrant.id
        script = self.makeScript([registrant.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(registrant_account_id, registrant_id)

    def test_skips_sprintspecification_registrant(self):
        person = self.factory.makePerson()
        sprint = self.factory.makeSprint()
        blueprint = self.factory.makeSpecification()
        removeSecurityProxy(blueprint).linkSprint(sprint, person)
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(person.account.id, person.id)

    def test_handles_social_accounts(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id
        getUtility(ISocialAccountSet).new(
            person,
            SocialPlatformType.MATRIX,
            {"username": "test-nickname", "homeserver": "ubuntu.com"},
        )
        getUtility(ISocialAccountSet).new(
            person, SocialPlatformType.GITHUB, {"username": "test-nickname"}
        )
        self.assertEqual(2, len(person.social_accounts))
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertEqual([], person.social_accounts)

    def test_skips_code_review_inline_comments(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id
        previewdiff = self.factory.makePreviewDiff()
        comment = previewdiff.branch_merge_proposal.createComment(
            person, "A review"
        )
        inline_comment_set = getUtility(ICodeReviewInlineCommentSet)
        inline_comment_set.ensureDraft(
            previewdiff, person, {"2": "Needs a docstring."}
        )
        inline_comment_set.publishDraft(previewdiff, person, comment)
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        [inline_comment] = inline_comment_set.getPublished(previewdiff)
        self.assertEqual(person, inline_comment["person"])
        self.assertEqual("Needs a docstring.", inline_comment["text"])

    def test_handles_code_review_inline_comment_drafts(self):
        # Unpublished drafts are only ever visible to their author, who can
        # no longer publish them, so they are removed.
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id
        previewdiff = self.factory.makePreviewDiff()
        getUtility(ICodeReviewInlineCommentSet).ensureDraft(
            previewdiff, person, {"2": "Half-written thought."}
        )
        self.assertEqual(
            {"2": "Half-written thought."},
            getUtility(ICodeReviewInlineCommentSet).getDraft(
                previewdiff, person
            ),
        )
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertIsNone(
            getUtility(ICodeReviewInlineCommentSet).getDraft(
                previewdiff, person
            )
        )

    def test_skips_revision_status_report_creator(self):
        person = self.factory.makePerson()
        person_id = person.id
        account_id = person.account.id
        repository = self.factory.makeGitRepository()
        report = self.factory.makeRevisionStatusReport(
            user=person, git_repository=repository
        )
        script = self.makeScript([person.name])
        with dbuser("launchpad"):
            self.runScript(script)
        self.assertRemoved(account_id, person_id)
        self.assertEqual(person, report.creator)
