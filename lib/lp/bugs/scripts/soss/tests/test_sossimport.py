from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import transaction
from zope.component import getUtility

from lp.app.enums import InformationType
from lp.app.errors import NotFoundError
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.bugs.interfaces.bugtask import BugTaskImportance, BugTaskStatus
from lp.bugs.scripts.soss import SOSSRecord
from lp.bugs.scripts.soss.sossimport import SOSSImporter
from lp.registry.interfaces.externalpackage import ExternalPackageType
from lp.registry.interfaces.sourcepackagename import ISourcePackageNameSet
from lp.testing import TestCaseWithFactory, person_logged_in
from lp.testing.layers import LaunchpadZopelessLayer


class TestSOSSImporter(TestCaseWithFactory):

    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.sampledata = Path(__file__).parent / "sampledata"
        self.file = self.sampledata / "CVE-2025-1979"

        with open(self.file, encoding="utf-8") as file:
            self.soss_record = SOSSRecord.from_yaml(file)

        self.cve = self.factory.makeCVE(sequence="2025-1979")
        self.owner = self.factory.makePerson()
        self.soss = self.factory.makeDistribution(
            name="soss",
            displayname="SOSS",
            owner=self.owner,
            information_type=InformationType.PROPRIETARY,
        )
        transaction.commit()

        self.bug_importer = getUtility(ILaunchpadCelebrities).bug_importer
        self.janitor = getUtility(ILaunchpadCelebrities).janitor
        self.source_package_name_set = getUtility(ISourcePackageNameSet)

        # Set up references
        self.description = (
            "Versions of the package ray before 2.43.0 are vulnerable to "
            "Insertion of Sensitive Information into Log File where the redis "
            "password is being logged in the standard logging. If the redis "
            "password is passed as an argument, it will be logged and could "
            "potentially leak the password.\r\rThis is only exploitable if:"
            "\r\r1) Logging is enabled;\r\r2) Redis is using password "
            "authentication;\r\r3) Those logs are accessible to an attacker, "
            "who can reach that redis instance.\r\r**Note:**\r\rIt is "
            "recommended that anyone who is running in this configuration "
            "should update to the latest version of Ray, then rotate their "
            "redis password.\n\n"
            "References:\n"
            "https://github.com/ray-project/ray/commit/"
            "64a2e4010522d60b90c389634f24df77b603d85d\n"
            "https://github.com/ray-project/ray/issues/50266\n"
            "https://github.com/ray-project/ray/pull/50409\n"
            "https://security.snyk.io/vuln/SNYK-PYTHON-RAY-8745212\n"
            "https://ubuntu.com/security/notices/SSN-148-1.json"
            "?show_hidden=true"
        )

        # Set up reference for bugtasks
        pyyaml = self.source_package_name_set.getOrCreateByName("pyyaml")
        ray = self.source_package_name_set.getOrCreateByName("ray")
        vllm = self.source_package_name_set.getOrCreateByName("vllm")

        self.bugtask_reference = [
            (
                self.soss.getExternalPackage(
                    name=pyyaml,
                    packagetype=ExternalPackageType.PYTHON,
                    channel=("jammy:2.22.0", "stable"),
                ),
                BugTaskStatus.INVALID,
                "",
                {"repositories": ("nvidia-pb3-python-stable-local",)},
            ),
            (
                self.soss.getExternalPackage(
                    name=ray,
                    packagetype=ExternalPackageType.CARGO,
                    channel=("focal:0.27.0", "stable"),
                ),
                BugTaskStatus.DEFERRED,
                "2.22.0+soss.1",
                {"repositories": ("nvidia-pb3-python-stable-local",)},
            ),
            (
                self.soss.getExternalPackage(
                    name=ray,
                    packagetype=ExternalPackageType.CONDA,
                    channel=("jammy:1.17.0", "stable"),
                ),
                BugTaskStatus.INVALID,
                "2.22.0+soss.1",
                {"repositories": ("nvidia-pb3-python-stable-local",)},
            ),
            (
                self.soss.getExternalPackage(
                    name=ray,
                    packagetype=ExternalPackageType.PYTHON,
                    channel=("jammy:2.22.0", "stable"),
                ),
                BugTaskStatus.FIXRELEASED,
                "2.22.0+soss.1",
                {"repositories": ("nvidia-pb3-python-stable-local",)},
            ),
            (
                self.soss.getExternalPackage(
                    name=vllm,
                    packagetype=ExternalPackageType.GENERIC,
                    channel=("noble:0.7.3", "stable"),
                ),
                BugTaskStatus.NEW,
                "",
                {"repositories": ("soss-src-stable-local",)},
            ),
            (
                self.soss.getExternalPackage(
                    name=vllm,
                    packagetype=ExternalPackageType.MAVEN,
                    channel=("noble:0.7.3", "stable"),
                ),
                BugTaskStatus.UNKNOWN,
                "",
                {"repositories": ("soss-src-stable-local",)},
            ),
        ]

        self.cvss = {
            "report@snyk.io": [
                {
                    "source": "report@snyk.io",
                    "vector": "CVSS:3.1/AV:L/AC:H/PR:L/UI:N/S:C/C:H/I:L/A:N",
                    "baseScore": 6.4,
                    "baseSeverity": "MEDIUM",
                }
            ],
            "security-advisories@github.com": [
                {
                    "source": "security-advisories@github.com",
                    "vector": "CVSS:3.1/AV:A/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H",
                    "baseScore": 9.0,
                    "baseSeverity": "CRITICAL",
                }
            ],
        }

        self.importance_explanation = (
            "Unrealistic exploitation scenario. Logs are stored locally "
            "and not transferred between agents, so local log access is "
            "the only conceivable method to view the password for the "
            "redis instance (i.e., no possibility of MitM to access the "
            "logs). Given the requirement for priviledged system access "
            'to access log files the real "danger" posed by the '
            "vulnerability is quite low, and that is reflected in this "
            "priority assignment. "
        )

        self.notes = (
            "username: since 1.0, a package issues a warning when text() is "
            "omitted this fix is not important, marking priority as low\n\n"
            "username: since 1.0, a package issues a warning when text() is "
            "omitted this fix is not important, marking priority as low\n\n"
            "sample note 2"
        )

        self.metadata = {
            "extra_attrs": {
                "Custom_attr_1_nvd_score_arrivial_date": "2025-08-03",
                "Custom_attr_2_assigned_time": "2025-08-10T11:22:33Z",
            }
        }

    def _check_bugtasks(
        self, bugtasks, bugtask_reference, importance, assignee
    ):
        self.assertEqual(len(bugtasks), len(bugtask_reference))

        for i, (target, status, status_explanation, metadata) in enumerate(
            bugtask_reference
        ):
            self.assertEqual(bugtasks[i].target, target)
            self.assertEqual(bugtasks[i].status, status)
            self.assertEqual(
                bugtasks[i].status_explanation, status_explanation
            )
            self.assertEqual(bugtasks[i].importance, importance)
            self.assertEqual(bugtasks[i].assignee, assignee)
            self.assertEqual(bugtasks[i].metadata, metadata)

    def _check_bug_fields(self, bug, bugtask_reference):
        """Helper function to check the imported bug"""
        self.assertEqual(bug.description, self.description)
        self.assertEqual(bug.title, self.cve.sequence)
        self.assertEqual(bug.information_type, InformationType.PROPRIETARY)
        self.assertEqual(bug.owner, self.bug_importer)

        self._check_bugtasks(
            bug.bugtasks,
            bugtask_reference,
            BugTaskImportance.LOW,
            self.janitor,
        )

    def _check_vulnerability_fields(self, vulnerability, bug):
        """Helper function to check the imported vulnerability"""
        self.assertEqual(vulnerability.distribution, self.soss)
        self.assertEqual(
            vulnerability.date_created.date(), datetime.utcnow().date()
        )
        self.assertEqual(
            vulnerability.date_made_public,
            datetime.fromisoformat("2025-03-06T05:15:16.213+00:00"),
        )
        self.assertEqual(vulnerability.date_notice_issued, None)
        self.assertEqual(vulnerability.date_coordinated_release, None)
        self.assertEqual(
            vulnerability.information_type, InformationType.PROPRIETARY
        )
        self.assertEqual(vulnerability.importance, BugTaskImportance.LOW)
        self.assertEqual(
            vulnerability.importance_explanation,
            self.importance_explanation,
        )
        self.assertEqual(vulnerability.creator, self.bug_importer)
        self.assertEqual(vulnerability.notes, self.notes)
        self.assertEqual(vulnerability.mitigation, None)
        self.assertEqual(vulnerability.cve, self.cve)

        self.assertEqual(vulnerability.cvss, self.cvss)
        self.assertEqual(vulnerability.metadata, self.metadata)

        self.assertEqual(len(vulnerability.bugs), 1)
        self.assertEqual(vulnerability.bugs[0], bug)

    def test_import_cve_from_file(self):
        """Test import a SOSS cve from file. It also imports again to check
        that we don't create duplicates."""
        file = self.sampledata / "CVE-2025-1979"

        soss_importer = SOSSImporter(
            self.soss, information_type=InformationType.PROPRIETARY
        )
        bug, vulnerability, created = soss_importer.import_cve_from_file(file)
        self.assertEqual(created, True)

        # Check bug fields
        self._check_bug_fields(bug, self.bugtask_reference)

        # Check vulnerability
        self._check_vulnerability_fields(vulnerability, bug)

        # Import again to check that it doesn't create new objects
        bug_copy, vulnerability_copy, created = (
            soss_importer.import_cve_from_file(file)
        )
        self.assertEqual(bug, bug_copy)
        self.assertEqual(vulnerability, vulnerability_copy)
        self.assertEqual(created, False)

    def test_create_update_bug(self):
        """Test create and update a bug from a SOSS cve file"""
        soss_importer = SOSSImporter(self.soss)
        bug = soss_importer._create_bug(self.soss_record, self.cve)
        soss_importer._create_or_update_bugtasks(bug, self.soss_record)

        self._check_bug_fields(bug, self.bugtask_reference)

        # Modify the soss_record and check that the bug changed
        new_cve = self.factory.makeCVE("2025-1234")

        self.soss_record.description = "New sample description"
        new_description = (
            f"{self.soss_record.description}\n\n"
            "References:\n"
            "https://github.com/ray-project/ray/commit/"
            "64a2e4010522d60b90c389634f24df77b603d85d\n"
            "https://github.com/ray-project/ray/issues/50266\n"
            "https://github.com/ray-project/ray/pull/50409\n"
            "https://security.snyk.io/vuln/SNYK-PYTHON-RAY-8745212\n"
            "https://ubuntu.com/security/notices/SSN-148-1.json"
            "?show_hidden=true"
        )

        self.soss_record.packages.pop(SOSSRecord.PackageTypeEnum.UNPACKAGED)
        self.soss_record.packages.pop(SOSSRecord.PackageTypeEnum.MAVEN)
        self.soss_record.packages.pop(SOSSRecord.PackageTypeEnum.PYTHON)

        soss_importer = SOSSImporter(
            self.soss, information_type=InformationType.PROPRIETARY
        )
        bug = soss_importer._update_bug(bug, self.soss_record, new_cve)
        soss_importer._create_or_update_bugtasks(bug, self.soss_record)
        transaction.commit()

        # Check bug fields
        self.assertEqual(bug.description, new_description)
        self.assertEqual(bug.title, new_cve.sequence)
        self.assertEqual(bug.information_type, InformationType.PROPRIETARY)

        # Check bugtasks
        bugtasks = bug.bugtasks
        bugtask_reference = self.bugtask_reference[1:3]
        self._check_bugtasks(
            bugtasks, bugtask_reference, BugTaskImportance.LOW, self.janitor
        )

    def test_create_update_vulnerability(self):
        """Test create and update a vulnerability from a SOSS cve file"""
        soss_importer = SOSSImporter(self.soss)
        bug = soss_importer._create_bug(self.soss_record, self.cve)
        vulnerability = soss_importer._create_vulnerability(
            self.soss_record, self.cve, self.soss
        )
        vulnerability.linkBug(bug, check_permissions=False)

        self.assertEqual(vulnerability.distribution, self.soss)
        self.assertEqual(
            vulnerability.date_created.date(), datetime.utcnow().date()
        )
        self.assertEqual(
            vulnerability.date_made_public,
            datetime.fromisoformat("2025-03-06T05:15:16.213+00:00"),
        )
        self.assertEqual(vulnerability.date_notice_issued, None)
        self.assertEqual(vulnerability.date_coordinated_release, None)
        self.assertEqual(
            vulnerability.information_type, InformationType.PROPRIETARY
        )
        self.assertEqual(vulnerability.importance, BugTaskImportance.LOW)
        self.assertEqual(
            vulnerability.importance_explanation,
            self.importance_explanation,
        )
        self.assertEqual(vulnerability.creator, self.bug_importer)
        self.assertEqual(
            vulnerability.notes,
            self.notes,
        )
        self.assertEqual(vulnerability.mitigation, None)
        self.assertEqual(vulnerability.cve, self.cve)

        self.assertEqual(vulnerability.cvss, self.cvss)

        self.assertEqual(len(vulnerability.bugs), 1)
        self.assertEqual(vulnerability.bugs[0], bug)

    def test_create_or_update_bugtasks(self):
        """Test that bugtasks are updated, removed, and modified correctly."""
        soss_importer = SOSSImporter(self.soss)
        bug = soss_importer._create_bug(self.soss_record, self.cve)
        soss_importer._create_or_update_bugtasks(bug, self.soss_record)

        self._check_bugtasks(
            bug.bugtasks,
            self.bugtask_reference,
            BugTaskImportance.LOW,
            self.janitor,
        )

        ray = self.source_package_name_set.getOrCreateByName("ray")
        vllm = self.source_package_name_set.getOrCreateByName("vllm")

        # Remove the PYTHON packages
        self.soss_record.packages.pop(SOSSRecord.PackageTypeEnum.PYTHON)

        # The two PYTHON packages are gone, and the CONDA package is modified.
        expected_updated_bugtasks = [
            (
                self.soss.getExternalPackage(
                    name=ray,
                    packagetype=ExternalPackageType.CARGO,
                    channel=("focal:0.27.0", "stable"),
                ),
                BugTaskStatus.DEFERRED,
                "2.22.0+soss.1",
                {"repositories": ("nvidia-pb3-python-stable-local",)},
            ),
            (
                self.soss.getExternalPackage(
                    name=ray,
                    packagetype=ExternalPackageType.CONDA,
                    channel=("noble:5.23.1", "stable"),  # Changed channel
                ),
                BugTaskStatus.DEFERRED,  # Changed status
                "test note",  # Changed note
                {"repositories": ("test-repo",)},  # Changed repo
            ),
            (
                self.soss.getExternalPackage(
                    name=vllm,
                    packagetype=ExternalPackageType.GENERIC,
                    channel=("noble:0.7.3", "stable"),
                ),
                BugTaskStatus.NEW,
                "",
                {"repositories": ("soss-src-stable-local",)},
            ),
            (
                self.soss.getExternalPackage(
                    name=vllm,
                    packagetype=ExternalPackageType.MAVEN,
                    channel=("noble:0.7.3", "stable"),
                ),
                BugTaskStatus.UNKNOWN,
                "",
                {"repositories": ("soss-src-stable-local",)},
            ),
        ]

        # Modify the soss_record to trigger the update
        self.soss_record.assigned_to = "bug-importer"
        self.soss_record.priority = SOSSRecord.PriorityEnum.HIGH

        # Modify the CONDA package
        self.soss_record.packages[SOSSRecord.PackageTypeEnum.CONDA] = (
            SOSSRecord.Package(
                name="ray",
                channel=SOSSRecord.Channel(value="noble:5.23.1/stable"),
                repositories=["test-repo"],
                status=SOSSRecord.PackageStatusEnum.DEFERRED,
                note="test note",
            ),
        )

        # Run the update
        soss_importer._create_or_update_bugtasks(bug, self.soss_record)
        transaction.commit()

        # Check against the new expected list
        self._check_bugtasks(
            bug.bugtasks,
            expected_updated_bugtasks,
            BugTaskImportance.HIGH,
            self.bug_importer,
        )

    def test_get_launchpad_cve(self):
        """Test get a cve from Launchpad"""
        soss_importer = SOSSImporter(self.soss)
        self.assertEqual(
            soss_importer._get_launchpad_cve("2025-1979"), self.cve
        )
        self.assertRaisesWithContent(
            NotFoundError,
            "'Could not find 2000-1111 in LP'",
            soss_importer._get_launchpad_cve,
            "2000-1111",
        )

    def test_make_bug_description(self):
        """Test make a bug description from a SOSSRecord"""
        description = SOSSImporter(self.soss)._make_bug_description(
            self.soss_record
        )
        self.assertEqual(description, self.description)

    def test_get_assignee(self):
        """Test get an assignee person from Launchpad"""
        soss_importer = SOSSImporter(self.soss)

        janitor = soss_importer._get_assignee("janitor")
        self.assertEqual(janitor, self.janitor)
        nonexistent = soss_importer._get_assignee("nonexistent")
        self.assertEqual(nonexistent, None)

    def test_get_or_create_external_package(self):
        """Test create an ExternalPackage from SOSSRecord"""
        soss_importer = SOSSImporter(self.soss)

        cargo_pkg = soss_importer._get_or_create_external_package(
            self.soss_record.packages[SOSSRecord.PackageTypeEnum.RUST][0],
            SOSSRecord.PackageTypeEnum.RUST,
        )
        self.assertEqual(cargo_pkg, self.bugtask_reference[1][0])

        generic_pkg = soss_importer._get_or_create_external_package(
            self.soss_record.packages[SOSSRecord.PackageTypeEnum.UNPACKAGED][
                0
            ],
            SOSSRecord.PackageTypeEnum.UNPACKAGED,
        )
        self.assertEqual(generic_pkg, self.bugtask_reference[4][0])

        maven_pkg = soss_importer._get_or_create_external_package(
            self.soss_record.packages[SOSSRecord.PackageTypeEnum.MAVEN][0],
            SOSSRecord.PackageTypeEnum.MAVEN,
        )
        self.assertEqual(maven_pkg, self.bugtask_reference[5][0])

    def test_prepare_cvss_data(self):
        """Test prepare the cvss json"""
        cvss = SOSSImporter(self.soss)._prepare_cvss_data(self.soss_record)
        self.assertEqual(cvss, self.cvss)

    def test_validate_soss_record(self):
        """Test validate the SOSSRecord"""
        soss_importer = SOSSImporter(self.soss)
        valid = soss_importer._validate_soss_record(
            self.soss_record, self.cve.sequence
        )
        self.assertEqual(valid, True)

        # SOSSRecord without packages is not valid
        self.soss_record.packages = {}
        self.assertRaisesWithContent(
            ValueError,
            "CVE-2025-1979: has no affected packages",
            soss_importer._validate_soss_record,
            self.soss_record,
            self.cve.sequence,
        )

        # SOSSRecord with candidate != sequence is not valid
        self.soss_record.candidate = "nonvalid"
        self.assertRaisesWithContent(
            ValueError,
            "CVE sequence mismatch: nonvalid != CVE-2025-1979",
            soss_importer._validate_soss_record,
            self.soss_record,
            self.cve.sequence,
        )

    def test_checkUserPermissions(self):
        soss_importer = SOSSImporter(self.soss)

        user = self.factory.makePerson()
        self.assertEqual(soss_importer.checkUserPermissions(user), False)
        self.assertEqual(soss_importer.checkUserPermissions(self.owner), True)

    def test_update_extra_attrs(self):
        """Test updating an already existing extra_attrs field"""

        soss_importer = SOSSImporter(self.soss)

        bug = soss_importer._create_bug(self.soss_record, self.cve)
        vulnerability = soss_importer._create_vulnerability(
            self.soss_record, self.cve, self.soss
        )
        vulnerability.linkBug(bug, check_permissions=False)

        self._check_vulnerability_fields(vulnerability, bug)

        # Modify the soss_record and check that the field has changed
        self.soss_record.extra_attrs = {"test_attr": "test_value"}

        soss_importer = SOSSImporter(
            self.soss, information_type=InformationType.PROPRIETARY
        )
        bug = soss_importer._update_bug(bug, self.soss_record, self.cve)
        vulnerability = soss_importer._update_vulnerability(
            vulnerability, self.soss_record
        )
        transaction.commit()

        # Update the reference as well
        self.metadata["extra_attrs"] = {"test_attr": "test_value"}

        self._check_vulnerability_fields(vulnerability, bug)

    def test_update_extra_attrs_with_no_value(self):
        """Test putting an empty extra_attrs field when one already exists"""

        soss_importer = SOSSImporter(self.soss)

        bug = soss_importer._create_bug(self.soss_record, self.cve)
        vulnerability = soss_importer._create_vulnerability(
            self.soss_record, self.cve, self.soss
        )
        vulnerability.linkBug(bug, check_permissions=False)

        self._check_vulnerability_fields(vulnerability, bug)

        # Modify the soss_record and check that the metadata field has changed
        self.soss_record.extra_attrs = None

        soss_importer = SOSSImporter(
            self.soss, information_type=InformationType.PROPRIETARY
        )
        bug = soss_importer._update_bug(bug, self.soss_record, self.cve)
        vulnerability = soss_importer._update_vulnerability(
            vulnerability, self.soss_record
        )
        transaction.commit()

        # Update the reference as well
        self.metadata = None

        self._check_vulnerability_fields(vulnerability, bug)

    def test_from_record_no_partial_write_on_bug_creation_failure(self):
        """from_record() must not leave an orphaned vulnerability if bug
        creation fails.

        SOSS creates the vulnerability before the bug, so a bug creation
        failure would leave an orphaned vulnerability. ImportVulnerabilityJob
        always calls transaction.commit() in its finally block, so without
        the transaction.abort() in from_record()'s except block the orphan
        would be committed. This test verifies the abort prevents that.
        """
        soss_importer = SOSSImporter(
            self.soss, information_type=InformationType.PROPRIETARY
        )

        with patch.object(
            SOSSImporter, "_create_bug", side_effect=RuntimeError("boom")
        ):
            self.assertRaises(
                RuntimeError,
                soss_importer.from_record,
                self.soss_record,
                "CVE-2025-1979",
            )

        # The vulnerability created before _create_bug was called must have
        # been rolled back by the transaction.abort() in the except block.

        admin = getUtility(ILaunchpadCelebrities).admin
        with person_logged_in(admin):
            self.assertEqual(len(list(self.cve.vulnerabilities)), 0)
        self.assertEqual(len(self.cve.bugs), 0)
