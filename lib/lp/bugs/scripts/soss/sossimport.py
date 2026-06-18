#  Copyright 2025 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

"""A SOSS (SOSS CVE Tracker) bug importer"""
import logging
import os
from collections import defaultdict
from datetime import timezone
from typing import Dict, List, Optional, Tuple, Union

import transaction
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.app.errors import NotFoundError
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.bugs.enums import VulnerabilityStatus
from lp.bugs.interfaces.bug import CreateBugParams, IBugSet
from lp.bugs.interfaces.bugtask import (
    BugTaskImportance,
    BugTaskStatus,
    IBugTaskSet,
)
from lp.bugs.interfaces.cve import ICveSet
from lp.bugs.interfaces.vulnerability import IVulnerabilitySet
from lp.bugs.model.bug import Bug as BugModel
from lp.bugs.model.cve import Cve as CveModel
from lp.bugs.model.vulnerability import Vulnerability
from lp.bugs.scripts.soss.models import SOSSRecord
from lp.bugs.scripts.svthandler import SVTImporter
from lp.registry.interfaces.externalpackage import ExternalPackageType
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.role import IPersonRoles
from lp.registry.interfaces.sourcepackagename import ISourcePackageNameSet
from lp.registry.model.distribution import Distribution
from lp.registry.model.externalpackage import ExternalPackage
from lp.registry.model.person import Person
from lp.registry.security import SecurityAdminDistribution

__all__ = [
    "SOSSImporter",
    "PRIORITY_ENUM_MAP",
    "PACKAGE_TYPE_MAP",
    "PACKAGE_STATUS_MAP",
    "DISTRIBUTION_NAME",
]

logger = logging.getLogger(__name__)

# Constants moved to module level with proper naming
PRIORITY_ENUM_MAP = {
    SOSSRecord.PriorityEnum.NEEDS_TRIAGE: BugTaskImportance.UNDECIDED,
    SOSSRecord.PriorityEnum.NEGLIGIBLE: BugTaskImportance.WISHLIST,
    SOSSRecord.PriorityEnum.LOW: BugTaskImportance.LOW,
    SOSSRecord.PriorityEnum.MEDIUM: BugTaskImportance.MEDIUM,
    SOSSRecord.PriorityEnum.HIGH: BugTaskImportance.HIGH,
    SOSSRecord.PriorityEnum.CRITICAL: BugTaskImportance.CRITICAL,
}

PACKAGE_TYPE_MAP = {
    SOSSRecord.PackageTypeEnum.CONDA: ExternalPackageType.CONDA,
    SOSSRecord.PackageTypeEnum.MAVEN: ExternalPackageType.MAVEN,
    SOSSRecord.PackageTypeEnum.PYTHON: ExternalPackageType.PYTHON,
    SOSSRecord.PackageTypeEnum.RUST: ExternalPackageType.CARGO,
    SOSSRecord.PackageTypeEnum.UNPACKAGED: ExternalPackageType.GENERIC,
}

PACKAGE_STATUS_MAP = {
    SOSSRecord.PackageStatusEnum.IGNORED: BugTaskStatus.WONTFIX,
    SOSSRecord.PackageStatusEnum.NEEDS_TRIAGE: BugTaskStatus.UNKNOWN,
    SOSSRecord.PackageStatusEnum.RELEASED: BugTaskStatus.FIXRELEASED,
    SOSSRecord.PackageStatusEnum.NOT_AFFECTED: BugTaskStatus.INVALID,
    SOSSRecord.PackageStatusEnum.DEFERRED: BugTaskStatus.DEFERRED,
    SOSSRecord.PackageStatusEnum.NEEDED: BugTaskStatus.NEW,
}

DISTRIBUTION_NAME = "soss"


class SOSSImporter(SVTImporter):
    """
    SOSSImporter is used to import SOSS CVE files to Launchpad database.
    """

    def __init__(
        self,
        distribution: Distribution,
        information_type: InformationType = InformationType.PROPRIETARY,
        dry_run: bool = False,
    ) -> None:
        self.information_type = information_type
        self.dry_run = dry_run
        self.bug_importer = getUtility(ILaunchpadCelebrities).bug_importer
        self.person_set = getUtility(IPersonSet)
        self.source_package_name_set = getUtility(ISourcePackageNameSet)
        self.bugtask_set = getUtility(IBugTaskSet)
        self.vulnerability_set = getUtility(IVulnerabilitySet)
        self.bug_set = getUtility(IBugSet)
        self.cve_set = getUtility(ICveSet)
        self.soss = distribution

    def import_cve_from_file(
        self, cve_path: str
    ) -> Tuple[BugModel, Vulnerability]:
        """Import CVE from file path."""
        cve_sequence = os.path.basename(cve_path)
        logger.info(f"[SOSSImporter] Importing {cve_sequence}")

        with open(cve_path, encoding="utf-8") as file:
            soss_record = SOSSRecord.from_yaml(file)

        bug, vulnerability, created = self.from_record(
            soss_record, cve_sequence
        )
        return bug, vulnerability, created

    def from_record(
        self, soss_record: SOSSRecord, cve_sequence: str
    ) -> Tuple[BugModel, Vulnerability]:
        """Import CVE from SOSS record."""
        self._validate_soss_record(soss_record, cve_sequence)
        lp_cve = self._get_launchpad_cve(cve_sequence)
        created = False

        # try-except block to ensure transaction is aborted on any exception,
        # preventing partial commits
        try:
            vulnerability = self._find_existing_vulnerability(
                lp_cve, self.soss
            )
            if not vulnerability:
                vulnerability = self._create_vulnerability(
                    soss_record, lp_cve, self.soss
                )
                created = True
            else:
                vulnerability = self._update_vulnerability(
                    vulnerability, soss_record
                )

            bug = self._find_existing_bug(soss_record, lp_cve, self.soss)
            if not bug:
                bug = self._create_bug(soss_record, lp_cve)
            else:
                bug = self._update_bug(bug, soss_record, lp_cve)

            vulnerability.linkBug(bug, check_permissions=False)

            # Creating a bug only creates the first bugtask
            self._create_or_update_bugtasks(bug, soss_record)
        except Exception:
            # This is needed here to prevent partial commits because even
            # though after this we are checking if vulnerability and bug are
            # created to commit, the ImportVulnerabilityJob's run method has
            # a finally block that commits regardless.
            transaction.abort()
            raise

        if self.dry_run:
            transaction.abort()
            logger.info(
                f"{cve_sequence}: dry-run mode enabled, all changes are "
                f"reverted."
            )
        else:
            transaction.commit()
            logger.info(
                "[SOSSImporter] Successfully committed changes for "
                f"{cve_sequence}"
            )

        return bug, vulnerability, created

    def _create_bug(
        self, soss_record: SOSSRecord, lp_cve: CveModel
    ) -> BugModel:
        """
        Create a Bug model based on the information contained in a
        SOSSRecord.

        :param soss_record: SOSSRecord with information from a SOSS cve
        :param lp_cve: Launchpad Cve model
        """
        packagetype, package = self._get_first_package_info(soss_record)
        assignee = self._get_assignee(soss_record.assigned_to)

        externalpackage = self._get_or_create_external_package(
            package, packagetype
        )
        metadata = {"repositories": package.repositories}

        # Create the bug, only first bugtask
        bug, _ = removeSecurityProxy(
            self.bug_set.createBug(
                CreateBugParams(
                    comment=self._make_bug_description(soss_record),
                    title=lp_cve.sequence,
                    information_type=self.information_type,
                    owner=self.bug_importer,
                    target=externalpackage,
                    status=PACKAGE_STATUS_MAP[package.status],
                    status_explanation=package.note,
                    assignee=assignee,
                    validate_assignee=False,
                    importance=PRIORITY_ENUM_MAP[soss_record.priority],
                    cve=lp_cve,
                    metadata=metadata,
                    check_permissions=False,
                ),
                notify_event=False,
            )
        )

        if not bug:
            raise ValueError(f"Error creating bug for {lp_cve.sequence}")

        logger.info(f"[SOSSImporter] Created bug with ID: {bug.id}")
        return bug

    def _update_bug(
        self, bug: BugModel, soss_record: SOSSRecord, lp_cve: CveModel
    ) -> BugModel:
        """
        Update a Bug model with the information contained in a SOSSRecord.

        :param bug: Bug model to be updated
        :param soss_record: SOSSRecord with information from a SOSS cve
        :param lp_cve: Launchpad Cve model
        """
        bug.description = self._make_bug_description(soss_record)
        bug.title = lp_cve.sequence
        bug.transitionToInformationType(
            self.information_type, self.bug_importer
        )

        logger.info(f"[SOSSImporter] Updated Bug with ID: {bug.id}")
        return bug

    def _create_vulnerability(
        self,
        soss_record: SOSSRecord,
        lp_cve: CveModel,
        distribution: Distribution,
    ) -> Vulnerability:
        """
        Create a Vulnerability instance based on the information from
        the given SOSSRecord instance and link to the specified Bug
        and LP's Cve model.

        :param soss_record: SOSSRecord with information from a SOSS cve
        :param lp_cve: Launchpad Cve model
        :param distribution: a Distribution affected by the vulnerability
        :return: a Vulnerability
        """

        metadata = None
        if soss_record.extra_attrs:
            metadata = {"extra_attrs": soss_record.extra_attrs}

        vulnerability: Vulnerability = removeSecurityProxy(
            self.vulnerability_set.new(
                distribution=distribution,
                status=VulnerabilityStatus.NEEDS_TRIAGE,
                importance=PRIORITY_ENUM_MAP[soss_record.priority],
                creator=self.bug_importer,
                information_type=self.information_type,
                cve=lp_cve,
                description=soss_record.description,
                notes=self._format_notes(soss_record.notes),
                mitigation=None,
                importance_explanation=soss_record.priority_reason,
                date_made_public=self._normalize_date_with_timezone(
                    soss_record.public_date
                ),
                date_notice_issued=None,
                date_coordinated_release=None,
                cvss=self._prepare_cvss_data(soss_record),
                metadata=metadata,
            )
        )

        if not vulnerability:
            raise ValueError(
                f"[SOSSImporter] Error creating vulnerability for "
                f"{lp_cve.sequence}"
            )

        logger.info(
            "[SOSSImporter] Created vulnerability with ID: "
            f"{vulnerability.id} for {distribution.name}",
        )

        return vulnerability

    def _update_vulnerability(
        self, vulnerability: Vulnerability, soss_record: SOSSRecord
    ) -> Vulnerability:
        """
        Update a Vulnerability model with the information
        contained in a SOSSRecord

        :param vulnerability: Vulnerability model to be updated
        :param soss_record: SOSSRecord with information from a SOSS cve
        """
        vulnerability.status = VulnerabilityStatus.NEEDS_TRIAGE
        vulnerability.description = soss_record.description
        vulnerability.notes = self._format_notes(soss_record.notes)
        vulnerability.mitigation = None
        vulnerability.importance = PRIORITY_ENUM_MAP[soss_record.priority]
        vulnerability.importance_explanation = soss_record.priority_reason
        vulnerability.date_made_public = self._normalize_date_with_timezone(
            soss_record.public_date
        )
        vulnerability.date_notice_issued = None
        vulnerability.date_coordinated_release = None
        vulnerability.cvss = self._prepare_cvss_data(soss_record)

        vulnerability.metadata = None
        if soss_record.extra_attrs:
            vulnerability.metadata = {"extra_attrs": soss_record.extra_attrs}

        logger.info(
            "[SOSSImporter] Updated Vulnerability with ID: "
            f"{vulnerability.id} for {vulnerability.distribution.name}",
        )
        return vulnerability

    def _find_existing_bug(
        self,
        soss_record: SOSSRecord,
        lp_cve: CveModel,
        distribution: Distribution,
    ) -> Optional[BugModel]:
        """Find existing bug for the given CVE."""
        vulnerability = self._find_existing_vulnerability(lp_cve, distribution)
        if not vulnerability:
            return None

        bugs = vulnerability.bugs
        if len(bugs) > 1:
            raise ValueError(
                "Multiple existing bugs found for CVE ",
                soss_record.sequence,
            )
        if bugs:
            return removeSecurityProxy(bugs[0])

        return None

    def _find_existing_vulnerability(
        self, lp_cve: CveModel, distribution: Distribution
    ) -> Optional[Vulnerability]:
        """Find existing vulnerability for the current distribution"""
        vulnerability = lp_cve.getDistributionVulnerability(distribution)
        return removeSecurityProxy(vulnerability)

    def _create_or_update_bugtasks(
        self, bug: BugModel, soss_record: SOSSRecord
    ) -> None:
        """
        Add bug tasks to the given Bug model based on the information
        from a SOSSRecord.

        This may be called multiple times, only new targets will be created.
        Existing targets will be updated. Packages that were removed from the
        SOSSRecord will be removed. Doing things in the same loop to prevent
        looping more than once in large bugtasks fields.

        :param bug: Bug model to be updated
        :param packages: list of SOSSRecord.Packages from a SOSSRecord
        """
        packages: List[SOSSRecord.Package] = soss_record.packages.items()
        assignee = self._get_assignee(soss_record.assigned_to)

        # Build a lookup dict for existing bug tasks
        bugtask_by_target = {
            task.target: removeSecurityProxy(task) for task in bug.bugtasks
        }

        for packagetype, package_list in packages:
            for package in package_list:
                target = self._get_or_create_external_package(
                    package, packagetype
                )
                metadata = (
                    {"repositories": package.repositories}
                    if package.repositories
                    else None
                )

                if target not in bugtask_by_target:
                    bugtask = removeSecurityProxy(
                        self.bugtask_set.createTask(
                            bug,
                            self.bug_importer,
                            target,
                            status=PACKAGE_STATUS_MAP[package.status],
                            importance=PRIORITY_ENUM_MAP[soss_record.priority],
                            assignee=assignee,
                            metadata=metadata,
                        )
                    )
                else:
                    bugtask = bugtask_by_target[target]

                    # This should not appear again, we use this to remove the
                    # not used bugtasks
                    bugtask_by_target.pop(target)

                    bugtask.transitionToStatus(
                        PACKAGE_STATUS_MAP[package.status]
                    )
                    bugtask.transitionToImportance(
                        PRIORITY_ENUM_MAP[soss_record.priority]
                    )
                    # We always have rights to change assignees
                    bugtask.transitionToAssignee(assignee, validate=False)
                    bugtask.metadata = metadata

                bugtask.status_explanation = package.note

        # Remove bugtasks that were deleted from the record
        for bugtask in bugtask_by_target.values():
            bugtask.destroySelf()

    def _get_launchpad_cve(self, cve_sequence: str) -> Optional[CveModel]:
        """Get CVE from Launchpad."""
        lp_cve: CveModel = removeSecurityProxy(self.cve_set[cve_sequence])
        if lp_cve is None:
            logger.warning(
                "[SOSSImporter] %s: could not find the CVE in LP. Aborting. "
                "%s was not imported.",
                cve_sequence,
                cve_sequence,
            )
            raise NotFoundError(f"Could not find {cve_sequence} in LP")
        return lp_cve

    def _make_bug_description(self, soss_record: SOSSRecord) -> str:
        """
        Some SOSSRecord fields can't be mapped to Launchpad models.

        They are saved to bug description.

        :param soss_record: SOSSRecord with information from UCT
        :return: bug description
        """
        parts = [soss_record.description] if soss_record.description else []
        if soss_record.references:
            parts.extend(["", "References:"])
            parts.extend(soss_record.references)
        return "\n".join(parts) if parts else "-"

    def _get_assignee(self, assigned_to: Optional[str]) -> Person:
        """Get assignee person object if assigned_to is provided."""
        if not assigned_to:
            return None

        person = self.person_set.getByName(assigned_to)
        if not person:
            logger.warning(f"[SOSSImporter] Assignee not found: {assigned_to}")

        return person

    def _get_or_create_external_package(
        self,
        package: SOSSRecord.Package,
        packagetype: SOSSRecord.PackageTypeEnum,
    ) -> ExternalPackage:
        """Get or create external package for the given package."""
        source_package_name = self.source_package_name_set.getOrCreateByName(
            package.name
        )
        return self.soss.getExternalPackage(
            name=source_package_name,
            packagetype=PACKAGE_TYPE_MAP[packagetype],
            channel=package.channel.value,
        )

    def _prepare_cvss_data(self, soss_record: SOSSRecord) -> Dict:
        """Prepare CVSS data from SOSS record."""
        cvss_data = defaultdict(list)
        for cvss in soss_record.cvss:
            cvss_data[cvss.source].append(cvss.to_dict())
        return dict(cvss_data)

    def _normalize_date_with_timezone(self, date_obj) -> Optional:
        """Normalize date to UTC timezone if needed."""
        if date_obj and date_obj.tzinfo is None:
            return date_obj.replace(tzinfo=timezone.utc)
        return date_obj

    def _validate_soss_record(
        self, soss_record: SOSSRecord, cve_sequence: str
    ) -> bool:
        """Validate SOSS record before processing."""
        if soss_record.candidate and soss_record.candidate != cve_sequence:
            logger.warning(
                "[SOSSImporter] CVE sequence mismatch: %s != %s",
                soss_record.candidate,
                cve_sequence,
            )
            raise ValueError(
                f"CVE sequence mismatch: {soss_record.candidate} != "
                f"{cve_sequence}"
            )

        if not soss_record.packages:
            logger.warning(
                "[SOSSImporter] %s: could not find any affected packages, "
                "aborting. %s was not imported.",
                cve_sequence,
                cve_sequence,
            )
            raise ValueError(f"{cve_sequence}: has no affected packages")

        return True

    def _get_first_package_info(
        self, soss_record: SOSSRecord
    ) -> Tuple[SOSSRecord.PackageTypeEnum, SOSSRecord.Package]:
        """Get first package type and package from SOSS record."""
        first_item = next(iter(soss_record.packages.items()))
        packagetype = first_item[0]
        package = first_item[1][0]
        return packagetype, package

    def checkUserPermissions(self, user):
        """See `SVTImporter`."""
        return SecurityAdminDistribution(self.soss).checkAuthenticated(
            IPersonRoles(user)
        )

    def _format_notes(self, notes: List[Union[Dict, str]]) -> str:
        """Return a string from a list of notes. Notes can contain dicts or
        strings.
        """
        formatted_notes = []
        for note in notes:
            if isinstance(note, dict):
                for key, value in note.items():
                    value = value.replace("\n", " ")
                    formatted_notes.append(f"{key}: {value}")
            else:
                formatted_notes.append(str(note))
        return "\n\n".join(formatted_notes)
