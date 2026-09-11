# Copyright 2010-2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test SourcePackageRelease."""

from textwrap import dedent

import transaction

from lp.services.webapp.authorization import check_permission
from lp.testing import ANONYMOUS, TestCaseWithFactory, login, login_person
from lp.testing.layers import LaunchpadFunctionalLayer


class TestSourcePackageRelease(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def test_uploader_no_uploader(self):
        spr = self.factory.makeSourcePackageRelease()
        self.assertIs(None, spr.uploader)

    def test___repr__(self):
        spr = self.factory.makeSourcePackageRelease()
        expected_repr = (
            "<{cls} {pkg_name} (id: {id}, " "version: {version})>"
        ).format(
            cls=spr.__class__.__name__,
            pkg_name=spr.name,
            id=spr.id,
            version=spr.version,
        )
        self.assertEqual(expected_repr, repr(spr))

    def test_uploader_dsc_package(self):
        owner = self.factory.makePerson()
        key = self.factory.makeGPGKey(owner)
        spr = self.factory.makeSourcePackageRelease(dscsigningkey=key)
        self.assertEqual(owner, spr.uploader)

    def test_uploader_recipe(self):
        recipe_build = self.factory.makeSourcePackageRecipeBuild()
        spr = self.factory.makeSourcePackageRelease(
            source_package_recipe_build=recipe_build
        )
        self.assertEqual(recipe_build.requester, spr.uploader)

    def test_user_defined_fields(self):
        release = self.factory.makeSourcePackageRelease(
            user_defined_fields=[
                ("Python-Version", ">= 2.4"),
                ("Other", "Bla"),
            ]
        )
        self.assertEqual(
            [["Python-Version", ">= 2.4"], ["Other", "Bla"]],
            release.user_defined_fields,
        )

    def test_homepage_default(self):
        # By default, no homepage is set.
        spr = self.factory.makeSourcePackageRelease()
        self.assertIsNone(spr.homepage)

    def test_homepage_empty(self):
        # The homepage field can be empty.
        spr = self.factory.makeSourcePackageRelease(homepage="")
        self.assertEqual("", spr.homepage)

    def test_homepage_set_invalid(self):
        # As the homepage field is inherited from the DSCFile, the URL
        # does not have to be valid.
        spr = self.factory.makeSourcePackageRelease(homepage="<invalid<url")
        self.assertEqual("<invalid<url", spr.homepage)

    def test_aggregate_changelog(self):
        # If since_version is passed the "changelog" entry returned
        # should contain the changelogs for all releases *since*
        # that version and up to and including the context SPR.
        changelog = self.factory.makeChangelog(
            spn="foo", versions=["1.3", "1.2", "1.1", "1.0"]
        )
        expected_changelog = dedent(
            """\
            foo (1.3) unstable; urgency=low

              * 1.3.

            foo (1.2) unstable; urgency=low

              * 1.2.

            foo (1.1) unstable; urgency=low

              * 1.1."""
        )
        spph = self.factory.makeSourcePackagePublishingHistory(
            sourcepackagename="foo", version="1.3", changelog=changelog
        )
        transaction.commit()  # Yay, librarian.

        observed = spph.sourcepackagerelease.aggregate_changelog(
            since_version="1.0"
        )
        self.assertEqual(expected_changelog, observed)

    def test_aggregate_changelog_invalid_utf8(self):
        # aggregate_changelog copes with invalid UTF-8.
        changelog_main = dedent(
            """\
            foo (1.0) unstable; urgency=low

              * 1.0 (héllo)."""
        ).encode("ISO-8859-1")
        changelog_trailer = (
            " -- Føo Bær <foo@example.com>  " "Tue, 01 Jan 1970 01:50:41 +0000"
        ).encode("ISO-8859-1")
        changelog_text = changelog_main + b"\n\n" + changelog_trailer
        changelog = self.factory.makeLibraryFileAlias(content=changelog_text)
        spph = self.factory.makeSourcePackagePublishingHistory(
            sourcepackagename="foo", version="1.0", changelog=changelog
        )
        transaction.commit()
        observed = spph.sourcepackagerelease.aggregate_changelog(
            since_version=None
        )
        self.assertEqual(changelog_main.decode("UTF-8", "replace"), observed)

    def test_null_string_in_copyright(self):
        test_string = "test string with null \0 character"
        package = self.factory.makeSourcePackageRelease(copyright=test_string)
        self.assertEqual(package.copyright, "test string with null  character")


class TestViewSourcePackageRelease(TestCaseWithFactory):
    """Tests for ViewSourcePackageRelease security adapter."""

    layer = LaunchpadFunctionalLayer

    def _makePublishedSPR(self, private=False, enabled=True):
        archive = self.factory.makeArchive(private=private, enabled=enabled)
        spph = self.factory.makeSourcePackagePublishingHistory(archive=archive)
        login_person(archive.owner)
        return spph.sourcepackagerelease

    def test_anon_can_view_spr_in_public_enabled_archive(self):
        spr = self._makePublishedSPR(private=False, enabled=True)
        login(ANONYMOUS)
        self.assertTrue(check_permission("launchpad.View", spr))

    def test_anon_cannot_view_spr_in_private_archive(self):
        spr = self._makePublishedSPR(private=True, enabled=True)
        login(ANONYMOUS)
        self.assertFalse(check_permission("launchpad.View", spr))

    def test_anon_cannot_view_spr_in_disabled_public_archive(self):
        # Regression test: checkUnauthenticated must also check
        # archive.enabled, matching ViewArchive.checkUnauthenticated.
        spr = self._makePublishedSPR(private=False, enabled=False)
        login(ANONYMOUS)
        self.assertFalse(check_permission("launchpad.View", spr))
