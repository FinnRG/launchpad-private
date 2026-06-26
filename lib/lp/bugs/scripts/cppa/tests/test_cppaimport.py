#  Copyright 2026 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

"""Unit tests for CPPAImporter."""

import transaction
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.bugs.enums import VulnerabilityStatus
from lp.bugs.interfaces.bugtask import BugTaskImportance, BugTaskStatus
from lp.bugs.scripts.cppa.cppaimport import (
    BUG_TASK_STATUS_MAP,
    CPPAImporter,
    CPPAImportError,
)
from lp.bugs.scripts.cppa.models import CPPARecord
from lp.bugs.scripts.uct.subprojects import PPAReference, SubProjectPPAs
from lp.registry.model.archivesourcepackage import ArchiveSourcePackage
from lp.registry.model.archivesourcepackageseries import (
    ArchiveSourcePackageSeries,
)
from lp.testing import TestCaseWithFactory
from lp.testing.layers import LaunchpadZopelessLayer


class TestCPPAImporterSetup(TestCaseWithFactory):
    """Helper base class that builds the common test fixture."""

    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.bug_importer = getUtility(ILaunchpadCelebrities).bug_importer

        # Distro series for the PPA
        self.focal = self.factory.makeDistroSeries(
            distribution=self.ubuntu, name="focal"
        )

        # PPA owner and archive
        self.ppa_owner = self.factory.makePerson(name="my-ppa-owner")
        self.ppa = self.factory.makeArchive(
            distribution=self.ubuntu,
            owner=self.ppa_owner,
            name="my-ppa",
        )

        # Pre-loaded subprojects config mapping "my-project/focal" → the PPA
        self.subprojects = {
            "my-project/focal": SubProjectPPAs(
                ubuntu_series="focal",
                ppa=PPAReference(
                    owner="my-ppa-owner",
                    archive="my-ppa",
                    pocket="release",
                ),
            )
        }

        # LP CVE entry
        self.lp_cve = self.factory.makeCVE(sequence="2013-1953")

    def _make_ubuntu_bug(self, importance=BugTaskImportance.MEDIUM):
        """Create a minimal Ubuntu bug+vulnerability that UCT would produce."""
        vulnerability = self.factory.makeVulnerability(
            distribution=self.ubuntu,
            status=VulnerabilityStatus.ACTIVE,
            importance=importance,
            creator=self.bug_importer,
            information_type=InformationType.PUBLICSECURITY,
            cve=self.lp_cve,
        )
        vulnerability = removeSecurityProxy(vulnerability)

        pkg_name = self.factory.makeSourcePackageName(name="sam2p")
        dsp = self.ubuntu.getSourcePackage(pkg_name)
        bug = removeSecurityProxy(
            self.factory.makeBug(
                target=dsp,
                owner=self.bug_importer,
                information_type=InformationType.PUBLICSECURITY,
            )
        )
        vulnerability.linkBug(bug, self.bug_importer)
        transaction.commit()
        return bug

    def _make_record(self, status=CPPARecord.PackageStatus.NEEDED):
        """Return a minimal CPPARecord for CVE-2013-1953."""
        return CPPARecord(
            candidate="CVE-2013-1953",
            packages=[
                CPPARecord.Package(
                    name="sam2p",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="my-project/focal",
                            status=status,
                            reason="",
                        )
                    ],
                )
            ],
        )

    def _make_importer(self, dry_run=False):
        return CPPAImporter(
            ubuntu=self.ubuntu,
            subprojects=self.subprojects,
            dry_run=dry_run,
        )


class TestCPPAImporterHappyPath(TestCPPAImporterSetup):
    """Tests for the core create/update logic."""

    def test_creates_ppa_series_bugtask(self):
        """Importing a CPPA record creates an ArchiveSourcePackageSeries
        bugtask on the existing Ubuntu bug."""
        bug = self._make_bug_with_no_ppa_tasks()
        importer = self._make_importer()
        importer.from_record(self._make_record(), "CVE-2013-1953")

        targets = {task.target for task in bug.bugtasks}
        ppa_series_targets = [
            t for t in targets if isinstance(t, ArchiveSourcePackageSeries)
        ]
        self.assertEqual(len(ppa_series_targets), 1)
        t = ppa_series_targets[0]
        self.assertEqual(t.archive, self.ppa)
        self.assertEqual(t.distroseries.name, "focal")
        self.assertEqual(t.sourcepackagename.name, "sam2p")

    def test_creates_ppa_package_bugtask(self):
        """Importing a CPPA record also creates an ArchiveSourcePackage
        (distro-level) bugtask on the existing Ubuntu bug."""
        bug = self._make_bug_with_no_ppa_tasks()
        importer = self._make_importer()
        importer.from_record(self._make_record(), "CVE-2013-1953")

        targets = {task.target for task in bug.bugtasks}
        asp_targets = [
            t for t in targets if isinstance(t, ArchiveSourcePackage)
        ]
        self.assertEqual(len(asp_targets), 1)
        t = asp_targets[0]
        self.assertEqual(t.archive, self.ppa)
        self.assertEqual(t.sourcepackagename.name, "sam2p")

    def test_bugtask_status_set_correctly(self):
        """The new series bugtask has the status from the CPPA record."""
        bug = self._make_bug_with_no_ppa_tasks()
        importer = self._make_importer()
        record = self._make_record(status=CPPARecord.PackageStatus.RELEASED)
        importer.from_record(record, "CVE-2013-1953")

        series_task = self._get_series_task(bug)
        self.assertEqual(series_task.status, BugTaskStatus.FIXRELEASED)

    def test_bugtask_status_explanation_set(self):
        """The status_explanation is taken from the reason field."""
        bug = self._make_bug_with_no_ppa_tasks()
        importer = self._make_importer()
        record = CPPARecord(
            candidate="CVE-2013-1953",
            packages=[
                CPPARecord.Package(
                    name="sam2p",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="my-project/focal",
                            status=CPPARecord.PackageStatus.RELEASED,
                            reason="1.2.3-4",
                        )
                    ],
                )
            ],
        )
        importer.from_record(record, "CVE-2013-1953")

        series_task = self._get_series_task(bug)
        self.assertEqual(series_task.status_explanation, "1.2.3-4")

    def test_bugtask_importance_inherited_from_bug(self):
        """New PPA bugtasks inherit importance from the package's DSP task."""
        bug = self._make_bug_with_no_ppa_tasks()
        # Before the first import the only task is the DSP task for sam2p.
        dsp_task = removeSecurityProxy(bug.bugtasks[0])
        dsp_task.transitionToImportance(
            BugTaskImportance.HIGH, self.bug_importer
        )

        importer = self._make_importer()
        importer.from_record(self._make_record(), "CVE-2013-1953")

        series_task = self._get_series_task(bug)
        self.assertEqual(series_task.importance, BugTaskImportance.HIGH)

    def test_updates_existing_bugtask_status(self):
        """When a bugtask already targets the PPA series, its status is
        updated rather than a duplicate being created."""
        bug = self._make_bug_with_no_ppa_tasks()

        # First import: NEEDED
        importer = self._make_importer()
        importer.from_record(
            self._make_record(CPPARecord.PackageStatus.NEEDED), "CVE-2013-1953"
        )
        # Second import: RELEASED
        importer.from_record(
            self._make_record(CPPARecord.PackageStatus.RELEASED),
            "CVE-2013-1953",
        )

        # There should still be exactly one series task, now with FIXRELEASED.
        series_tasks = [
            t
            for t in bug.bugtasks
            if isinstance(t.target, ArchiveSourcePackageSeries)
        ]
        self.assertEqual(len(series_tasks), 1)
        self.assertEqual(series_tasks[0].status, BugTaskStatus.FIXRELEASED)

    def test_updates_existing_bugtask_importance(self):
        """On update the importance is also refreshed from the bug."""
        bug = self._make_bug_with_no_ppa_tasks()
        importer = self._make_importer()
        importer.from_record(self._make_record(), "CVE-2013-1953")

        # Find the Ubuntu DSP (DistributionSourcePackage) task specifically.
        # After the first import, bugtasks also includes ASP and ASPS tasks
        # which sort before the DSP in bugtask_sort_key, so we must not rely
        # on bugtasks[0].
        dsp_task = removeSecurityProxy(
            next(
                t
                for t in bug.bugtasks
                if not isinstance(
                    t.target,
                    (ArchiveSourcePackage, ArchiveSourcePackageSeries),
                )
            )
        )
        dsp_task.transitionToImportance(
            BugTaskImportance.CRITICAL, self.bug_importer
        )

        # Re-import: importance should be propagated from the DSP task.
        importer.from_record(self._make_record(), "CVE-2013-1953")
        series_task = self._get_series_task(bug)
        self.assertEqual(series_task.importance, BugTaskImportance.CRITICAL)

    def test_no_duplicate_ppa_package_task_for_multiple_series(self):
        """When a package appears in two series of the same PPA, only one
        distro-level ArchiveSourcePackage bugtask is created."""
        # Add a second series "jammy" to the subprojects config.
        self.factory.makeDistroSeries(distribution=self.ubuntu, name="jammy")
        subprojects = dict(self.subprojects)
        subprojects["my-project/jammy"] = SubProjectPPAs(
            ubuntu_series="jammy",
            ppa=PPAReference(
                owner="my-ppa-owner",
                archive="my-ppa",
                pocket="release",
            ),
        )
        bug = self._make_bug_with_no_ppa_tasks()
        record = CPPARecord(
            candidate="CVE-2013-1953",
            packages=[
                CPPARecord.Package(
                    name="sam2p",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="my-project/focal",
                            status=CPPARecord.PackageStatus.NEEDED,
                            reason="",
                        ),
                        CPPARecord.SeriesPackageStatus(
                            series="my-project/jammy",
                            status=CPPARecord.PackageStatus.RELEASED,
                            reason="1.2.3",
                        ),
                    ],
                )
            ],
        )
        importer = CPPAImporter(
            ubuntu=self.ubuntu,
            subprojects=subprojects,
        )
        importer.from_record(record, "CVE-2013-1953")

        asp_tasks = [
            t
            for t in bug.bugtasks
            if isinstance(t.target, ArchiveSourcePackage)
        ]
        self.assertEqual(len(asp_tasks), 1, "Expected a single ASP task")

        asps_tasks = [
            t
            for t in bug.bugtasks
            if isinstance(t.target, ArchiveSourcePackageSeries)
        ]
        self.assertEqual(len(asps_tasks), 2, "Expected two ASPS tasks")

    def test_per_package_importances_are_respected(self):
        """Each PPA bugtask inherits the importance of its own DSP task.

        When a bug has two packages with different importances, CPPA must
        assign each PPA task the importance of the matching DSP task rather
        than a single bug-wide value.
        """
        pkg2_name = self.factory.makeSourcePackageName(name="another-pkg")
        dsp2 = self.ubuntu.getSourcePackage(pkg2_name)

        bug = self._make_ubuntu_bug()
        # sam2p is the only task before import; set it to CRITICAL.
        dsp_task1 = removeSecurityProxy(bug.bugtasks[0])
        dsp_task1.transitionToImportance(
            BugTaskImportance.CRITICAL, self.bug_importer
        )
        # Add a second DSP task for another-pkg with HIGH importance.
        dsp_task2 = removeSecurityProxy(
            self.factory.makeBugTask(
                bug=bug, target=dsp2, owner=self.bug_importer
            )
        )
        dsp_task2.transitionToImportance(
            BugTaskImportance.HIGH, self.bug_importer
        )

        record = CPPARecord(
            candidate="CVE-2013-1953",
            packages=[
                CPPARecord.Package(
                    name="sam2p",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="my-project/focal",
                            status=CPPARecord.PackageStatus.NEEDED,
                            reason="",
                        )
                    ],
                ),
                CPPARecord.Package(
                    name="another-pkg",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="my-project/focal",
                            status=CPPARecord.PackageStatus.RELEASED,
                            reason="",
                        )
                    ],
                ),
            ],
        )
        importer = self._make_importer()
        importer.from_record(record, "CVE-2013-1953")

        tasks_by_package = {
            t.target.sourcepackagename.name: removeSecurityProxy(t)
            for t in bug.bugtasks
            if isinstance(t.target, ArchiveSourcePackageSeries)
        }
        self.assertEqual(
            tasks_by_package["sam2p"].importance, BugTaskImportance.CRITICAL
        )
        self.assertEqual(
            tasks_by_package["another-pkg"].importance, BugTaskImportance.HIGH
        )

    def test_activity_log_entry_created(self):
        """A bug activity entry is recorded for each successful import."""
        bug = self._make_bug_with_no_ppa_tasks()
        importer = self._make_importer()
        importer.from_record(self._make_record(), "CVE-2013-1953")

        messages = [a.message for a in bug.activity]
        self.assertIn("CPPA CVE entry CVE-2013-1953", messages)

    def _make_bug_with_no_ppa_tasks(self):
        """Wrapper around _make_ubuntu_bug for readability."""
        return self._make_ubuntu_bug()

    def _get_series_task(self, bug):
        """Return the first ArchiveSourcePackageSeries bugtask on *bug*."""
        series_tasks = [
            removeSecurityProxy(t)
            for t in bug.bugtasks
            if isinstance(t.target, ArchiveSourcePackageSeries)
        ]
        self.assertEqual(len(series_tasks), 1)
        return series_tasks[0]


class TestCPPAImporterErrorCases(TestCPPAImporterSetup):
    """Tests for conditions that should raise CPPAImportError."""

    def test_raises_when_cve_not_in_launchpad(self):
        """CPPAImportError raised when the CVE is not in LP."""
        importer = self._make_importer()
        record = CPPARecord(
            candidate="CVE-9999-9999",
            packages=[],
        )
        self.assertRaises(
            CPPAImportError,
            importer.from_record,
            record,
            "CVE-9999-9999",
        )

    def test_raises_when_no_ubuntu_vulnerability(self):
        """CPPAImportError raised when no Ubuntu vulnerability exists yet."""
        # lp_cve exists but no vulnerability/bug have been created by UCT
        importer = self._make_importer()
        self.assertRaises(
            CPPAImportError,
            importer.from_record,
            self._make_record(),
            "CVE-2013-1953",
        )

    def test_raises_when_vulnerability_has_no_bug(self):
        """CPPAImportError raised when the vulnerability has no linked bug."""
        vulnerability = self.factory.makeVulnerability(
            distribution=self.ubuntu,
            cve=self.lp_cve,
        )
        removeSecurityProxy(vulnerability)

        importer = self._make_importer()
        self.assertRaises(
            CPPAImportError,
            importer.from_record,
            self._make_record(),
            "CVE-2013-1953",
        )

    def test_raises_on_cve_sequence_mismatch(self):
        """CPPAImportError raised when record.candidate != cve_sequence."""
        importer = self._make_importer()
        record = self._make_record()
        self.assertRaises(
            CPPAImportError,
            importer.from_record,
            record,
            "CVE-0000-0000",  # wrong sequence
        )

    def test_raises_on_duplicate_series_target(self):
        """CPPAImportError raised when the same (package, archive, series)
        target appears twice in the same import batch."""
        self._make_ubuntu_bug()
        # Two identical SeriesPackageStatus entries resolve to the same
        # ArchiveSourcePackageSeries target.
        record = CPPARecord(
            candidate="CVE-2013-1953",
            packages=[
                CPPARecord.Package(
                    name="sam2p",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="my-project/focal",
                            status=CPPARecord.PackageStatus.NEEDED,
                            reason="",
                        ),
                        CPPARecord.SeriesPackageStatus(
                            series="my-project/focal",
                            status=CPPARecord.PackageStatus.RELEASED,
                            reason="1.2.3",
                        ),
                    ],
                )
            ],
        )
        importer = self._make_importer()
        self.assertRaises(
            CPPAImportError,
            importer.from_record,
            record,
            "CVE-2013-1953",
        )


class TestCPPAImporterSkips(TestCPPAImporterSetup):
    """Tests for entries that should be silently skipped."""

    def test_skips_unknown_subproject(self):
        """Entries with an unknown subproject key are skipped without error."""
        bug = self._make_ubuntu_bug()
        initial_task_count = len(bug.bugtasks)

        record = CPPARecord(
            candidate="CVE-2013-1953",
            packages=[
                CPPARecord.Package(
                    name="sam2p",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="unknown-project/focal",
                            status=CPPARecord.PackageStatus.NEEDED,
                            reason="",
                        )
                    ],
                )
            ],
        )
        importer = self._make_importer()
        # Should not raise
        importer.from_record(record, "CVE-2013-1953")

        # No new bugtasks should have been created.
        self.assertEqual(len(bug.bugtasks), initial_task_count)

    def test_skips_unknown_source_package(self):
        """Packages not yet known to Launchpad are skipped without error."""
        bug = self._make_ubuntu_bug()
        initial_task_count = len(bug.bugtasks)

        record = CPPARecord(
            candidate="CVE-2013-1953",
            packages=[
                CPPARecord.Package(
                    name="never-heard-of-this",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="my-project/focal",
                            status=CPPARecord.PackageStatus.NEEDED,
                            reason="",
                        )
                    ],
                )
            ],
        )
        importer = self._make_importer()
        importer.from_record(record, "CVE-2013-1953")

        self.assertEqual(len(bug.bugtasks), initial_task_count)

    def test_skips_unknown_ppa(self):
        """Entries whose PPA cannot be resolved are skipped without error."""
        subprojects = {
            "my-project/focal": SubProjectPPAs(
                ubuntu_series="focal",
                ppa=PPAReference(
                    owner="does-not-exist",
                    archive="my-ppa",
                    pocket="release",
                ),
            )
        }
        bug = self._make_ubuntu_bug()
        initial_task_count = len(bug.bugtasks)

        importer = CPPAImporter(ubuntu=self.ubuntu, subprojects=subprojects)
        importer.from_record(self._make_record(), "CVE-2013-1953")

        self.assertEqual(len(bug.bugtasks), initial_task_count)

    def test_skips_unknown_distro_series(self):
        """Entries whose Ubuntu series cannot be resolved are skipped."""
        subprojects = {
            "my-project/focal": SubProjectPPAs(
                ubuntu_series="nonexistent-series",
                ppa=PPAReference(
                    owner="my-ppa-owner",
                    archive="my-ppa",
                    pocket="release",
                ),
            )
        }
        bug = self._make_ubuntu_bug()
        initial_task_count = len(bug.bugtasks)

        importer = CPPAImporter(ubuntu=self.ubuntu, subprojects=subprojects)
        importer.from_record(self._make_record(), "CVE-2013-1953")

        self.assertEqual(len(bug.bugtasks), initial_task_count)


class TestCPPAImporterDryRun(TestCPPAImporterSetup):
    """Tests for dry_run mode."""

    def test_dry_run_does_not_commit(self):
        """In dry-run mode no bugtasks are persisted."""
        bug = self._make_ubuntu_bug()
        initial_task_count = len(bug.bugtasks)

        importer = self._make_importer(dry_run=True)
        importer.from_record(self._make_record(), "CVE-2013-1953")

        # After rollback the bug should have the original number of tasks.
        self.assertEqual(len(bug.bugtasks), initial_task_count)


class TestBugTaskStatusMap(TestCPPAImporterSetup):
    """Verify the completeness of ."""

    def test_all_package_statuses_are_mapped(self):
        """Every CPPARecord.PackageStatus value must appear in the map."""
        unmapped = [
            s for s in CPPARecord.PackageStatus if s not in BUG_TASK_STATUS_MAP
        ]
        self.assertEqual(
            unmapped,
            [],
            f"PackageStatus values missing from BUG_TASK_STATUS_MAP: "
            f"{unmapped}",
        )

    def test_pending_maps_to_fixcommitted(self):
        self.assertEqual(
            BUG_TASK_STATUS_MAP[CPPARecord.PackageStatus.PENDING],
            BugTaskStatus.FIXCOMMITTED,
        )


class TestCPPAImporterPermissions(TestCPPAImporterSetup):
    """Tests for checkUserPermissions."""

    def test_unprivileged_user_denied(self):
        """A random user is denied."""
        importer = self._make_importer()
        user = self.factory.makePerson()
        self.assertFalse(importer.checkUserPermissions(user))

    def test_security_admin_allowed(self):
        """A member of the security admin team is allowed."""
        importer = self._make_importer()
        security_team = self.factory.makeTeam()
        removeSecurityProxy(self.ubuntu).security_admin = security_team
        admin = self.factory.makePerson()
        removeSecurityProxy(security_team).addMember(admin, admin)
        self.assertTrue(importer.checkUserPermissions(admin))
