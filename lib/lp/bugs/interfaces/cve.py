# Copyright 2009-2011 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""CVE interfaces."""

__all__ = [
    "CveStatus",
    "ICve",
    "ICveSet",
]

from typing import Iterator, Optional

from lazr.enum import DBEnumeratedType, DBItem
from lazr.restful.declarations import (
    REQUEST_USER,
    call_with,
    collection_default_content,
    export_read_operation,
    exported,
    exported_as_webservice_collection,
    exported_as_webservice_entry,
    operation_for_version,
    operation_parameters,
)
from lazr.restful.fields import CollectionField, Reference
from zope.interface import Attribute, Interface
from zope.schema import Choice, Datetime, Dict, Int, List, Text, TextLine

from lp import _
from lp.app.validators.validation import valid_cve_sequence
from lp.services.config import config


class CveStatus(DBEnumeratedType):
    """The Status of this item in the CVE Database.

    When a potential problem is reported to the CVE authorities they assign
    a CAN number to it. At a later stage, that may be converted into a CVE
    number. This indicator tells us whether or not the issue is believed to
    be a CAN or a CVE.
    """

    CANDIDATE = DBItem(
        1,
        """
        Candidate

        The vulnerability is a candidate which hasn't yet been confirmed and
        given "Entry" status.
        """,
    )

    ENTRY = DBItem(
        2,
        """
        Entry

        This vulnerability or threat has been assigned a CVE number, and is
        fully documented. It has been through the full CVE verification
        process.
        """,
    )

    DEPRECATED = DBItem(
        3,
        """
        Deprecated

        This entry is deprecated, and should no longer be referred to in
        general correspondence. There is either a newer entry that better
        defines the problem, or the original candidate was never promoted to
        "Entry" status.
        """,
    )

    REJECTED = DBItem(
        4,
        """
        Rejected

        This CVE has been rejected or withdrawn by its CVE Numbering Authority.
        """,
    )

    PLACEHOLDER = DBItem(
        99,
        """
        Placeholder

        This CVE entry was automatically created as a placeholder because it
        was not yet present in the Launchpad CVE database.
        """,
    )


@exported_as_webservice_entry(as_of="beta")
class ICve(Interface):
    """A single CVE database entry."""

    id = Int(title=_("ID"), required=True, readonly=True)
    sequence = exported(
        TextLine(
            title=_("CVE Sequence Number"),
            description=_("Should take the form XXXX-XXXX, all digits."),
            required=True,
            readonly=True,
            constraint=valid_cve_sequence,
        )
    )
    status = exported(
        Choice(
            title=_("Current CVE State"),
            default=CveStatus.CANDIDATE,
            description=_(
                "Whether or not the "
                "vulnerability has been reviewed and assigned a "
                "full CVE number, or is still considered a "
                "Candidate, or is deprecated."
            ),
            required=True,
            readonly=True,
            vocabulary=CveStatus,
        )
    )
    description = exported(
        TextLine(
            title=_("Title"),
            description=_(
                "A description of the CVE issue. This will be "
                "updated regularly from the CVE database."
            ),
            required=True,
            readonly=True,
        )
    )
    datecreated = exported(
        Datetime(title=_("Date Created"), required=True, readonly=True),
        exported_as="date_created",
    )
    datemodified = exported(
        Datetime(title=_("Date Modified"), required=True, readonly=True),
        exported_as="date_modified",
    )
    bugs = exported(
        CollectionField(
            title=_("Bugs related to this CVE entry."),
            readonly=True,
            value_type=Reference(schema=Interface),
        )
    )  # Redefined in bug.py.

    # Other attributes.
    url = exported(
        TextLine(
            title=_("URL"),
            description=_(
                "Return a URL to the site that has the CVE "
                "data for this CVE reference."
            ),
            readonly=True,
        )
    )
    displayname = exported(
        TextLine(
            title=_("Display Name"),
            description=_(
                "A very brief name describing " "the ref and state."
            ),
            readonly=True,
        ),
        exported_as="display_name",
    )
    title = exported(
        TextLine(
            title=_("Title"),
            description=_("A title for the CVE"),
            readonly=True,
        )
    )
    references = Attribute("The set of CVE References for this CVE.")

    vulnerabilities = Attribute("Vulnerabilities related to this CVE entry.")

    date_made_public = exported(
        Datetime(title=_("Date Made Public"), required=False, readonly=True),
        as_of="devel",
    )

    discovered_by = exported(
        TextLine(
            title=_("Discovered by"),
            description=_(
                "The name of person(s) or organization that discovered the CVE"
            ),
            required=False,
            readonly=True,
        ),
        as_of="devel",
    )

    cvss = exported(
        List(
            title=_("CVSS"),
            description=_(
                "The CVSS vector strings from various authorities "
                "that publish it."
            ),
            required=False,
            readonly=True,
        ),
        as_of="devel",
    )

    metadata = exported(
        Dict(
            title=_("metadata"),
            description=_("CVE metadata."),
            key_type=Text(),
            value_type=Text(),
            required=False,
            readonly=True,
        ),
        as_of="devel",
    )

    def createReference(source, content, url=None):
        """Create a new CveReference for this CVE."""

    def removeReference(ref):
        """Remove a CveReference."""

    def getDistributionVulnerability(self, distribution):
        """Return the linked vulnerability for the given distribution."""

    def getVulnerabilitiesVisibleToUser(user):
        """Return the linked vulnerabilities visible to the given user."""


@exported_as_webservice_collection(ICve)
class ICveSet(Interface):
    """The set of ICve objects."""

    title = Attribute("Title")

    def __getitem__(key):
        """Get a Cve by sequence number."""

    def __iter__():
        """Iterate through all the Cve records."""

    def new(
        sequence,
        description,
        cvestate=CveStatus.CANDIDATE,
        date_made_public=None,
        discovered_by=None,
        cvss=None,
    ):
        """Create a new ICve."""

    @collection_default_content()
    def getAll():
        """Return all ICVEs"""

    def latest(quantity=5):
        """Return the most recently created CVE's, newest first, up to the
        number given in quantity."""

    def latest_modified(quantity=5):
        """Return the most recently modified CVE's, newest first, up to the
        number given in quantity."""

    def search(text):
        """Search the CVE database for matching CVE entries."""

    def getFilteredCves(
        in_distribution: Optional[List] = None,
        not_in_distribution: Optional[List] = None,
        modified_since: Optional[Datetime] = None,
        offset: int = 0,
        limit: int = config.launchpad.default_batch_size,
    ) -> Iterator[str]:
        """Return an iterator of cve sequences that matches the given filters.

        :param in_distribution: filter cves that have a vulnerability for all
            of these distributions.
        :param not_in_distribution: filter cves that have no vulnerability for
            any of these distributions.
        :param modified_since: only updated cves after `modified_since` will be
            returned.
        :param offset: offset of cves to return. It defaults to 0.
        :param limit: return `limit` cves at max. It defaults to
            config.launchpad.default_batch_size.
        """

    # in_distribution patched in lp.bugs.interfaces.webservice
    # not_in_distribution patched in lp.bugs.interfaces.webservice
    @operation_parameters(
        in_distribution=List(
            title=_("Distributions linked to the cve"),
            value_type=Reference(schema=Interface),
            required=False,
        ),
        not_in_distribution=List(
            title=_("Distributions not linked to the cve"),
            value_type=Reference(schema=Interface),
            required=False,
        ),
        modified_since=Datetime(
            title=_("Minimum cve.datemodified"),
            description=_("Ignore cves that are older than this."),
            required=False,
        ),
        offset=Int(
            title=_("Offset of cves to return"),
            required=False,
            default=0,
        ),
        limit=Int(
            title=_("Maximum number of cves to return"),
            required=False,
            default=config.launchpad.default_batch_size,
        ),
    )
    @call_with(requester=REQUEST_USER)
    @export_read_operation()
    @operation_for_version("devel")
    def advancedSearch(
        requester,
        in_distribution: Optional[List] = None,
        not_in_distribution: Optional[List] = None,
        modified_since: Optional[Datetime] = None,
        offset: int = 0,
        limit: int = config.launchpad.default_batch_size,
    ) -> dict:
        """Return cve sequences that matches the given filters.

        :param in_distribution: filter cves that have a vulnerability for all
            of these distributions.
        :param not_in_distribution: filter cves that have no vulnerability for
            any of these distributions.
        :param modified_since: only updated cves after `modified_since` will be
            returned.
        :param offset: offset of cves to return. It defaults to 0.
        :param limit: return `limit` cves at max. It defaults to
            config.launchpad.default_batch_size.
        """

    def inText(text):
        """Find one or more Cve's by analysing the given text.

        This will look for references to CVE or CAN numbers, and return the
        CVE references. It will create any CVE's that it sees which are
        already not in the database. It returns the list of all the CVE's it
        found in the text.
        """

    def getBugCvesForBugTasks(bugtasks, cve_mapper=None):
        """Return (Bug, Cve) tuples that correspond to the supplied bugtasks.

        Returns an iterable of (Bug, Cve) tuples for bugs related to the
        supplied sequence of bugtasks.

        If a function cve_mapper is specified, a sequence of tuples
        (bug, cve_mapper(cve)) is returned.
        """

    def getBugCveCount():
        """Return the number of CVE bug links there is in Launchpad."""
