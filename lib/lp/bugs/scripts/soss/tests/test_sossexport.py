from pathlib import Path

from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.bugs.interfaces.bugtask import BugTaskImportance, BugTaskStatus
from lp.bugs.interfaces.cve import ICveSet
from lp.bugs.scripts.soss import SOSSRecord
from lp.bugs.scripts.soss.sossexport import SOSSExporter
from lp.bugs.scripts.soss.sossimport import SOSSImporter
from lp.testing import TestCaseWithFactory
from lp.testing.layers import LaunchpadZopelessLayer


class TestSOSSExporter(TestCaseWithFactory):

    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.cve_set = getUtility(ICveSet)
        self.bug_importer = getUtility(ILaunchpadCelebrities).bug_importer

        self.sampledata = Path(__file__).parent / "sampledata"
        self.factory.makePerson(name="octagalland")
        self.soss = self.factory.makeDistribution(
            name="soss",
            displayname="SOSS",
            information_type=InformationType.PROPRIETARY,
        )
        self._makeCves()

        self.soss_importer = SOSSImporter(self.soss)
        self.soss_exporter = SOSSExporter()

    def _makeCves(self):
        for file in self.sampledata.iterdir():
            cve_sequence = file.name
            if not self.cve_set[cve_sequence]:
                self.factory.makeCVE(sequence=cve_sequence)

    def test_get_packages(self):
        """Test get SOSSRecord.Package list from bugtasks."""
        for file in self.sampledata.iterdir():
            with open(file) as f:
                soss_record = SOSSRecord.from_yaml(f)

            bug, _, _ = self.soss_importer.import_cve_from_file(file)

            naked_bug = removeSecurityProxy(bug)
            packages = self.soss_exporter._get_packages(naked_bug.bugtasks)
            self.assertEqual(soss_record.packages, packages)

    def test_get_cvss(self):
        """Test get SOSSRecord.CVSS list from vulnerability.cvss."""
        for file in self.sampledata.iterdir():
            cve_sequence = file.name
            if not self.cve_set[cve_sequence]:
                self.factory.makeCVE(sequence=cve_sequence)

            with open(file) as f:
                soss_record = SOSSRecord.from_yaml(f)

            _, vulnerability, _ = self.soss_importer.import_cve_from_file(file)
            naked_vulnerability = removeSecurityProxy(vulnerability)
            cvss = self.soss_exporter._get_cvss(naked_vulnerability.cvss)

            self.assertEqual(soss_record.cvss, cvss)

    def test_to_record(self):
        """Test that imported and exported SOSSRecords match."""
        soss_importer = SOSSImporter(
            self.soss, information_type=InformationType.PROPRIETARY
        )

        for file in self.sampledata.iterdir():
            with open(file) as f:
                soss_record = SOSSRecord.from_yaml(f)

            bug, vulnerability, _ = soss_importer.import_cve_from_file(file)
            exported = self.soss_exporter.to_record(bug, vulnerability)

            self.assertEqual(soss_record, exported)

    def test_to_record_defaults(self):
        """Test that exported SOSSRecords have a default BugTask.status and
        Vulnerability.importance. There are some values that don't have an
        equivalent, so we set the default one. Those values won't be set by the
        importer, but a manual change can set them."""
        soss_importer = SOSSImporter(
            self.soss, information_type=InformationType.PROPRIETARY
        )

        file = next(self.sampledata.iterdir())
        bug, vulnerability, _ = soss_importer.import_cve_from_file(file)

        bugtask = bug.bugtasks[0]

        # These values are not in PACKAGE_STATUS_MAP or PRIORITY_ENUM_MAP
        bugtask.transitionToStatus(BugTaskStatus.CONFIRMED)
        vulnerability.importance = BugTaskImportance.UNKNOWN

        # This does not raise an Exception as we are using defaults
        exported = self.soss_exporter.to_record(bug, vulnerability)
        self.assertTrue(isinstance(exported, SOSSRecord))

    def test_import_export(self):
        """Integration test that checks that cve files imported and exported
        match."""
        soss_importer = SOSSImporter(
            self.soss, information_type=InformationType.PROPRIETARY
        )

        for file in self.sampledata.iterdir():

            bug, vulnerability, _ = soss_importer.import_cve_from_file(file)
            exported = self.soss_exporter.to_record(bug, vulnerability)

            with open(file) as f:
                self.assertEqual(f.read(), exported.to_str())
