#  Copyright 2026 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.errors import NotFoundError
from lp.bugs.interfaces.bug import IBugSet
from lp.bugs.interfaces.bugtask import BugTaskStatus
from lp.bugs.model.bug import Bug as BugModel
from lp.bugs.model.vulnerability import Vulnerability
from lp.bugs.scripts.cppa.models import CPPARecord
from lp.bugs.scripts.svthandler import SVTExporter
from lp.bugs.scripts.uct.subprojects import (
    SubProjectPPAs,
    create_archive_to_subproject_map,
)
from lp.registry.interfaces.distribution import IDistributionSet
from lp.registry.interfaces.person import IPerson
from lp.registry.interfaces.role import IPersonRoles
from lp.registry.model.archivesourcepackageseries import (
    ArchiveSourcePackageSeries,
)
from lp.registry.security import SecurityAdminDistribution

__all__ = [
    "CPPAExporter",
    "BUG_TASK_STATUS_MAP_REVERSE",
]

logger = logging.getLogger(__name__)

# Reverse of cppaimport.BUG_TASK_STATUS_MAP: BugTaskStatus → PackageStatus.
BUG_TASK_STATUS_MAP_REVERSE = {
    BugTaskStatus.WONTFIX: CPPARecord.PackageStatus.IGNORED,
    BugTaskStatus.UNKNOWN: CPPARecord.PackageStatus.NEEDS_TRIAGE,
    BugTaskStatus.DOESNOTEXIST: CPPARecord.PackageStatus.DOES_NOT_EXIST,
    BugTaskStatus.FIXRELEASED: CPPARecord.PackageStatus.RELEASED,
    BugTaskStatus.INVALID: CPPARecord.PackageStatus.NOT_AFFECTED,
    BugTaskStatus.DEFERRED: CPPARecord.PackageStatus.DEFERRED,
    BugTaskStatus.NEW: CPPARecord.PackageStatus.NEEDED,
    BugTaskStatus.FIXCOMMITTED: CPPARecord.PackageStatus.PENDING,
    BugTaskStatus.INPROGRESS: CPPARecord.PackageStatus.IN_PROGRESS,
}


class CPPAExporter(SVTExporter):
    """Exports the PPA bugtasks of a bug as a :class:`CPPARecord`.

    :param subprojects: Mapping from subproject key (e.g.
        ``"ppa-alias/focal"``) to the corresponding PPA.
    """

    def __init__(self, subprojects: Dict[str, SubProjectPPAs]) -> None:
        # Reverse map: (archive_name, series_name) to subproject_key
        self._archive_to_subproject = create_archive_to_subproject_map(
            subprojects
        )

    def to_record(
        self,
        bug: BugModel,
        vulnerability: Vulnerability,
    ) -> CPPARecord:
        """Export PPA bugtasks from *bug* as a :class:`CPPARecord`.

        :param bug: The :class:`Bug` model to export.
        :param vulnerability: The :class:`Vulnerability` linked to *bug*.
            Must have an associated CVE (used to derive the CVE sequence).
        :returns: A :class:`CPPARecord` ready for serialisation.
        :raises ValueError: When *bug* or *vulnerability* is ``None``, or
            the vulnerability has no linked CVE.
        """
        if bug is None:
            raise ValueError("Bug can't be None")
        if vulnerability is None:
            raise ValueError("Vulnerability can't be None")
        if vulnerability.cve is None:
            raise ValueError(
                f"Vulnerability {vulnerability.id} has no linked CVE"
            )

        cve_sequence = f"CVE-{vulnerability.cve.sequence}"

        # packages_by_name: package_name to list[SeriesPackageStatus]
        packages_by_name: Dict[str, list] = defaultdict(list)

        for bug_task in bug.bugtasks:
            target = removeSecurityProxy(bug_task.target)
            if not isinstance(target, ArchiveSourcePackageSeries):
                continue

            archive_name = target.archive.name
            archive_owner = target.archive.owner.name
            series_name = target.distroseries.name

            subproject_key = self._archive_to_subproject.get(
                (archive_owner, archive_name, series_name)
            )
            if subproject_key is None:
                continue

            lp_status = bug_task.status
            package_status = BUG_TASK_STATUS_MAP_REVERSE.get(lp_status)
            if package_status is None:
                logger.warning(
                    "[CPPAExporter] %s: unmapped BugTaskStatus %r for "
                    "archive=%r series=%r, defaulting to needs-triage.",
                    cve_sequence,
                    lp_status,
                    archive_name,
                    series_name,
                )
                package_status = CPPARecord.PackageStatus.NEEDS_TRIAGE

            packages_by_name[target.sourcepackagename.name].append(
                CPPARecord.SeriesPackageStatus(
                    series=subproject_key,
                    status=package_status,
                    reason=bug_task.status_explanation or "",
                )
            )

        packages = [
            CPPARecord.Package(name=pkg_name, statuses=statuses)
            for pkg_name, statuses in packages_by_name.items()
        ]
        return CPPARecord(candidate=cve_sequence, packages=packages)

    def export_bug_to_cppa_file(
        self, bug_id: int, output_dir: Path
    ) -> List[Path]:
        """Export a bug's CPPA bugtasks as per-project CVE files.

        One file is written per distinct project (the part of a subproject
        key before the ``/``). For example, subproject keys
        ``"project1/noble"`` and ``"project1/jammy"`` both contribute to
        ``<output_dir>/project1/CVE-XXXX-YYYY``.

        :param bug_id: ID of the :class:`Bug` model to export.
        :param output_dir: Root directory; project subdirectories are created
            automatically.
        :returns: List of paths written, empty when the bug cannot be found,
            has no vulnerability with a linked CVE, or has no CPPA bugtasks.
        """
        try:
            bug = getUtility(IBugSet).get(bug_id)
        except NotFoundError:
            bug = None
        if not bug:
            logger.error(
                "[CPPAExporter] Could not find bug with ID: %s", bug_id
            )
            return []

        vulnerabilities = list(bug.vulnerabilities)
        if not vulnerabilities:
            logger.error(
                "[CPPAExporter] Bug %s has no vulnerabilities", bug_id
            )
            return []

        vulnerability = vulnerabilities[0]
        if not vulnerability.cve:
            logger.error(
                "[CPPAExporter] Bug %s - vulnerability %s has no linked CVE",
                bug_id,
                vulnerability.id,
            )
            return []

        record = self.to_record(
            removeSecurityProxy(bug), removeSecurityProxy(vulnerability)
        )
        if record is None:
            return []

        saved_paths = self.write_record_to_dir(record, output_dir)
        for path in saved_paths:
            logger.info("[CPPAExporter] Bug %s exported to %s", bug_id, path)
        return saved_paths

    def write_record_to_dir(
        self, record: CPPARecord, output_dir: Path
    ) -> List[Path]:
        """Split *record* by project and write one file per project subdir.

        :param record: :class:`CPPARecord` to serialise.
        :param output_dir: Root directory; project subdirectories are created
            automatically.
        :returns: List of paths written.
        """
        projects = defaultdict(lambda: defaultdict(list))
        for pkg in record.packages:
            for status in pkg.statuses:
                project_name = status.series.split("/")[0]
                projects[project_name][pkg.name].append(status)

        saved_paths = []
        for project_name, packages_map in projects.items():
            project_record = CPPARecord(
                candidate=record.candidate,
                packages=[
                    CPPARecord.Package(name=name, statuses=statuses)
                    for name, statuses in packages_map.items()
                ],
            )
            project_dir = output_dir / project_name
            project_dir.mkdir(parents=True, exist_ok=True)
            saved_paths.append(project_record.save(project_dir))

        return saved_paths

    def checkUserPermissions(self, user: IPerson) -> bool:
        """Check that *user* may use this exporter.

        Delegates to the same security check used by the UCT exporter.
        """
        ubuntu = getUtility(IDistributionSet).getByName("ubuntu")
        return SecurityAdminDistribution(ubuntu).checkAuthenticated(
            IPersonRoles(user)
        )
