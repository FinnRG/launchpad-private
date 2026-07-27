# Copyright 2009-2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Database garbage collection."""

__all__ = [
    "DailyDatabaseGarbageCollector",
    "FrequentDatabaseGarbageCollector",
    "HourlyDatabaseGarbageCollector",
    "load_garbo_job_state",
    "save_garbo_job_state",
]

import json
import logging
import multiprocessing
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import iso8601
import six
import transaction
from contrib.glock import GlobalLock, LockAlreadyAcquired
from psycopg2 import IntegrityError
from storm.databases.postgres import Returning
from storm.expr import (
    SQL,
    And,
    Cast,
    Coalesce,
    Except,
    In,
    Join,
    Max,
    Min,
    Or,
    Row,
    Select,
    Update,
)
from storm.info import ClassAlias
from storm.store import EmptyResultSet
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.answers.model.answercontact import AnswerContact
from lp.archivepublisher.publishing import BY_HASH_STAY_OF_EXECUTION
from lp.archiveuploader.tagfiles import (
    TagFileParseError,
    parse_tagfile_content,
)
from lp.bugs.interfaces.bug import IBugSet
from lp.bugs.model.bug import Bug
from lp.bugs.model.bugattachment import BugAttachment
from lp.bugs.model.bugnotification import BugNotification
from lp.bugs.model.bugwatch import BugWatchActivity
from lp.bugs.scripts.checkwatches.scheduler import (
    MAX_SAMPLE_SIZE,
    BugWatchScheduler,
)
from lp.code.enums import GitRepositoryStatus, RevisionStatusArtifactType
from lp.code.interfaces.revision import IRevisionSet
from lp.code.model.codeimportevent import CodeImportEvent
from lp.code.model.codeimportresult import CodeImportResult
from lp.code.model.diff import Diff, PreviewDiff
from lp.code.model.gitrepository import GitRepository
from lp.code.model.revision import RevisionAuthor, RevisionCache
from lp.code.model.revisionstatus import RevisionStatusArtifact
from lp.oci.model.ocirecipebuild import OCIFile
from lp.registry.interfaces.person import IPersonSet
from lp.registry.model.distribution import Distribution
from lp.registry.model.gpgkey import GPGKey
from lp.registry.model.person import Person
from lp.registry.model.product import Product
from lp.registry.model.sourcepackagename import SourcePackageName
from lp.registry.model.teammembership import TeamMembership, TeamParticipation
from lp.services.config import config
from lp.services.database import postgresql
from lp.services.database.bulk import create, dbify_value, load_related
from lp.services.database.constants import UTC_NOW
from lp.services.database.interfaces import IPrimaryStore
from lp.services.database.sqlbase import (
    convert_storm_clause_to_string,
    cursor,
    session_store,
    sqlvalues,
)
from lp.services.database.stormexpr import BulkUpdate, Values
from lp.services.encoding import guess as guess_encoding
from lp.services.features import (
    getFeatureFlag,
    install_feature_controller,
    make_script_feature_controller,
)
from lp.services.identity.interfaces.account import AccountStatus
from lp.services.identity.interfaces.emailaddress import EmailAddressStatus
from lp.services.identity.model.account import Account
from lp.services.identity.model.emailaddress import EmailAddress
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.model.job import Job
from lp.services.librarian.model import TimeLimitedToken
from lp.services.log.logger import PrefixFilter
from lp.services.looptuner import TunableLoop
from lp.services.mail.helpers import get_email_template
from lp.services.mail.mailwrapper import MailWrapper
from lp.services.mail.sendmail import (
    format_address,
    set_immediate_mail_delivery,
    simple_sendmail,
)
from lp.services.openid.model.openidconsumer import OpenIDConsumerNonce
from lp.services.propertycache import cachedproperty
from lp.services.scripts.base import (
    LOCK_PATH,
    LaunchpadCronScript,
    SilentLaunchpadScriptFailure,
)
from lp.services.session.model import SessionData
from lp.services.signing.interfaces.signingkey import IArchiveSigningKeySet
from lp.services.verification.model.logintoken import LoginToken
from lp.services.webapp.publisher import canonical_url
from lp.services.webhooks.interfaces import IWebhookJobSource
from lp.services.webhooks.model import WebhookJob
from lp.snappy.model.snapbuild import SnapFile
from lp.snappy.model.snapbuildjob import SnapBuildJobType
from lp.soyuz.enums import (
    ArchivePublishingMethod,
    ArchivePurpose,
    ArchiveRepositoryFormat,
    ArchiveSubscriberStatus,
)
from lp.soyuz.interfaces.publishing import active_publishing_status
from lp.soyuz.model.archive import Archive
from lp.soyuz.model.archiveauthtoken import ArchiveAuthToken
from lp.soyuz.model.archivefile import ArchiveFile
from lp.soyuz.model.archivesubscriber import ArchiveSubscriber
from lp.soyuz.model.binarypackagebuild import BinaryPackageBuild
from lp.soyuz.model.binarypackagerelease import BinaryPackageRelease
from lp.soyuz.model.distributionsourcepackagecache import (
    DistributionSourcePackageCache,
)
from lp.soyuz.model.livefsbuild import LiveFSFile
from lp.soyuz.model.publishing import (
    BinaryPackagePublishingHistory,
    SourcePackagePublishingHistory,
)
from lp.soyuz.model.reporting import LatestPersonSourcePackageReleaseCache
from lp.soyuz.model.sourcepackagerelease import SourcePackageRelease
from lp.translations.interfaces.potemplate import IPOTemplateSet
from lp.translations.model.potmsgset import POTMsgSet
from lp.translations.model.potranslation import POTranslation
from lp.translations.model.translationmessage import TranslationMessage
from lp.translations.model.translationtemplateitem import (
    TranslationTemplateItem,
)
from lp.translations.scripts.scrub_pofiletranslator import (
    ScrubPOFileTranslator,
)

ONE_DAY_IN_SECONDS = 24 * 60 * 60


# Garbo jobs may choose to persist state between invocations, if it is likely
# that not all data can be processed in a single run. These utility methods
# provide convenient access to that state data.
def load_garbo_job_state(job_name):
    # Load the json state data for the given job name.
    job_data = (
        IPrimaryStore(Person)
        .execute(
            "SELECT json_data FROM GarboJobState WHERE name = ?",
            params=(six.ensure_text(job_name),),
        )
        .get_one()
    )
    if job_data:
        return json.loads(job_data[0])
    return None


def save_garbo_job_state(job_name, job_data):
    # Save the json state data for the given job name.
    store = IPrimaryStore(Person)
    json_data = json.dumps(job_data, ensure_ascii=False)
    result = store.execute(
        "UPDATE GarboJobState SET json_data = ? WHERE name = ?",
        params=(json_data, six.ensure_text(job_name)),
    )
    if result.rowcount == 0:
        store.execute(
            "INSERT INTO GarboJobState(name, json_data) " "VALUES (?, ?)",
            params=(six.ensure_text(job_name), six.ensure_text(json_data)),
        )


class BulkPruner(TunableLoop):
    """A abstract ITunableLoop base class for simple pruners.

    This is designed for the case where calculating the list of items
    is expensive, and this list may be huge. For this use case, it
    is impractical to calculate a batch of ids to remove each
    iteration.

    One approach is using a temporary table, populating it
    with the set of items to remove at the start. However, this
    approach can perform badly as you either need to prune the
    temporary table as you go, or using OFFSET to skip to the next
    batch to remove which gets slower as we progress further through
    the list.

    Instead, this implementation declares a CURSOR that can be used
    across multiple transactions, allowing us to calculate the set
    of items to remove just once and iterate over it, avoiding the
    seek-to-batch issues with a temporary table and OFFSET yet
    deleting batches of rows in separate transactions.
    """

    # The Storm database class for the table we are removing records
    # from. Must be overridden.
    target_table_class = None

    # The column name in target_table we use as the key. The type must
    # match that returned by the ids_to_prune_query and the
    # target_table_key_type. May be overridden.
    target_table_key = "id"

    # SQL type of the target_table_key. May be overridden.
    target_table_key_type = "id integer"

    # An SQL query returning a list of ids to remove from target_table.
    # The query must return a single column named 'id' and should not
    # contain duplicates. Must be overridden.
    ids_to_prune_query = None

    # See `TunableLoop`. May be overridden.
    maximum_chunk_size = 10000

    def getStore(self):
        """The primary Store for the table we are pruning.

        May be overridden.
        """
        return IPrimaryStore(self.target_table_class)

    _unique_counter = 0

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)

        self.store = self.getStore()
        self.target_table_name = self.target_table_class.__storm_table__

        self._unique_counter += 1
        self.cursor_name = (
            "bulkprunerid_%s_%d"
            % (self.__class__.__name__, self._unique_counter)
        ).lower()

        # Open the cursor.
        self.store.execute(
            "DECLARE %s NO SCROLL CURSOR WITH HOLD FOR %s"
            % (self.cursor_name, self.ids_to_prune_query)
        )

    _num_removed = None

    def isDone(self):
        """See `ITunableLoop`."""
        return self._num_removed == 0

    def __call__(self, chunk_size):
        """See `ITunableLoop`."""
        result = self.store.execute(
            """
            DELETE FROM %s
            WHERE (%s) IN (
                SELECT * FROM
                cursor_fetch('%s', %d) AS f(%s))
            """
            % (
                self.target_table_name,
                self.target_table_key,
                self.cursor_name,
                chunk_size,
                self.target_table_key_type,
            )
        )
        self._num_removed = result.rowcount
        transaction.commit()

    def cleanUp(self):
        """See `ITunableLoop`."""
        self.store.execute("CLOSE %s" % self.cursor_name)


class LoginTokenPruner(BulkPruner):
    """Remove old LoginToken rows.

    After 1 year, they are useless even for archaeology.
    """

    target_table_class = LoginToken
    ids_to_prune_query = """
        SELECT id FROM LoginToken WHERE
        created < CURRENT_TIMESTAMP - CAST('1 year' AS interval)
        """


class POTranslationPruner(BulkPruner):
    """Remove unlinked POTranslation entries.

    XXX bug=723596 StuartBishop: This job only needs to run once per month.
    """

    target_table_class = POTranslation
    ids_to_prune_query = """
        SELECT POTranslation.id AS id FROM POTranslation
        EXCEPT (
            SELECT msgstr0 FROM TranslationMessage
                WHERE msgstr0 IS NOT NULL

            UNION ALL SELECT msgstr1 FROM TranslationMessage
                WHERE msgstr1 IS NOT NULL

            UNION ALL SELECT msgstr2 FROM TranslationMessage
                WHERE msgstr2 IS NOT NULL

            UNION ALL SELECT msgstr3 FROM TranslationMessage
                WHERE msgstr3 IS NOT NULL

            UNION ALL SELECT msgstr4 FROM TranslationMessage
                WHERE msgstr4 IS NOT NULL

            UNION ALL SELECT msgstr5 FROM TranslationMessage
                WHERE msgstr5 IS NOT NULL
            )
        """


class SessionPruner(BulkPruner):
    """Base class for session removal."""

    target_table_class = SessionData
    target_table_key = "client_id"
    target_table_key_type = "id text"


class AntiqueSessionPruner(SessionPruner):
    """Remove sessions not accessed for 60 days"""

    ids_to_prune_query = """
        SELECT client_id AS id FROM SessionData
        WHERE last_accessed < CURRENT_TIMESTAMP - CAST('60 days' AS interval)
        """


class UnusedSessionPruner(SessionPruner):
    """Remove sessions older than 1 day with no authentication credentials."""

    ids_to_prune_query = """
        SELECT client_id AS id FROM SessionData
        WHERE
            last_accessed < CURRENT_TIMESTAMP - CAST('1 day' AS interval)
            AND client_id NOT IN (
                SELECT client_id
                FROM SessionPkgData
                WHERE
                    product_id = 'launchpad.authenticateduser'
                    AND key='logintime')
        """


class DuplicateSessionPruner(SessionPruner):
    """Remove all but the most recent 6 authenticated sessions for a user.

    We sometimes see users with dozens or thousands of authenticated
    sessions. To limit exposure to replay attacks, we remove all but
    the most recent 6 of them for a given user.
    """

    ids_to_prune_query = """
        SELECT client_id AS id
        FROM (
            SELECT
                sessiondata.client_id,
                last_accessed,
                rank() OVER pickle AS rank
            FROM SessionData, SessionPkgData
            WHERE
                SessionData.client_id = SessionPkgData.client_id
                AND product_id = 'launchpad.authenticateduser'
                AND key='accountid'
            WINDOW pickle AS (PARTITION BY pickle ORDER BY last_accessed DESC)
            ) AS whatever
        WHERE
            rank > 6
            AND last_accessed < CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                - CAST('1 hour' AS interval)
        """


class PreviewDiffPruner(BulkPruner):
    """A BulkPruner to remove old PreviewDiffs.

    We remove all but the latest PreviewDiff for each BranchMergeProposal.
    All PreviewDiffs containing published or draft inline comments
    (CodeReviewInlineComment{,Draft}) are also preserved.
    """

    target_table_class = PreviewDiff
    ids_to_prune_query = """
        SELECT id
            FROM
            (SELECT PreviewDiff.id,
                rank() OVER (PARTITION BY PreviewDiff.branch_merge_proposal
                ORDER BY PreviewDiff.date_created DESC) AS pos
            FROM previewdiff) AS ss
        WHERE pos > 1
        EXCEPT SELECT previewdiff FROM CodeReviewInlineComment
        EXCEPT SELECT previewdiff FROM CodeReviewInlineCommentDraft
        """


class DiffPruner(BulkPruner):
    """A BulkPruner to remove all unreferenced Diffs."""

    target_table_class = Diff
    ids_to_prune_query = """
        SELECT id FROM diff EXCEPT (SELECT diff FROM previewdiff UNION ALL
            SELECT diff FROM incrementaldiff)
        """


class UnlinkedAccountPruner(BulkPruner):
    """Remove Account records not linked to a Person."""

    target_table_class = Account
    # We join with EmailAddress to ensure we only attempt removal after
    # the EmailAddress rows have been removed by
    # AccountOnlyEmailAddressPruner. We join with Person to work around
    # records with bad crosslinks. These bad crosslinks will be fixed by
    # dropping the EmailAddress.account column.
    ids_to_prune_query = """
        SELECT Account.id
        FROM Account
        LEFT OUTER JOIN Person ON Account.id = Person.account
        WHERE Person.id IS NULL
        """


class BugSummaryJournalRollup(TunableLoop):
    """Rollup BugSummaryJournal rows into BugSummary."""

    maximum_chunk_size = 5000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.store = IPrimaryStore(Bug)

    def isDone(self):
        has_more = self.store.execute(
            "SELECT EXISTS (SELECT TRUE FROM BugSummaryJournal LIMIT 1)"
        ).get_one()[0]
        return not has_more

    def __call__(self, chunk_size):
        chunk_size = int(chunk_size + 0.5)
        self.store.execute(
            "SELECT bugsummary_rollup_journal(%s)",
            (chunk_size,),
            noresult=True,
        )
        self.store.commit()


class PopulateDistributionSourcePackageCache(TunableLoop):
    """Populate the DistributionSourcePackageCache table.

    Ensure that new source publications have a row in
    DistributionSourcePackageCache.
    """

    maximum_chunk_size = 1000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.store = IPrimaryStore(DistributionSourcePackageCache)
        # Keep a record of the processed source publication ID so we know
        # where the job got up to.
        self.last_spph_id = 0
        self.job_name = self.__class__.__name__
        job_data = load_garbo_job_state(self.job_name)
        if job_data:
            self.last_spph_id = job_data.get("last_spph_id", 0)

    def getPendingUpdates(self):
        # Load the latest published source publication data.
        origin = [
            SourcePackagePublishingHistory,
            Join(
                SourcePackageName,
                SourcePackageName.id
                == SourcePackagePublishingHistory.sourcepackagename_id,
            ),
            Join(
                Archive,
                Archive.id == SourcePackagePublishingHistory.archive_id,
            ),
        ]
        rows = self.store.using(*origin).find(
            (
                SourcePackagePublishingHistory.id,
                Archive.id,
                Archive.distribution_id,
                SourcePackageName.id,
                SourcePackageName.name,
            ),
            SourcePackagePublishingHistory.status.is_in(
                active_publishing_status
            ),
            SourcePackagePublishingHistory.id > self.last_spph_id,
        )
        return rows.order_by(SourcePackagePublishingHistory.id)

    def isDone(self):
        return self.getPendingUpdates().is_empty()

    def __call__(self, chunk_size):
        # Create a map of new source publications, keyed on (archive,
        # distribution, SPN).
        cache_filter_data = []
        new_records = {}
        for new_publication in self.getPendingUpdates()[:chunk_size]:
            (
                spph_id,
                archive_id,
                distribution_id,
                spn_id,
                spn_name,
            ) = new_publication
            cache_filter_data.append((archive_id, distribution_id, spn_id))
            new_records[(archive_id, distribution_id, spn_id)] = spn_name
            self.last_spph_id = spph_id

        # Gather all the current cached records corresponding to the data in
        # the current batch.
        existing_records = set()
        rows = self.store.find(
            DistributionSourcePackageCache,
            In(
                Row(
                    DistributionSourcePackageCache.archive_id,
                    DistributionSourcePackageCache.distribution_id,
                    DistributionSourcePackageCache.sourcepackagename_id,
                ),
                [Row(cache_key) for cache_key in cache_filter_data],
            ),
        )
        for dspc in rows:
            existing_records.add(
                (
                    dspc.archive_id,
                    dspc.distribution_id,
                    dspc.sourcepackagename_id,
                )
            )

        # Bulk-create missing cache rows.
        inserts = []
        for data in set(new_records) - existing_records:
            archive_id, distribution_id, spn_id = data
            inserts.append(
                (archive_id, distribution_id, spn_id, new_records[data])
            )
        if inserts:
            create(
                (
                    DistributionSourcePackageCache.archive_id,
                    DistributionSourcePackageCache.distribution_id,
                    DistributionSourcePackageCache.sourcepackagename_id,
                    DistributionSourcePackageCache.name,
                ),
                inserts,
            )

        self.store.flush()
        save_garbo_job_state(
            self.job_name, {"last_spph_id": self.last_spph_id}
        )
        transaction.commit()


class PopulateLatestPersonSourcePackageReleaseCache(TunableLoop):
    """Populate the LatestPersonSourcePackageReleaseCache table.

    The LatestPersonSourcePackageReleaseCache contains 2 sets of data, one set
    for package maintainers and another for package creators. This job iterates
    over the SPPH records, populating the cache table.
    """

    maximum_chunk_size = 1000

    cache_columns = (
        LatestPersonSourcePackageReleaseCache.maintainer_id,
        LatestPersonSourcePackageReleaseCache.creator_id,
        LatestPersonSourcePackageReleaseCache.upload_archive_id,
        LatestPersonSourcePackageReleaseCache.upload_distroseries_id,
        LatestPersonSourcePackageReleaseCache.sourcepackagename_id,
        LatestPersonSourcePackageReleaseCache.archive_purpose,
        LatestPersonSourcePackageReleaseCache.publication_id,
        LatestPersonSourcePackageReleaseCache.dateuploaded,
        LatestPersonSourcePackageReleaseCache.sourcepackagerelease_id,
    )

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.store = IPrimaryStore(LatestPersonSourcePackageReleaseCache)
        # Keep a record of the processed source package release id and data
        # type (creator or maintainer) so we know where to job got up to.
        self.last_spph_id = 0
        self.job_name = self.__class__.__name__
        job_data = load_garbo_job_state(self.job_name)
        if job_data:
            self.last_spph_id = job_data.get("last_spph_id", 0)

    def getPendingUpdates(self):
        # Load the latest published source package release data.
        spph = SourcePackagePublishingHistory
        origin = [
            SourcePackageRelease,
            Join(
                spph,
                And(
                    spph.sourcepackagerelease_id == SourcePackageRelease.id,
                    spph.archive_id == SourcePackageRelease.upload_archive_id,
                ),
            ),
            Join(Archive, Archive.id == spph.archive_id),
        ]
        rs = (
            self.store.using(*origin)
            .find(
                (
                    SourcePackageRelease.id,
                    SourcePackageRelease.creator_id,
                    SourcePackageRelease.maintainer_id,
                    SourcePackageRelease.upload_archive_id,
                    Archive.purpose,
                    SourcePackageRelease.upload_distroseries_id,
                    SourcePackageRelease.sourcepackagename_id,
                    SourcePackageRelease.dateuploaded,
                    spph.id,
                ),
                spph.id > self.last_spph_id,
            )
            .order_by(spph.id)
        )
        return rs

    def isDone(self):
        return self.getPendingUpdates().is_empty()

    def __call__(self, chunk_size):
        cache_filter_data = []
        new_records = dict()
        person_ids = set()
        # Create a map of new published spr data for creators and maintainers.
        # The map is keyed on (creator/maintainer, archive, spn, distroseries).
        for new_published_spr_data in self.getPendingUpdates()[:chunk_size]:
            (
                spr_id,
                creator_id,
                maintainer_id,
                archive_id,
                purpose,
                distroseries_id,
                spn_id,
                dateuploaded,
                spph_id,
            ) = new_published_spr_data
            cache_filter_data.append((archive_id, distroseries_id, spn_id))

            value = (purpose, spph_id, dateuploaded, spr_id)
            maintainer_key = (
                maintainer_id,
                None,
                archive_id,
                distroseries_id,
                spn_id,
            )
            creator_key = (
                None,
                creator_id,
                archive_id,
                distroseries_id,
                spn_id,
            )
            new_records[maintainer_key] = list(maintainer_key + value)
            new_records[creator_key] = list(creator_key + value)
            person_ids.add(maintainer_id)
            person_ids.add(creator_id)
            self.last_spph_id = spph_id

        # Gather all the current cached reporting records corresponding to the
        # data in the current batch. We select matching records from the
        # reporting cache table based on
        # (archive_id, distroseries_id, sourcepackagename_id).
        existing_records = dict()
        lpsprc = LatestPersonSourcePackageReleaseCache
        rs = self.store.find(
            lpsprc,
            In(
                Row(
                    lpsprc.upload_archive_id,
                    lpsprc.upload_distroseries_id,
                    lpsprc.sourcepackagename_id,
                ),
                [Row(cache_key) for cache_key in cache_filter_data],
            ),
        )
        for lpsprc_record in rs:
            key = (
                lpsprc_record.maintainer_id,
                lpsprc_record.creator_id,
                lpsprc_record.upload_archive_id,
                lpsprc_record.upload_distroseries_id,
                lpsprc_record.sourcepackagename_id,
            )
            existing_records[key] = lpsprc_record.dateuploaded

        # Gather account statuses for creators and maintainers.
        # Deactivating or closing an account removes its LPSPRC rows, and we
        # don't want to resurrect them.
        account_statuses = dict(
            self.store.find(
                (Person.id, Account.status),
                Person.id.is_in(person_ids),
                Person.account == Account.id,
            )
        )
        ignore_statuses = (AccountStatus.DEACTIVATED, AccountStatus.CLOSED)
        for new_record in new_records.values():
            if (
                new_record[0] is not None
                and account_statuses.get(new_record[0]) in ignore_statuses
            ):
                new_record[0] = None
            if (
                new_record[1] is not None
                and account_statuses.get(new_record[1]) in ignore_statuses
            ):
                new_record[1] = None

        # Figure out what records from the new published spr data need to be
        # inserted and updated into the cache table.
        inserts = dict()
        updates = dict()
        for key, new_published_spr_data in new_records.items():
            existing_dateuploaded = existing_records.get(key, None)
            (
                new_maintainer,
                new_creator,
                _,
                _,
                _,
                _,
                _,
                new_dateuploaded,
                _,
            ) = new_published_spr_data
            if new_maintainer is None and new_creator is None:
                continue
            elif existing_dateuploaded is None:
                target = inserts
            else:
                target = updates

            existing_action = target.get(key, None)
            if (
                existing_action is None
                or existing_action[7] < new_dateuploaded
            ):
                target[key] = new_published_spr_data

        if inserts:
            # Do a bulk insert.
            create(self.cache_columns, inserts.values())
        if updates:
            # Do a bulk update.
            cols = [
                ("maintainer", "integer"),
                ("creator", "integer"),
                ("upload_archive", "integer"),
                ("upload_distroseries", "integer"),
                ("sourcepackagename", "integer"),
                ("archive_purpose", "integer"),
                ("publication", "integer"),
                ("date_uploaded", "timestamp without time zone"),
                ("sourcepackagerelease", "integer"),
            ]
            values = [
                [
                    dbify_value(col, val)[0]
                    for (col, val) in zip(self.cache_columns, data)
                ]
                for data in updates.values()
            ]

            cache_data_expr = Values("cache_data", cols, values)
            cache_data = ClassAlias(lpsprc, "cache_data")

            # The columns to be updated.
            updated_columns = dict(
                [
                    (lpsprc.dateuploaded, cache_data.dateuploaded),
                    (
                        lpsprc.sourcepackagerelease_id,
                        cache_data.sourcepackagerelease_id,
                    ),
                    (lpsprc.publication_id, cache_data.publication_id),
                ]
            )
            # The update filter.
            filter = And(
                Or(
                    cache_data.creator_id == None,
                    lpsprc.creator_id == cache_data.creator_id,
                ),
                Or(
                    cache_data.maintainer_id == None,
                    lpsprc.maintainer_id == cache_data.maintainer_id,
                ),
                lpsprc.upload_archive_id == cache_data.upload_archive_id,
                lpsprc.upload_distroseries_id
                == cache_data.upload_distroseries_id,
                lpsprc.sourcepackagename_id == cache_data.sourcepackagename_id,
            )

            self.store.execute(
                BulkUpdate(
                    updated_columns,
                    table=LatestPersonSourcePackageReleaseCache,
                    values=cache_data_expr,
                    where=filter,
                )
            )
        self.store.flush()
        save_garbo_job_state(
            self.job_name, {"last_spph_id": self.last_spph_id}
        )
        transaction.commit()


class OpenIDConsumerNoncePruner(TunableLoop):
    """An ITunableLoop to prune old OpenIDConsumerNonce records.

    We remove all OpenIDConsumerNonce records older than 1 day.
    """

    maximum_chunk_size = 6 * 60 * 60  # 6 hours in seconds.

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.store = IPrimaryStore(OpenIDConsumerNonce)
        self.earliest_timestamp = self.store.find(
            Min(OpenIDConsumerNonce.timestamp)
        ).one()
        utc_now = int(time.mktime(time.gmtime()))
        self.earliest_wanted_timestamp = utc_now - ONE_DAY_IN_SECONDS

    def isDone(self):
        return (
            self.earliest_timestamp is None
            or self.earliest_timestamp >= self.earliest_wanted_timestamp
        )

    def __call__(self, chunk_size):
        self.earliest_timestamp = min(
            self.earliest_wanted_timestamp,
            self.earliest_timestamp + chunk_size,
        )

        self.log.debug(
            "Removing OpenIDConsumerNonce rows older than %s"
            % self.earliest_timestamp
        )

        self.store.find(
            OpenIDConsumerNonce,
            OpenIDConsumerNonce.timestamp < self.earliest_timestamp,
        ).remove()
        transaction.commit()


class OpenIDConsumerAssociationPruner(TunableLoop):
    minimum_chunk_size = 3500
    maximum_chunk_size = 50000

    table_name = "OpenIDConsumerAssociation"

    _num_removed = None

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.store = IPrimaryStore(OpenIDConsumerNonce)

    def __call__(self, chunksize):
        result = self.store.execute(
            """
            DELETE FROM %s
            WHERE (server_url, handle) IN (
                SELECT server_url, handle FROM %s
                WHERE issued + lifetime <
                    EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)
                LIMIT %d
                )
            """
            % (self.table_name, self.table_name, int(chunksize))
        )
        self._num_removed = result.rowcount
        transaction.commit()

    def isDone(self):
        return self._num_removed == 0


class RevisionCachePruner(TunableLoop):
    """A tunable loop to remove old revisions from the cache."""

    maximum_chunk_size = 100

    def isDone(self):
        """We are done when there are no old revisions to delete."""
        epoch = datetime.now(timezone.utc) - timedelta(days=30)
        store = IPrimaryStore(RevisionCache)
        results = store.find(
            RevisionCache, RevisionCache.revision_date < epoch
        )
        return results.count() == 0

    def __call__(self, chunk_size):
        """Delegate to the `IRevisionSet` implementation."""
        getUtility(IRevisionSet).pruneRevisionCache(chunk_size)
        transaction.commit()


class CodeImportEventPruner(BulkPruner):
    """Prune `CodeImportEvent`s that are more than a month old.

    Events that happened more than 30 days ago are really of no
    interest to us.
    """

    target_table_class = CodeImportEvent
    ids_to_prune_query = """
        SELECT id FROM CodeImportEvent
        WHERE date_created < CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
            - CAST('30 days' AS interval)
        """


class CodeImportResultPruner(BulkPruner):
    """A TunableLoop to prune unwanted CodeImportResult rows.

    Removes CodeImportResult rows if they are older than 30 days
    and they are not one of the most recent results for that
    CodeImport.
    """

    target_table_class = CodeImportResult
    ids_to_prune_query = """
        SELECT id FROM (
            SELECT id, date_created, rank() OVER w AS rank
            FROM CodeImportResult
            WINDOW w AS (PARTITION BY code_import ORDER BY date_created DESC)
            ) AS whatever
        WHERE
            rank > %s
            AND date_created < CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                - CAST('30 days' AS interval)
            """ % sqlvalues(
        config.codeimport.consecutive_failure_limit - 1
    )


class RevisionAuthorEmailLinker(TunableLoop):
    """A TunableLoop that links `RevisionAuthor` objects to `Person` objects.

    `EmailAddress` objects are looked up for `RevisionAuthor` objects
    that have not yet been linked to a `Person`.  If the
    `EmailAddress` is linked to a person, then the `RevisionAuthor` is
    linked to the same.
    """

    maximum_chunk_size = 1000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.author_store = IPrimaryStore(RevisionAuthor)
        self.email_store = IPrimaryStore(EmailAddress)

        (self.min_author_id, self.max_author_id) = self.author_store.find(
            (Min(RevisionAuthor.id), Max(RevisionAuthor.id))
        ).one()

        self.next_author_id = self.min_author_id

    def isDone(self):
        return (
            self.min_author_id is None
            or self.next_author_id > self.max_author_id
        )

    def __call__(self, chunk_size):
        result = self.author_store.find(
            RevisionAuthor,
            RevisionAuthor.id >= self.next_author_id,
            RevisionAuthor.person_id == None,
            RevisionAuthor.email != None,
        )
        result.order_by(RevisionAuthor.id)
        authors = list(result[:chunk_size])

        # No more authors found.
        if len(authors) == 0:
            self.next_author_id = self.max_author_id + 1
            transaction.commit()
            return

        emails = dict(
            self.email_store.find(
                (EmailAddress.email.lower(), EmailAddress.person_id),
                EmailAddress.email.lower().is_in(
                    [author.email.lower() for author in authors]
                ),
                EmailAddress.status.is_in(
                    [
                        EmailAddressStatus.PREFERRED,
                        EmailAddressStatus.VALIDATED,
                    ]
                ),
                EmailAddress.person != None,
            )
        )

        if emails:
            for author in authors:
                person_id = emails.get(author.email.lower())
                if person_id is None:
                    continue
                author.person_id = person_id

        self.next_author_id = authors[-1].id + 1
        transaction.commit()


class PersonPruner(TunableLoop):
    maximum_chunk_size = 1000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.offset = 1
        self.store = IPrimaryStore(Person)
        self.log.debug("Creating LinkedPeople temporary table.")
        self.store.execute(
            "CREATE TEMPORARY TABLE LinkedPeople(person integer primary key)"
        )
        # Prefill with Person entries created after our OpenID provider
        # started creating personless accounts on signup.
        self.log.debug(
            "Populating LinkedPeople with post-OpenID created Person."
        )
        self.store.execute(
            """
            INSERT INTO LinkedPeople
            SELECT id FROM Person
            WHERE datecreated > '2009-04-01'
            """
        )
        transaction.commit()
        for (
            from_table,
            from_column,
            to_table,
            to_column,
            _,
            _,
        ) in postgresql.listReferences(cursor(), "person", "id"):
            # Skip things that don't link to Person.id or that link to it from
            # TeamParticipation or EmailAddress, as all Person entries will be
            # linked to from these tables.  Similarly, PersonSettings can
            # simply be deleted if it exists, because it has a 1 (or 0) to 1
            # relationship with Person.
            if (
                to_table != "person"
                or to_column != "id"
                or from_table
                in ("teamparticipation", "emailaddress", "personsettings")
            ):
                continue
            self.log.debug(
                "Populating LinkedPeople from %s.%s"
                % (from_table, from_column)
            )
            self.store.execute(
                """
                INSERT INTO LinkedPeople
                SELECT DISTINCT %(from_column)s AS person
                FROM %(from_table)s
                WHERE %(from_column)s IS NOT NULL
                EXCEPT ALL
                SELECT person FROM LinkedPeople
                """
                % dict(from_table=from_table, from_column=from_column)
            )
            transaction.commit()

        self.log.debug("Creating UnlinkedPeople temporary table.")
        self.store.execute(
            """
            CREATE TEMPORARY TABLE UnlinkedPeople(
                id serial primary key, person integer);
            """
        )
        self.log.debug("Populating UnlinkedPeople.")
        self.store.execute(
            """
            INSERT INTO UnlinkedPeople (person) (
                SELECT id AS person FROM Person
                WHERE teamowner IS NULL
                EXCEPT ALL
                SELECT person FROM LinkedPeople);
            """
        )
        transaction.commit()
        self.log.debug("Indexing UnlinkedPeople.")
        self.store.execute(
            """
            CREATE UNIQUE INDEX unlinkedpeople__person__idx ON
                UnlinkedPeople(person);
            """
        )
        self.log.debug("Analyzing UnlinkedPeople.")
        self.store.execute(
            """
            ANALYZE UnlinkedPeople;
            """
        )
        self.log.debug("Counting UnlinkedPeople.")
        self.max_offset = self.store.execute(
            "SELECT MAX(id) FROM UnlinkedPeople"
        ).get_one()[0]
        if self.max_offset is None:
            self.max_offset = -1  # Trigger isDone() now.
            self.log.debug("No Person records to remove.")
        else:
            self.log.info("%d Person records to remove." % self.max_offset)
        # Don't keep any locks open - we might block.
        transaction.commit()

    def isDone(self):
        return self.offset > self.max_offset

    def __call__(self, chunk_size):
        subquery = """
            SELECT person FROM UnlinkedPeople
            WHERE id BETWEEN %d AND %d
            """ % (
            self.offset,
            self.offset + chunk_size - 1,
        )
        people_ids = ",".join(
            str(item[0]) for item in self.store.execute(subquery).get_all()
        )
        self.offset += chunk_size
        try:
            # This would be dangerous if we were deleting a
            # team, so join with Person to ensure it isn't one
            # even in the rare case a person is converted to
            # a team during this run.
            self.store.execute(
                """
                DELETE FROM TeamParticipation
                USING Person
                WHERE TeamParticipation.person = Person.id
                    AND Person.teamowner IS NULL
                    AND Person.id IN (%s)
                """
                % people_ids
            )
            self.store.execute(
                """
                DELETE FROM EmailAddress
                WHERE person IN (%s)
                """
                % people_ids
            )
            # This cascade deletes any PersonSettings records.
            self.store.execute(
                """
                DELETE FROM Person
                WHERE id IN (%s)
                """
                % people_ids
            )
            transaction.commit()
            self.log.debug(
                "Deleted the following unlinked people: %s" % people_ids
            )
        except IntegrityError:
            # This case happens when a Person is linked to something
            # during the run. It is unlikely to occur, so just ignore
            # it again. Everything will clear up next run.
            transaction.abort()
            self.log.warning(
                "Failed to delete %d Person records. Left for next time."
                % chunk_size
            )


class TeamMembershipPruner(BulkPruner):
    """Remove team memberships for merged people.

    People merge can leave team membership records behind because:
        * The membership duplicates another membership.
        * The membership would have created a cyclic relationshop.
        * The operation avoid a race condition.
    """

    target_table_class = TeamMembership
    ids_to_prune_query = """
        SELECT TeamMembership.id
        FROM TeamMembership, Person
        WHERE
            TeamMembership.person = person.id
            AND person.merged IS NOT NULL
        """


class BugNotificationPruner(BulkPruner):
    """Prune `BugNotificationRecipient` records no longer of interest.

    We discard all rows older than 30 days that have been sent. We
    keep 30 days worth or records to help diagnose email delivery issues.
    """

    target_table_class = BugNotification
    ids_to_prune_query = """
        SELECT BugNotification.id FROM BugNotification
        WHERE date_emailed < CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
            - CAST('30 days' AS interval)
        """


class AnswerContactPruner(BulkPruner):
    """Remove old answer contacts which are no longer required.

    Remove a person as an answer contact if:
      their account has been deactivated for more than one day,
      suspended for more than one week, or
      marked as deceased for more than one week.
    """

    target_table_class = AnswerContact
    ids_to_prune_query = """
        SELECT DISTINCT AnswerContact.id
        FROM AnswerContact, Person, Account
        WHERE
            AnswerContact.person = Person.id
            AND Person.account = Account.id
            AND (
                (Account.date_status_set <
                CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                - CAST('1 day' AS interval)
                AND Account.status = %s)
                OR
                (Account.date_status_set <
                CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                - CAST('7 days' AS interval)
                AND Account.status IN %s)
            )
        """ % (
        AccountStatus.DEACTIVATED.value,
        (AccountStatus.SUSPENDED.value, AccountStatus.DECEASED.value),
    )


class BranchJobPruner(BulkPruner):
    """Prune `BranchJob`s that are in a final state and more than a month old.

    When a BranchJob is completed, it gets set to a final state. These jobs
    should be pruned from the database after a month.
    """

    target_table_class = Job
    ids_to_prune_query = """
        SELECT DISTINCT Job.id
        FROM Job, BranchJob
        WHERE
            Job.id = BranchJob.job
            AND Job.date_finished < CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                - CAST('30 days' AS interval)
        """


class GitJobPruner(BulkPruner):
    """Prune `GitJob`s that are in a final state and more than a month old.

    When a GitJob is completed, it gets set to a final state. These jobs
    should be pruned from the database after a month.
    """

    target_table_class = Job
    ids_to_prune_query = """
        SELECT DISTINCT Job.id
        FROM Job, GitJob
        WHERE
            Job.id = GitJob.job
            AND Job.date_finished < CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                - CAST('30 days' AS interval)
        """


class BranchMergeProposalJobPruner(BulkPruner):
    """Prune `BranchMergeProposalJob`s that are in a final state and more
    than a month old.

    When a BranchMergeProposalJob is completed, it gets set to a final
    state. These jobs should be pruned from the database after a month.
    """

    target_table_class = Job
    ids_to_prune_query = """
        SELECT DISTINCT Job.id
        FROM Job, BranchMergeProposalJob
        WHERE
            Job.id = BranchMergeProposalJob.job
            AND Job.date_finished < CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                - CAST('30 days' AS interval)
        """


class SnapBuildJobPruner(BulkPruner):
    """Prune `SnapBuildJob`s that are in a final state and more than a month
    old.

    When a SnapBuildJob is completed, it gets set to a final state. These
    jobs should be pruned from the database after a month, unless they are
    the most recent job for their SnapBuild.
    """

    target_table_class = Job
    ids_to_prune_query = """
        SELECT id
        FROM (
            SELECT
                Job.id,
                Job.date_finished,
                rank() OVER (
                    PARTITION BY SnapBuildJob.snapbuild
                    ORDER BY SnapBuildJob.job DESC) AS rank
            FROM Job JOIN SnapBuildJob ON Job.id = SnapBuildJob.job) AS jobs
        WHERE
            rank > 1
            AND date_finished < CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                - CAST('30 days' AS interval)
        """


class WebhookJobPruner(TunableLoop):
    """Prune `WebhookJobs` that finished more than a month ago."""

    maximum_chunk_size = 5000

    @property
    def old_jobs(self):
        return (
            IPrimaryStore(WebhookJob)
            .using(WebhookJob, Job)
            .find(
                (WebhookJob.job_id,),
                Job.id == WebhookJob.job_id,
                Job.date_finished
                < UTC_NOW - Cast(timedelta(days=30), "interval"),
            )
        )

    def __call__(self, chunksize):
        getUtility(IWebhookJobSource).deleteByIDs(
            list(self.old_jobs[: int(chunksize)].values(WebhookJob.job_id))
        )
        transaction.commit()

    def isDone(self):
        return self.old_jobs.is_empty()


class BugHeatUpdater(TunableLoop):
    """A `TunableLoop` for bug heat calculations."""

    maximum_chunk_size = 5000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.transaction = transaction
        self.total_processed = 0
        self.is_done = False
        self.offset = 0

        self.store = IPrimaryStore(Bug)

    @property
    def _outdated_bugs(self):
        try:
            last_updated_cutoff = iso8601.parse_date(
                getFeatureFlag("bugs.heat_updates.cutoff")
            )
        except iso8601.ParseError:
            return EmptyResultSet()
        outdated_bugs = getUtility(IBugSet).getBugsWithOutdatedHeat(
            last_updated_cutoff
        )
        # We remove the security proxy so that we can access the set()
        # method of the result set.
        return removeSecurityProxy(outdated_bugs)

    def isDone(self):
        """See `ITunableLoop`."""
        # When the main loop has no more Bugs to process it sets
        # offset to None. Until then, it always has a numerical
        # value.
        return self._outdated_bugs.is_empty()

    def __call__(self, chunk_size):
        """Retrieve a batch of Bugs and update their heat.

        See `ITunableLoop`.
        """
        chunk_size = int(chunk_size + 0.5)
        outdated_bugs = self._outdated_bugs[:chunk_size]
        # We don't use outdated_bugs.set() here to work around
        # Storm Bug #820290.
        outdated_bug_ids = [bug.id for bug in outdated_bugs]
        self.log.debug("Updating heat for %s bugs", len(outdated_bug_ids))
        IPrimaryStore(Bug).find(Bug, Bug.id.is_in(outdated_bug_ids)).set(
            heat=SQL("calculate_bug_heat(Bug.id)"), heat_last_updated=UTC_NOW
        )
        transaction.commit()


class BugWatchActivityPruner(BulkPruner):
    """A TunableLoop to prune BugWatchActivity entries."""

    target_table_class = BugWatchActivity
    # For each bug_watch, remove all but the most recent MAX_SAMPLE_SIZE
    # entries.
    ids_to_prune_query = """
        SELECT id FROM (
            SELECT id, rank() OVER w AS rank
            FROM BugWatchActivity
            WINDOW w AS (PARTITION BY bug_watch ORDER BY id DESC)
            ) AS whatever
        WHERE rank > %s
        """ % sqlvalues(
        MAX_SAMPLE_SIZE
    )


class ObsoleteBugAttachmentPruner(BulkPruner):
    """Delete bug attachments without a LibraryFileContent record.

    Our database schema allows LibraryFileAlias records that have no
    corresponding LibraryFileContent records.

    This class deletes bug attachments that reference such "content free"
    and thus completely useless LFA records.
    """

    target_table_class = BugAttachment
    ids_to_prune_query = """
        SELECT BugAttachment.id
        FROM BugAttachment, LibraryFileAlias
        WHERE
            BugAttachment.libraryfile = LibraryFileAlias.id
            AND LibraryFileAlias.content IS NULL
        """


class OldTimeLimitedTokenDeleter(TunableLoop):
    """Delete expired url access tokens from the session DB."""

    maximum_chunk_size = 24 * 60 * 60  # 24 hours in seconds.

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.store = session_store()
        self._update_oldest()

    def _update_oldest(self):
        self.oldest_age = self.store.execute(
            """
            SELECT COALESCE(EXTRACT(EPOCH FROM
                CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                - MIN(created)), 0)
            FROM TimeLimitedToken
            """
        ).get_one()[0]

    def isDone(self):
        return self.oldest_age <= ONE_DAY_IN_SECONDS

    def __call__(self, chunk_size):
        self.oldest_age = max(ONE_DAY_IN_SECONDS, self.oldest_age - chunk_size)

        self.log.debug(
            "Removed TimeLimitedToken rows older than %d seconds"
            % self.oldest_age
        )
        self.store.find(
            TimeLimitedToken,
            TimeLimitedToken.created
            < SQL(
                "CURRENT_TIMESTAMP AT TIME ZONE 'UTC' - interval '%d seconds'"
                % ONE_DAY_IN_SECONDS
            ),
        ).remove()
        transaction.commit()
        self._update_oldest()


class SuggestiveTemplatesCacheUpdater(TunableLoop):
    """Refresh the SuggestivePOTemplate cache.

    This isn't really a TunableLoop.  It just pretends to be one to fit
    in with the garbo crowd.
    """

    maximum_chunk_size = 1

    done = False

    def isDone(self):
        """See `TunableLoop`."""
        return self.done

    def __call__(self, chunk_size):
        """See `TunableLoop`."""
        utility = getUtility(IPOTemplateSet)
        utility.wipeSuggestivePOTemplatesCache()
        utility.populateSuggestivePOTemplatesCache()
        transaction.commit()
        self.done = True


class UnusedPOTMsgSetPruner(TunableLoop):
    """Cleans up unused POTMsgSets."""

    done = False
    offset = 0
    maximum_chunk_size = 50000

    def isDone(self):
        """See `TunableLoop`."""
        return self.offset >= len(self.msgset_ids_to_remove)

    @cachedproperty
    def msgset_ids_to_remove(self):
        """The IDs of the POTMsgSets to remove."""
        return self._get_msgset_ids_to_remove()

    def _get_msgset_ids_to_remove(self, ids=None):
        """Return a distinct list of IDs of the POTMsgSets to remove.

        :param ids: a list of POTMsgSet ids to filter. If ids is None,
            all unused POTMsgSet in the database are returned.
        """
        if ids is None:
            constraints = dict(
                tti_constraint="AND TRUE", potmsgset_constraint="AND TRUE"
            )
        else:
            ids_in = ", ".join([str(id) for id in ids])
            constraints = dict(
                tti_constraint="AND tti.potmsgset IN (%s)" % ids_in,
                potmsgset_constraint="AND POTMsgSet.id IN (%s)" % ids_in,
            )
        query = (
            """
            -- Get all POTMsgSet IDs which are obsolete (sequence == 0)
            -- and are not used (sequence != 0) in any other template.
            SELECT POTMsgSet
              FROM TranslationTemplateItem tti
              WHERE sequence=0
              %(tti_constraint)s
              AND NOT EXISTS(
                SELECT id
                  FROM TranslationTemplateItem
                  WHERE potmsgset = tti.potmsgset AND sequence != 0)
            UNION
            -- Get all POTMsgSet IDs which are not referenced
            -- by any of the templates (they must have TTI rows for that).
            (SELECT POTMsgSet.id
               FROM POTMsgSet
               LEFT OUTER JOIN TranslationTemplateItem
                 ON TranslationTemplateItem.potmsgset = POTMsgSet.id
               WHERE
                 TranslationTemplateItem.potmsgset IS NULL
                 %(potmsgset_constraint)s);
            """
            % constraints
        )
        store = IPrimaryStore(POTMsgSet)
        results = store.execute(query)
        ids_to_remove = {id for (id,) in results.get_all()}
        return list(ids_to_remove)

    def __call__(self, chunk_size):
        """See `TunableLoop`."""
        # We cast chunk_size to an int to avoid issues with slicing
        # (DBLoopTuner passes in a float).
        chunk_size = int(chunk_size)
        msgset_ids = self.msgset_ids_to_remove[self.offset :][:chunk_size]
        msgset_ids_to_remove = self._get_msgset_ids_to_remove(msgset_ids)
        # Remove related TranslationTemplateItems.
        store = IPrimaryStore(POTMsgSet)
        related_ttis = store.find(
            TranslationTemplateItem,
            In(TranslationTemplateItem.potmsgset_id, msgset_ids_to_remove),
        )
        related_ttis.remove()
        # Remove related TranslationMessages.
        related_translation_messages = store.find(
            TranslationMessage,
            In(TranslationMessage.potmsgset_id, msgset_ids_to_remove),
        )
        related_translation_messages.remove()
        store.find(POTMsgSet, In(POTMsgSet.id, msgset_ids_to_remove)).remove()
        self.offset = self.offset + chunk_size
        transaction.commit()


class UnusedProductAccessPolicyPruner(TunableLoop):
    """Deletes unused AccessPolicy and AccessPolicyGrants for products."""

    maximum_chunk_size = 5000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.start_at = 1
        self.store = IPrimaryStore(Product)

    def findProducts(self):
        return self.store.find(Product, Product.id >= self.start_at).order_by(
            Product.id
        )

    def isDone(self):
        return self.findProducts().is_empty()

    def __call__(self, chunk_size):
        products = list(self.findProducts()[:chunk_size])
        for product in products:
            product._pruneUnusedPolicies()
        self.start_at = products[-1].id + 1
        transaction.commit()


class UnusedDistributionAccessPolicyPruner(TunableLoop):
    """Deletes unused AccessPolicy and AccessPolicyGrants for distributions."""

    maximum_chunk_size = 5000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.start_at = 1
        self.store = IPrimaryStore(Distribution)

    def findDistributions(self):
        return self.store.find(
            Distribution, Distribution.id >= self.start_at
        ).order_by(Distribution.id)

    def isDone(self):
        return self.findDistributions().is_empty()

    def __call__(self, chunk_size):
        distributions = list(self.findDistributions()[:chunk_size])
        for distribution in distributions:
            distribution._pruneUnusedPolicies()
        self.start_at = distributions[-1].id + 1
        transaction.commit()


class ProductVCSPopulator(TunableLoop):
    """Populates product.vcs from product.inferred_vcs if not set."""

    maximum_chunk_size = 5000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.start_at = 1
        self.store = IPrimaryStore(Product)

    def findProducts(self):
        products = self.store.find(
            Product, Product.id >= self.start_at, Product.vcs == None
        )
        return products.order_by(Product.id)

    def isDone(self):
        return self.findProducts().is_empty()

    def __call__(self, chunk_size):
        products = list(self.findProducts()[:chunk_size])
        for product in products:
            product.vcs = product.inferred_vcs
        self.start_at = products[-1].id + 1
        transaction.commit()


class LiveFSFilePruner(BulkPruner):
    """A BulkPruner to remove old `LiveFSFile`s.

    We remove binary files attached to `LiveFSBuild`s that are more than
    `LiveFS.keep_binary_files_interval` old and that are not set as base
    images for a `DistroArchSeries`; these files are very large and are only
    useful for builds in progress.

    DAS base images are excluded because
    `DistroArchSeries.setChrootFromBuild` takes a `LiveFSBuild` and we want
    to have the option of reverting to a previous base image shortly after
    upgrading to a newer one.

    Text files are typically small (<1MiB) and useful for retrospective
    analysis, so we preserve those indefinitely.
    """

    target_table_class = LiveFSFile
    # Note that a NULL keep_binary_files_interval disables pruning, due to
    # SQL NULL propagation.
    ids_to_prune_query = """
        SELECT DISTINCT LiveFSFile.id
        FROM LiveFSFile, LiveFSBuild, LiveFS, LibraryFileAlias
        WHERE
            LiveFSFile.livefsbuild = LiveFSBuild.id
            AND LiveFSBuild.livefs = LiveFS.id
            AND LiveFSFile.libraryfile = LibraryFileAlias.id
            AND LiveFSBuild.date_finished <
                CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                - LiveFS.keep_binary_files_interval
            AND LibraryFileAlias.mimetype != 'text/plain'
        EXCEPT
            SELECT LiveFSFile.id
            FROM LiveFSFile, PocketChroot
            WHERE LiveFSFile.libraryfile = PocketChroot.chroot
        """


class SnapFilePruner(BulkPruner):
    """Prune old `SnapFile`s that have been uploaded to the store.

    Snaps attached to `SnapBuild`s are typically very large, and once
    they've been uploaded to the store we don't really need to keep them in
    Launchpad as well.  Most other files are either small or don't exist
    anywhere else, so we preserve those indefinitely.

    `.debug` files are large, so we prune those once the associated snap
    builds themselves have been uploaded to the store even though they don't
    themselves get uploaded anywhere else.  It's up to anyone producing
    these to deal with scraping them before they're pruned; they have about
    a week after the build finishes to do so.
    """

    target_table_class = SnapFile
    ids_to_prune_query = """
        SELECT DISTINCT SnapFile.id
        FROM SnapFile, SnapBuild, SnapBuildJob, Job, LibraryFileAlias
        WHERE
            SnapFile.snapbuild = SnapBuild.id
            AND SnapBuildJob.snapbuild = SnapBuild.id
            AND SnapBuildJob.job_type = %s
            AND SnapBuildJob.job = Job.id
            AND Job.status = %s
            AND Job.date_finished <
                CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                - CAST('7 days' AS INTERVAL)
            AND SnapFile.libraryfile = LibraryFileAlias.id
            AND (LibraryFileAlias.filename LIKE '%%.snap'
                 OR LibraryFileAlias.filename LIKE '%%.debug')
        """ % (
        SnapBuildJobType.STORE_UPLOAD.value,
        JobStatus.COMPLETED.value,
    )


class OCIFilePruner(BulkPruner):
    """Prune old `OCIFile`s that have expired."""

    target_table_class = OCIFile
    ids_to_prune_query = """
        SELECT DISTINCT OCIFile.id
        FROM OCIFile
        WHERE
            OCIFile.date_last_used <
                CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                - CAST('7 days' AS INTERVAL)
        """


class GitRepositoryPruner(TunableLoop):
    """Remove GitRepositories that are "CREATING" for far too long."""

    maximum_chunk_size = 500
    repository_creation_timeout = timedelta(hours=1)

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.store = IPrimaryStore(GitRepository)

    def findRepositories(self):
        min_date = UTC_NOW - Cast(self.repository_creation_timeout, "interval")
        repositories = self.store.find(
            GitRepository,
            GitRepository.status == GitRepositoryStatus.CREATING,
            GitRepository.date_created < min_date,
        )
        return repositories.order_by(GitRepository.date_created)

    def isDone(self):
        return self.findRepositories().is_empty()

    def __call__(self, chunk_size):
        for repository in self.findRepositories()[:chunk_size]:
            repository.destroySelf(break_references=True)
        transaction.commit()


class ArchiveSubscriptionExpirer(BulkPruner):
    """Expire archive subscriptions as necessary.

    If an `ArchiveSubscriber`'s date_expires has passed, then set its status
    to EXPIRED.
    """

    target_table_class = ArchiveSubscriber

    ids_to_prune_query = convert_storm_clause_to_string(
        Select(
            ArchiveSubscriber.id,
            where=And(
                ArchiveSubscriber.status == ArchiveSubscriberStatus.CURRENT,
                ArchiveSubscriber.date_expires != None,
                ArchiveSubscriber.date_expires <= UTC_NOW,
            ),
        )
    )

    maximum_chunk_size = 1000

    _num_removed = None

    def __call__(self, chunk_size):
        """See `ITunableLoop`."""
        chunk_size = int(chunk_size + 0.5)
        newly_expired_subscriptions = list(
            self.store.find(
                ArchiveSubscriber,
                ArchiveSubscriber.id.is_in(
                    SQL(
                        "SELECT * FROM cursor_fetch(%s, %s) AS f(id integer)",
                        params=(self.cursor_name, chunk_size),
                    )
                ),
            )
        )
        load_related(Archive, newly_expired_subscriptions, ["archive_id"])
        load_related(Person, newly_expired_subscriptions, ["subscriber_id"])
        subscription_names = [
            sub.displayname for sub in newly_expired_subscriptions
        ]
        if subscription_names:
            self.store.find(
                ArchiveSubscriber,
                ArchiveSubscriber.id.is_in(
                    [sub.id for sub in newly_expired_subscriptions]
                ),
            ).set(status=ArchiveSubscriberStatus.EXPIRED)
            self.log.info(
                "Expired subscriptions: %s" % ", ".join(subscription_names)
            )
        self._num_removed = len(subscription_names)
        transaction.commit()


class ArchiveAuthTokenDeactivator(BulkPruner):
    """Deactivate archive auth tokens as necessary.

    If an active token for a PPA no longer has any subscribers, we
    deactivate the token, and send an email to the person whose subscription
    was cancelled.
    """

    target_table_class = ArchiveAuthToken

    # A token is invalid if it is active and the token owner is *not* a
    # subscriber to the archive that the token is for.  The subscription can
    # be either direct or through a team.
    ids_to_prune_query = convert_storm_clause_to_string(
        Except(
            # All valid tokens.
            Select(
                ArchiveAuthToken.id,
                tables=[ArchiveAuthToken],
                where=And(
                    ArchiveAuthToken.name == None,
                    ArchiveAuthToken.date_deactivated == None,
                ),
            ),
            # Active tokens for which there is a matching current archive
            # subscription for a team of which the token owner is a member.
            # Removing these from the set of all valid tokens leaves only the
            # invalid tokens.
            Select(
                ArchiveAuthToken.id,
                tables=[
                    ArchiveAuthToken,
                    ArchiveSubscriber,
                    TeamParticipation,
                ],
                where=And(
                    ArchiveAuthToken.name == None,
                    ArchiveAuthToken.date_deactivated == None,
                    ArchiveAuthToken.archive_id
                    == ArchiveSubscriber.archive_id,
                    ArchiveSubscriber.status
                    == ArchiveSubscriberStatus.CURRENT,
                    ArchiveSubscriber.subscriber_id
                    == TeamParticipation.team_id,
                    TeamParticipation.person_id == ArchiveAuthToken.person_id,
                ),
            ),
        )
    )

    maximum_chunk_size = 10

    def _sendCancellationEmail(self, token):
        """Send an email to the person whose subscription was cancelled."""
        if token.archive.suppress_subscription_notifications:
            # Don't send an email if they should be suppressed for the
            # archive.
            return
        send_to_person = token.person
        ppa_name = token.archive.displayname
        ppa_owner_url = canonical_url(token.archive.owner)
        subject = "PPA access cancelled for %s" % ppa_name
        template = get_email_template(
            "ppa-subscription-cancelled.txt", app="soyuz"
        )

        if send_to_person.is_team:
            raise AssertionError(
                "Token.person is a team, it should always be individuals."
            )

        if send_to_person.preferredemail is None:
            # The person has no preferred email set, so we don't email them.
            return

        to_address = [send_to_person.preferredemail.email]
        replacements = {
            "recipient_name": send_to_person.display_name,
            "ppa_name": ppa_name,
            "ppa_owner_url": ppa_owner_url,
        }
        body = MailWrapper(72).format(template % replacements, force_wrap=True)

        from_address = format_address(
            ppa_name, config.canonical.noreply_from_address
        )

        headers = {
            "Sender": config.canonical.bounce_address,
        }

        simple_sendmail(from_address, to_address, subject, body, headers)

    def __call__(self, chunk_size):
        """See `ITunableLoop`."""
        chunk_size = int(chunk_size + 0.5)
        tokens = list(
            self.store.find(
                ArchiveAuthToken,
                ArchiveAuthToken.id.is_in(
                    SQL(
                        "SELECT * FROM cursor_fetch(%s, %s) AS f(id integer)",
                        params=(self.cursor_name, chunk_size),
                    )
                ),
            )
        )
        affected_ppas = load_related(Archive, tokens, ["archive_id"])
        load_related(Person, affected_ppas, ["owner_id"])
        getUtility(IPersonSet).getPrecachedPersonsFromIDs(
            [token.person_id for token in tokens], need_preferred_email=True
        )
        for token in tokens:
            self._sendCancellationEmail(token)
        self.store.find(
            ArchiveAuthToken,
            ArchiveAuthToken.id.is_in([token.id for token in tokens]),
        ).set(date_deactivated=UTC_NOW)
        self.log.info(
            "Deactivated %s tokens, %s PPAs affected"
            % (len(tokens), len(affected_ppas))
        )
        self._num_removed = len(tokens)
        transaction.commit()


class RevisionStatusReportPruner(BulkPruner):
    """Removes old revision status reports and their artifacts."""

    older_than = 90  # artifacts older than 90 days
    target_table_class = RevisionStatusArtifact
    ids_to_prune_query = """
        SELECT DISTINCT RevisionStatusArtifact.id
        FROM RevisionStatusArtifact, RevisionStatusReport
        WHERE
            RevisionStatusArtifact.report = RevisionStatusReport.id
            AND RevisionStatusReport.date_created <
            CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                - CAST('%s days' AS INTERVAL)
            AND RevisionStatusArtifact.type = %d
        """ % (
        older_than,
        RevisionStatusArtifactType.BINARY.value,
    )


class ArchiveArtifactoryColumnsPopulator(TunableLoop):
    """Populate new `Archive` columns used for Artifactory publishing."""

    maximum_chunk_size = 5000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.start_at = 1
        self.store = IPrimaryStore(Archive)

    def findArchives(self):
        return self.store.find(
            Archive,
            Archive.id >= self.start_at,
            Or(
                Archive._publishing_method == None,
                Archive._repository_format == None,
            ),
        ).order_by(Archive.id)

    def isDone(self):
        return self.findArchives().is_empty()

    def __call__(self, chunk_size):
        archives = list(self.findArchives()[:chunk_size])
        ids = [archive.id for archive in archives]
        self.store.execute(
            Update(
                {
                    Archive._publishing_method: Coalesce(
                        Archive._publishing_method,
                        ArchivePublishingMethod.LOCAL.value,
                    ),
                    Archive._repository_format: Coalesce(
                        Archive._repository_format,
                        ArchiveRepositoryFormat.DEBIAN.value,
                    ),
                },
                where=Archive.id.is_in(ids),
                table=Archive,
            )
        )
        self.start_at = archives[-1].id + 1
        transaction.commit()


class SourcePackagePublishingHistoryFormatPopulator(TunableLoop):
    """Populate new `SPPH.format` column."""

    maximum_chunk_size = 5000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.start_at = 1
        self.store = IPrimaryStore(SourcePackagePublishingHistory)

    def findPublications(self):
        return self.store.find(
            SourcePackagePublishingHistory,
            SourcePackagePublishingHistory.id >= self.start_at,
            SourcePackagePublishingHistory._format == None,
        ).order_by(SourcePackagePublishingHistory.id)

    def isDone(self):
        return self.findPublications().is_empty()

    def __call__(self, chunk_size):
        spphs = list(self.findPublications()[:chunk_size])
        ids = [spph.id for spph in spphs]
        self.store.execute(
            BulkUpdate(
                {
                    SourcePackagePublishingHistory._format: (
                        SourcePackageRelease.format
                    )
                },
                table=SourcePackagePublishingHistory,
                values=SourcePackageRelease,
                where=And(
                    SourcePackagePublishingHistory.sourcepackagerelease
                    == SourcePackageRelease.id,
                    SourcePackagePublishingHistory.id.is_in(ids),
                ),
            )
        )
        self.start_at = spphs[-1].id + 1
        transaction.commit()


class SourcePackageReleaseHomepagePopulator(TunableLoop):
    """Populate `SourcePackageRelease.homepage` from the stored .dsc.

    The Debian importer (gina) did not import the Homepage field, so
    some source package releases imported from Debian are missing their
    homepage.
    """

    maximum_chunk_size = 1000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.start_at = 1
        self.store = IPrimaryStore(SourcePackageRelease)

    def findReleases(self):
        return self.store.find(
            SourcePackageRelease,
            SourcePackageRelease.id >= self.start_at,
            SourcePackageRelease.homepage == None,
            SourcePackageRelease.dsc != None,
        ).order_by(SourcePackageRelease.id)

    def isDone(self):
        return self.findReleases().is_empty()

    def __call__(self, chunk_size):
        sprs = list(self.findReleases()[:chunk_size])
        for spr in sprs:
            try:
                dsc_contents = parse_tagfile_content(spr.dsc.encode("utf-8"))
            except TagFileParseError as e:
                self.log.warning(
                    "Failed to parse stored dsc for "
                    "SourcePackageRelease %d: %s" % (spr.id, e)
                )
                continue
            homepage = dsc_contents.get("Homepage")
            if homepage is not None:
                spr.homepage = guess_encoding(homepage)
        self.start_at = sprs[-1].id + 1
        transaction.commit()


class BinaryPackagePublishingHistoryFormatPopulator(TunableLoop):
    """Populate new `BPPH.binarypackageformat` column."""

    maximum_chunk_size = 5000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.start_at = 1
        self.store = IPrimaryStore(BinaryPackagePublishingHistory)

    def findPublications(self):
        return self.store.find(
            BinaryPackagePublishingHistory,
            BinaryPackagePublishingHistory.id >= self.start_at,
            BinaryPackagePublishingHistory._binarypackageformat == None,
        ).order_by(BinaryPackagePublishingHistory.id)

    def isDone(self):
        return self.findPublications().is_empty()

    def __call__(self, chunk_size):
        bpphs = list(self.findPublications()[:chunk_size])
        ids = [bpph.id for bpph in bpphs]
        self.store.execute(
            BulkUpdate(
                {
                    BinaryPackagePublishingHistory._binarypackageformat: (
                        BinaryPackageRelease.binpackageformat
                    )
                },
                table=BinaryPackagePublishingHistory,
                values=BinaryPackageRelease,
                where=And(
                    BinaryPackagePublishingHistory.binarypackagerelease
                    == BinaryPackageRelease.id,
                    BinaryPackagePublishingHistory.id.is_in(ids),
                ),
            )
        )
        self.start_at = bpphs[-1].id + 1
        transaction.commit()


# XXX cjwatson 2022-09-12: Remove this when it is complete.
class BinaryPackagePublishingHistorySPNPopulator(BulkPruner):
    """Populate the new BPPH.sourcepackagename column."""

    target_table_class = BinaryPackagePublishingHistory

    ids_to_prune_query = convert_storm_clause_to_string(
        Select(
            BinaryPackagePublishingHistory.id,
            where=And(
                BinaryPackagePublishingHistory.sourcepackagename == None,
                BinaryPackagePublishingHistory.binarypackagerelease
                == BinaryPackageRelease.id,
                BinaryPackageRelease.build != None,
            ),
        )
    )

    def __call__(self, chunk_size):
        """See `TunableLoop`."""
        chunk_size = int(chunk_size + 0.5)
        ids = [
            row[0]
            for row in self.store.execute(
                SQL(
                    "SELECT * FROM cursor_fetch(%s, %s) AS f(id integer)",
                    params=(self.cursor_name, chunk_size),
                )
            )
        ]
        BPPH = BinaryPackagePublishingHistory
        update = Returning(
            BulkUpdate(
                {
                    BPPH.sourcepackagename_id: (
                        SourcePackageRelease.sourcepackagename_id
                    )
                },
                table=BPPH,
                values=(
                    BinaryPackageRelease,
                    BinaryPackageBuild,
                    SourcePackageRelease,
                ),
                where=And(
                    BPPH.binarypackagerelease == BinaryPackageRelease.id,
                    BinaryPackageRelease.build == BinaryPackageBuild.id,
                    BinaryPackageBuild.source_package_release
                    == SourcePackageRelease.id,
                    BPPH.id.is_in(ids),
                ),
            ),
            columns=(BPPH.id,),
        )
        if ids:
            updated_ids = list(self.store.execute(update))
            self._num_removed = len(updated_ids)
        else:
            self._num_removed = 0
        transaction.commit()


class ArchiveFileDatePopulator(TunableLoop):
    """Populates ArchiveFile.date_superseded."""

    maximum_chunk_size = 5000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.start_at = 1
        self.store = IPrimaryStore(ArchiveFile)

    def findArchiveFiles(self):
        archive_files = self.store.find(
            ArchiveFile,
            ArchiveFile.id >= self.start_at,
            ArchiveFile.date_superseded == None,
            ArchiveFile.scheduled_deletion_date != None,
        )
        return archive_files.order_by(ArchiveFile.id)

    def isDone(self):
        return self.findArchiveFiles().is_empty()

    def __call__(self, chunk_size):
        archive_files = list(self.findArchiveFiles()[:chunk_size])
        for archive_file in archive_files:
            archive_file.date_superseded = (
                archive_file.scheduled_deletion_date
                - timedelta(days=BY_HASH_STAY_OF_EXECUTION)
            )
        self.start_at = archive_files[-1].id + 1
        transaction.commit()


class UpdatePPASigningKeyFingerprintToRSA4096Key(TunableLoop):
    """
    Update the signing key fingerprint of archives with an rsa1024
    signing key and an rsa4096 signing key to be the fingerprint of
    the rsa4096 signing key.
    """

    maximum_chunk_size = 1000

    def __init__(self, log, abort_time=None):
        super().__init__(log, abort_time)
        self.store = IPrimaryStore(Archive)
        self.start_at = 1

    def findAffectedArchives(self):
        join = (
            Archive,
            Join(
                GPGKey,
                GPGKey.fingerprint == Archive.signing_key_fingerprint,
            ),
        )
        return (
            self.store.using(*join)
            .find(
                Archive,
                Archive.purpose == ArchivePurpose.PPA,
                GPGKey.keysize == 1024,
            )
            .order_by(Archive.id)
        )

    def updateSigningKeyFingerprint(self, archive):
        archive_signing_key_set = getUtility(IArchiveSigningKeySet)
        rsa4096_key = archive_signing_key_set.get4096BitRSASigningKey(archive)
        if rsa4096_key:
            archive.signing_key_fingerprint = rsa4096_key.fingerprint
        else:
            self.log.debug(
                "Did not find an 4096-bit RSA signing key for '%s'.",
                archive.reference,
            )

    def isDone(self):
        return self.findAffectedArchives().is_empty()

    def __call__(self, chunk_size):
        for archive in self.findAffectedArchives()[:chunk_size]:
            self.log.debug(
                "Updating the signing key of '%s'.", archive.reference
            )
            self.updateSigningKeyFingerprint(archive)
        transaction.commit()


class BaseDatabaseGarbageCollector(LaunchpadCronScript):
    """Abstract base class to run a collection of TunableLoops."""

    script_name = None  # Script name for locking and database user. Override.
    tunable_loops = None  # Collection of TunableLoops. Override.
    continue_on_failure = False  # If True, an exception in a tunable loop
    # does not cause the script to abort.

    # Default run time of the script in seconds. Override.
    default_abort_script_time = None

    # _maximum_chunk_size is used to override the defined
    # maximum_chunk_size to allow our tests to ensure multiple calls to
    # __call__ are required without creating huge amounts of test data.
    _maximum_chunk_size = None

    def __init__(self, test_args=None):
        super().__init__(
            self.script_name,
            dbuser=self.script_name.replace("-", "_"),
            test_args=test_args,
        )

    def add_my_options(self):
        self.parser.add_option(
            "-x",
            "--experimental",
            dest="experimental",
            default=False,
            action="store_true",
            help="Run experimental jobs. Normally this is just for staging.",
        )
        self.parser.add_option(
            "--abort-script",
            dest="abort_script",
            default=self.default_abort_script_time,
            action="store",
            type="float",
            metavar="SECS",
            help="Abort script after SECS seconds [Default %d]."
            % self.default_abort_script_time,
        )
        self.parser.add_option(
            "--abort-task",
            dest="abort_task",
            default=None,
            action="store",
            type="float",
            metavar="SECS",
            help="Abort a task if it runs over SECS seconds "
            "[Default (threads * abort_script / tasks)].",
        )
        self.parser.add_option(
            "--threads",
            dest="threads",
            default=multiprocessing.cpu_count(),
            action="store",
            type="int",
            metavar="NUM",
            help="Run NUM tasks in parallel [Default %d]."
            % multiprocessing.cpu_count(),
        )

    def main(self):
        self.start_time = time.time()

        # Any email we send can safely be queued until the transaction is
        # committed.
        set_immediate_mail_delivery(False)

        # Stores the number of failed tasks.
        self.failure_count = 0

        # Copy the list so we can safely consume it.
        tunable_loops = list(self.tunable_loops)
        if self.options.experimental:
            tunable_loops.extend(self.experimental_tunable_loops)

        threads = set()
        for count in range(0, self.options.threads):
            thread = threading.Thread(
                target=self.run_tasks_in_thread,
                name="Worker-%d" % (count + 1,),
                args=(tunable_loops,),
            )
            thread.start()
            threads.add(thread)

        # Block until all the worker threads have completed. We block
        # until the script timeout is hit, plus 60 seconds. We wait the
        # extra time because the loops are supposed to shut themselves
        # down when the script timeout is hit, and the extra time is to
        # give them a chance to clean up.
        for thread in threads:
            time_to_go = self.get_remaining_script_time() + 60
            if time_to_go > 0:
                thread.join(time_to_go)
            else:
                break

        if self.get_remaining_script_time() < 0:
            self.logger.info(
                "Script aborted after %d seconds.", self.script_timeout
            )

        if tunable_loops:
            self.logger.warning("%d tasks did not run.", len(tunable_loops))

        if self.failure_count:
            self.logger.error("%d tasks failed.", self.failure_count)
            raise SilentLaunchpadScriptFailure(self.failure_count)

    def get_remaining_script_time(self):
        return self.start_time + self.script_timeout - time.time()

    @property
    def script_timeout(self):
        a_very_long_time = 31536000  # 1 year
        return self.options.abort_script or a_very_long_time

    def get_loop_logger(self, loop_name):
        """Retrieve a logger for use by a particular task.

        The logger will be configured to add the loop_name as a
        prefix to all log messages, making interleaved output from
        multiple threads somewhat readable.
        """
        loop_logger = logging.getLogger("garbo." + loop_name)
        for filter in loop_logger.filters:
            if isinstance(filter, PrefixFilter):
                return loop_logger  # Already have a PrefixFilter attached.
        loop_logger.addFilter(PrefixFilter(loop_name))
        return loop_logger

    def get_loop_abort_time(self, num_remaining_tasks):
        # How long until the task should abort.
        if self.options.abort_task is not None:
            # Task timeout specified on command line.
            abort_task = self.options.abort_task

        elif num_remaining_tasks <= self.options.threads:
            # We have a thread for every remaining task. Let
            # the task run until the script timeout.
            self.logger.debug2("Task may run until script timeout.")
            abort_task = self.get_remaining_script_time()

        else:
            # Evenly distribute the remaining time to the
            # remaining tasks.
            abort_task = (
                self.options.threads
                * self.get_remaining_script_time()
                / num_remaining_tasks
            )

        return min(abort_task, self.get_remaining_script_time())

    def run_tasks_in_thread(self, tunable_loops):
        """Worker thread target to run tasks.

        Tasks are removed from tunable_loops and run one at a time,
        until all tasks that can be run have been run or the script
        has timed out.
        """
        self.logger.debug(
            "Worker thread %s running.", threading.current_thread().name
        )
        install_feature_controller(make_script_feature_controller(self.name))
        self.login()

        while True:
            # How long until the script should abort.
            if self.get_remaining_script_time() <= 0:
                # Exit silently. We warn later.
                self.logger.debug(
                    "Worker thread %s detected script timeout.",
                    threading.current_thread().name,
                )
                break

            try:
                tunable_loop_class = tunable_loops.pop(0)
            except IndexError:
                # We catch the exception rather than checking the
                # length first to avoid race conditions with other
                # threads.
                break

            loop_name = tunable_loop_class.__name__

            loop_logger = self.get_loop_logger(loop_name)

            # Acquire a lock for the task. Multiple garbo processes
            # might be running simultaneously.
            loop_lock_path = os.path.join(
                LOCK_PATH, "launchpad-garbo-%s.lock" % loop_name
            )
            # No logger - too noisy, so report issues ourself.
            loop_lock = GlobalLock(loop_lock_path, logger=None)
            try:
                loop_lock.acquire()
                loop_logger.debug("Acquired lock %s.", loop_lock_path)
            except LockAlreadyAcquired:
                # If the lock cannot be acquired, but we have plenty
                # of time remaining, just put the task back to the
                # end of the queue.
                if self.get_remaining_script_time() > 60:
                    loop_logger.debug3(
                        "Unable to acquire lock %s. Running elsewhere?",
                        loop_lock_path,
                    )
                    time.sleep(0.3)  # Avoid spinning.
                    tunable_loops.append(tunable_loop_class)
                # Otherwise, emit a warning and skip the task.
                else:
                    loop_logger.warning(
                        "Unable to acquire lock %s. Running elsewhere?",
                        loop_lock_path,
                    )
                continue

            try:
                loop_logger.info("Running %s", loop_name)

                abort_time = self.get_loop_abort_time(len(tunable_loops) + 1)
                loop_logger.debug2(
                    "Task will be terminated in %0.3f seconds", abort_time
                )

                tunable_loop = tunable_loop_class(
                    abort_time=abort_time, log=loop_logger
                )

                # Allow the test suite to override the chunk size.
                if self._maximum_chunk_size is not None:
                    tunable_loop.maximum_chunk_size = self._maximum_chunk_size

                try:
                    tunable_loop.run()
                    loop_logger.debug("%s completed successfully.", loop_name)
                except Exception:
                    loop_logger.exception("Unhandled exception")
                    self.failure_count += 1

            finally:
                loop_lock.release()
                loop_logger.debug("Released lock %s.", loop_lock_path)
                transaction.abort()


class FrequentDatabaseGarbageCollector(BaseDatabaseGarbageCollector):
    """Run every 5 minutes.

    This may become even more frequent in the future.

    Jobs with low overhead can go here to distribute work more evenly.
    """

    script_name = "garbo-frequently"
    tunable_loops = [
        AntiqueSessionPruner,
        ArchiveSubscriptionExpirer,
        BugSummaryJournalRollup,
        BugWatchScheduler,
        OpenIDConsumerAssociationPruner,
        OpenIDConsumerNoncePruner,
        PopulateDistributionSourcePackageCache,
        PopulateLatestPersonSourcePackageReleaseCache,
    ]
    experimental_tunable_loops = []

    # 5 minutes minus 20 seconds for cleanup. This helps ensure the
    # script is fully terminated before the next scheduled hourly run
    # kicks in.
    default_abort_script_time = 60 * 5 - 20


class HourlyDatabaseGarbageCollector(BaseDatabaseGarbageCollector):
    """Run every hour.

    Jobs we want to run fairly often but have noticeable overhead go here.
    """

    script_name = "garbo-hourly"
    tunable_loops = [
        ArchiveAuthTokenDeactivator,
        BugHeatUpdater,
        DuplicateSessionPruner,
        GitRepositoryPruner,
        RevisionCachePruner,
        UnusedSessionPruner,
        UpdatePPASigningKeyFingerprintToRSA4096Key,
    ]
    experimental_tunable_loops = []

    # 1 hour, minus 5 minutes for cleanup. This ensures the script is
    # fully terminated before the next scheduled hourly run kicks in.
    default_abort_script_time = 60 * 55


class DailyDatabaseGarbageCollector(BaseDatabaseGarbageCollector):
    """Run every day.

    Jobs that don't need to be run frequently.

    If there is low overhead, consider putting these tasks in more
    frequently invoked lists to distribute the work more evenly.
    """

    script_name = "garbo-daily"
    tunable_loops = [
        AnswerContactPruner,
        ArchiveArtifactoryColumnsPopulator,
        ArchiveFileDatePopulator,
        BinaryPackagePublishingHistoryFormatPopulator,
        BinaryPackagePublishingHistorySPNPopulator,
        BranchJobPruner,
        BranchMergeProposalJobPruner,
        BugNotificationPruner,
        BugWatchActivityPruner,
        CodeImportEventPruner,
        CodeImportResultPruner,
        DiffPruner,
        GitJobPruner,
        LiveFSFilePruner,
        LoginTokenPruner,
        OCIFilePruner,
        ObsoleteBugAttachmentPruner,
        OldTimeLimitedTokenDeleter,
        POTranslationPruner,
        PreviewDiffPruner,
        ProductVCSPopulator,
        RevisionAuthorEmailLinker,
        RevisionStatusReportPruner,
        ScrubPOFileTranslator,
        SnapBuildJobPruner,
        SnapFilePruner,
        SourcePackagePublishingHistoryFormatPopulator,
        SourcePackageReleaseHomepagePopulator,
        SuggestiveTemplatesCacheUpdater,
        TeamMembershipPruner,
        UnlinkedAccountPruner,
        UnusedDistributionAccessPolicyPruner,
        UnusedPOTMsgSetPruner,
        UnusedProductAccessPolicyPruner,
        WebhookJobPruner,
    ]
    experimental_tunable_loops = [
        PersonPruner,
    ]

    # 1 day, minus 30 minutes for cleanup. This ensures the script is
    # fully terminated before the next scheduled daily run kicks in.
    default_abort_script_time = 60 * 60 * 23.5
