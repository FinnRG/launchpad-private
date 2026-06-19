#  Copyright 2024 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

from pathlib import Path

from lp.bugs.scripts.cppa.models import CPPARecord
from lp.testing import TestCase


class TestCPPARecord(TestCase):
    """Tests for CPPARecord data model."""

    def setUp(self):
        super().setUp()
        self.record = CPPARecord(
            candidate="CVE-2013-1953",
            packages=[
                CPPARecord.Package(
                    name="sam2p",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="project1/trusty",
                            status=CPPARecord.PackageStatus.NEEDED,
                            reason="",
                        ),
                        CPPARecord.SeriesPackageStatus(
                            series="project1/precise",
                            status=CPPARecord.PackageStatus.RELEASED,
                            reason="0.49.1-1",
                        ),
                        CPPARecord.SeriesPackageStatus(
                            series="project2/xenial",
                            status=CPPARecord.PackageStatus.RELEASED,
                            reason="0.49.4-3",
                        ),
                    ],
                ),
            ],
        )

    def test_load_save(self):
        """Test loading from file and saving to file."""
        load_from = Path(__file__).parent / "sampledata" / "CVE-2013-1953"
        cppa_record = CPPARecord.load(load_from)
        self.assertDictEqual(
            self.record.__dict__,
            cppa_record.__dict__,
        )

        output_dir = Path(self.makeTemporaryDirectory())
        saved_to_path = cppa_record.save(output_dir)
        self.assertEqual(output_dir / "CVE-2013-1953", saved_to_path)
        self.assertEqual(load_from.read_text(), saved_to_path.read_text())

    def test_from_str(self):
        """Test parsing from string."""
        load_from = Path(__file__).parent / "sampledata" / "CVE-2013-1953"
        with open(load_from, "rb") as f:
            string = f.read()

        record = CPPARecord.from_str(string)

        # from_str creates a temp file with random CVE ID, so only compare
        # packages
        self.assertEqual(record.packages, self.record.packages)
        self.assertTrue(record.candidate.startswith("CVE-"))

    def test_to_str(self):
        """Test serialization to string."""
        load_from = Path(__file__).parent / "sampledata" / "CVE-2013-1953"
        with open(load_from) as f:
            expected_record = f.read()
        record_str = self.record.to_str()
        self.assertEqual(expected_record, record_str)

    def test_load_with_reasons(self):
        """Test loading a file with status reasons."""
        load_from = Path(__file__).parent / "sampledata" / "CVE-2024-12345"
        cppa_record = CPPARecord.load(load_from)

        expected = CPPARecord(
            candidate="CVE-2024-12345",
            packages=[
                CPPARecord.Package(
                    name="test-package",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="project1/jammy",
                            status=CPPARecord.PackageStatus.NEEDED,
                            reason="",
                        ),
                    ],
                ),
                CPPARecord.Package(
                    name="test-package+extra",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="project1/jammy",
                            status=CPPARecord.PackageStatus.RELEASED,
                            reason="1.0.0-1",
                        ),
                    ],
                ),
                CPPARecord.Package(
                    name="another.package",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="project1/focal",
                            status=CPPARecord.PackageStatus.DEFERRED,
                            reason="waiting for update",
                        ),
                    ],
                ),
                CPPARecord.Package(
                    name="pkg-name",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="project1/bionic",
                            status=CPPARecord.PackageStatus.PENDING,
                            reason="2.0.0-1ubuntu1",
                        ),
                    ],
                ),
            ],
        )

        self.assertDictEqual(expected.__dict__, cppa_record.__dict__)

    def test_to_str_minimal(self):
        """Test serialization of minimal CPPARecord."""
        minimal_record = CPPARecord(
            candidate="CVE-2024-00001",
            packages=[],
        )

        serialized = minimal_record.to_str()

        # Empty packages should produce empty output
        self.assertEqual("", serialized)

    def test_to_str_single_package(self):
        """Test serialization of single package with multiple statuses."""
        record = CPPARecord(
            candidate="CVE-2024-00002",
            packages=[
                CPPARecord.Package(
                    name="testpkg",
                    statuses=[
                        CPPARecord.SeriesPackageStatus(
                            series="project1/jammy",
                            status=CPPARecord.PackageStatus.NEEDED,
                            reason="",
                        ),
                        CPPARecord.SeriesPackageStatus(
                            series="project1/focal",
                            status=CPPARecord.PackageStatus.RELEASED,
                            reason="1.2.3-4",
                        ),
                    ],
                ),
            ],
        )

        serialized = record.to_str()

        expected = (
            "project1/jammy_testpkg: needed\n"
            "project1/focal_testpkg: released (1.2.3-4)\n"
        )
        self.assertEqual(expected, serialized)

    def test_package_name_special_characters(self):
        """Test handling of package names with special characters."""
        content = (
            "project1/jammy_lib-test.package+extra: needed\n"
            "project1/focal_my-pkg_2: released (1.0-1)\n"
        )

        record = CPPARecord.from_str(content.encode())

        self.assertTrue(record.candidate.startswith("CVE-"))
        self.assertEqual(2, len(record.packages))
        self.assertEqual("lib-test.package+extra", record.packages[0].name)
        self.assertEqual("my-pkg_2", record.packages[1].name)

    def test_ppa_series_format(self):
        """Test handling of PPA series format (e.g., project1/xenial)."""
        content = (
            "project1/trusty_linux: needed\n"
            "project2/xenial_linux: released (4.4.0-1.2)\n"
        )

        record = CPPARecord.from_str(content.encode())

        self.assertTrue(record.candidate.startswith("CVE-"))
        self.assertEqual(1, len(record.packages))
        self.assertEqual("linux", record.packages[0].name)
        self.assertEqual(2, len(record.packages[0].statuses))

        self.assertEqual(
            "project1/trusty", record.packages[0].statuses[0].series
        )
        self.assertEqual(
            "project2/xenial", record.packages[0].statuses[1].series
        )

    def test_all_status_types(self):
        """Test all supported status types."""
        content = (
            "project1/jammy_pkg1: needed\n"
            "project1/jammy_pkg2: released (1.0)\n"
            "project1/jammy_pkg3: pending (2.0)\n"
            "project1/jammy_pkg4: ignored (reason)\n"
            "project1/jammy_pkg5: not-affected (3.0)\n"
            "project1/jammy_pkg6: needs-triage\n"
            "project1/jammy_pkg7: deferred (later)\n"
            "project1/jammy_pkg8: DNE\n"
            "project1/jammy_pkg9: in-progress\n"
        )

        record = CPPARecord.from_str(content.encode())

        self.assertEqual(9, len(record.packages))
        self.assertEqual(
            CPPARecord.PackageStatus.NEEDED,
            record.packages[0].statuses[0].status,
        )
        self.assertEqual(
            CPPARecord.PackageStatus.RELEASED,
            record.packages[1].statuses[0].status,
        )
        self.assertEqual(
            CPPARecord.PackageStatus.PENDING,
            record.packages[2].statuses[0].status,
        )
        self.assertEqual(
            CPPARecord.PackageStatus.IGNORED,
            record.packages[3].statuses[0].status,
        )
        self.assertEqual(
            CPPARecord.PackageStatus.NOT_AFFECTED,
            record.packages[4].statuses[0].status,
        )
        self.assertEqual(
            CPPARecord.PackageStatus.NEEDS_TRIAGE,
            record.packages[5].statuses[0].status,
        )
        self.assertEqual(
            CPPARecord.PackageStatus.DEFERRED,
            record.packages[6].statuses[0].status,
        )
        self.assertEqual(
            CPPARecord.PackageStatus.DOES_NOT_EXIST,
            record.packages[7].statuses[0].status,
        )
        self.assertEqual(
            CPPARecord.PackageStatus.IN_PROGRESS,
            record.packages[8].statuses[0].status,
        )

    def test_status_with_reason_in_parens(self):
        """Test status with reason in parentheses."""
        content = (
            "project1/jammy_pkg1: ignored (end of life, was needed)\n"
            "project1/focal_pkg2: released (1.2.3-4ubuntu5)\n"
        )

        record = CPPARecord.from_str(content.encode())

        self.assertEqual(2, len(record.packages))
        self.assertEqual(
            "end of life, was needed",
            record.packages[0].statuses[0].reason,
        )
        self.assertEqual(
            "1.2.3-4ubuntu5", record.packages[1].statuses[0].reason
        )

    def test_empty_file(self):
        """Test handling of empty file."""
        import tempfile

        content = ""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="", delete=False, prefix="CVE-2024-"
        ) as f:
            f.write(content)
            temp_path = Path(f.name)

        try:
            record = CPPARecord.load(temp_path)
            self.assertTrue(record.candidate.startswith("CVE-"))
            self.assertEqual(0, len(record.packages))
        finally:
            temp_path.unlink()

    def test_missing_cve_id(self):
        """Test handling of file without CVE- prefix in filename."""
        import tempfile

        content = "project1/jammy_pkg: needed\n"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="", delete=False, prefix="INVALID-"
        ) as f:
            f.write(content)
            temp_path = Path(f.name)

        try:
            error = self.assertRaises(ValueError, CPPARecord.load, temp_path)
            self.assertIn("must start with 'CVE-'", str(error))
        finally:
            temp_path.unlink()

    def test_invalid_status(self):
        """Test parsing of invalid status should skip with warning."""
        content = "project1/jammy_pkg: invalid-status\n"

        record = CPPARecord.from_str(content.encode())
        self.assertTrue(record.candidate.startswith("CVE-"))
        self.assertEqual(0, len(record.packages))  # Line was skipped

    def test_malformed_line_no_underscore(self):
        """Test handling of malformed line without underscore."""
        content = "jammypkg: needed\n"

        record = CPPARecord.from_str(content.encode())
        self.assertTrue(record.candidate.startswith("CVE-"))
        self.assertEqual(0, len(record.packages))  # Line was skipped

    def test_malformed_line_no_colon(self):
        """Test handling of malformed line without colon."""
        content = "jammy_pkg needed\n"

        record = CPPARecord.from_str(content.encode())
        self.assertTrue(record.candidate.startswith("CVE-"))
        self.assertEqual(0, len(record.packages))  # Line was skipped

    def test_whitespace_handling(self):
        """Test handling of extra whitespace."""
        content = (
            "  project1/jammy_pkg1: needed  \n"
            "project1/focal_pkg2: released (1.0)  \n"
            "\n"  # Empty line
            "project1/bionic_pkg3: pending (2.0)\n"
        )

        record = CPPARecord.from_str(content.encode())

        self.assertTrue(record.candidate.startswith("CVE-"))
        self.assertEqual(3, len(record.packages))
        self.assertEqual("pkg1", record.packages[0].name)
        self.assertEqual("pkg2", record.packages[1].name)
        self.assertEqual("pkg3", record.packages[2].name)

    def test_duplicate_package_series_combinations(self):
        """Test that a duplicate series/package combination raises an error."""
        content = (
            "project1/jammy_pkg: needed\n"
            "project1/focal_pkg: released (1.0)\n"
            "project1/jammy_pkg: released (2.0)\n"  # Duplicate
        )

        self.assertRaises(ValueError, CPPARecord.from_str, content.encode())

    def test_round_trip_consistency(self):
        """Test that load->save->load produces identical results."""
        load_from = Path(__file__).parent / "sampledata" / "CVE-2013-1953"
        original = CPPARecord.load(load_from)

        temp_dir = Path(self.makeTemporaryDirectory())
        saved_path = original.save(temp_dir)

        reloaded = CPPARecord.load(saved_path)

        self.assertDictEqual(original.__dict__, reloaded.__dict__)

    def test_case_sensitivity(self):
        """Test that package names and series preserve case."""
        content = "Project1/jammy_MyPackage: needed\n"

        record = CPPARecord.from_str(content.encode())

        self.assertTrue(record.candidate.startswith("CVE-"))
        self.assertEqual("MyPackage", record.packages[0].name)
        self.assertEqual(
            "Project1/jammy", record.packages[0].statuses[0].series
        )
