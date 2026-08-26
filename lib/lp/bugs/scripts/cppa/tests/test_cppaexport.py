#  Copyright 2026 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

"""Unit tests for CPPAExporter."""

from pathlib import Path

from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.bugs.enums import VulnerabilityStatus
from lp.bugs.interfaces.bugtask import BugTaskImportance, IBugTaskSet
from lp.bugs.scripts.cppa.cppaexport import (
    BUG_TASK_STATUS_MAP_REVERSE,
    CPPAExporter,
)
from lp.bugs.scripts.cppa.cppaimport import BUG_TASK_STATUS_MAP, CPPAImporter
from lp.bugs.scripts.cppa.models import CPPARecord
from lp.bugs.scripts.uct.subprojects import PPAReference, SubProjectPPAs
from lp.registry.model.archivesourcepackageseries import (
    ArchiveSourcePackageSeries,
)
from lp.testing import TestCaseWithFactory
from lp.testing.layers import LaunchpadZopelessLayer


class TestCPPAExporterSetup(TestCaseWithFactory):
    """Helper base class that builds the common test fixture."""

    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.bug_importer = getUtility(ILaunchpadCelebrities).bug_importer

        # Two distro series
        self.focal = self.factory.makeDistroSeries(
            distribution=self.ubuntu, name="focal"
        )
        self.jammy = self.factory.makeDistroSeries(
            distribution=self.ubuntu, name="jammy"
        )

        # PPA owner + two PPAs
        self.ppa_owner = self.factory.makePerson(name="my-ppa-owner")
        self.ppa = self.factory.makeArchive(
            distribution=self.ubuntu,
            owner=self.ppa_owner,
            name="my-ppa",
        )
        self.ppa_security = self.factory.makeArchive(
            distribution=self.ubuntu,
            owner=self.ppa_owner,
            name="my-ppa-security",
        )

        # Subprojects config: three keys covering two PPAs and two series
        self.subprojects = {
            "my-project/focal": SubProjectPPAs(
                ubuntu_series="focal",
                ppa=PPAReference(
                    owner="my-ppa-owner",
                    archive="my-ppa",
                    pocket="release",
                ),
            ),
            "my-project/jammy": SubProjectPPAs(
                ubuntu_series="jammy",
                ppa=PPAReference(
                    owner="my-ppa-owner",
                    archive="my-ppa",
                    pocket="release",
                ),
            ),
            "my-security/focal": SubProjectPPAs(
                ubuntu_series="focal",
                ppa=PPAReference(
                    owner="my-ppa-owner",
                    archive="my-ppa-security",
                    pocket="release",
                ),
            ),
        }

        # LP CVE entry
        self.lp_cve = self.factory.makeCVE(sequence="2022-1234")

        self.exporter = CPPAExporter(self.subprojects)

    def _make_ubuntu_bug(self):
        """Create a minimal Ubuntu vulnerability + bug as UCT would produce."""
        vulnerability = removeSecurityProxy(
            self.factory.makeVulnerability(
                distribution=self.ubuntu,
                status=VulnerabilityStatus.ACTIVE,
                importance=BugTaskImportance.MEDIUM,
                creator=self.bug_importer,
                information_type=InformationType.PUBLICSECURITY,
                cve=self.lp_cve,
            )
        )
        pkg_name = self.factory.makeSourcePackageName(name="mypkg")
        dsp = self.ubuntu.getSourcePackage(pkg_name)
        bug = removeSecurityProxy(
            self.factory.makeBug(
                target=dsp,
                owner=self.bug_importer,
                information_type=InformationType.PUBLICSECURITY,
            )
        )
        vulnerability.linkBug(bug, self.bug_importer)
        return bug, vulnerability

    def _import_ppa_task(
        self,
        bug,
        subproject_key,
        pkg_name,
        cppa_status=CPPARecord.PackageStatus.RELEASED,
        reason="",
    ):
        """Add a PPA bugtask to *bug* via CPPAImporter"""
        pkg_name_str = pkg_name if isinstance(pkg_name, str) else pkg_name.name
        record = CPPARecord(
            candidate=self.lp_cve.sequence,
            packages=[
                CPPARecord.Package(
                    name=pkg_name_str,
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series=subproject_key,
                            status=cppa_status,
                            reason=reason,
                        )
                    ],
                )
            ],
        )
        CPPAImporter(
            ubuntu=self.ubuntu, subprojects=self.subprojects
        ).update_bug(bug, record)


class TestBugTaskStatusMapReverse(TestCPPAExporterSetup):
    """BUG_TASK_STATUS_MAP_REVERSE is the inverse of BUG_TASK_STATUS_MAP"""

    def test_reverse_map_covers_all_import_statuses(self):
        """Every status reachable via import can be exported."""
        for cppa_status, lp_status in BUG_TASK_STATUS_MAP.items():
            self.assertIn(
                lp_status,
                BUG_TASK_STATUS_MAP_REVERSE,
                f"BugTaskStatus.{lp_status.name} not in reverse map "
                f"(imported from CPPARecord.PackageStatus.{cppa_status.name})",
            )

    def test_reverse_map_round_trips(self):
        """Every PackageStatus to BugTaskStatus conversion in the import map
        can be converted back to the same PackageStatus via the reverse map."""
        for cppa_status, lp_status in BUG_TASK_STATUS_MAP.items():
            exported = BUG_TASK_STATUS_MAP_REVERSE[lp_status]
            self.assertEqual(
                cppa_status,
                exported,
                f"Round-trip failed: "
                f"CPPARecord.PackageStatus.{cppa_status.name} to "
                f"BugTaskStatus.{lp_status.name} to "
                f"CPPARecord.PackageStatus.{exported.name}",
            )


class TestCPPAExporterToRecord(TestCPPAExporterSetup):
    """Tests for CPPAExporter.to_record()."""

    def test_to_record_empty_ppa_tasks(self):
        """to_record returns an empty CPPARecord when the bug has no
        ArchiveSourcePackageSeries tasks for known CPPA archives."""
        bug, vulnerability = self._make_ubuntu_bug()

        record = self.exporter.to_record(bug, vulnerability)

        self.assertIsNotNone(record)
        self.assertEqual([], record.packages)

    def test_to_record_single_ppa_series_task(self):
        """to_record produces one Package entry with one SeriesPackageStatus
        for a single ArchiveSourcePackageSeries task."""
        bug, vulnerability = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename
        self._import_ppa_task(
            bug,
            "my-project/focal",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.RELEASED,
            reason="1.0-1",
        )

        record = self.exporter.to_record(bug, vulnerability)

        self.assertEqual(self.lp_cve.sequence, record.candidate)
        self.assertEqual(1, len(record.packages))
        pkg = record.packages[0]
        self.assertEqual(pkg_name.name, pkg.name)
        self.assertEqual(1, len(pkg.statuses))
        s = pkg.statuses[0]
        self.assertEqual("my-project/focal", s.series)
        self.assertEqual(CPPARecord.PackageStatus.RELEASED, s.status)
        self.assertEqual("1.0-1", s.reason)

    def test_to_record_multiple_series_same_package(self):
        """Two ArchiveSourcePackageSeries tasks for the same package but
        different series produce two SeriesPackageStatus entries under a
        single Package."""
        bug, vulnerability = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename
        self._import_ppa_task(
            bug,
            "my-project/focal",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.RELEASED,
        )
        self._import_ppa_task(
            bug,
            "my-project/jammy",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.NEEDED,
        )

        record = self.exporter.to_record(bug, vulnerability)

        self.assertEqual(1, len(record.packages))
        pkg = record.packages[0]
        self.assertEqual(2, len(pkg.statuses))
        series_keys = {s.series for s in pkg.statuses}
        self.assertEqual({"my-project/focal", "my-project/jammy"}, series_keys)

    def test_to_record_multiple_packages(self):
        """Tasks for different packages produce separate Package entries."""
        bug, vulnerability = self._make_ubuntu_bug()
        pkg_name1 = bug.bugtasks[0].target.sourcepackagename
        pkg_name2 = self.factory.makeSourcePackageName(name="otherpkg")
        self._import_ppa_task(
            bug,
            "my-project/focal",
            pkg_name1,
            cppa_status=CPPARecord.PackageStatus.RELEASED,
        )
        self._import_ppa_task(
            bug,
            "my-project/jammy",
            pkg_name2,
            cppa_status=CPPARecord.PackageStatus.NEEDED,
        )

        record = self.exporter.to_record(bug, vulnerability)

        pkg_names = {p.name for p in record.packages}
        self.assertIn(pkg_name1.name, pkg_names)
        self.assertIn(pkg_name2.name, pkg_names)

    def test_to_record_multiple_ppas(self):
        """Tasks for the same package in two different CPPA PPAs produce two
        SeriesPackageStatus entries (one per subproject key)."""
        bug, vulnerability = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename
        self._import_ppa_task(
            bug,
            "my-project/focal",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.RELEASED,
        )
        self._import_ppa_task(
            bug,
            "my-security/focal",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.NEEDED,
        )

        record = self.exporter.to_record(bug, vulnerability)

        self.assertEqual(1, len(record.packages))
        pkg = record.packages[0]
        self.assertEqual(2, len(pkg.statuses))
        series_keys = {s.series for s in pkg.statuses}
        self.assertIn("my-project/focal", series_keys)
        self.assertIn("my-security/focal", series_keys)

    def test_to_record_all_status_values(self):
        """Every exportable BugTaskStatus maps back to the correct
        PackageStatus even after to_record."""
        bug, vulnerability = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename

        for (
            lp_status,
            expected_cppa_status,
        ) in BUG_TASK_STATUS_MAP_REVERSE.items():
            # Use a unique distroseries per status so each ASPS is distinct.
            extra_series = self.factory.makeDistroSeries(
                distribution=self.ubuntu,
                name=f"series-{lp_status.name.lower()}",
            )
            # Register the new series so the exporter and importer recognise
            # it.
            key = f"my-project/{extra_series.name}"
            self.subprojects[key] = SubProjectPPAs(
                ubuntu_series=extra_series.name,
                ppa=PPAReference(
                    owner="my-ppa-owner",
                    archive="my-ppa",
                    pocket="release",
                ),
            )
            self.exporter._archive_to_subproject[
                ("my-ppa-owner", "my-ppa", extra_series.name)
            ] = key

            self._import_ppa_task(
                bug,
                key,
                pkg_name,
                cppa_status=expected_cppa_status,
            )

            record = self.exporter.to_record(bug, vulnerability)
            status_by_key = {
                s.series: s.status
                for pkg in record.packages
                for s in pkg.statuses
            }
            self.assertIn(key, status_by_key, f"Missing key {key!r}")
            self.assertEqual(
                expected_cppa_status,
                status_by_key[key],
                f"Status round-trip failed for "
                f"BugTaskStatus.{lp_status.name}",
            )

    def test_to_record_skips_tasks_not_in_subprojects(self):
        """ArchiveSourcePackageSeries tasks whose (archive, series) combination
        is not in the CPPA subprojects config are silently ignored, this
        covers UCT-imported ESM/other PPA tasks."""
        bug, vulnerability = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename

        # A UCT-imported ESM PPA task (entirely unknown archive).
        esm_owner = self.factory.makePerson(name="ubuntu-esm")
        esm_ppa = self.factory.makeArchive(
            distribution=self.ubuntu,
            owner=esm_owner,
            name="esm-infra-security",
        )
        esm_asps = ArchiveSourcePackageSeries(
            archive=esm_ppa,
            distroseries=self.focal,
            sourcepackagename=pkg_name,
        )
        # A task for a known CPPA archive but a series not in any subproject
        # key.
        unknown_series = self.factory.makeDistroSeries(
            distribution=self.ubuntu, name="unknown-series"
        )
        unknown_asps = ArchiveSourcePackageSeries(
            archive=self.ppa,
            distroseries=unknown_series,
            sourcepackagename=pkg_name,
        )
        getUtility(IBugTaskSet).createManyTasks(
            bug,
            self.bug_importer,
            [esm_asps, unknown_asps],
            check_ppa_source_package=False,
        )

        record = self.exporter.to_record(bug, vulnerability)

        self.assertIsNotNone(record)
        self.assertEqual([], record.packages)

    def test_to_record_raises_on_none_bug(self):
        """to_record raises ValueError when bug is None."""
        _, vulnerability = self._make_ubuntu_bug()
        self.assertRaisesRegex(
            ValueError,
            "Bug can't be None",
            self.exporter.to_record,
            None,
            vulnerability,
        )

    def test_to_record_raises_on_none_vulnerability(self):
        """to_record raises ValueError when vulnerability is None."""
        bug, _ = self._make_ubuntu_bug()
        self.assertRaisesRegex(
            ValueError,
            "Vulnerability can't be None",
            self.exporter.to_record,
            bug,
            None,
        )

    def test_to_record_raises_on_vulnerability_without_cve(self):
        """to_record raises ValueError when the vulnerability has no linked
        CVE."""
        bug, _ = self._make_ubuntu_bug()
        vulnerability_no_cve = removeSecurityProxy(
            self.factory.makeVulnerability(
                distribution=self.ubuntu,
                status=VulnerabilityStatus.ACTIVE,
                importance=BugTaskImportance.MEDIUM,
                creator=self.bug_importer,
                information_type=InformationType.PUBLICSECURITY,
                cve=None,
            )
        )
        self.assertRaisesRegex(
            ValueError,
            "has no linked CVE",
            self.exporter.to_record,
            bug,
            vulnerability_no_cve,
        )

    def test_to_record_status_explanation_preserved(self):
        """status_explanation on the bugtask is preserved as the ``reason``
        field in the exported SeriesPackageStatus."""
        bug, vulnerability = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename
        self._import_ppa_task(
            bug,
            "my-project/focal",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.RELEASED,
            reason="2.0-1ubuntu3",
        )

        record = self.exporter.to_record(bug, vulnerability)

        self.assertEqual("2.0-1ubuntu3", record.packages[0].statuses[0].reason)

    def test_to_record_missing_status_explanation_is_empty_string(self):
        """When status_explanation is None/empty on the bugtask, the exported
        reason is an empty string."""
        bug, vulnerability = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename
        self._import_ppa_task(
            bug,
            "my-project/focal",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.NEEDED,
        )

        record = self.exporter.to_record(bug, vulnerability)

        self.assertEqual("", record.packages[0].statuses[0].reason)

    def test_to_record_ignores_non_asps_tasks(self):
        """Only ArchiveSourcePackageSeries tasks contribute statuses."""
        bug, vulnerability = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename
        self._import_ppa_task(
            bug,
            "my-project/focal",
            pkg_name,
        )

        record = self.exporter.to_record(bug, vulnerability)

        # Only the one ArchiveSourcePackageSeries task appears.
        self.assertEqual(1, len(record.packages))
        self.assertEqual(1, len(record.packages[0].statuses))


class TestCPPAExporterExportBugFile(TestCPPAExporterSetup):
    """Tests for CPPAExporter.export_bug_to_cppa_file()."""

    def test_export_bug_to_cppa_file_creates_file(self):
        """export_bug_to_cppa_file writes a CPPA file named after the CVE
        and returns its path."""
        bug, _ = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename
        self._import_ppa_task(
            bug,
            "my-project/focal",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.RELEASED,
            reason="1.0-1",
        )

        output_dir = Path(self.makeTemporaryDirectory())
        saved_paths = self.exporter.export_bug_to_cppa_file(bug.id, output_dir)

        self.assertEqual(1, len(saved_paths))
        saved_path = saved_paths[0]
        self.assertTrue(saved_path.exists())
        self.assertEqual(
            output_dir / "my-project" / self.lp_cve.sequence,
            saved_path,
        )

    def test_export_bug_to_cppa_file_content_round_trips(self):
        """The exported file can be loaded back as a CPPARecord with the
        same package/status data."""
        bug, _ = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename
        self._import_ppa_task(
            bug,
            "my-project/focal",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.RELEASED,
            reason="1.0-1",
        )

        output_dir = Path(self.makeTemporaryDirectory())
        saved_paths = self.exporter.export_bug_to_cppa_file(bug.id, output_dir)

        loaded = CPPARecord.load(saved_paths[0])
        self.assertEqual(self.lp_cve.sequence, loaded.candidate)
        self.assertEqual(1, len(loaded.packages))
        self.assertEqual(pkg_name.name, loaded.packages[0].name)
        s = loaded.packages[0].statuses[0]
        self.assertEqual("my-project/focal", s.series)
        self.assertEqual(CPPARecord.PackageStatus.RELEASED, s.status)
        self.assertEqual("1.0-1", s.reason)

    def test_export_bug_to_cppa_file_missing_bug_returns_empty(self):
        """export_bug_to_cppa_file returns [] when the bug ID does not
        exist."""
        output_dir = Path(self.makeTemporaryDirectory())

        with self.expectedLog(".*Could not find bug with ID"):
            result = self.exporter.export_bug_to_cppa_file(
                999999999, output_dir
            )

        self.assertEqual([], result)

    def test_export_bug_to_cppa_file_no_vulnerability_returns_empty(self):
        """export_bug_to_cppa_file returns [] when the bug has no
        vulnerabilities."""
        dsp = self.factory.makeDistributionSourcePackage(
            distribution=self.ubuntu
        )
        bug = removeSecurityProxy(
            self.factory.makeBug(
                target=dsp,
                owner=self.bug_importer,
                information_type=InformationType.PUBLICSECURITY,
            )
        )

        output_dir = Path(self.makeTemporaryDirectory())

        with self.expectedLog(".*has no vulnerabilities"):
            result = self.exporter.export_bug_to_cppa_file(bug.id, output_dir)

        self.assertEqual([], result)

    def test_export_bug_to_cppa_file_no_cve_returns_empty(self):
        """export_bug_to_cppa_file returns [] when the vulnerability has
        no linked CVE."""
        dsp = self.factory.makeDistributionSourcePackage(
            distribution=self.ubuntu
        )
        bug = removeSecurityProxy(
            self.factory.makeBug(
                target=dsp,
                owner=self.bug_importer,
                information_type=InformationType.PUBLICSECURITY,
            )
        )
        vulnerability = removeSecurityProxy(
            self.factory.makeVulnerability(
                distribution=self.ubuntu,
                status=VulnerabilityStatus.ACTIVE,
                importance=BugTaskImportance.MEDIUM,
                creator=self.bug_importer,
                information_type=InformationType.PUBLICSECURITY,
                cve=None,
            )
        )
        vulnerability.linkBug(bug, self.bug_importer)

        output_dir = Path(self.makeTemporaryDirectory())

        with self.expectedLog(".*has no linked CVE"):
            result = self.exporter.export_bug_to_cppa_file(bug.id, output_dir)

        self.assertEqual([], result)

    def test_export_bug_to_cppa_file_no_cppa_tasks_returns_empty(self):
        """export_bug_to_cppa_file returns [] (and writes no file) when the
        bug has a vulnerability + CVE but no CPPA bugtasks."""
        bug, _ = self._make_ubuntu_bug()

        output_dir = Path(self.makeTemporaryDirectory())
        result = self.exporter.export_bug_to_cppa_file(bug.id, output_dir)

        self.assertEqual([], result)
        # No file should have been written.
        self.assertEqual([], list(output_dir.iterdir()))

    def test_export_bug_to_cppa_file_multiple_projects_separate_dirs(self):
        """When statuses span multiple projects, one file per project is
        written into its own subdirectory of output_dir."""
        bug, _ = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename
        self._import_ppa_task(
            bug,
            "my-project/focal",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.RELEASED,
            reason="1.0-1",
        )
        self._import_ppa_task(
            bug,
            "my-security/focal",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.NEEDED,
        )

        output_dir = Path(self.makeTemporaryDirectory())
        saved_paths = self.exporter.export_bug_to_cppa_file(bug.id, output_dir)

        self.assertEqual(2, len(saved_paths))
        dirs = {p.parent.name for p in saved_paths}
        self.assertEqual({"my-project", "my-security"}, dirs)
        for path in saved_paths:
            self.assertEqual(self.lp_cve.sequence, path.name)
            self.assertTrue(path.exists())

    def test_export_bug_to_cppa_file_content(self):
        """Multiple series from the same project are merged into a single
        file under that project's subdirectory, with one status line per
        series."""
        bug, _ = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename
        self._import_ppa_task(
            bug,
            "my-project/focal",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.RELEASED,
            reason="1.0-1",
        )
        self._import_ppa_task(
            bug,
            "my-project/jammy",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.NEEDED,
            reason="",
        )
        self._import_ppa_task(
            bug,
            "my-security/focal",
            pkg_name,
            cppa_status=CPPARecord.PackageStatus.NOT_AFFECTED,
        )

        output_dir = Path(self.makeTemporaryDirectory())
        saved_paths = self.exporter.export_bug_to_cppa_file(bug.id, output_dir)

        # Two project directories written.
        self.assertEqual(2, len(saved_paths))
        by_project = {p.parent.name: p for p in saved_paths}
        self.assertIn("my-project", by_project)
        self.assertIn("my-security", by_project)

        # my-project file: exactly focal + jammy lines, nothing else.
        self.assertEqual(
            "my-project/focal_mypkg: released (1.0-1)\n"
            "my-project/jammy_mypkg: needed\n",
            by_project["my-project"].read_text(),
        )

        # my-security file: exactly one focal line, nothing else.
        self.assertEqual(
            "my-security/focal_mypkg: not-affected\n",
            by_project["my-security"].read_text(),
        )


class TestCPPAImportExportRoundTrip(TestCPPAExporterSetup):
    """Import via CPPAImporter then export via CPPAExporter and verify the
    CPPARecord survives the round-trip."""

    def _make_cppa_record(self, pkg_name, statuses):
        """Build a CPPARecord with the given package/statuses list."""
        return CPPARecord(
            candidate=self.lp_cve.sequence,
            packages=[
                CPPARecord.Package(
                    name=pkg_name,
                    statuses=statuses,
                )
            ],
        )

    def test_multiple_statuses_round_trip(self):
        """Multiple series entries for one package all survive round-trip."""
        bug, vulnerability = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename

        original_record = self._make_cppa_record(
            pkg_name.name,
            [
                CPPARecord.SeriesPackageStatus(
                    series="my-project/focal",
                    status=CPPARecord.PackageStatus.RELEASED,
                    reason="1.0-1",
                ),
                CPPARecord.SeriesPackageStatus(
                    series="my-project/jammy",
                    status=CPPARecord.PackageStatus.NEEDED,
                    reason="",
                ),
            ],
        )

        importer = CPPAImporter(
            ubuntu=self.ubuntu, subprojects=self.subprojects
        )
        importer.update_bug(bug, original_record)

        exported_record = self.exporter.to_record(bug, vulnerability)

        self.assertEqual(1, len(exported_record.packages))
        exported_statuses = {
            s.series: s for s in exported_record.packages[0].statuses
        }
        self.assertIn("my-project/focal", exported_statuses)
        self.assertIn("my-project/jammy", exported_statuses)
        self.assertEqual(
            CPPARecord.PackageStatus.RELEASED,
            exported_statuses["my-project/focal"].status,
        )
        self.assertEqual(
            CPPARecord.PackageStatus.NEEDED,
            exported_statuses["my-project/jammy"].status,
        )

    def test_non_cppa_ppa_tasks_excluded_from_export(self):
        """Tasks added by UCT for non-CPPA PPAs are present on the bug but
        excluded from the CPPAExporter output."""
        bug, vulnerability = self._make_ubuntu_bug()
        pkg_name = bug.bugtasks[0].target.sourcepackagename

        # Simulate a UCT-imported ESM PPA task on the same bug.
        esm_owner = self.factory.makePerson(name="ubuntu-esm")
        esm_ppa = self.factory.makeArchive(
            distribution=self.ubuntu,
            owner=esm_owner,
            name="esm-infra-security",
        )
        esm_asps = ArchiveSourcePackageSeries(
            archive=esm_ppa,
            distroseries=self.focal,
            sourcepackagename=pkg_name,
        )
        getUtility(IBugTaskSet).createManyTasks(
            bug,
            self.bug_importer,
            [esm_asps],
            check_ppa_source_package=False,
        )

        # Also add a genuine CPPA task.
        original_record = self._make_cppa_record(
            pkg_name.name,
            [
                CPPARecord.SeriesPackageStatus(
                    series="my-project/focal",
                    status=CPPARecord.PackageStatus.RELEASED,
                    reason="9.9-1",
                ),
            ],
        )
        importer = CPPAImporter(
            ubuntu=self.ubuntu, subprojects=self.subprojects
        )
        importer.update_bug(bug, original_record)

        exported_record = self.exporter.to_record(bug, vulnerability)

        # Only the CPPA task appears; the ESM task is excluded.
        self.assertEqual(1, len(exported_record.packages))
        statuses = exported_record.packages[0].statuses
        self.assertEqual(1, len(statuses))
        self.assertEqual("my-project/focal", statuses[0].series)
