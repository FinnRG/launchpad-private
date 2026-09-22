import typing
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from unittest.mock import MagicMock

import transaction
from debian.deb822 import Changes
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.feeds.feed import FeedEntry
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.soyuz.enums import PackageUploadCustomFormat, PackageUploadStatus
from lp.soyuz.feed.packageupload import NewPackageUploadsFeed
from lp.soyuz.interfaces.queue import PACKAGE_UPLOAD_STATUS_MAPPING_TO_STR
from lp.testing import (
    ILaunchpadCelebrities,
    TestCaseWithFactory,
    admin_logged_in,
    login,
    record_two_runs,
)
from lp.testing.layers import LaunchpadFunctionalLayer
from lp.testing.matchers import HasQueryCount
from lp.testing.sampledata import USER_EMAIL

if typing.TYPE_CHECKING:
    from lp.registry.interfaces.distroseries import IDistroSeries
    from lp.registry.interfaces.person import IPerson
    from lp.soyuz.interfaces.queue import IPackageUpload
    from lp.soyuz.interfaces.sourcepackagerelease import ISourcePackageRelease


def _recover_html_source(feed_typed_data):
    """Recover the HTML source a real Atom reader would see for content.

    feed-entry-atom.pt inserts a FeedTypedData's .content verbatim as the
    text of a <content type="..."> element:

        <content tal:content="structure entry/content/content"
                 tal:attributes="type entry/content/content_type" />

    Reconstruct exactly that element and parse it with a real XML parser.
    This proves the value is well-formed XML -- an unescaped "&" or "<"
    leaking through would raise ParseError here rather than silently
    passing a substring check -- and returns the HTML string a reader
    would go on to parse next.
    """
    content_xml = "<content type=%r>%s</content>" % (
        feed_typed_data.content_type,
        feed_typed_data.content,
    )
    element = ET.fromstring(content_xml)
    return element.text or ""


class _TagCollectingParser(HTMLParser):
    """Records every start tag and all text data seen while parsing.

    Used to check what an actual HTML parser -- standing in for a feed
    reader/aggregator -- does with the HTML source recovered from a feed
    entry's content, which is the point at which a type="html" payload
    gets interpreted as markup.
    """

    def __init__(self):
        super().__init__()
        self.tags = []
        self.text_chunks = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

    def handle_data(self, data):
        self.text_chunks.append(data)


def _assert_tags_equal(test, parser, expected_tags):
    """Assert an HTML parser saw exactly the expected tags, no more.

    The body is always wrapped in "pre"; the only other tag it can ever
    contain is the hand-built "a" changes-file link. There is no
    linkification of the body itself, so nothing else should appear
    regardless of payload.
    """
    test.assertEqual(list(expected_tags), parser.tags)


class TestNewPackageUploadsFeed(TestCaseWithFactory):
    """Tests for NewPackageUploadsFeed."""

    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        login(USER_EMAIL)

    def _make_feed(self, distroseries):
        return NewPackageUploadsFeed(
            distroseries,
            LaunchpadTestRequest(
                SERVER_URL="http://feeds.launchpad.test/ubuntu/%s/%s.atom"
                % (distroseries.name, NewPackageUploadsFeed.feedname)
            ),
        )

    def _make_signed_upload(
        self,
        distroseries,
        archive=None,
        pocket=PackagePublishingPocket.RELEASE,
        status=PackageUploadStatus.DONE,
        changelog_entry="* Fix a bug",
        creator=None,
        signer=None,
        section_name=None,
    ):
        """Create a signed source-only upload that passes all feed filters.

        By default produces a DONE, RELEASE-pocket, main-archive, signed
        source upload
        """
        if creator is None:
            creator = self.factory.makePerson()
        if signer is None:
            signer = creator
        spr = self.factory.makeSourcePackageRelease(
            distroseries=distroseries,
            creator=creator,
            changelog_entry=changelog_entry,
            section_name=section_name,
        )
        upload = self.factory.makePackageUpload(
            distroseries=distroseries,
            archive=archive or distroseries.main_archive,
            pocket=pocket,
            status=status,
            signing_key=MagicMock(owner=signer, fingerprint="A" * 40),
            changes_file_content=Changes(
                {
                    "Format": "1.7",
                    "Changed-By": "%s <%s>"
                    % (
                        creator.displayname,
                        (
                            creator.preferredemail.email
                            if creator.preferredemail
                            else "noreply@example.com"
                        ),
                    ),
                }
            )
            .dump()
            .encode("UTF-8"),
        )
        with admin_logged_in():  # admin access needed to add source to upload
            upload.addSource(spr)
        return upload

    def test_title_contains_distroseries_name(self):
        distroseries = self.factory.makeDistroSeries(name="focal")
        feed = self._make_feed(distroseries)
        self.assertIn("Focal", feed.title)

    def test_getItemsWorker_returns_empty_list_when_no_uploads(self):
        distroseries = self.factory.makeDistroSeries()
        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()
        self.assertEqual(items, [])

    def test_getItemsWorker_respects_quantity_limit(self):
        class LimitedFeed(NewPackageUploadsFeed):
            quantity = 3

        distroseries = self.factory.makeDistroSeries()
        for _ in range(4):
            self._make_signed_upload(distroseries)
        transaction.commit()

        feed = LimitedFeed(
            distroseries,
            LaunchpadTestRequest(
                SERVER_URL="http://feeds.launchpad.test/ubuntu/%s/%s.atom"
                % (distroseries.name, LimitedFeed.feedname)
            ),
        )

        self.assertEqual(3, len(feed._getItemsWorker()))

    def test_itemToFeedEntry_returns_expected_feedentry(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        creator = self.factory.makePerson()
        signer = self.factory.makePerson()
        upload = self._make_signed_upload(
            distroseries,
            creator=creator,
            signer=signer,
            changelog_entry="some changes",
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)

        self.assertIsInstance(entry, FeedEntry)
        captured_action = (
            entry.title.content.split("(")[-1].rstrip(")").lower()
        )
        expected_action = PACKAGE_UPLOAD_STATUS_MAPPING_TO_STR[
            PackageUploadStatus.ACCEPTED
        ]
        self.assertEqual(captured_action, expected_action)
        self.assertEqual(len(entry.authors), 1)
        self.assertIs(entry.authors[0].name, creator.displayname)
        self.assertIn(upload.displayname, entry.link_alternate)
        self.assertIn(upload.displayversion, entry.link_alternate)
        self.assertIn("some changes", entry.content.content)
        self.assertIn("Changed-By:", entry.content.content)

    def test_entry_content_includes_changelog(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(
            distroseries, changelog_entry="* Fixed everything\n* Best app ever"
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)

        self.assertIn("Fixed everything", entry.content.content)
        self.assertIn("Best app ever", entry.content.content)

    def test_entry_content_xml_escaping_changelog_renders_correctly(
        self,
    ) -> None:
        """Checks that special chars are properly escaped in the feed.

        The content is HTML (wrapped in <pre>) embedded in an XML document,
        so the raw content is escaped once for the HTML layer (making it
        safe to render as literal text rather than markup), and the
        resulting string is escaped again by FeedTypedData for the XML
        layer.  A reader unwinds this in the opposite order: one XML-unescape
        recovers the HTML source (literal <pre> tags plus HTML-escaped
        entities), then one HTML-render turns those entities back into the
        literal characters the uploader wrote -- without ever treating them
        as markup.
        """
        distroseries = self.factory.makeDistroSeries()
        changelog_text = (
            "This & that <test> \"quotes\" 'apostrophes' and what if there is "
            "a 🚀 or let's go even further with '<script>alert(\"x\")</script>"
            " & some text'"
        )

        upload = self._make_signed_upload(
            distroseries, changelog_entry=changelog_text
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)

        self.assertEqual("html", entry.content.content_type)

        expected_double_escaped_text = (
            "This &amp;amp; that &amp;lt;test&amp;gt; &amp;quot;quotes&amp;"
            "quot; &amp;#x27;apostrophes&amp;#x27; and what if there is a "
            "\U0001f680 or let&amp;#x27;s go even further with &amp;#x27;"
            "&amp;lt;script&amp;gt;alert(&amp;quot;x&amp;quot;)&amp;lt;/scri"
            "pt&amp;gt; &amp;amp; some text&amp;#x27;"
        )
        self.assertIn(expected_double_escaped_text, entry.content.content)

        self.assertNotIn("<script>", entry.content.content)
        self.assertNotIn("<test>", entry.content.content)

        # Round-trip through a real XML parser then a real HTML parser,
        # simulating what an actual reader would do, rather than just
        # comparing escaped strings by hand.
        html_source = _recover_html_source(entry.content)
        parser = _TagCollectingParser()
        parser.feed(html_source)
        _assert_tags_equal(self, parser, expected_tags=["pre", "a"])
        self.assertIn(changelog_text, "".join(parser.text_chunks))

    def test_entry_content_preserves_linebreaks_and_indentation(self) -> None:
        """Changelog line breaks and leading indentation survive.

        <pre> preserves whitespace verbatim once an HTML renderer decodes
        the entities, so the newlines and leading spaces the uploader wrote
        remain in the text -- rather than being collapsible to one line,
        as a plain type="text" construct legitimately would be under
        RFC 4287.
        """
        distroseries = self.factory.makeDistroSeries()
        changelog_text = (
            "* Fixed everything\n  - sub point one\n  - sub point two"
        )
        upload = self._make_signed_upload(
            distroseries, changelog_entry=changelog_text
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)

        html_source = _recover_html_source(entry.content)
        self.assertTrue(html_source.startswith('<pre class="wrap">'))

        parser = _TagCollectingParser()
        parser.feed(html_source)
        rendered_text = "".join(parser.text_chunks)
        _assert_tags_equal(self, parser, expected_tags=["pre", "a"])
        self.assertIn("* Fixed everything", rendered_text)
        self.assertIn("  - sub point one", rendered_text)
        self.assertIn("  - sub point two", rendered_text)
        self.assertIn("* Fixed everything\n  - sub point one", rendered_text)

    def test_entry_content_does_not_linkify_bug_references(self) -> None:
        """LP: #XXXXX and bug numbers stay as plain text.

        Linkifying them would need a root URL to build an absolute link,
        and can trigger DB lookups on uploader-controlled text (e.g. FAQ
        references), so the body is never linkified.
        """
        distroseries = self.factory.makeDistroSeries()
        changelog_text = "* Fix LP: #123456 and see bug 654321"
        upload = self._make_signed_upload(
            distroseries, changelog_entry=changelog_text
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)

        html_source = _recover_html_source(entry.content)
        parser = _TagCollectingParser()
        parser.feed(html_source)

        # Only the changes-file link is a real anchor; nothing from the
        # changelog body should have been turned into one.
        _assert_tags_equal(self, parser, expected_tags=["pre", "a"])
        self.assertIn(changelog_text, "".join(parser.text_chunks))

    def test_entry_content_includes_changesfile_link(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(distroseries)
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)

        self.assertIn("Changes file:", entry.content.content)
        self.assertIn(upload.changes_file_url, entry.content.content)

    def test_entry_content_omits_changesfile_link_when_missing(self) -> None:
        """itemToFeedEntry does not crash when changesfile is absent."""
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(distroseries)
        removeSecurityProxy(upload).changesfile = None
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)

        self.assertIsInstance(entry, FeedEntry)
        self.assertNotIn("Changes file:", entry.content.content)

    def test_entry_content_includes_email_addresses(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        creator = self.factory.makePerson()
        upload = self._make_signed_upload(
            distroseries, creator=creator, signer=creator
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)
        expected = "Changed-By: %s &amp;lt;%s&amp;gt;" % (
            creator.displayname,
            creator.preferredemail.email,
        )

        self.assertIn(expected, entry.content.content)

    def test_entry_content_signed_by_shown_when_different_from_creator(
        self,
    ) -> None:
        """Signed-By line appears when signing_key_owner != spr.creator"""
        distroseries = self.factory.makeDistroSeries()
        creator = self.factory.makePerson()
        signer = self.factory.makePerson()
        upload = self._make_signed_upload(
            distroseries, creator=creator, signer=signer
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)

        self.assertIn("Signed-By:", entry.content.content)
        self.assertIn(signer.displayname, entry.content.content)

    def test_entry_content_signed_by_omitted_when_same_as_creator(
        self,
    ) -> None:
        """Signed-By line is omitted when signing_key_owner == spr.creator"""
        distroseries = self.factory.makeDistroSeries()
        creator = self.factory.makePerson()
        upload = self._make_signed_upload(
            distroseries, creator=creator, signer=creator
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)

        self.assertNotIn("Signed-By:", entry.content.content)

    def test_spr_none_does_not_crash(self) -> None:
        # A custom-only upload (spr=None) that passes the filters produces a
        # valid feed entry without crashing.
        distroseries = self.factory.makeDistroSeries()
        signer = self.factory.makePerson()
        # can't use `_make_signed_upload` since it creates an SPR,
        # but we want no SPR here
        upload = self.factory.makePackageUpload(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackageUploadStatus.DONE,
            signing_key=MagicMock(owner=signer, fingerprint="C" * 40),
            changes_file_content=b"Format: 1.7\n\n",
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=None)

        self.assertIsInstance(entry, FeedEntry)
        # Falls back to distroseries URL when spr is None.
        self.assertIn(distroseries.name, entry.link_alternate)

    def test_only_main_archive_uploads_are_included(self):
        distroseries = self.factory.makeDistroSeries()

        # Upload to a PPA — should not appear
        ppa = self.factory.makeArchive(distribution=distroseries.distribution)
        self._make_signed_upload(distroseries, archive=ppa)

        # Upload to main — should appear
        main_upload = self._make_signed_upload(distroseries)
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(1, len(items))
        self.assertIn(main_upload.displayname, items[0].link_alternate)

    def test_feed_only_contains_accepted_or_done_uploads(self):
        """Only uploads with status ACCEPTED or DONE appear in the feed.

        PackageUploadStatus has five states: NEW, UNAPPROVED, ACCEPTED, DONE,
        and REJECTED.  The feed query is restricted to ACCEPTED and DONE;
        any other status is invisible.  We name each upload after its status
        so we can verify precisely which two packages are returned.
        """
        distroseries = self.factory.makeDistroSeries()
        status_to_name = {
            PackageUploadStatus.NEW: "pkg-new",
            PackageUploadStatus.UNAPPROVED: "pkg-unapproved",
            PackageUploadStatus.ACCEPTED: "pkg-accepted",
            PackageUploadStatus.DONE: "pkg-done",
            PackageUploadStatus.REJECTED: "pkg-rejected",
        }
        for status, pkg_name in status_to_name.items():
            spn = self.factory.makeSourcePackageName(name=pkg_name)
            upload = self._make_signed_upload(distroseries, status=status)
            removeSecurityProxy(
                upload.sourcepackagerelease
            ).sourcepackagename = spn
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(2, len(items))
        titles = {item.title.content.lower() for item in items}
        self.assertTrue(
            any("pkg-accepted" in t for t in titles)
            and any("pkg-done" in t for t in titles),
        )

    def test_backports_excluded(self):
        distroseries = self.factory.makeDistroSeries()
        _ = self._make_signed_upload(
            distroseries, pocket=PackagePublishingPocket.BACKPORTS
        )
        release_upload = self._make_signed_upload(
            distroseries, pocket=PackagePublishingPocket.RELEASE
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(len(items), 1)
        self.assertIn(release_upload.displayname, items[0].link_alternate)

    def test_binary_non_security_upload_is_excluded(self):
        # A source+binary upload in a non-SECURITY pocket is excluded.
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(
            distroseries, pocket=PackagePublishingPocket.UPDATES
        )
        build = self.factory.makeBinaryPackageBuild()
        with admin_logged_in():  # admin access needed to add build to upload
            upload.addBuild(build)
        transaction.commit()

        feed = self._make_feed(distroseries)

        self.assertEqual(0, len(feed._getItemsWorker()))

    def test_binary_security_upload_with_source_is_included(self):
        # A source+binary upload in the SECURITY pocket is included.
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(
            distroseries, pocket=PackagePublishingPocket.SECURITY
        )
        build = self.factory.makeBinaryPackageBuild()
        with admin_logged_in():  # admin access needed to add build to upload
            upload.addBuild(build)
        transaction.commit()

        feed = self._make_feed(distroseries)

        self.assertEqual(1, len(feed._getItemsWorker()))

    def test_binary_only_security_upload_is_excluded(self):
        # A SECURITY upload with binaries but no SPR is excluded.
        # Such uploads have no source package to surface in the feed.
        distroseries = self.factory.makeDistroSeries()
        signer = self.factory.makePerson()
        # can't use `_make_signed_upload` since it creates an SPR,
        # but we want no SPR here
        self.factory.makePackageUpload(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            pocket=PackagePublishingPocket.SECURITY,
            status=PackageUploadStatus.DONE,
            signing_key=MagicMock(owner=signer, fingerprint="D" * 40),
            changes_file_content=b"Format: 1.7\n\n",
        )
        # a regular signed RELEASE upload that should appear
        release_upload = self._make_signed_upload(distroseries)
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(1, len(items))
        self.assertIn(release_upload.displayname, items[0].link_alternate)

    def test_recipe_build_upload_is_excluded(self):
        # Uploads from a source package recipe build are excluded.
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(distroseries)
        recipe_build = self.factory.makeSourcePackageRecipeBuild()
        removeSecurityProxy(
            upload.sourcepackagerelease
        ).source_package_recipe_build_id = recipe_build.id
        transaction.commit()

        feed = self._make_feed(distroseries)

        self.assertEqual(0, len(feed._getItemsWorker()))

    def test_translations_section_uploads_are_excluded(self):
        # Language-pack uploads (translations section) are excluded.
        distroseries = self.factory.makeDistroSeries()
        self._make_signed_upload(distroseries, section_name="translations")
        main_upload = self._make_signed_upload(distroseries)
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(1, len(items))
        self.assertIn(main_upload.displayname, items[0].link_alternate)

    def test_custom_translation_upload_is_excluded(self):
        # Translation-only uploads, which have no SPR or builds, are excluded.
        distroseries = self.factory.makeDistroSeries()
        # can't use `_make_signed_upload` since it creates an SPR,
        # but we want no SPR here
        upload = self.factory.makePackageUpload(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackageUploadStatus.DONE,
            signing_key=MagicMock(
                owner=self.factory.makePerson(), fingerprint="A" * 40
            ),
            changes_file_content=b"Format: 1.7\n\n",
        )
        with admin_logged_in():  # admin access needed to add custom to upload
            upload.addCustom(
                self.factory.makeLibraryFileAlias(),
                PackageUploadCustomFormat.DEBIAN_INSTALLER,
            )
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(0, len(items))

    def test_auto_sync_katie_upload_is_excluded(self):
        # Source-only uploads whose creator is the katie auto-sync user are
        # excluded when not targeting the SECURITY pocket.
        distroseries = self.factory.makeDistroSeries()
        katie_upload = self._make_signed_upload(distroseries)
        katie = getUtility(ILaunchpadCelebrities).katie
        removeSecurityProxy(katie_upload.sourcepackagerelease).creator = katie
        manual_upload = self._make_signed_upload(distroseries)
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(1, len(items))
        self.assertIn(manual_upload.displayname, items[0].link_alternate)

    def _make_debian_sync(
        self,
        distroseries: "IDistroSeries",
        requester: "typing.Optional[IPerson]" = None,
    ) -> "typing.Tuple[IPackageUpload, ISourcePackageRelease]":
        debian = self.factory.makeDistribution()
        unstable = self.factory.makeDistroSeries(
            distribution=debian, name="unstable"
        )
        publication = self.factory.makeSourcePackagePublishingHistory(
            distroseries=unstable,
            archive=debian.main_archive,
            pocket=PackagePublishingPocket.RELEASE,
        )
        spr = publication.sourcepackagerelease
        removeSecurityProxy(spr).changelog_entry = "* Changes from Debian"
        with admin_logged_in():
            job = self.factory.makePlainPackageCopyJob(
                package_name=spr.name,
                package_version=spr.version,
                source_archive=debian.main_archive,
                target_archive=distroseries.main_archive,
                target_distroseries=distroseries,
                target_pocket=PackagePublishingPocket.RELEASE,
                requester=requester,
            )
            upload = self.factory.makePackageUpload(
                distroseries=distroseries,
                archive=distroseries.main_archive,
                status=PackageUploadStatus.DONE,
                package_copy_job=removeSecurityProxy(job).context,
            )
        return upload, spr

    def test_debian_sync_is_included(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        upload, spr = self._make_debian_sync(distroseries)
        removeSecurityProxy(spr).creator = getUtility(
            ILaunchpadCelebrities
        ).katie
        transaction.commit()

        items = self._make_feed(distroseries)._getItemsWorker()

        self.assertEqual(1, len(items))
        self.assertIn(spr.name, items[0].title.content)
        self.assertIn("Accepted", items[0].title.content)
        self.assertIn(spr.version, items[0].link_alternate)
        self.assertIn("Changes from Debian", items[0].content.content)

    def test_debian_sync_entry_uses_supplied_source(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        upload, spr = self._make_debian_sync(distroseries)
        transaction.commit()

        entry = self._make_feed(distroseries).itemToFeedEntry(upload, spr=spr)

        self.assertIn(spr.name, entry.title.content)
        self.assertIn(spr.version, entry.link_alternate)
        self.assertIn("Changes from Debian", entry.content.content)

    def test_copy_sources_uses_latest_publication(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        upload, spr = self._make_debian_sync(distroseries)
        source_archive = upload.package_copy_job.source_archive
        source_series = source_archive.distribution["unstable"]
        latest_spr = self.factory.makeSourcePackageRelease(
            sourcepackagename=spr.sourcepackagename,
            version=spr.version,
            distroseries=source_series,
        )
        self.factory.makeSourcePackagePublishingHistory(
            sourcepackagerelease=latest_spr,
            distroseries=source_series,
            archive=source_archive,
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        self.assertEqual(latest_spr, feed._getCopySources([upload])[upload.id])

    def test_copy_sources_respects_source_series_and_pocket(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        upload, spr = self._make_debian_sync(distroseries)
        copy_job = removeSecurityProxy(upload.package_copy_job)
        source_archive = copy_job.source_archive
        source_series = source_archive.distribution["unstable"]
        copy_job.metadata = dict(
            copy_job.metadata,
            source_distroseries=source_series.name,
            source_pocket=PackagePublishingPocket.RELEASE.value,
        )
        other_series = self.factory.makeDistroSeries(
            distribution=source_archive.distribution
        )
        for series, pocket in (
            (other_series, PackagePublishingPocket.RELEASE),
            (source_series, PackagePublishingPocket.SECURITY),
        ):
            other_spr = self.factory.makeSourcePackageRelease(
                sourcepackagename=spr.sourcepackagename,
                version=spr.version,
                distroseries=series,
            )
            self.factory.makeSourcePackagePublishingHistory(
                sourcepackagerelease=other_spr,
                distroseries=series,
                archive=source_archive,
                pocket=pocket,
            )
        transaction.commit()

        feed = self._make_feed(distroseries)
        self.assertEqual(spr, feed._getCopySources([upload])[upload.id])

    def test_copy_sources_matches_exact_version(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        upload, spr = self._make_debian_sync(distroseries)
        copy_job = removeSecurityProxy(upload.package_copy_job)
        source_archive = copy_job.source_archive
        source_series = source_archive.distribution["unstable"]
        removeSecurityProxy(spr).version = "1.0"
        copy_job.metadata = dict(copy_job.metadata, package_version="1.0")
        other_spr = self.factory.makeSourcePackageRelease(
            sourcepackagename=spr.sourcepackagename,
            version="1.0-0",
            distroseries=source_series,
        )
        self.factory.makeSourcePackagePublishingHistory(
            sourcepackagerelease=other_spr,
            distroseries=source_series,
            archive=source_archive,
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        self.assertEqual(spr, feed._getCopySources([upload])[upload.id])

    def test_debian_auto_sync_is_excluded(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        self._make_debian_sync(
            distroseries, requester=getUtility(ILaunchpadCelebrities).katie
        )
        transaction.commit()

        self.assertEqual([], self._make_feed(distroseries)._getItemsWorker())

    def test_debian_sync_with_missing_source_is_excluded(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        upload, spr = self._make_debian_sync(distroseries)
        removeSecurityProxy(upload.package_copy_job).package_name = (
            "missing-source"
        )
        transaction.commit()

        self.assertEqual([], self._make_feed(distroseries)._getItemsWorker())

    def test_signed_source_upload_is_included(self):
        # A regular signed source upload passes all filters and appears.
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(distroseries)
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(1, len(items))
        self.assertIn(upload.displayname, items[0].link_alternate)

    def test_feed_query_count_is_constant(self):
        # Query count does not grow with the number of uploads
        distroseries = self.factory.makeDistroSeries()
        feed = self._make_feed(distroseries)

        def make_signed_upload():
            self._make_signed_upload(distroseries)

        def render_feed():
            transaction.commit()
            feed._getItemsWorker()

        recorder1, recorder2 = record_two_runs(
            tested_method=render_feed,
            item_creator=make_signed_upload,
            first_round_number=1,
            second_round_number=5,
        )

        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    def test_feed_query_count_is_constant_with_syncs(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        feed = self._make_feed(distroseries)

        def make_sync() -> None:
            self._make_debian_sync(
                distroseries, requester=self.factory.makePerson()
            )

        def render_feed() -> typing.List[FeedEntry]:
            transaction.commit()
            return feed._getItemsWorker()

        recorder1, recorder2 = record_two_runs(
            tested_method=render_feed,
            item_creator=make_sync,
            first_round_number=1,
            second_round_number=5,
        )

        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))
        self.assertEqual(6, len(render_feed()))


class TestNewPackageUploadsFeedSecurity(TestCaseWithFactory):
    """Security-focused tests for NewPackageUploadsFeed.

    These target the specific vulnerability class this feed is exposed to:
    markup/script injection via uploader-controlled text (changelog body,
    Changed-By/Maintainer/Signed-By names) rendered as type="html" Atom
    content. SQL injection, command injection, and path traversal are not
    applicable here -- the query path is fully ORM-parameterized and no
    filesystem or shell operations occur in this module.
    """

    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        login(USER_EMAIL)

    def _make_feed(self, distroseries):
        return NewPackageUploadsFeed(
            distroseries,
            LaunchpadTestRequest(
                SERVER_URL="http://feeds.launchpad.test/ubuntu/%s/%s.atom"
                % (distroseries.name, NewPackageUploadsFeed.feedname)
            ),
        )

    def _make_signed_upload(
        self,
        distroseries,
        changelog_entry="* Fix a bug",
        creator=None,
        signer=None,
        maintainer=None,
    ):
        if creator is None:
            creator = self.factory.makePerson()
        if signer is None:
            signer = creator
        spr = self.factory.makeSourcePackageRelease(
            distroseries=distroseries,
            creator=creator,
            maintainer=maintainer,
            changelog_entry=changelog_entry,
        )
        upload = self.factory.makePackageUpload(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            pocket=PackagePublishingPocket.RELEASE,
            status=PackageUploadStatus.DONE,
            signing_key=MagicMock(owner=signer, fingerprint="A" * 40),
            changes_file_content=Changes(
                {
                    "Format": "1.7",
                    "Changed-By": "%s <%s>"
                    % (
                        creator.displayname,
                        (
                            creator.preferredemail.email
                            if creator.preferredemail
                            else "noreply@example.com"
                        ),
                    ),
                }
            )
            .dump()
            .encode("UTF-8"),
        )
        with admin_logged_in():
            upload.addSource(spr)
        return upload

    def _parse_content_as_a_reader_would(self, entry):
        """Simulate what a conformant Atom reader does with entry.content.

        Recovers the HTML source via _recover_html_source (proving it is
        well-formed XML in the process) and parses that HTML with a real
        parser, so assertions below are on rendered tags/text -- i.e. on
        behaviour -- rather than on a hand-computed escaped string.
        """
        self.assertEqual("html", entry.content.content_type)
        html_source = _recover_html_source(entry.content)
        parser = _TagCollectingParser()
        parser.feed(html_source)
        return parser

    def test_changelog_script_tag_is_not_parsed_as_markup(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        payload = "<script>alert(document.cookie)</script>"
        upload = self._make_signed_upload(
            distroseries, changelog_entry=payload
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)
        parser = self._parse_content_as_a_reader_would(entry)

        _assert_tags_equal(self, parser, expected_tags=["pre", "a"])
        self.assertIn(
            "<script>alert(document.cookie)</script>",
            "".join(parser.text_chunks),
        )

        # feed-html.pt renders html_page_content directly as live markup,
        # with no escaping of its own, so it needs the same check.
        html_page_parser = _TagCollectingParser()
        html_page_parser.feed(entry.content.html_page_content)
        _assert_tags_equal(self, html_page_parser, expected_tags=["pre", "a"])

    def test_changelog_event_handler_attribute_is_not_parsed_as_markup(
        self,
    ) -> None:
        distroseries = self.factory.makeDistroSeries()
        payload = '<img src=x onerror="alert(1)">'
        upload = self._make_signed_upload(
            distroseries, changelog_entry=payload
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)
        parser = self._parse_content_as_a_reader_would(entry)

        _assert_tags_equal(self, parser, expected_tags=["pre", "a"])
        self.assertIn(payload, "".join(parser.text_chunks))

        html_page_parser = _TagCollectingParser()
        html_page_parser.feed(entry.content.html_page_content)
        _assert_tags_equal(self, html_page_parser, expected_tags=["pre", "a"])

    def test_changed_by_display_name_with_markup_is_not_parsed(self) -> None:
        # Changed-By is built from Person.displayname, which is
        # user-settable free text and carries the same injection risk as
        # the changelog body. It must be equally inert.
        distroseries = self.factory.makeDistroSeries()
        creator = self.factory.makePerson(
            displayname="<script>alert(1)</script>"
        )
        upload = self._make_signed_upload(distroseries, creator=creator)
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)
        parser = self._parse_content_as_a_reader_would(entry)

        _assert_tags_equal(self, parser, expected_tags=["pre", "a"])

        html_page_parser = _TagCollectingParser()
        html_page_parser.feed(entry.content.html_page_content)
        _assert_tags_equal(self, html_page_parser, expected_tags=["pre", "a"])

    def test_maintainer_display_name_with_markup_is_not_parsed(self) -> None:
        distroseries = self.factory.makeDistroSeries()
        maintainer = self.factory.makePerson(
            displayname="<img src=x onerror=alert(1)>"
        )
        upload = self._make_signed_upload(distroseries, maintainer=maintainer)
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)
        parser = self._parse_content_as_a_reader_would(entry)

        _assert_tags_equal(self, parser, expected_tags=["pre", "a"])

        html_page_parser = _TagCollectingParser()
        html_page_parser.feed(entry.content.html_page_content)
        _assert_tags_equal(self, html_page_parser, expected_tags=["pre", "a"])

    def test_entry_content_does_not_escape_html_twice_incorrectly(
        self,
    ) -> None:
        # Guard against a regression where content is escaped a
        # different number of times than intended: exactly two escape
        # passes (HTML layer, then XML layer) should be applied, so a
        # literal "<" in the changelog must appear as the doubly-escaped
        # "&amp;lt;" in the raw content string -- never unescaped, never
        # escaped once (e.g. a stray `structured()` instead of
        # `.escapedtext` would drop the HTML layer), and never escaped
        # three times.
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(
            distroseries, changelog_entry="a < b"
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload, spr=upload.sourcepackagerelease)

        self.assertIn("a &amp;lt; b", entry.content.content)
        self.assertNotIn("a < b", entry.content.content)
        self.assertNotIn("a &lt; b", entry.content.content)
        self.assertNotIn("a &amp;amp;lt; b", entry.content.content)
