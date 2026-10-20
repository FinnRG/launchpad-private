# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Job classes related to BranchMergeProposals are in here.

This includes both jobs for the proposals themselves, or jobs that are
creating proposals, or diffs relating to the proposals.
"""

__all__ = [
    "AutopkgtestRequestNotReady",
    "BranchMergeProposalJob",
    "BranchMergeProposalJobDerived",
    "BranchMergeProposalJobSource",
    "BranchMergeProposalJobType",
    "CodeReviewCommentEmailJob",
    "GenerateIncrementalDiffJob",
    "MergeProposalNeedsReviewEmailJob",
    "MergeProposalUpdatedEmailJob",
    "RequestAutopkgtestJob",
    "ReviewRequestedEmailJob",
    "UpdatePreviewDiffJob",
]

import json
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import six
from lazr.delegates import delegate_to
from lazr.enum import DBEnumeratedType, DBItem
from storm.expr import And, Desc, Or
from storm.locals import Int, Reference, Unicode
from storm.store import Store
from zope.component import getUtility, queryUtility
from zope.interface import implementer, provider
from zope.security.proxy import removeSecurityProxy

from lp.app.errors import NotFoundError
from lp.code.adapters.branch import BranchMergeProposalDelta
from lp.code.enums import (
    BranchMergeProposalStatus,
    BranchType,
    RevisionStatusResult,
)
from lp.code.errors import BranchHasPendingWrites, UpdatePreviewDiffNotReady
from lp.code.interfaces.autopkgtest import (
    IRequestAutopkgtestBackend,
    IRequestAutopkgtestJob,
    IRequestAutopkgtestJobSource,
)
from lp.code.interfaces.branchmergeproposal import (
    IBranchMergeProposalJob,
    IBranchMergeProposalJobSource,
    ICodeReviewCommentEmailJob,
    ICodeReviewCommentEmailJobSource,
    IGenerateIncrementalDiffJob,
    IGenerateIncrementalDiffJobSource,
    IMergeProposalNeedsReviewEmailJob,
    IMergeProposalNeedsReviewEmailJobSource,
    IMergeProposalUpdatedEmailJob,
    IMergeProposalUpdatedEmailJobSource,
    IReviewRequestedEmailJob,
    IReviewRequestedEmailJobSource,
    IUpdatePreviewDiffJob,
    IUpdatePreviewDiffJobSource,
)
from lp.code.interfaces.revision import IRevisionSet
from lp.code.interfaces.revisionstatus import IRevisionStatusReportSet
from lp.code.mail.branch import RecipientReason
from lp.code.mail.branchmergeproposal import BMPMailer
from lp.code.mail.codereviewcomment import CodeReviewCommentMailer
from lp.code.model.branchmergeproposal import BranchMergeProposal
from lp.code.model.diff import PreviewDiff
from lp.codehosting.bzrutils import server
from lp.codehosting.vfs import get_ro_server
from lp.registry.interfaces.person import IPersonSet
from lp.services.config import config
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.stormbase import StormBase
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.model.job import EnumeratedSubclass, Job
from lp.services.job.runner import BaseRunnableJob, BaseRunnableJobSource
from lp.services.mail.sendmail import format_address_for_person
from lp.services.webapp import canonical_url
from lp.soyuz.enums import PackagePublishingStatus


class BranchMergeProposalJobType(DBEnumeratedType):
    """Values that ICodeImportJob.state can take."""

    MERGE_PROPOSAL_NEEDS_REVIEW = DBItem(
        0,
        """
        Merge proposal needs review

        This job sends mail to all interested parties about the proposal.
        """,
    )

    UPDATE_PREVIEW_DIFF = DBItem(
        1,
        """
        Update the preview diff for the BranchMergeProposal.

        This job generates the preview diff for a BranchMergeProposal.
        """,
    )

    CODE_REVIEW_COMMENT_EMAIL = DBItem(
        2,
        """
        Send the code review comment to the subscribers.

        This job sends the email to the merge proposal subscribers and
        reviewers.
        """,
    )

    REVIEW_REQUEST_EMAIL = DBItem(
        3,
        """
        Send the review request email to the requested reviewer.

        This job sends an email to the requested reviewer, or members of the
        requested reviewer team asking them to review the proposal.
        """,
    )

    MERGE_PROPOSAL_UPDATED = DBItem(
        4,
        """
        Merge proposal updated

        This job sends an email to the subscribers informing them of fields
        that have been changed on the merge proposal itself.
        """,
    )

    GENERATE_INCREMENTAL_DIFF = DBItem(
        5,
        """
        Generate incremental diff

        This job generates an incremental diff for a merge proposal.""",
    )

    CREATE_VALIDATION_ARCHIVE = DBItem(
        6,
        """
        Create a validation archive

        Create a contributor-owned PPA and request a source package Git build.
        """,
    )

    REQUEST_AUTOPKGTEST = DBItem(
        7,
        """
        Request autopkgtests

        Request tests for a validation package and architecture.
        """,
    )


@implementer(IBranchMergeProposalJob)
class BranchMergeProposalJob(StormBase):
    """Base class for jobs related to branch merge proposals."""

    __storm_table__ = "BranchMergeProposalJob"

    id = Int(primary=True)

    jobID = Int("job")
    job = Reference(jobID, Job.id)

    branch_merge_proposalID = Int("branch_merge_proposal", allow_none=False)
    branch_merge_proposal = Reference(
        branch_merge_proposalID, BranchMergeProposal.id
    )

    job_type = DBEnum(enum=BranchMergeProposalJobType, allow_none=False)

    _json_data = Unicode("json_data")

    @property
    def metadata(self):
        return json.loads(self._json_data)

    def __init__(self, branch_merge_proposal, job_type, metadata):
        """Constructor.

        :param branch_merge_proposal: The proposal this job relates to.
        :param job_type: The BranchMergeProposalJobType of this job.
        :param metadata: The type-specific variables, as a JSON-compatible
            dict.
        """
        super().__init__()
        json_data = json.dumps(metadata)
        self.job = Job()
        self.branch_merge_proposal = branch_merge_proposal
        self.job_type = job_type
        # XXX AaronBentley 2009-01-29 bug=322819: This should be a bytestring,
        # but the DB representation is unicode.
        self._json_data = six.ensure_text(json_data)

    def sync(self):
        store = Store.of(self)
        store.flush()
        store.autoreload(self)

    def destroySelf(self):
        Store.of(self).remove(self)

    @classmethod
    def get(klass, key):
        """Return the instance of this class whose key is supplied.

        :raises: NotFoundError
        """
        instance = IStore(klass).get(klass, key)
        if instance is None:
            raise NotFoundError(
                "No occurrence of %s has key %s" % (klass.__name__, key)
            )
        return instance

    def makeDerived(self):
        return BranchMergeProposalJobDerived.makeSubclass(self)


@delegate_to(IBranchMergeProposalJob)
class BranchMergeProposalJobDerived(
    BaseRunnableJob, metaclass=EnumeratedSubclass
):
    """Intermediate class for deriving from BranchMergeProposalJob."""

    def __init__(self, job):
        self.context = job

    def __repr__(self):
        bmp = self.branch_merge_proposal
        return "<%(job_type)s job for merge %(merge_id)s on %(branch)s>" % {
            "job_type": self.context.job_type.name,
            "merge_id": bmp.id,
            "branch": bmp.merge_source.unique_name,
        }

    @classmethod
    def create(cls, bmp):
        """See `IMergeProposalCreationJob`."""
        return cls._create(bmp, {})

    @classmethod
    def _create(cls, bmp, metadata):
        base_job = BranchMergeProposalJob(bmp, cls.class_job_type, metadata)
        job = cls(base_job)
        job.celeryRunOnCommit()
        IStore(BranchMergeProposalJob).flush()
        return job

    @classmethod
    def get(cls, job_id):
        """Get a job by id.

        :return: the BranchMergeProposalJob with the specified id, as the
            current BranchMergeProposalJobDereived subclass.
        :raises: NotFoundError if there is no job with the specified id,
            or its job_type does not match the desired subclass.
        """
        job = BranchMergeProposalJob.get(job_id)
        if job.job_type != cls.class_job_type:
            raise NotFoundError(
                "No object found with id %d and type %s"
                % (job_id, cls.class_job_type.title)
            )
        return cls(job)

    @classmethod
    def iterReady(klass):
        """Iterate through all ready BranchMergeProposalJobs."""
        from lp.code.model.branch import Branch

        jobs = IPrimaryStore(Branch).find(
            (BranchMergeProposalJob),
            And(
                BranchMergeProposalJob.job_type == klass.class_job_type,
                BranchMergeProposalJob.job == Job.id,
                Job.id.is_in(Job.ready_jobs),
                BranchMergeProposalJob.branch_merge_proposal
                == BranchMergeProposal.id,
                Or(
                    And(
                        BranchMergeProposal.source_branch == Branch.id,
                        # A proposal isn't considered ready if it has no
                        # revisions, or if it is hosted but pending a mirror.
                        Branch.revision_count > 0,
                        Or(
                            Branch.next_mirror_time == None,
                            Branch.branch_type != BranchType.HOSTED,
                        ),
                    ),
                    BranchMergeProposal.source_git_repository != None,
                ),
            ),
        )
        jobs = jobs.config(distinct=True)
        return (klass(job) for job in jobs)

    def getOopsVars(self):
        """See `IRunnableJob`."""
        vars = BaseRunnableJob.getOopsVars(self)
        bmp = self.context.branch_merge_proposal
        vars.extend(
            [
                ("branchmergeproposal_job_id", self.context.id),
                ("branchmergeproposal_job_type", self.context.job_type.title),
                ("merge_source", bmp.merge_source.unique_name),
                ("merge_target", bmp.merge_target.unique_name),
            ]
        )
        return vars


@implementer(IMergeProposalNeedsReviewEmailJob)
@provider(IMergeProposalNeedsReviewEmailJobSource)
class MergeProposalNeedsReviewEmailJob(BranchMergeProposalJobDerived):
    """See `IMergeProposalNeedsReviewEmailJob`."""

    class_job_type = BranchMergeProposalJobType.MERGE_PROPOSAL_NEEDS_REVIEW

    config = config.IBranchMergeProposalJobSource

    def run(self):
        """See `IMergeProposalNeedsReviewEmailJob`."""
        mailer = BMPMailer.forCreation(
            self.branch_merge_proposal, self.branch_merge_proposal.registrant
        )
        mailer.sendAll()

    def getOopsRecipients(self):
        return [self.branch_merge_proposal.registrant.preferredemail.email]

    def getOperationDescription(self):
        return "notifying people about the proposal to merge %s into %s" % (
            self.branch_merge_proposal.merge_source.identity,
            self.branch_merge_proposal.merge_target.identity,
        )


@implementer(IUpdatePreviewDiffJob)
@provider(IUpdatePreviewDiffJobSource)
class UpdatePreviewDiffJob(BranchMergeProposalJobDerived):
    """A job to update the preview diff for a branch merge proposal.

    Provides class methods to create and retrieve such jobs.
    """

    class_job_type = BranchMergeProposalJobType.UPDATE_PREVIEW_DIFF

    # Not bzrsyncd_job: bzrsyncd is being decommissioned, and this job
    # also handles Git merge proposals, which bzrsyncd never served.
    task_queue = "launchpad_job"

    config = config.IBranchMergeProposalJobSource

    user_error_types = (UpdatePreviewDiffNotReady,)

    retry_error_types = (BranchHasPendingWrites,)

    max_retries = 20

    lease_duration = timedelta(minutes=10)

    def checkReady(self):
        """Is this job ready to run?"""
        bmp = self.branch_merge_proposal
        url = canonical_url(bmp)
        if bmp.source_branch is not None:
            if bmp.source_branch.last_scanned_id is None:
                raise UpdatePreviewDiffNotReady(
                    "The source branch of %s has no revisions." % url
                )
            if bmp.target_branch.last_scanned_id is None:
                raise UpdatePreviewDiffNotReady(
                    "The target branch of %s has no revisions." % url
                )
            if bmp.source_branch.pending_writes:
                raise BranchHasPendingWrites(
                    "The source branch of %s has pending writes." % url
                )

    def run(self):
        """See `IRunnableJob`."""
        self.checkReady()
        if self.branch_merge_proposal.source_git_ref is not None:
            # Update related bug links based on commits in the source branch.
            self.branch_merge_proposal.updateRelatedBugsFromSource()
        with ExitStack() as stack:
            if self.branch_merge_proposal.source_branch is not None:
                stack.enter_context(server(get_ro_server(), no_replace=True))
            with BranchMergeProposalDelta.monitor(self.branch_merge_proposal):
                PreviewDiff.fromBranchMergeProposal(self.branch_merge_proposal)

    def getOperationDescription(self):
        return "generating the diff for a merge proposal"

    def getErrorRecipients(self):
        """Return a list of email-ids to notify about user errors."""
        registrant = self.branch_merge_proposal.registrant
        return format_address_for_person(registrant)


@implementer(ICodeReviewCommentEmailJob)
@provider(ICodeReviewCommentEmailJobSource)
class CodeReviewCommentEmailJob(BranchMergeProposalJobDerived):
    """A job to send a code review comment.

    Provides class methods to create and retrieve such jobs.
    """

    class_job_type = BranchMergeProposalJobType.CODE_REVIEW_COMMENT_EMAIL

    config = config.IBranchMergeProposalJobSource

    def run(self):
        """See `IRunnableJob`."""
        mailer = CodeReviewCommentMailer.forCreation(self.code_review_comment)
        mailer.sendAll()

    @classmethod
    def create(cls, code_review_comment):
        """See `ICodeReviewCommentEmailJobSource`."""
        metadata = cls.getMetadata(code_review_comment)
        bmp = code_review_comment.branch_merge_proposal
        return cls._create(bmp, metadata)

    @staticmethod
    def getMetadata(code_review_comment):
        return {"code_review_comment": code_review_comment.id}

    @property
    def code_review_comment(self):
        """Get the code review comment."""
        return self.branch_merge_proposal.getComment(
            self.metadata["code_review_comment"]
        )

    def getOopsVars(self):
        """See `IRunnableJob`."""
        vars = BranchMergeProposalJobDerived.getOopsVars(self)
        vars.extend(
            [
                ("code_review_comment", self.metadata["code_review_comment"]),
            ]
        )
        return vars

    def getErrorRecipients(self):
        """Return a list of email-ids to notify about user errors."""
        commenter = self.code_review_comment.message.owner
        return [format_address_for_person(commenter)]

    def getOperationDescription(self):
        return "emailing a code review comment"


@implementer(IReviewRequestedEmailJob)
@provider(IReviewRequestedEmailJobSource)
class ReviewRequestedEmailJob(BranchMergeProposalJobDerived):
    """Send email to the reviewer telling them to review the proposal.

    Provides class methods to create and retrieve such jobs.
    """

    class_job_type = BranchMergeProposalJobType.REVIEW_REQUEST_EMAIL

    config = config.IBranchMergeProposalJobSource

    def run(self):
        """See `IRunnableJob`."""
        reason = RecipientReason.forReviewer(
            self.branch_merge_proposal, True, self.reviewer
        )
        mailer = BMPMailer.forReviewRequest(
            reason, self.branch_merge_proposal, self.requester
        )
        mailer.sendAll()

    @classmethod
    def create(cls, review_request):
        """See `IReviewRequestedEmailJobSource`."""
        metadata = cls.getMetadata(review_request)
        bmp = review_request.branch_merge_proposal
        return cls._create(bmp, metadata)

    @staticmethod
    def getMetadata(review_request):
        return {
            "reviewer": review_request.reviewer.name,
            "requester": review_request.registrant.name,
        }

    @property
    def reviewer(self):
        """The person or team who has been asked to review."""
        return getUtility(IPersonSet).getByName(self.metadata["reviewer"])

    @property
    def requester(self):
        """The person who requested the review to be done."""
        return getUtility(IPersonSet).getByName(self.metadata["requester"])

    def getOopsVars(self):
        """See `IRunnableJob`."""
        vars = BranchMergeProposalJobDerived.getOopsVars(self)
        vars.extend(
            [
                ("reviewer", self.metadata["reviewer"]),
                ("requester", self.metadata["requester"]),
            ]
        )
        return vars

    def getErrorRecipients(self):
        """Return a list of email-ids to notify about user errors."""
        recipients = []
        if self.requester is not None:
            recipients.append(format_address_for_person(self.requester))
        return recipients

    def getOperationDescription(self):
        return "emailing a reviewer requesting a review"


@implementer(IMergeProposalUpdatedEmailJob)
@provider(IMergeProposalUpdatedEmailJobSource)
class MergeProposalUpdatedEmailJob(BranchMergeProposalJobDerived):
    """Send email to the subscribers informing them of updated fields.

    When attributes of the merge proposal are edited, we inform the
    subscribers.
    """

    class_job_type = BranchMergeProposalJobType.MERGE_PROPOSAL_UPDATED

    config = config.IBranchMergeProposalJobSource

    def run(self):
        """See `IRunnableJob`."""
        mailer = BMPMailer.forModification(
            self.branch_merge_proposal, self.delta_text, self.editor
        )
        mailer.sendAll()

    @classmethod
    def create(cls, merge_proposal, delta_text, editor):
        """See `IReviewRequestedEmailJobSource`."""
        metadata = cls.getMetadata(delta_text, editor)
        return cls._create(merge_proposal, metadata)

    @staticmethod
    def getMetadata(delta_text, editor):
        metadata = {"delta_text": delta_text}
        if editor is not None:
            metadata["editor"] = editor.name
        return metadata

    @property
    def editor(self):
        """The person who updated the merge proposal."""
        editor_name = self.metadata.get("editor")
        if editor_name is None:
            return None
        else:
            return getUtility(IPersonSet).getByName(editor_name)

    @property
    def delta_text(self):
        """The changes that were made to the merge proposal."""
        return self.metadata["delta_text"]

    def getOopsVars(self):
        """See `IRunnableJob`."""
        vars = BranchMergeProposalJobDerived.getOopsVars(self)
        vars.extend(
            [
                ("editor", self.metadata.get("editor", "(not set)")),
                ("delta_text", self.metadata["delta_text"]),
            ]
        )
        return vars

    def getErrorRecipients(self):
        """Return a list of email-ids to notify about user errors."""
        recipients = []
        if self.editor is not None:
            recipients.append(format_address_for_person(self.editor))
        return recipients

    def getOperationDescription(self):
        return "emailing subscribers about merge proposal changes"


@implementer(IGenerateIncrementalDiffJob)
@provider(IGenerateIncrementalDiffJobSource)
class GenerateIncrementalDiffJob(BranchMergeProposalJobDerived):
    """A job to generate an incremental diff for a branch merge proposal.

    Provides class methods to create and retrieve such jobs.
    """

    class_job_type = BranchMergeProposalJobType.GENERATE_INCREMENTAL_DIFF

    task_queue = "bzrsyncd_job"

    config = config.IBranchMergeProposalJobSource

    lease_duration = timedelta(minutes=10)

    def run(self):
        revision_set = getUtility(IRevisionSet)
        old_revision = revision_set.getByRevisionId(self.old_revision_id)
        new_revision = revision_set.getByRevisionId(self.new_revision_id)
        with server(get_ro_server(), no_replace=True):
            self.branch_merge_proposal.generateIncrementalDiff(
                old_revision, new_revision
            )

    @classmethod
    def create(cls, merge_proposal, old_revision_id, new_revision_id):
        metadata = cls.getMetadata(old_revision_id, new_revision_id)
        return cls._create(merge_proposal, metadata)

    @staticmethod
    def getMetadata(old_revision_id, new_revision_id):
        return {
            "old_revision_id": old_revision_id,
            "new_revision_id": new_revision_id,
        }

    @property
    def old_revision_id(self):
        """The old revision id for the diff."""
        return self.metadata["old_revision_id"]

    @property
    def new_revision_id(self):
        """The new revision id for the diff."""
        return self.metadata["new_revision_id"]

    def getOopsVars(self):
        """See `IRunnableJob`."""
        vars = BranchMergeProposalJobDerived.getOopsVars(self)
        vars.extend(
            [
                ("old_revision_id", self.metadata["old_revision_id"]),
                ("new_revision_id", self.metadata["new_revision_id"]),
            ]
        )
        return vars

    def getOperationDescription(self):
        return "generating an incremental diff for a merge proposal"

    def getErrorRecipients(self):
        """Return a list of email-ids to notify about user errors."""
        registrant = self.branch_merge_proposal.registrant
        return format_address_for_person(registrant)


class AutopkgtestRequestNotReady(Exception):
    """The request cannot yet be executed safely."""


@implementer(IRequestAutopkgtestJob)
@provider(IRequestAutopkgtestJobSource)
class RequestAutopkgtestJob(BranchMergeProposalJobDerived):
    """Request autopkgtests for one validation package and architecture.

    Created when a binary package is published, this job saves the archive,
    source version, built commit, architecture, and validation options. A
    worker checks that the validation is still active and the publication
    still matches those details, then delegates submission to the configured
    autopkgtest backend.
    """

    class_job_type = BranchMergeProposalJobType.REQUEST_AUTOPKGTEST
    config = config.IBranchMergeProposalJobSource
    retry_error_types = (AutopkgtestRequestNotReady,)
    max_retries = 25

    @classmethod
    def create(cls, validation, publication, attempt=0):
        from lp.soyuz.model.mergeproposalvalidation import (
            MergeProposalValidation,
        )

        if attempt is not None and (type(attempt) is not int or attempt < 0):
            raise ValueError("Attempt must be a nonnegative integer or None")
        source = publication.build.source_package_release
        git_build = source.source_package_git_build
        store = IPrimaryStore(MergeProposalValidation)
        store.flush()
        store.execute(
            "SELECT id FROM MergeProposalValidation WHERE id = %s FOR UPDATE",
            (validation.id,),
        )
        store.autoreload(validation)
        if (
            validation.status
            not in (
                RevisionStatusResult.WAITING,
                RevisionStatusResult.RUNNING,
            )
            or validation.date_finished is not None
        ):
            return None
        identity = {
            "validation_id": validation.id,
            "source_package_release_id": source.id,
            "distro_arch_series_id": publication.distroarchseries.id,
        }
        existing = store.find(
            BranchMergeProposalJob,
            branch_merge_proposal=validation.branch_merge_proposal,
            job_type=cls.class_job_type,
        )
        matching = [
            job
            for job in existing
            if all(
                job.metadata.get(key) == value
                for key, value in identity.items()
            )
        ]
        if attempt is None:
            attempt = (
                max(
                    (job.metadata.get("attempt", 0) for job in matching),
                    default=-1,
                )
                + 1
            )
        for job in matching:
            if job.metadata.get("attempt", 0) == attempt:
                return cls(job)
        metadata = dict(
            identity,
            attempt=attempt,
            publication_id=publication.id,
            source_package_git_build_id=git_build.id,
            archive_id=publication.archive.id,
            commit_sha1=git_build.commit_sha1,
            source_package_name=source.sourcepackagename.name,
            source_package_version=str(source.version),
            parameters={
                key: value
                for key, value in validation.parameters.items()
                if key.startswith("autopkgtest-")
            },
        )
        job = cls._create(validation.branch_merge_proposal, metadata)
        job.getOrCreateRevisionStatusReport(validation, publication)
        return job

    def getOrCreateRevisionStatusReport(self, validation, publication):
        """Create the attempt's waiting check in the caller's transaction."""
        from lp.code.model.revisionstatus import RevisionStatusReport

        metadata = self.metadata
        report_id = metadata.get(
            "report_id", (metadata.get("autopkgtest") or {}).get("report_id")
        )
        if report_id is not None:
            report = IStore(RevisionStatusReport).get(
                RevisionStatusReport, report_id
            )
            if report is None:
                raise AutopkgtestRequestNotReady(
                    "Autopkgtest report is missing"
                )
            return report
        request_key = str(uuid4())
        arch = publication.distroarchseries
        source = publication.build.source_package_release
        report = removeSecurityProxy(getUtility(IRevisionStatusReportSet)).new(
            creator=validation.requester,
            title="autopkgtest: %s (%s)"
            % (metadata["source_package_name"], arch.architecturetag),
            git_repository=self.branch_merge_proposal.source_git_repository,
            commit_sha1=metadata["commit_sha1"],
            result=RevisionStatusResult.WAITING,
            distro_arch_series=arch,
            group_name="package-validation",
            source_package_git_build=source.source_package_git_build,
            properties={
                "validation_id": validation.id,
                "request_job_id": self.id,
                "request_key": request_key,
                "attempt": metadata.get("attempt", 0),
                "check_key": "autopkgtest/%s" % arch.architecturetag,
            },
        )
        IStore(report).flush()
        metadata.update(report_id=report.id, request_key=request_key)
        self.context._json_data = json.dumps(metadata)
        return report

    @classmethod
    def retry(cls, job):
        from lp.code.model.revisionstatus import RevisionStatusReport
        from lp.code.scripts.revisionstatuswatch import FINAL_RESULTS
        from lp.soyuz.model.mergeproposalvalidation import (
            MergeProposalValidation,
        )
        from lp.soyuz.model.publishing import BinaryPackagePublishingHistory

        metadata = job.metadata
        report_id = metadata.get(
            "report_id", (metadata.get("autopkgtest") or {}).get("report_id")
        )
        store = IPrimaryStore(MergeProposalValidation)
        report = (
            None
            if report_id is None
            else store.get(RevisionStatusReport, report_id)
        )
        if report is None or report.result not in FINAL_RESULTS:
            raise ValueError(
                "Only terminal autopkgtest reports can be retried"
            )
        validation = store.get(
            MergeProposalValidation, metadata["validation_id"]
        )
        publication = store.get(
            BinaryPackagePublishingHistory, metadata["publication_id"]
        )
        if validation is None or publication is None:
            raise ValueError("The validation or publication no longer exists")
        store.execute(
            "SELECT id FROM MergeProposalValidation WHERE id = %s FOR UPDATE",
            (validation.id,),
        )
        matching = store.find(
            BranchMergeProposalJob,
            branch_merge_proposal=validation.branch_merge_proposal,
            job_type=cls.class_job_type,
        )
        if any(
            candidate.metadata.get("validation_id") == validation.id
            and candidate.metadata.get("source_package_release_id")
            == metadata["source_package_release_id"]
            and candidate.metadata.get("distro_arch_series_id")
            == metadata["distro_arch_series_id"]
            and candidate.metadata.get("attempt", 0)
            > metadata.get("attempt", 0)
            for candidate in matching
        ):
            raise ValueError(
                "Only the latest autopkgtest attempt can be retried"
            )
        try:
            context = job.getAutopkgtestContext()
        except AutopkgtestRequestNotReady as error:
            raise ValueError("Autopkgtests cannot be retried: %s" % error)
        if context is None:
            raise ValueError("The validation is no longer active")
        retry_job = cls.create(validation, publication, attempt=None)
        if retry_job is not None:
            retry_metadata = retry_job.metadata
            retry_metadata["parameters"] = metadata["parameters"]
            retry_job.context._json_data = json.dumps(retry_metadata)
        return retry_job

    def updateAutopkgtestState(self, state):
        metadata = self.metadata
        metadata["autopkgtest"] = state
        self.context._json_data = json.dumps(metadata)

    def getAutopkgtestContext(self):
        from lp.soyuz.model.mergeproposalvalidation import (
            MergeProposalValidation,
        )
        from lp.soyuz.model.publishing import BinaryPackagePublishingHistory

        metadata = self.metadata
        store = IPrimaryStore(MergeProposalValidation)
        validation = store.get(
            MergeProposalValidation, metadata["validation_id"]
        )
        publication = store.get(
            BinaryPackagePublishingHistory, metadata["publication_id"]
        )
        if validation is None or publication is None:
            return
        proposal = self.branch_merge_proposal
        if (
            validation.status
            not in (
                RevisionStatusResult.WAITING,
                RevisionStatusResult.RUNNING,
            )
            or validation.date_finished is not None
            or proposal.queue_status
            in (
                BranchMergeProposalStatus.MERGED,
                BranchMergeProposalStatus.REJECTED,
                BranchMergeProposalStatus.SUPERSEDED,
            )
            or not publication.archive.is_ppa
            or publication.archive.private
            or proposal.source_git_repository is None
            or proposal.target_git_repository is None
            or proposal.source_git_repository.private
            or proposal.target_git_repository.private
        ):
            return
        if publication.status != PackagePublishingStatus.PUBLISHED:
            raise AutopkgtestRequestNotReady("Package is not published.")
        binary_build = publication.build
        source = (
            None
            if binary_build is None
            else binary_build.source_package_release
        )
        git_build = None if source is None else source.source_package_git_build
        if (
            validation.branch_merge_proposal_id != proposal.id
            or validation.archive_id != metadata["archive_id"]
            or validation.commit_sha1 != metadata["commit_sha1"]
            or publication.archive_id != metadata["archive_id"]
            or publication.distroarchseries.id
            != metadata["distro_arch_series_id"]
            or source is None
            or source.id != metadata["source_package_release_id"]
            or source.sourcepackagename.name != metadata["source_package_name"]
            or str(source.version) != metadata["source_package_version"]
            or git_build is None
            or git_build.id != metadata["source_package_git_build_id"]
            or git_build.commit_sha1 != metadata["commit_sha1"]
            or git_build.git_repository != proposal.source_git_repository
        ):
            raise AutopkgtestRequestNotReady("Validation identity changed.")
        return validation, publication

    def run(self):
        context = self.getAutopkgtestContext()
        backend = queryUtility(IRequestAutopkgtestBackend)
        if context is None:
            if backend is not None:
                backend.cancelTests(self)
            return
        if backend is None:
            raise AutopkgtestRequestNotReady(
                "Autopkgtest request execution is not configured."
            )
        backend.requestTests(self)


@provider(IBranchMergeProposalJobSource)
class BranchMergeProposalJobSource(BaseRunnableJobSource):
    """Provide a job source for all merge proposal jobs.

    Only one job for any particular merge proposal is returned.
    """

    @staticmethod
    def get(job_id):
        """Get a job by id.

        :return: the BranchMergeProposalJob with the specified id, as the
            current BranchMergeProposalJobDereived subclass.
        :raises: NotFoundError if there is no job with the specified id,
            or its job_type does not match the desired subclass.
        """
        job = BranchMergeProposalJob.get(job_id)
        return job.makeDerived()

    @staticmethod
    def iterReady(job_type=None):
        clauses = [
            BranchMergeProposalJob.job == Job.id,
            Job._status.is_in([JobStatus.WAITING, JobStatus.RUNNING]),
            BranchMergeProposalJob.branch_merge_proposal
            == BranchMergeProposal.id,
        ]
        if job_type is not None:
            clauses.append(BranchMergeProposalJob.job_type == job_type)
        else:
            clauses.append(
                BranchMergeProposalJob.job_type
                != BranchMergeProposalJobType.CREATE_VALIDATION_ARCHIVE
            )
        jobs = IPrimaryStore(BranchMergeProposalJob).find(
            (BranchMergeProposalJob, Job, BranchMergeProposal), And(*clauses)
        )
        # Order by the job status first (to get running before waiting), then
        # the date_created, then job type.  This should give us all creation
        # jobs before comment jobs.
        jobs = jobs.order_by(
            Desc(Job._status),
            Job.date_created,
            Desc(BranchMergeProposalJob.job_type),
        )
        # Now only return one job for any given merge proposal.
        ready_jobs = []
        seen_merge_proposals = set()
        for bmp_job, job, bmp in jobs:
            # If we've seen this merge proposal already, skip this job.
            if bmp.id in seen_merge_proposals:
                continue
            # We have now seen this merge proposal.
            seen_merge_proposals.add(bmp.id)
            # If the job is running or can't currently be run due to its
            # lease or its start time, then skip it.
            if (
                job.status == JobStatus.RUNNING
                or (
                    job.lease_expires is not None
                    and job.lease_expires >= datetime.now(timezone.utc)
                )
                or (
                    job.scheduled_start is not None
                    and job.scheduled_start > datetime.now(timezone.utc)
                )
            ):
                continue
            derived_job = bmp_job.makeDerived()
            # If the job is an update preview diff, then check that it is
            # ready.
            if IUpdatePreviewDiffJob.providedBy(derived_job):
                try:
                    derived_job.checkReady()
                except (UpdatePreviewDiffNotReady, BranchHasPendingWrites):
                    # If the job was created under 15 minutes ago wait a bit.
                    minutes = (
                        config.codehosting.update_preview_diff_ready_timeout
                    )
                    cut_off_time = datetime.now(timezone.utc) - timedelta(
                        minutes=minutes
                    )
                    if job.date_created > cut_off_time:
                        continue
            ready_jobs.append(derived_job)
        return ready_jobs
