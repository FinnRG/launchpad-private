# Copyright 2010-2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from urllib.parse import urlparse

from lazr.restful.interfaces import IJSONRequestCache
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.bugs.browser.bug import BugInformationTypePortletView, BugView
from lp.services.webapp.publisher import canonical_url
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.testing import TestCaseWithFactory, login
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.pages import extract_text, find_tag_by_id
from lp.testing.views import create_initialized_view


class TestBugView(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        login("test@canonical.com")
        self.bug = self.factory.makeBug()
        self.view = BugView(self.bug, LaunchpadTestRequest())

    def test_regular_attachments_dont_include_invalid_records(self):
        # As reported in bug 542274, rendering the link to bug
        # attchments that do not have a LibraryFileContent record,
        # leads to an OOPS. Ensure that such attachments do not appear
        # in BugViewMixin.regular_attachments and BugViewMixin.patches.
        self.factory.makeBugAttachment(
            bug=self.bug, description="regular attachment", is_patch=False
        )
        attachment = self.factory.makeBugAttachment(
            bug=self.bug, description="bad regular attachment", is_patch=False
        )
        removeSecurityProxy(attachment.libraryfile).content = None
        self.assertEqual(
            ["regular attachment"],
            [attachment.title for attachment in self.view.regular_attachments],
        )

    def test_patches_dont_include_invalid_records(self):
        # As reported in bug 542274, rendering the link to bug
        # attchments that do not have a LibraryFileContent record,
        # leads to an OOPS. Ensure that such attachments do not appear
        # in BugViewMixin.regular_attachments and BugViewMixin.patches.
        self.factory.makeBugAttachment(
            bug=self.bug, description="patch", is_patch=True
        )
        patch = self.factory.makeBugAttachment(
            bug=self.bug, description="bad patch", is_patch=True
        )
        removeSecurityProxy(patch.libraryfile).content = None
        self.assertEqual(
            ["patch"],
            [attachment.title for attachment in self.view.patches],
        )


def _text(tag):
    return " ".join(extract_text(tag).split())


class TestBugViewPresences(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        login("test@canonical.com")
        self.bug = self.factory.makeBug()

    def test_presence_portlet_data(self):
        spn = self.factory.makeSourcePackageName()
        break_fix_data = [{"break": "abc", "fix": "def"}, {"break": "123"}]
        self.factory.makeBugPresence(
            bug=self.bug,
            source_package_name=spn,
            break_fix_data=break_fix_data,
        )
        view = BugView(self.bug, LaunchpadTestRequest())
        self.assertEqual(
            [
                {
                    "source_package_name": spn,
                    "product": None,
                    "distribution": None,
                    "git_repository": None,
                    "break_fix": [
                        {"break": "abc", "fix": "def"},
                        {"break": "123", "fix": None},
                    ],
                }
            ],
            view.presence_portlet_data,
        )

    def _getPortlet(self):
        view = create_initialized_view(self.bug, "+portlet-presence")
        portlet = find_tag_by_id(view.render(), "portlet-presence")
        self.assertIsNotNone(portlet)
        return portlet

    def test_portlet_source_package(self):
        spn = self.factory.makeSourcePackageName()
        self.factory.makeBugPresence(
            bug=self.bug,
            source_package_name=spn,
            break_fix_data=[{"break": "abc", "fix": "def"}],
        )
        portlet = self._getPortlet()
        self.assertEqual("Bug presence", portlet.h2.string.strip())
        items = portlet.ul.find_all("li", recursive=False)
        self.assertEqual(1, len(items))
        self.assertEqual(
            "Source package: " + spn.name, _text(items[0].find("div"))
        )
        self.assertEqual(spn.name, items[0].find("strong").string)
        self.assertEqual(
            ["abc", "def"], [c.string for c in items[0].find_all("code")]
        )
        self.assertEqual("Broken: abc Fixed: def", _text(items[0].ul.li))

    def test_portlet_link_targets(self):
        product = self.factory.makeProduct()
        distribution = self.factory.makeDistribution()
        repository = self.factory.makeGitRepository()
        for target in (
            {"product": product},
            {"distribution": distribution},
            {"git_repository": repository},
        ):
            self.factory.makeBugPresence(bug=self.bug, **target)
        portlet = self._getPortlet()
        items = portlet.ul.find_all("li", recursive=False)
        self.assertEqual(3, len(items))
        for item, label, obj in zip(
            items,
            ("Product:", "Distribution:", "Repository:"),
            (product, distribution, repository),
        ):
            div = item.find("div")
            self.assertTrue(_text(div).startswith(label))
            self.assertEqual(
                urlparse(canonical_url(obj)).path,
                urlparse(div.find("a")["href"]).path,
            )
            self.assertIsNone(item.ul)

    def test_portlet_break_fix_without_fix(self):
        self.factory.makeBugPresence(
            bug=self.bug,
            product=self.factory.makeProduct(),
            break_fix_data=[{"break": "123"}],
        )
        item = self._getPortlet().ul.li
        self.assertEqual("Broken: 123", _text(item.ul.li))

    def test_portlet_view_class(self):
        view = create_initialized_view(self.bug, "+portlet-presence")
        self.assertIsInstance(view, BugView)

    def test_portlet_hidden_without_presences(self):
        view = create_initialized_view(self.bug, "+portlet-presence")
        self.assertNotIn("portlet-presence", view.render())


class TestBugInformationTypePortletView(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        login("test@canonical.com")
        self.bug = self.factory.makeBug()
        self.view = BugInformationTypePortletView(
            self.bug, LaunchpadTestRequest()
        )

    def test_information_type(self):
        self.bug.transitionToInformationType(
            InformationType.USERDATA, self.bug.owner
        )
        self.assertEqual(
            self.bug.information_type.title, self.view.information_type
        )
        self.assertEqual(
            self.bug.information_type.description,
            self.view.information_type_description,
        )

    def test_information_type_css_class(self):
        self.bug.transitionToInformationType(
            InformationType.USERDATA, self.bug.owner
        )
        self.assertEqual("sprite private", self.view.information_type_css)
        self.bug.transitionToInformationType(
            InformationType.PUBLICSECURITY, self.bug.owner
        )
        self.assertEqual("sprite public", self.view.information_type_css)

    def test_proprietary_excluded_for_normal_projects(self):
        # The Proprietary information type isn't in the JSON request cache for
        # normal projects without proprietary bugs configured.
        self.view.initialize()
        cache = IJSONRequestCache(self.view.request)
        expected = [
            InformationType.PUBLIC.name,
            InformationType.PUBLICSECURITY.name,
            InformationType.PRIVATESECURITY.name,
            InformationType.USERDATA.name,
        ]
        self.assertContentEqual(
            expected,
            [
                type["value"]
                for type in cache.objects["information_type_data"].values()
            ],
        )
