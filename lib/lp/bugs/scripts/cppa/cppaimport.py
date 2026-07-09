#  Copyright 2026 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

"""A CPPA bug importer.

This importer adds/updates PPA bugtasks on bugs that were already imported
by the UCT importer. It does not create bugs or vulnerabilities.

For each CVE in a CPPA file the importer will:

1. Verify the CVE has an existing Ubuntu vulnerability and a linked bug
   (imported by UCT).  If not, raise CPPAImportError.
2. For every package/series entry in the CPPA file, look up the customer PPA
   using the pre-loaded subprojects config.
3. Resolve both the ArchiveSourcePackage (distro-level) and the
   ArchiveSourcePackageSeries (series-level) bug task targets.
4. Create or update those bug tasks on the existing bug.
"""
import itertools
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import transaction
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.bugs.interfaces.bugactivity import IBugActivitySet
from lp.bugs.interfaces.bugtask import (
    BugTaskImportance,
    BugTaskStatus,
    IBugTaskSet,
)
from lp.bugs.interfaces.cve import ICveSet
from lp.bugs.model.bug import Bug as BugModel
from lp.bugs.model.cve import Cve as CveModel
from lp.bugs.model.vulnerability import Vulnerability
from lp.bugs.scripts.cppa.models import CPPARecord
from lp.bugs.scripts.svthandler import SVTImporter
from lp.bugs.scripts.uct.models import CVE
from lp.bugs.scripts.uct.subprojects import SubProjectPPAs
from lp.registry.interfaces.person import IPerson
from lp.registry.interfaces.role import IPersonRoles
from lp.registry.model.archivesourcepackageseries import (
    ArchiveSourcePackageSeries,
)
from lp.registry.model.distribution import Distribution
from lp.registry.model.sourcepackagename import SourcePackageName
from lp.registry.security import SecurityAdminDistribution
from lp.services.database.constants import UTC_NOW
from lp.services.database.interfaces import IStore

__all__ = [
    "CPPAImporter",
    "CPPAImportError",
    "BUG_TASK_STATUS_MAP",
]

logger = logging.getLogger(__name__)

# Maps CPPA PackageStatus values to Launchpad BugTaskStatus values.
BUG_TASK_STATUS_MAP = {
    CPPARecord.PackageStatus.IGNORED: BugTaskStatus.WONTFIX,
    CPPARecord.PackageStatus.NEEDS_TRIAGE: BugTaskStatus.UNKNOWN,
    CPPARecord.PackageStatus.DOES_NOT_EXIST: BugTaskStatus.DOESNOTEXIST,
    CPPARecord.PackageStatus.RELEASED: BugTaskStatus.FIXRELEASED,
    CPPARecord.PackageStatus.NOT_AFFECTED: BugTaskStatus.INVALID,
    CPPARecord.PackageStatus.DEFERRED: BugTaskStatus.DEFERRED,
    CPPARecord.PackageStatus.NEEDED: BugTaskStatus.NEW,
    CPPARecord.PackageStatus.PENDING: BugTaskStatus.FIXCOMMITTED,
    CPPARecord.PackageStatus.IN_PROGRESS: BugTaskStatus.INPROGRESS,
}


class CPPAImportError(Exception):
    """Raised when a CPPA CVE cannot be imported."""


class CPPAImporter(SVTImporter):
    """Adds/updates customer-PPA bugtasks on bugs already imported by UCT.

    Implements the :class:`SVTImporter` interface so it can be used inside
    :class:`ImportVulnerabilityJob` alongside the UCT and SOSS importers.
    CPPA never creates bugs, so the *created* flag in the returned 3-tuple
    is always ``False``.

    :param ubuntu: The Ubuntu distribution object.
    :param subprojects: Mapping from subproject key (e.g. ``"esm-apps/focal"``)
        to the corresponding PPA.
    :param information_type: Privacy level applied to newly created bugtasks.
    :param dry_run: When ``True`` all database changes are rolled back after
        each CVE is processed.
    :param cache_entities: Optional pre-populated cache dict shared across
        importer instances to avoid redundant DB look-ups within a batch run.
    """

    def __init__(
        self,
        ubuntu: Distribution,
        subprojects: Dict[str, SubProjectPPAs],
        information_type: InformationType = InformationType.PUBLICSECURITY,
        dry_run: bool = False,
        cache_entities: Optional[Dict[str, Dict[Any, Any]]] = None,
    ) -> None:
        self.dry_run = dry_run
        self.bug_importer = getUtility(ILaunchpadCelebrities).bug_importer
        self.ubuntu = ubuntu
        self.information_type = information_type
        self.cache_entities = cache_entities or CVE.new_cache()
        self.subprojects = subprojects

    def import_cve_from_file(
        self, cve_path: Path
    ) -> Tuple[BugModel, Vulnerability, bool]:
        """Load a CPPA CVE file and import it.

        :param cve_path: Path to the CPPA CVE file.  The filename must start
            with ``CVE-``.
        :returns: ``(bug, vulnerability, False)``, CPPA never creates bugs.
        """
        logger.info("[CPPAImporter] Importing %s", cve_path)
        record = CPPARecord.load(cve_path)
        return self.import_cve(record)

    def from_record(
        self, record: CPPARecord, cve_sequence: str
    ) -> Tuple[BugModel, Vulnerability, bool]:
        """Import a :class:`CPPARecord`.

        :param record: Parsed CPPA CVE record.
        :param cve_sequence: Expected CVE identifier (e.g. "CVE-2013-1953").
            Import is aborted when this does not match `record.candidate`.
        :returns: ``(bug, vulnerability, False)``, CPPA never creates bugs.
        :raises CPPAImportError: When the CVE sequence does not match.
        """
        if record.candidate != cve_sequence:
            raise CPPAImportError(
                "[CPPAImporter] CVE sequence mismatch: "
                f"{record.candidate!r} != {cve_sequence!r}"
            )
        return self.import_cve(record)

    def import_cve(
        self, record: CPPARecord
    ) -> Tuple[BugModel, Vulnerability, bool]:
        """Import a `CPPARecord` into Launchpad.

        Looks up the existing Ubuntu bug created by the UCT importer and
        creates or updates PPA bugtasks on it.

        :param record: Parsed CPPA CVE record.
        :returns: ``(bug, vulnerability, False)``, CPPA never creates bugs.
        :raises CPPAImportError: When the CVE is not found in Launchpad, or no
            Ubuntu vulnerability / bug exists for it yet.
        """
        lp_cve = self._get_launchpad_cve(record.candidate)
        bug, vulnerability = self._find_existing_bug(lp_cve)

        try:
            self.update_bug(bug, record)

            getUtility(IBugActivitySet).new(
                bug=bug.id,
                datechanged=UTC_NOW,
                person=self.bug_importer,
                whatchanged="bug",
                message=f"CPPA CVE entry {record.candidate}",
            )
        except Exception:
            transaction.abort()
            raise

        if self.dry_run:
            transaction.abort()
            logger.info(
                "[CPPAImporter] %s: dry-run mode, changes rolled back.",
                record.candidate,
            )
        else:
            transaction.commit()
            logger.info(
                "[CPPAImporter] %s: successfully committed changes.",
                record.candidate,
            )

        # created=False: CPPA only updates existing UCT-imported bugs.
        return bug, vulnerability, False

    def update_bug(self, bug: BugModel, record: CPPARecord) -> BugModel:
        """Update *bug* with the PPA bugtasks described in *record*.

        Creates any missing PPA bugtasks and updates their statuses and
        importances.  Mirrors :meth:`UCTImporter.update_bug`.

        :param bug: The existing Launchpad bug to update.
        :param record: Parsed CPPA CVE record.
        :returns: The updated bug.
        """
        package_importances = self._importance_by_package_name(bug)
        ppa_packages, ppa_series_packages = self._resolve_ppa_packages(record)
        self._create_bug_tasks(bug, ppa_packages, ppa_series_packages)
        self._update_statuses_and_importances(
            bug, package_importances, ppa_packages, ppa_series_packages
        )

        # TODO: ilkeremrekoc 2026-07-03: CPPAs should not change the whole
        # bug's information type, but as it stands, we do not have a way to
        # control access per bugtask.  For now, we just make sure the bug is
        # at least private. This should be removed when per-bugtask access
        # control is implemented.
        if bug.information_type != InformationType.PRIVATESECURITY:
            bug.transitionToInformationType(
                InformationType.PRIVATESECURITY, self.bug_importer
            )
        return bug

    def _get_launchpad_cve(self, cve_sequence: str) -> CveModel:
        """Return the Launchpad Cve record or raise CPPAImportError."""
        lp_cve: CveModel = removeSecurityProxy(
            getUtility(ICveSet)[cve_sequence]
        )
        if lp_cve is None:
            raise CPPAImportError(
                f"[CPPAImporter] {cve_sequence}: CVE not found in Launchpad. "
                "It must be imported by UCT first."
            )
        return lp_cve

    def _find_existing_bug(
        self, lp_cve: CveModel
    ) -> Tuple[BugModel, Vulnerability]:
        """Return ``(bug, vulnerability)`` for lp_cve or raise CPPAImportError.

        The UCT importer creates exactly one bug per CVE linked via an Ubuntu
        vulnerability.  If that bug is missing we cannot proceed.
        """
        vulnerability: Optional[Vulnerability] = removeSecurityProxy(
            lp_cve.getDistributionVulnerability(self.ubuntu)
        )
        if vulnerability is None:
            raise CPPAImportError(
                f"[CPPAImporter] {lp_cve.sequence}: no Ubuntu vulnerability "
                "found. The CVE must be imported by UCT first."
            )

        bugs = vulnerability.bugs
        if not bugs:
            raise CPPAImportError(
                f"[CPPAImporter] {lp_cve.sequence}: Ubuntu vulnerability "
                f"{vulnerability.id} has no linked bugs. "
                "The CVE must be imported by UCT first."
            )
        if len(bugs) > 1:
            raise CPPAImportError(
                f"[CPPAImporter] {lp_cve.sequence}: multiple bugs found for "
                f"Ubuntu vulnerability {vulnerability.id}."
            )

        return removeSecurityProxy(bugs[0]), vulnerability

    def _resolve_ppa_packages(
        self, record: CPPARecord
    ) -> Tuple[List[CVE.PPAPackage], List[CVE.PPASeriesPackage]]:
        """Resolve *record* into distro-level and series-level target lists.

        Unknown subproject keys and unresolvable PPAs/series are logged and
        skipped so a single bad entry does not block the rest.

        Returns a 2-tuple of:
        * ``ppa_packages``: one :class:`ArchiveSourcePackage` per
          (package, archive) pair.
        * ``ppa_series_packages``: one :class:`ArchiveSourcePackageSeries`
          per (package, archive, series) entry in *record*.
        """
        ppa_packages: List[CVE.PPAPackage] = []
        ppa_series_packages: List[CVE.PPASeriesPackage] = []
        ppa_package_keys_seen = set()

        all_names = {p.name for p in record.packages}
        spn_by_name = {
            spn.name: spn
            for spn in IStore(SourcePackageName).find(
                SourcePackageName,
                SourcePackageName.name.is_in(all_names),
            )
        }

        for package in record.packages:
            source_package_name = spn_by_name.get(package.name)
            if source_package_name is None:
                logger.warning(
                    "[CPPAImporter] Source package %r not found in Launchpad. "
                    "Skipping.",
                    package.name,
                )
                continue

            for status_entry in package.statuses:
                subproject_key = status_entry.series
                subproject_ppas = self.subprojects.get(subproject_key)
                if subproject_ppas is None:
                    logger.warning(
                        "[CPPAImporter] Unknown subproject key %r for package "
                        "%r. Skipping.",
                        subproject_key,
                        package.name,
                    )
                    continue

                ppa_ref = subproject_ppas.ppa
                ubuntu_series_name = subproject_ppas.ubuntu_series

                archive = CVE._get_ppa_archive(
                    ppa_ref, self.ubuntu, self.cache_entities
                )
                if archive is None:
                    logger.warning(
                        "[CPPAImporter] Could not resolve PPA %s/%s for "
                        "subproject %r. Skipping.",
                        ppa_ref.owner,
                        ppa_ref.archive,
                        subproject_key,
                    )
                    continue

                distro_series = CVE.get_distro_series(
                    ubuntu_series_name, self.cache_entities
                )
                if distro_series is None:
                    logger.warning(
                        "[CPPAImporter] Could not resolve distro series %r "
                        "for subproject %r. Skipping.",
                        ubuntu_series_name,
                        subproject_key,
                    )
                    continue

                ppa_package_key = (source_package_name, archive)
                if ppa_package_key not in ppa_package_keys_seen:
                    ppa_package_keys_seen.add(ppa_package_key)
                    ppa_packages.append(
                        CVE.PPAPackage(
                            target=archive.getArchiveSourcePackage(
                                source_package_name, check_publication=False
                            ),
                            package_name=source_package_name,
                            importance=None,
                            tags=set(),
                        )
                    )

                ppa_series_packages.append(
                    CVE.PPASeriesPackage(
                        target=ArchiveSourcePackageSeries(
                            archive=archive,
                            distroseries=distro_series,
                            sourcepackagename=source_package_name,
                        ),
                        package_name=source_package_name,
                        importance=None,
                        status=BUG_TASK_STATUS_MAP[status_entry.status],
                        status_explanation=status_entry.reason,
                    )
                )

        return ppa_packages, ppa_series_packages

    def _create_bug_tasks(
        self,
        bug: BugModel,
        ppa_packages: List[CVE.PPAPackage],
        ppa_series_packages: List[CVE.PPASeriesPackage],
    ) -> None:
        """Create bugtasks for any targets not already on *bug*.

        Raises `CPPAImportError` if the same target appears more than
        once in the import batch.
        """
        existing_targets = {t.target for t in bug.bugtasks}
        seen_targets = set(existing_targets)
        targets_to_create = []
        duplicate_targets = []

        for package in itertools.chain(ppa_packages, ppa_series_packages):
            target = package.target
            if target not in seen_targets:
                seen_targets.add(target)
                targets_to_create.append(target)
            elif target not in existing_targets:
                duplicate_targets.append(target)

        if duplicate_targets:
            target_names = ", ".join(
                t.bugtargetdisplayname for t in duplicate_targets
            )
            logger.error(
                "[CPPAImporter] Duplicate targets in package lists: %s",
                target_names,
            )
            raise CPPAImportError(
                f"Duplicate targets in package lists: {target_names}"
            )

        if targets_to_create:
            getUtility(IBugTaskSet).createManyTasks(
                bug,
                self.bug_importer,
                targets_to_create,
                check_ppa_source_package=False,
            )

    def _update_statuses_and_importances(
        self,
        bug: BugModel,
        package_importances: Dict[str, BugTaskImportance],
        ppa_packages: List[CVE.PPAPackage],
        ppa_series_packages: List[CVE.PPASeriesPackage],
    ) -> None:
        """Update importances and statuses of bugtasks from the resolved lists.

        Distro-level tasks (:class:`ArchiveSourcePackage`) receive importance
        only.  Series-level tasks (:class:`ArchiveSourcePackageSeries`) receive
        importance, status, and status_explanation.

        Importance is looked up per package name from *package_importances*
        (built by :meth:`_importance_by_package_name`), which already encodes
        the fallback as the defaultdict default.
        """
        bug_task_by_target = {t.target: t for t in bug.bugtasks}

        for pp in ppa_packages:
            importance = package_importances[pp.package_name.name]
            task = bug_task_by_target[pp.target]
            task.transitionToImportance(importance, self.bug_importer)

        for psp in ppa_series_packages:
            importance = package_importances[psp.package_name.name]
            task = bug_task_by_target[psp.target]
            task.transitionToImportance(importance, self.bug_importer)
            task.transitionToStatus(psp.status, self.bug_importer)
            task.status_explanation = psp.status_explanation

    def _importance_by_package_name(
        self, bug: BugModel
    ) -> Dict[str, BugTaskImportance]:
        """Return a per-package importance mapping.

        Reads importances from the existing Ubuntu distro-level bugtasks
        (targets of type :class:`DistributionSourcePackage`).  The returned
        defaultdict falls back to the importance of the bare Ubuntu
        distribution task (if one exists), and finally to ``UNDECIDED``, for
        any package name not explicitly present.

        :returns: A defaultdict mapping source package name to importance.
        """
        fallback = BugTaskImportance.UNDECIDED
        per_package: Dict[str, BugTaskImportance] = {}

        for task in bug.bugtasks:
            task = removeSecurityProxy(task)
            if (
                task.distribution_id is not None
                and task.sourcepackagename_id is not None
                and task.distroseries_id is None
                and task.archive_id is None
            ):
                # DistributionSourcePackage task — the normal UCT case.
                per_package[task.sourcepackagename.name] = task.importance
            elif (
                task.distribution_id is not None
                and task.sourcepackagename_id is None
                and task.distroseries_id is None
                and task.archive_id is None
            ):
                # Bare Distribution (Ubuntu) task — fallback when UCT created
                # the bug without Ubuntu source packages.  No DSP tasks will
                # follow, so stop iterating.
                fallback = task.importance
                break

        result = defaultdict(lambda: fallback)
        result.update(per_package)
        return result

    def checkUserPermissions(self, user: IPerson) -> bool:
        """Check that *user* may use this importer.

        Delegates to the security check used by the UCT importer.
        """
        return SecurityAdminDistribution(self.ubuntu).checkAuthenticated(
            IPersonRoles(user)
        )
