# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for CVE views."""

from zope.security.proxy import removeSecurityProxy

from lp.services.beautifulsoup import BeautifulSoup
from lp.testing import TestCaseWithFactory
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.views import create_initialized_view


class TestCveIndexView(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def makeView(self, cvss):
        cve = self.factory.makeCVE("2026-0001")
        removeSecurityProxy(cve).cvss = cvss
        return create_initialized_view(cve, "+index")

    def test_cvss_entries(self):
        view = self.makeView(
            [
                {
                    "format": "CVSS",
                    "cvssV3_1": {
                        "version": "3.1",
                        "baseScore": 9.8,
                        "baseSeverity": "CRITICAL",
                        "vectorString": (
                            "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
                        ),
                    },
                },
                {
                    "cvssV2_0": {
                        "version": "2.0",
                        "baseScore": 7.5,
                        "vectorString": "AV:N/AC:L/Au:N/C:P/I:P/A:P",
                    }
                },
            ]
        )
        self.assertEqual(
            [
                {
                    "authority": "CNA",
                    "score": "9.8",
                    "severity": "CRITICAL",
                    "vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
                },
                {
                    "authority": "CNA",
                    "score": "7.5",
                    "severity": None,
                    "vector": "AV:N/AC:L/Au:N/C:P/I:P/A:P",
                },
            ],
            view.cvss_entries,
        )

    def test_cvss_entries_skips_missing_vector(self):
        view = self.makeView([{"cvssV3_1": {"baseScore": 5.0}}])
        self.assertEqual([], view.cvss_entries)

    def test_cvss_entries_empty(self):
        self.assertEqual([], self.makeView(None).cvss_entries)
        self.assertEqual([], self.makeView([]).cvss_entries)

    def test_cvss_entries_ignores_other_shapes(self):
        self.assertEqual(
            [],
            self.makeView(
                {"nvd": ["CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H [9.8]"]}
            ).cvss_entries,
        )
        self.assertEqual(
            [],
            self.makeView(
                ["text", {"other": {}}, {"cvssV3_1": "bad"}]
            ).cvss_entries,
        )

    def test_page_shows_cvss_section(self):
        view = self.makeView(
            [
                {
                    "cvssV3_1": {
                        "version": "3.1",
                        "baseScore": 9.8,
                        "baseSeverity": "CRITICAL",
                        "vectorString": "CVSS:3.1/AV:N",
                    }
                }
            ]
        )
        soup = BeautifulSoup(view.render())
        section = soup.find("div", id="cvss")
        cells = [td.get_text() for td in section.find_all("td")]
        self.assertEqual(["CNA", "9.8", "CRITICAL", "CVSS:3.1/AV:N"], cells)

    def test_page_hides_cvss_section_without_cvss(self):
        soup = BeautifulSoup(self.makeView(None).render())
        self.assertIsNone(soup.find("div", id="cvss"))
