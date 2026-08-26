#  Copyright 2025 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

"""A SOSS (SOSS CVE Tracker) bug exporter"""
import logging
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Union

from zope.component import getUtility

from lp.bugs.model.bug import Bug as BugModel
from lp.bugs.model.bugtask import BugTask
from lp.bugs.model.vulnerability import Vulnerability
from lp.bugs.scripts.soss.models import SOSSRecord
from lp.bugs.scripts.soss.sossimport import (
    PACKAGE_STATUS_MAP,
    PACKAGE_TYPE_MAP,
    PRIORITY_ENUM_MAP,
)
from lp.bugs.scripts.svthandler import SVTExporter
from lp.registry.interfaces.distribution import IDistributionSet
from lp.registry.interfaces.role import IPersonRoles
from lp.registry.security import SecurityAdminDistribution

__all__ = [
    "SOSSExporter",
]

logger = logging.getLogger(__name__)

# Constants moved to module level with proper naming
PRIORITY_ENUM_MAP_REVERSE = {v: k for k, v in PRIORITY_ENUM_MAP.items()}

PACKAGE_TYPE_MAP_REVERSE = {v: k for k, v in PACKAGE_TYPE_MAP.items()}

PACKAGE_STATUS_MAP_REVERSE = {v: k for k, v in PACKAGE_STATUS_MAP.items()}


class SOSSExporter(SVTExporter):
    """
    SOSSExporter is used to export Launchpad Vulnerability data to SOSS CVE
    files.
    """

    def _get_packages(
        self, bugtasks: List[BugTask]
    ) -> Dict[SOSSRecord.PackageTypeEnum, SOSSRecord.Package]:
        """Get a dict of SOSSRecord.PackageTypeEnum: SOSSRecord.Package from a
        bugtask list.
        """
        packages = defaultdict(list)
        for bugtask in bugtasks:
            pkg = SOSSRecord.Package(
                name=bugtask.target.name,
                channel=SOSSRecord.Channel(
                    value="/".join(s for s in bugtask.channel if s is not None)
                ),
                repositories=bugtask.metadata.get("repositories", ()),
                status=SOSSRecord.PackageStatusEnum(
                    PACKAGE_STATUS_MAP_REVERSE.get(
                        bugtask.status,
                        SOSSRecord.PackageStatusEnum.NEEDS_TRIAGE,
                    )
                ),
                note=bugtask.status_explanation or "",
            )
            packages[PACKAGE_TYPE_MAP_REVERSE[bugtask.packagetype]].append(pkg)

        ordered_packages = {
            k: sorted(packages[k])
            for k in PACKAGE_TYPE_MAP_REVERSE.values()
            if packages[k]
        }

        return ordered_packages

    def _get_cvss(self, cvss: Dict) -> List[SOSSRecord.CVSS]:
        """Get a list of SOSSRecord.CVSS from a cvss dict"""
        cvss_list = []
        for authority in cvss.keys():
            for c in cvss[authority]:
                cvss_list.append(
                    SOSSRecord.CVSS(
                        c.get("source"),
                        c.get("vector"),
                        c.get("baseScore"),
                        c.get("baseSeverity"),
                    )
                )
        return cvss_list

    def _get_extra_attrs(self, vulnerability: Vulnerability) -> Optional[Dict]:
        """Get the extra_attrs dict from vulnerability metadata if it exists"""

        if isinstance(vulnerability.metadata, dict):
            extra_attrs = vulnerability.metadata.get("extra_attrs")

            # Ensure extra_attrs is a sorted dict (since the database doesn't
            # guarantee order)
            if isinstance(extra_attrs, dict):
                extra_attrs = dict(
                    sorted(extra_attrs.items(), key=lambda item: item[0])
                )

            return extra_attrs

        return None

    def to_record(
        self,
        bug: BugModel,
        vulnerability: Vulnerability,
    ) -> SOSSRecord:
        """Return a SOSSRecord exporting Launchpad data for the specified
        cve_sequence.
        """
        self._validate_to_record_args(bug, vulnerability)

        # Parse bug
        desc_parts = bug.description.rsplit("\n\nReferences:\n", maxsplit=1)
        references = desc_parts[1].split("\n") if len(desc_parts) > 1 else []

        # Parse bug.bugtasks
        packages = self._get_packages(bug.bugtasks)
        assigned_to = (
            bug.bugtasks[0].assignee.name if bug.bugtasks[0].assignee else ""
        )

        # Parse vulnerability.metadata["extra_attrs"]
        extra_attrs = self._get_extra_attrs(vulnerability)

        # Parse vulnerability
        public_date = self._normalize_date_without_timezone(
            vulnerability.date_made_public
        )
        notes = self._format_notes(vulnerability.notes)
        priority = SOSSRecord.PriorityEnum(
            PRIORITY_ENUM_MAP_REVERSE.get(
                vulnerability.importance, SOSSRecord.PriorityEnum.NEEDS_TRIAGE
            )
        )

        return SOSSRecord(
            references=references,
            notes=notes,
            priority=priority,
            priority_reason=vulnerability.importance_explanation,
            assigned_to=assigned_to,
            packages=packages,
            candidate=vulnerability.cve.sequence,
            description=vulnerability.description,
            cvss=self._get_cvss(vulnerability.cvss),
            public_date=public_date,
            extra_attrs=extra_attrs,
        )

    def _format_notes(self, notes: str) -> List[Union[Dict, str]]:
        """Return a list of dicts or strings using from the notes string. Each
        dict contains the user and the note added.
        """
        if not notes:
            return []

        formatted_notes = []
        for note in notes.split("\n\n"):
            if len(note.split(":", maxsplit=1)) == 2:
                key, value = note.split(":")
                formatted_notes.append({key: value.strip()})
            else:
                # Fallback to simple string
                formatted_notes.append(str(note))

        return formatted_notes

    def _validate_to_record_args(
        self,
        bug: BugModel,
        vulnerability: Vulnerability,
    ):
        required_args = {
            "Bug": bug,
            "Vulnerability": vulnerability,
        }

        for name, value in required_args.items():
            if value is None:
                logger.error(f"[SOSSExporter] {name} can't be None")
                raise ValueError(f"{name} can't be None")

    def _normalize_date_without_timezone(
        self, date_obj: datetime
    ) -> Optional[datetime]:
        """Normalize date to no timezone if needed."""
        if date_obj and date_obj.tzinfo is not None:
            return date_obj.replace(tzinfo=None)
        return date_obj

    def checkUserPermissions(self, user):
        """Only users with security admin permissions to SOSS can use
        this handler"""
        soss = getUtility(IDistributionSet).getByName("soss")
        return SecurityAdminDistribution(soss).checkAuthenticated(
            IPersonRoles(user)
        )
