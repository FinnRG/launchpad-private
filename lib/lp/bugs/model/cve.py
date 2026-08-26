# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "Cve",
    "CveSet",
]

import operator
from datetime import timezone

from storm.databases.postgres import JSON
from storm.locals import DateTime, Desc, Int, ReferenceSet, Store, Unicode
from zope.component import getUtility
from zope.interface import implementer
from zope.security.interfaces import Unauthorized

from lp.app.validators.cve import (
    VULNERABILITY_TEXT_PATTERN,
    VULNERABILITY_URL_PREFIXES,
    valid_cve,
    valid_vulnerability_id,
)
from lp.bugs.interfaces.buglink import IBugLinkTarget
from lp.bugs.interfaces.cve import CveStatus, ICve, ICveSet
from lp.bugs.model.bug import Bug
from lp.bugs.model.buglinktarget import BugLinkTargetMixin
from lp.bugs.model.cvereference import CveReference
from lp.bugs.model.vulnerability import (
    Vulnerability,
    get_vulnerability_privacy_filter,
)
from lp.registry.interfaces.role import IPersonRoles
from lp.registry.model.distribution import Distribution
from lp.registry.security import SecurityAdminDistribution
from lp.services.config import config
from lp.services.database import bulk
from lp.services.database.constants import UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.database.stormexpr import fti_search
from lp.services.webapp.interfaces import ILaunchBag
from lp.services.xref.interfaces import IXRefSet
from lp.services.xref.model import XRef


@implementer(ICve, IBugLinkTarget)
class Cve(StormBase, BugLinkTargetMixin):
    """A CVE database record."""

    __storm_table__ = "Cve"

    id = Int(primary=True)

    sequence = Unicode(allow_none=False)
    status = DBEnum(name="status", enum=CveStatus, allow_none=False)
    description = Unicode(allow_none=False)
    datecreated = DateTime(
        tzinfo=timezone.utc, allow_none=False, default=UTC_NOW
    )
    datemodified = DateTime(
        tzinfo=timezone.utc, allow_none=False, default=UTC_NOW
    )

    references = ReferenceSet(
        id, "CveReference.cve_id", order_by="CveReference.id"
    )

    date_made_public = DateTime(tzinfo=timezone.utc, allow_none=True)
    discovered_by = Unicode(allow_none=True)
    cvss = JSON(name="cvss", allow_none=True)
    metadata = JSON(name="metadata", allow_none=True)

    def __init__(
        self,
        sequence,
        status,
        description,
        date_made_public=None,
        discovered_by=None,
        cvss=None,
        metadata=None,
    ):
        super().__init__()
        self.sequence = sequence
        self.status = status
        self.description = description
        self.date_made_public = date_made_public
        self.discovered_by = discovered_by
        self.cvss = cvss
        self.metadata = metadata

    @property
    def url(self):
        """See ICve."""
        for prefix, template in VULNERABILITY_URL_PREFIXES.items():
            if self.sequence.startswith(prefix):
                return template.format(seq=self.sequence)
        # For other advisory schemes there is no known canonical URL.
        return None

    @property
    def displayname(self):
        # sequence always includes its prefix (e.g. "CVE-2024-1234",
        # "GHSA-2c6m-6gqh-6qg3", "ABD-...")
        return self.sequence

    @property
    def title(self):
        return "%s (%s)" % (self.displayname, self.status.title)

    @property
    def bugs(self):
        bug_ids = [
            int(id)
            for _, id in getUtility(IXRefSet).findFrom(
                ("cve", self.sequence), types=["bug"]
            )
        ]
        return list(
            sorted(bulk.load(Bug, bug_ids), key=operator.attrgetter("id"))
        )

    def getDistributionVulnerability(self, distribution):
        """See `ICve`."""
        return (
            Store.of(self)
            .find(
                Vulnerability,
                Vulnerability.cve == self,
                Vulnerability.distribution_id == distribution.id,
            )
            .one()
        )

    def getVulnerabilitiesVisibleToUser(self, user):
        """See `ICve`."""
        vulnerabilities = Store.of(self).find(
            Vulnerability,
            Vulnerability.cve == self,
            get_vulnerability_privacy_filter(user),
        )
        vulnerabilities.order_by(Desc(Vulnerability.date_created))

        def preload_distributions(rows):
            bulk.load_related(Distribution, rows, ["distribution_id"])

        return DecoratedResultSet(
            vulnerabilities,
            pre_iter_hook=preload_distributions,
        )

    @property
    def vulnerabilities(self):
        """See `ICve`."""
        return self.getVulnerabilitiesVisibleToUser(
            getUtility(ILaunchBag).user
        )

    # CveReference's
    def createReference(self, source, content, url=None):
        """See ICveReference."""
        return CveReference(cve=self, source=source, content=content, url=url)

    def removeReference(self, ref):
        assert ref.cve == self
        Store.of(ref).remove(ref)

    def createBugLink(self, bug, props=None):
        """See BugLinkTargetMixin."""
        if props is None:
            props = {}
        # XXX: Should set creator.
        getUtility(IXRefSet).create(
            {("cve", self.sequence): {("bug", str(bug.id)): props}}
        )

    def deleteBugLink(self, bug):
        """See BugLinkTargetMixin."""
        getUtility(IXRefSet).delete(
            {("cve", self.sequence): [("bug", str(bug.id))]}
        )


@implementer(ICveSet)
class CveSet:
    """The full set of ICve's."""

    def __init__(self, bug=None):
        """See ICveSet."""
        self.title = "The Common Vulnerabilities and Exposures database"

    def __getitem__(self, sequence):
        """See ICveSet."""
        # Normalise bare CVE sequences typed by users (e.g. "1999-1717")
        # to their full prefixed form for backward compatibility.
        if valid_cve(sequence):
            sequence = "CVE-" + sequence
        if not valid_vulnerability_id(sequence):
            return None
        cve = IStore(Cve).find(Cve, sequence=sequence).one()
        # TODO: remove once the DB migration to prefixed sequences is complete.
        # Fall back to the bare YYYY-NNNN form for CVE/CAN records not yet
        # migrated.
        if cve is None and sequence.startswith(("CVE-", "CAN-")):
            cve = IStore(Cve).find(Cve, Cve.sequence == sequence[4:]).one()
        return cve

    def getAll(self):
        """See ICveSet."""
        return IStore(Cve).find(Cve).order_by(Desc(Cve.datemodified))

    def __iter__(self):
        """See ICveSet."""
        return iter(IStore(Cve).find(Cve))

    def new(
        self,
        sequence,
        description,
        status=CveStatus.CANDIDATE,
        date_made_public=None,
        discovered_by=None,
        cvss=None,
        metadata=None,
    ):
        """See ICveSet."""
        # TODO: remove once the DB migration to prefixed sequences is complete.
        # Legacy records may still be stored as bare YYYY-NNNN sequences.
        # If a bare equivalent already exists, return it to avoid a duplicate.
        if sequence.startswith(("CVE-", "CAN-")):
            bare = sequence[4:]  # strip the 4-char "CVE-" / "CAN-" prefix
            existing = IStore(Cve).find(Cve, Cve.sequence == bare).one()
            if existing is not None:
                return existing

        cve = Cve(
            sequence=sequence,
            status=status,
            description=description,
            date_made_public=date_made_public,
            discovered_by=discovered_by,
            cvss=cvss,
            metadata=metadata,
        )

        IStore(Cve).add(cve)
        return cve

    def latest(self, quantity=5):
        """See ICveSet."""
        return (
            IStore(Cve)
            .find(Cve)
            .order_by(Desc(Cve.datecreated))
            .config(limit=quantity)
        )

    def latest_modified(self, quantity=5):
        """See ICveSet."""
        return (
            IStore(Cve)
            .find(Cve)
            .order_by(Desc(Cve.datemodified))
            .config(limit=quantity)
        )

    def search(self, text):
        """See ICveSet."""
        return (
            IStore(Cve)
            .find(Cve, fti_search(Cve, text))
            .order_by(Desc(Cve.datemodified))
            .config(distinct=True)
        )

    def getFilteredCves(
        self,
        in_distribution=None,
        not_in_distribution=None,
        modified_since=None,
        offset=0,
        limit=config.launchpad.default_batch_size,
    ):
        """See `ICveSet`."""
        in_distribution_id = [d.id for d in in_distribution or []]
        in_distribution_len = len(in_distribution_id)

        not_in_distribution_id = [d.id for d in not_in_distribution or []]

        # Storm does not support the FILTER and ANY
        # same query using storm models would be less performant
        query = """
                SELECT c.sequence
                FROM cve c
                LEFT JOIN vulnerability v ON c.id = v.cve
                WHERE (%s is NULL OR c.datemodified > %s)
                GROUP BY c.id
                HAVING
                    (
                        %s = 0
                        OR COUNT(DISTINCT v.distribution) FILTER (
                            WHERE v.distribution = ANY(%s)
                        ) = %s
                    )
                    AND COUNT(DISTINCT v.distribution) FILTER (
                        WHERE v.distribution = ANY(%s)
                    ) = 0
                ORDER BY c.datemodified DESC
                OFFSET %s
                LIMIT %s;
        """

        # params are sanitized using store.execute() second param
        params = (
            modified_since,
            modified_since,
            in_distribution_len,
            in_distribution_id,
            in_distribution_len,
            not_in_distribution_id,
            offset,
            limit,
        )

        store = IStore(Vulnerability)
        result = store.execute(query, params)
        for r in result:
            yield r[0]

    def advancedSearch(
        self,
        requester,
        in_distribution=None,
        not_in_distribution=None,
        modified_since=None,
        offset=0,
        limit=config.launchpad.default_batch_size,
    ):
        """See `ICveSet`."""
        if not requester:
            raise Unauthorized(
                "Only authenticated users can use this endpoint"
            )

        for distro in (in_distribution or []) + (not_in_distribution or []):
            user = IPersonRoles(requester)
            if not SecurityAdminDistribution(distro).checkAuthenticated(user):
                raise Unauthorized(
                    f"Only security admins can use {distro.name} as a filter"
                )

        cves = list(
            self.getFilteredCves(
                in_distribution,
                not_in_distribution,
                modified_since,
                offset,
                limit,
            )
        )
        return {"cves": cves}

    def inText(self, text):
        """See ICveSet."""
        # let's look for matching entries
        cves = set()

        for match in VULNERABILITY_TEXT_PATTERN.finditer(text):
            # let's get the core CVE data
            sequence = match.group(0)
            # see if there is already a matching CVE ref in the db, and if
            # not, then create it
            cve = self[sequence]
            if cve is None:
                cve = self.new(
                    sequence=sequence,
                    status=CveStatus.DEPRECATED,
                    description="This CVE was automatically created from "
                    "a reference found in an email or other text. If you "
                    "are reading this, then this CVE entry is probably "
                    "erroneous, since this text should be replaced by "
                    "the official CVE description automatically.",
                )
            cves.add(cve)

        return sorted(cves, key=lambda a: a.sequence)

    def getBugCvesForBugTasks(self, bugtasks, cve_mapper=None):
        """See ICveSet."""
        bugs = bulk.load_related(Bug, bugtasks, ("bug_id",))
        if len(bugs) == 0:
            return []
        store = Store.of(bugtasks[0])

        xrefs = getUtility(IXRefSet).findFromMany(
            [("bug", str(bug.id)) for bug in bugs], types=["cve"]
        )
        bugcve_ids = set()
        for bug_key in xrefs:
            for cve_key in xrefs[bug_key]:
                bugcve_ids.add((int(bug_key[1]), cve_key[1]))

        bugcve_ids = list(sorted(bugcve_ids))

        cves = store.find(
            Cve, Cve.sequence.is_in([seq for _, seq in bugcve_ids])
        )

        if cve_mapper is None:
            cvemap = {cve.sequence: cve for cve in cves}
        else:
            cvemap = {cve.sequence: cve_mapper(cve) for cve in cves}
        bugmap = {bug.id: bug for bug in bugs}
        return [
            (bugmap[bug_id], cvemap[cve_sequence])
            for bug_id, cve_sequence in bugcve_ids
        ]

    def getBugCveCount(self):
        """See ICveSet."""
        return (
            IStore(XRef)
            .find(XRef, XRef.from_type == "bug", XRef.to_type == "cve")
            .count()
        )
