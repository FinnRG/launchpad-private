# Copyright 2014-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import soupmatchers
from fixtures import FakeLogger
from testtools.matchers import MatchesSetwise, MatchesStructure, Not
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.beautifulsoup import BeautifulSoup
from lp.services.features.testing import FeatureFixture
from lp.services.webapp import canonical_url
from lp.services.webapp.authorization import check_permission
from lp.soyuz.browser.archive import ArchiveIndexActionsMenu
from lp.soyuz.enums import PackagePublishingStatus
from lp.soyuz.interfaces.archive import (
    DEFAULT_PIN_PRIORITY,
    MAXIMUM_PIN_PRIORITY,
    MINIMUM_PIN_PRIORITY,
    CannotModifyArchiveProcessor,
)
from lp.testing import (
    TestCaseWithFactory,
    admin_logged_in,
    login_person,
    person_logged_in,
    record_two_runs,
)
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.matchers import HasQueryCount
from lp.testing.pages import extract_text
from lp.testing.views import create_initialized_view


class TestArchiveIndexView(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def test_index_page_without_packages(self):
        ppa = self.factory.makeArchive()
        self.factory.makeSourcePackagePublishingHistory(
            archive=ppa, status=PackagePublishingStatus.DELETED
        )
        owner = login_person(ppa.owner)
        browser = self.getUserBrowser(canonical_url(ppa), user=owner)
        html = browser.contents
        empty_package_msg_exists = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "no packages message", "div", attrs={"id": "empty-result"}
            ),
        )
        self.assertThat(
            html,
            Not(empty_package_msg_exists),
            'Message "No matching package for (...)" should not appear',
        )


class TestArchiveEditView(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        # None of the Ubuntu series in sampledata have amd64.  Add it to
        # breezy so that it shows up in the list of available processors.
        self.ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        proc_amd64 = getUtility(IProcessorSet).getByName("amd64")
        self.factory.makeDistroArchSeries(
            distroseries=self.ubuntu.getSeries("breezy-autotest"),
            architecturetag="amd64",
            processor=proc_amd64,
        )

    def assertArchiveProcessors(self, archive, names):
        with person_logged_in(archive.owner):
            self.assertContentEqual(
                names, [processor.name for processor in archive.processors]
            )

    def assertProcessorControls(self, processors_control, enabled, disabled):
        matchers = [
            MatchesStructure.byEquality(optionValue=name, disabled=False)
            for name in enabled
        ]
        matchers.extend(
            [
                MatchesStructure.byEquality(optionValue=name, disabled=True)
                for name in disabled
            ]
        )
        self.assertThat(processors_control.controls, MatchesSetwise(*matchers))

    def test_display_processors(self):
        ppa = self.factory.makeArchive()
        owner = login_person(ppa.owner)
        browser = self.getUserBrowser(
            canonical_url(ppa) + "/+edit", user=owner
        )
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(
            ["Intel 386 (386)", "AMD 64bit (amd64)", "HPPA Processor (hppa)"],
            [extract_text(option) for option in processors.displayOptions],
        )
        self.assertContentEqual(["386", "amd64", "hppa"], processors.options)

    def test_edit_processors(self):
        ppa = self.factory.makeArchive()
        self.assertArchiveProcessors(ppa, ["386", "amd64", "hppa"])
        browser = self.getUserBrowser(
            canonical_url(ppa) + "/+edit", user=ppa.owner
        )
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(["386", "amd64", "hppa"], processors.value)
        processors.value = ["386", "amd64"]
        browser.getControl("Save").click()
        self.assertArchiveProcessors(ppa, ["386", "amd64"])

    def test_edit_with_invisible_processor(self):
        # It's possible for existing archives to have an enabled processor
        # that's no longer usable with any non-obsolete distroseries, which
        # will mean it's hidden from the UI, but the non-admin
        # Archive.setProcessors isn't allowed to disable it.  Editing the
        # processor list of such an archive leaves the invisible processor
        # intact.
        proc_386 = getUtility(IProcessorSet).getByName("386")
        proc_amd64 = getUtility(IProcessorSet).getByName("amd64")
        proc_armel = self.factory.makeProcessor(
            name="armel", restricted=True, build_by_default=False
        )
        ppa = self.factory.makeArchive()
        ppa.setProcessors([proc_386, proc_amd64, proc_armel])
        browser = self.getUserBrowser(
            canonical_url(ppa) + "/+edit", user=ppa.owner
        )
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(["386", "amd64"], processors.value)
        processors.value = ["amd64"]
        browser.getControl("Save").click()
        self.assertArchiveProcessors(ppa, ["amd64", "armel"])

    def test_edit_processors_restricted(self):
        # A restricted processor is shown with a disabled (greyed out)
        # checkbox in the UI, and the processor cannot be enabled.
        self.useFixture(FakeLogger())
        proc_armhf = self.factory.makeProcessor(
            name="armhf", restricted=True, build_by_default=False
        )
        self.factory.makeDistroArchSeries(
            distroseries=self.ubuntu.getSeries("breezy-autotest"),
            architecturetag="armhf",
            processor=proc_armhf,
        )
        ppa = self.factory.makeArchive()
        self.assertArchiveProcessors(ppa, ["386", "amd64", "hppa"])
        browser = self.getUserBrowser(
            canonical_url(ppa) + "/+edit", user=ppa.owner
        )
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(["386", "amd64", "hppa"], processors.value)
        self.assertProcessorControls(
            processors, ["386", "amd64", "hppa"], ["armhf"]
        )
        # Even if the user works around the disabled checkbox and forcibly
        # enables it, they can't enable the restricted processor.
        for control in processors.controls:
            if control.optionValue == "armhf":
                del control._control.attrs["disabled"]
        processors.value = ["386", "amd64", "armhf"]
        self.assertRaises(
            CannotModifyArchiveProcessor, browser.getControl("Save").click
        )

    def test_edit_processors_restricted_already_enabled(self):
        # A restricted processor that is already enabled is shown with a
        # disabled (greyed out) checkbox in the UI.  This causes form
        # submission to omit it, but the validation code fixes that up
        # behind the scenes so that we don't get
        # CannotModifyArchiveProcessor.
        proc_386 = getUtility(IProcessorSet).getByName("386")
        proc_amd64 = getUtility(IProcessorSet).getByName("amd64")
        proc_armhf = self.factory.makeProcessor(
            name="armhf", restricted=True, build_by_default=False
        )
        self.factory.makeDistroArchSeries(
            distroseries=self.ubuntu.getSeries("breezy-autotest"),
            architecturetag="armhf",
            processor=proc_armhf,
        )
        ppa = self.factory.makeArchive()
        ppa.setProcessors([proc_386, proc_amd64, proc_armhf])
        self.assertArchiveProcessors(ppa, ["386", "amd64", "armhf"])
        browser = self.getUserBrowser(
            canonical_url(ppa) + "/+edit", user=ppa.owner
        )
        processors = browser.getControl(name="field.processors")
        # armhf is checked but disabled.
        self.assertContentEqual(["386", "amd64", "armhf"], processors.value)
        self.assertProcessorControls(
            processors, ["386", "amd64", "hppa"], ["armhf"]
        )
        processors.value = ["386"]
        browser.getControl("Save").click()
        self.assertArchiveProcessors(ppa, ["386", "armhf"])


class ArchiveEditDependenciesTestMixin:
    """Shared fixture for the +edit-dependencies pin priority tests."""

    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.owner = self.factory.makePerson()
        self.ppa = self.factory.makeArchive(owner=self.owner)

    def addDependency(self, pin_priority=None):
        """Record a PPA dependency for the context PPA."""
        dependency = self.factory.makeArchive()
        with person_logged_in(self.owner):
            if pin_priority is None:
                self.ppa.addArchiveDependency(
                    dependency, PackagePublishingPocket.RELEASE
                )
            else:
                self.ppa.addArchiveDependency(
                    dependency,
                    PackagePublishingPocket.RELEASE,
                    pin_priority=pin_priority,
                )
        return dependency

    def addInvisibleDependency(self, pin_priority=None):
        """Record a dependency on a PPA that the owner cannot view.

        The dependency archive is made private *after* the dependency is
        recorded, so that this does not depend on whether
        addArchiveDependency permits private dependencies.
        """
        dependency = self.addDependency(pin_priority=pin_priority)
        naked_dependency = removeSecurityProxy(dependency)
        naked_dependency.private = True
        naked_dependency.buildd_secret = "sekrit"
        # Guard the premise of every test using this helper.
        with person_logged_in(self.owner):
            self.assertFalse(
                check_permission("launchpad.View", dependency),
                "The dependency should not be visible to the PPA owner.",
            )
        return dependency

    def getRecord(self, dependency):
        with person_logged_in(self.owner):
            return self.ppa.getArchiveDependency(dependency)

    def getPinPriority(self, dependency):
        return self.getRecord(dependency).pin_priority

    def makeView(self):
        return create_initialized_view(
            self.ppa, "+edit-dependencies", principal=self.owner
        )

    def renderSoup(self):
        with person_logged_in(self.owner):
            return BeautifulSoup(self.makeView().render(), "html.parser")

    def getText(self, node):
        """extract_text() with line breaks collapsed.

        The template wraps its prose across several source lines, and
        extract_text preserves those newlines.
        """
        return " ".join(extract_text(node).split())


class TestArchiveEditDependenciesRendering(
    ArchiveEditDependenciesTestMixin, TestCaseWithFactory
):
    """Tests for the pin priority markup of +edit-dependencies."""

    def test_table_exposes_priority_bounds(self):
        # The bounds are rendered from the same constants used by the form
        # and the model validation, so the JavaScript cannot drift from
        # them.
        self.addDependency()
        table = self.renderSoup().find("table", {"id": "ppa-dependencies"})
        self.assertIsNotNone(table)
        self.assertEqual(
            str(MINIMUM_PIN_PRIORITY), table["data-priority-minimum"]
        )
        self.assertEqual(
            str(MAXIMUM_PIN_PRIORITY), table["data-priority-maximum"]
        )

    def test_help_text_documents_default_and_bounds(self):
        # The tooltip quotes the same constants, and is the target of the
        # aria-describedby that the JavaScript sets on each input.
        self.addDependency()
        help_text = self.getText(
            self.renderSoup().find(id="pin-priority-help")
        )
        self.assertIn("The default is %d." % DEFAULT_PIN_PRIORITY, help_text)
        self.assertIn(
            "Allowed values: %d to %d."
            % (MINIMUM_PIN_PRIORITY, MAXIMUM_PIN_PRIORITY),
            help_text,
        )

    def test_row_renders_name_priority_and_removal_controls(self):
        # Each dependency row carries the hooks the JavaScript relies on.
        dependency = self.addDependency(pin_priority=700)
        record = self.getRecord(dependency)
        row = self.renderSoup().find("tr", {"id": "ppa-dependency-0"})
        self.assertEqual(
            record.title, extract_text(row.find(class_="dependency-name"))
        )
        priority_input = row.find("td", {"class": "pin-priority-cell"}).find(
            "input"
        )
        self.assertEqual(
            "field.priority_%d" % dependency.id, priority_input["name"]
        )
        self.assertEqual("700", priority_input["value"])
        # Rendered as a plain text input; the JavaScript upgrades it to a
        # number input, so the form must work without that upgrade.
        self.assertEqual("text", priority_input["type"])
        checkbox = row.find("input", {"class": "line-through-when-checked"})
        self.assertEqual("field.selected_dependencies", checkbox["name"])
        self.assertEqual(dependency.reference, checkbox["value"])
        # The status region starts empty so that its later population is
        # announced by the polite live region.
        status = row.find(class_="pending-removal-status")
        self.assertEqual("", extract_text(status))
        self.assertEqual("polite", status["aria-live"])
        self.assertEqual(status["id"], checkbox["aria-describedby"])

    def test_no_table_without_dependencies(self):
        soup = self.renderSoup()
        self.assertIsNone(soup.find("table", {"id": "ppa-dependencies"}))
        self.assertIn(
            "No dependencies recorded for this PPA yet", self.getText(soup)
        )

    def test_primary_dependency_alone_renders_no_table(self):
        # A non-PPA dependency is configured by the primary_dependencies
        # radio group, so it produces no row.  has_dependencies is true,
        # though, so the "no dependencies" message is suppressed as well
        # and the section renders empty.
        with person_logged_in(self.owner):
            self.ppa.addArchiveDependency(
                self.ppa.distribution.main_archive,
                PackagePublishingPocket.UPDATES,
            )
        soup = self.renderSoup()
        self.assertIsNone(soup.find("table", {"id": "ppa-dependencies"}))
        self.assertNotIn(
            "No dependencies recorded for this PPA yet", self.getText(soup)
        )

    def test_invisible_dependency_is_not_linked(self):
        # No link is rendered for a dependency the user cannot view, so
        # the row explains why instead of silently degrading to plain
        # text.
        self.addInvisibleDependency()
        row = self.renderSoup().find("tr", {"id": "ppa-dependency-0"})
        name = row.find(class_="dependency-name")
        self.assertEqual("span", name.name)
        self.assertIsNone(
            row.find("td", {"class": "dependency-name-cell"}).find("a")
        )
        self.assertIsNotNone(row.find(class_="private-badge"))
        # The explanation is wired up for assistive technologies.
        marker = row.find(class_="private-dependency")
        explanation = row.find("span", {"class": "sr-only"})
        self.assertEqual(explanation["id"], marker["aria-describedby"])
        self.assertIn("This is a private PPA.", self.getText(explanation))

    def test_invisible_dependency_renders_no_priority_input(self):
        # createPriorityFields skips dependencies the user cannot view, so
        # the priority cell is empty -- even though the screen reader text
        # claims the priority can still be changed.
        self.addInvisibleDependency()
        row = self.renderSoup().find("tr", {"id": "ppa-dependency-0"})
        self.assertIsNone(
            row.find("td", {"class": "pin-priority-cell"}).find("input")
        )
        self.assertIn("You can still remove it", self.getText(row))

    def test_invisible_dependency_has_no_priority_field(self):
        # The dependency is listed, but no priority widget is built for it.
        invisible = self.addInvisibleDependency()
        with person_logged_in(self.owner):
            view = self.makeView()
            self.assertIsNone(view.widgets.get("priority_%d" % invisible.id))
            [item] = view.dependency_items
            self.assertIsNone(item["url"])
            self.assertIsNone(item["priority_widget"])
            self.assertEqual(invisible.reference, item["token"])

    def test_mixed_visibility_renders_all_rows(self):
        # Both dependencies get a row and a removal checkbox, but only the
        # visible one gets a priority input.
        self.addDependency()
        self.addInvisibleDependency()
        soup = self.renderSoup()
        cells = soup.find_all("td", {"class": "pin-priority-cell"})
        self.assertEqual(2, len(cells))
        self.assertEqual(
            1,
            len([cell for cell in cells if cell.find("input") is not None]),
        )
        self.assertEqual(
            2,
            len(
                soup.find_all("input", {"class": "line-through-when-checked"})
            ),
        )


class TestArchiveEditDependenciesSubmission(
    ArchiveEditDependenciesTestMixin, TestCaseWithFactory
):
    """Tests for saving pin priority changes."""

    def getBrowser(self):
        return self.getUserBrowser(
            canonical_url(self.ppa) + "/+edit-dependencies", user=self.owner
        )

    def assertPriorityRejected(self, dependency, value):
        """Submitting `value` reports an error and changes nothing."""
        browser = self.getBrowser()
        browser.getControl(name="field.priority_%d" % dependency.id).value = (
            value
        )
        browser.getControl("Save").click()
        cell = BeautifulSoup(browser.contents, "html.parser").find(
            "td", {"class": "pin-priority-cell"}
        )
        self.assertIsNotNone(
            cell.find("div", {"class": "message"}),
            "Expected a widget error in the priority cell.",
        )
        self.assertNotIn("Priority updated", browser.contents)
        self.assertEqual(DEFAULT_PIN_PRIORITY, self.getPinPriority(dependency))

    def getBaseForm(self):
        """Form data mirroring the current state of the page."""
        with person_logged_in(self.owner):
            view = self.makeView()
            data = {"field.actions.save": "Save"}
            for name in ("primary_dependencies", "primary_components"):
                widget = view.widgets.get(name)
                if widget is None:
                    continue
                value = widget._getFormValue()
                if value is None:
                    continue
                data["field." + name] = widget.vocabulary.getTerm(value).token
            return data

    def submit(self, form):
        with person_logged_in(self.owner):
            return create_initialized_view(
                self.ppa,
                "+edit-dependencies",
                principal=self.owner,
                method="POST",
                form=form,
            )

    def test_priority_of_invisible_dependency_cannot_be_set(self):
        # A crafted submission naming a dependency the user cannot view is
        # ignored: no such field exists, so the value never reaches
        # _update_dependencies_priorities.  The visible dependency in the
        # same submission proves the form was processed.
        visible = self.addDependency()
        invisible = self.addInvisibleDependency()
        form = self.getBaseForm()
        form["field.priority_%d" % visible.id] = "700"
        form["field.priority_%d" % invisible.id] = "700"
        view = self.submit(form)
        self.assertEqual([], view.errors)
        self.assertEqual(700, self.getPinPriority(visible))
        self.assertEqual(DEFAULT_PIN_PRIORITY, self.getPinPriority(invisible))

    def test_invisible_dependency_can_be_removed(self):
        # The row states that a dependency the user cannot view can still
        # be removed from the list.
        invisible = self.addInvisibleDependency()
        form = self.getBaseForm()
        form["field.selected_dependencies"] = [invisible.reference]
        view = self.submit(form)
        self.assertEqual([], view.errors)
        self.assertIsNone(self.getRecord(invisible))

    def test_save_updates_priority(self):
        dependency = self.addDependency()
        browser = self.getBrowser()
        browser.getControl(name="field.priority_%d" % dependency.id).value = (
            "700"
        )
        browser.getControl("Save").click()
        self.assertEqual(700, self.getPinPriority(dependency))
        self.assertIn("Priority updated", browser.contents)

    def test_save_without_change_reports_nothing(self):
        # Only changed priorities are stored and announced.
        dependency = self.addDependency()
        browser = self.getBrowser()
        browser.getControl("Save").click()
        self.assertEqual(DEFAULT_PIN_PRIORITY, self.getPinPriority(dependency))
        self.assertNotIn("Priority updated", browser.contents)

    def test_save_rejects_priority_above_maximum(self):
        dependency = self.addDependency()
        self.assertPriorityRejected(dependency, str(MAXIMUM_PIN_PRIORITY + 1))

    def test_save_rejects_priority_below_minimum(self):
        dependency = self.addDependency()
        self.assertPriorityRejected(dependency, str(MINIMUM_PIN_PRIORITY - 1))

    def test_save_rejects_non_integer_priority(self):
        dependency = self.addDependency()
        self.assertPriorityRejected(dependency, "high")

    def test_removal_takes_precedence_over_priority_change(self):
        # A dependency marked for removal in the same submission is
        # removed, and its priority change is discarded.
        dependency = self.addDependency()
        browser = self.getBrowser()
        browser.getControl(name="field.priority_%d" % dependency.id).value = (
            "700"
        )
        browser.getControl(name="field.selected_dependencies").value = [
            dependency.reference
        ]
        browser.getControl("Save").click()
        self.assertIsNone(self.getRecord(dependency))
        self.assertNotIn("Priority updated", browser.contents)

    def test_update_priorities_is_all_or_nothing(self):
        # save_action bails out before applying anything if any priority
        # is invalid.  The Int fields carry min and max, so the form never
        # reaches this check; drive the method directly instead.
        good = self.addDependency()
        bad = self.addDependency()
        view = self.makeView()
        data = {
            "priority_%d" % good.id: 700,
            "priority_%d" % bad.id: MAXIMUM_PIN_PRIORITY + 1,
            "selected_dependencies": [],
        }
        with person_logged_in(self.owner):
            self.assertFalse(view._update_dependencies_priorities(data))
        self.assertEqual(DEFAULT_PIN_PRIORITY, self.getPinPriority(good))
        self.assertEqual(DEFAULT_PIN_PRIORITY, self.getPinPriority(bad))
        self.assertEqual(
            "Priority must be between %d and %d."
            % (MINIMUM_PIN_PRIORITY, MAXIMUM_PIN_PRIORITY),
            view.getFieldError("priority_%d" % bad.id),
        )


class TestArchiveCopyPackagesView(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_query_count(self):
        person = self.factory.makePerson()
        source = self.factory.makeArchive()

        def create_targets():
            self.factory.makeArchive(
                owner=self.factory.makeTeam(members=[person])
            )
            archive = self.factory.makeArchive()
            with admin_logged_in():
                archive.newComponentUploader(person, "main")

        nb_objects = 2
        login_person(person)
        recorder1, recorder2 = record_two_runs(
            lambda: create_initialized_view(
                source, "+copy-packages", principal=person
            ),
            create_targets,
            nb_objects,
        )
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))


class TestArchiveWebhooksView(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def test_webhooks_link_enabled_for_owner(self):
        """The webhooks menu link is enabled for archive owners."""
        archive = self.factory.makeArchive()
        with FeatureFixture({"archive.webhooks.enabled": "on"}):
            with person_logged_in(archive.owner):
                menu = ArchiveIndexActionsMenu(archive)
                self.assertTrue(menu.webhooks().enabled)

    def test_webhooks_link_disabled_without_feature_flag(self):
        """The webhooks menu link is disabled without feature flag."""
        archive = self.factory.makeArchive()
        with person_logged_in(archive.owner):
            menu = ArchiveIndexActionsMenu(archive)
            self.assertFalse(menu.webhooks().enabled)

    def test_webhooks_link_disabled_for_non_owner(self):
        """The webhooks menu link is disabled for non-owners."""
        archive = self.factory.makeArchive()
        other_person = self.factory.makePerson()
        with FeatureFixture({"archive.webhooks.enabled": "on"}):
            with person_logged_in(other_person):
                menu = ArchiveIndexActionsMenu(archive)
                self.assertFalse(menu.webhooks().enabled)
