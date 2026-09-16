from typing import List

from storm.expr import SQL
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.feeds.feed import (
    FeedBase,
    FeedEntry,
    FeedPerson,
    FeedTypedData,
)
from lp.services.librarian.browser import ProxiedLibraryFileAlias
from lp.services.webapp import canonical_url
from lp.services.webapp.escaping import structured
from lp.soyuz.enums import PackageUploadStatus
from lp.soyuz.interfaces.archive import CannotCopy
from lp.soyuz.interfaces.queue import (
    PACKAGE_UPLOAD_STATUS_MAPPING_TO_STR,
    IPackageUpload,
    IPackageUploadSet,
)
from lp.soyuz.mail.packageupload import calculate_subject

__all__ = [
    "NewPackageUploadsFeed",
]


def _format_person(person):
    """Format a person as 'Name <email>' for the feed content body"""
    if person.safe_email_or_blank:
        return "%s <%s>" % (person.displayname, person.safe_email_or_blank)
    return person.displayname


class NewPackageUploadsFeed(FeedBase):
    usedfor = IDistroSeries
    feedname: str = "new-package-uploads"

    @property
    def title(self) -> str:
        return "%s — new package uploads" % self.context.name.capitalize()

    @property
    def logo(self) -> str:
        return self.site_url + "/@@/ubuntu-icon"

    @property
    def quantity(self) -> int:
        return 100

    def _getSourcePackageRelease(self, upload):
        if upload.contains_copy:
            try:
                publication = (
                    upload.concrete_package_copy_job.findSourcePublication()
                )
                return publication.sourcepackagerelease
            # TODO: fix exception name, makes no sense to be raising
            # "CannotCopy" from a method to find a source publication
            except CannotCopy:
                return None
        return upload.sourcepackagerelease

    def _getItemsWorker(self) -> List[FeedEntry]:
        uploads = list(
            getUtility(IPackageUploadSet).getAll(
                self.context,
                status=[
                    PackageUploadStatus.ACCEPTED,
                    PackageUploadStatus.DONE,
                ],
                archive=self.context.main_archive,
                pocket=[  # skip BACKPORTS
                    PackagePublishingPocket.RELEASE,
                    PackagePublishingPocket.SECURITY,
                    PackagePublishingPocket.UPDATES,
                    PackagePublishingPocket.PROPOSED,
                ],
            )
            # For ordering by date, we're doing the below instead of simply
            # `.order_by(Desc(PackageUpload.date_created))` because the
            # PackageUpload import causes a warning from the
            # lib/lp/scripts/utilities/importpedant.py script.
            .order_by(SQL("PackageUpload.date_created DESC"))
            # fetch extra to account for filtering below
            .config(limit=self.quantity * 2)
        )

        if not uploads:
            return []

        katie_id = getUtility(ILaunchpadCelebrities).katie.id

        # XXX shreyamalviya 2026-05-04: This whole filtering logic should
        # ideally be converted to a SQL query in the future. This was left like
        # this for now to mimic the existing logic in the mailer that
        # determines which uploads to send notifications for, and in the
        # interest of time.
        filtered_uploads = []
        for upload in uploads:
            spr = self._getSourcePackageRelease(upload)

            changed_by_id = None
            if upload.contains_copy:
                if spr is None:
                    continue
                requester = upload.package_copy_job.job.requester
                if requester is not None and requester.preferredemail:
                    changed_by_id = requester.id
            changed_by_id = changed_by_id or (spr.creator_id if spr else None)

            # Skip binary/mixed uploads unless it's a security upload
            if (
                upload.builds
                and upload.pocket != PackagePublishingPocket.SECURITY
            ):
                continue
            # Skip recipe builds
            if (
                spr
                and removeSecurityProxy(spr).source_package_recipe_build_id
                is not None
            ):
                continue
            # Skip translations/language packs
            if spr and spr.section.name == "translations":
                continue
            if spr is None and not upload.builds and upload.customfiles:
                # Translations-only custom uploads have no spr or builds,
                # only customfiles
                continue
            # Skip binary-only security
            if (
                upload.pocket == PackagePublishingPocket.SECURITY
                and spr is None
            ):
                continue
            # Skip auto-syncs: source-only, Changed-By is the Katie user,
            # non-security (mirrors is_auto_sync_upload in the mailer)
            if (
                spr
                and not upload.builds
                and changed_by_id == katie_id
                and upload.pocket != PackagePublishingPocket.SECURITY
            ):
                continue

            filtered_uploads.append((upload, spr))
            if len(filtered_uploads) >= self.quantity:
                break

        if not filtered_uploads:
            return []

        # Collect all person IDs we'll need ahead and load them in one query
        person_ids = set()
        for upload, spr in filtered_uploads:
            if spr:
                if spr.creator_id:
                    person_ids.add(spr.creator_id)
                if spr.maintainer_id:
                    person_ids.add(spr.maintainer_id)
            if removeSecurityProxy(upload).signing_key_owner_id:
                person_ids.add(
                    removeSecurityProxy(upload).signing_key_owner_id
                )
        if person_ids:
            list(
                getUtility(IPersonSet).getPrecachedPersonsFromIDs(
                    person_ids, need_validity=True
                )
            )

        return [
            self.itemToFeedEntry(upload, spr=spr)
            for upload, spr in filtered_uploads
        ]

    def itemToFeedEntry(self, upload: IPackageUpload, spr=None) -> FeedEntry:
        if spr is None:
            spr = self._getSourcePackageRelease(upload)
        distroseries = upload.distroseries

        title = FeedTypedData(
            calculate_subject(
                spr=spr,
                bprs=upload.builds,
                customfiles=upload.customfiles,
                archive=upload.archive,
                distroseries=distroseries,
                pocket=upload.pocket,
                action=PACKAGE_UPLOAD_STATUS_MAPPING_TO_STR[upload.status],
            )
        )

        if spr:
            link_alternate = canonical_url(
                distroseries.distribution.getSourcePackageRelease(spr),
                rootsite="mainsite",
            )
        else:
            link_alternate = canonical_url(upload, rootsite="mainsite")

        # Build content directly from preloaded SPR fields to avoid expensive
        # methods that do librarian downloads and/or extra queries
        lines = []
        if spr and spr.changelog_entry:
            lines.append("%s\n" % spr.changelog_entry)
        if spr:
            lines.append("Date: %s" % spr.dateuploaded)
            if spr.creator:
                lines.append("Changed-By: %s" % _format_person(spr.creator))
            if spr.maintainer:
                lines.append("Maintainer: %s" % _format_person(spr.maintainer))

        # direct signed uploads give the signer; syncs/recipes/copies give None
        # because nobody signed these
        signer = removeSecurityProxy(upload).signing_key_owner
        if signer and (not spr or signer != spr.creator):
            lines.append("Signed-By: %s" % _format_person(signer))

        lines.append(link_alternate)

        changes_file_url = None
        if upload.changesfile is not None:
            changes_file_url = ProxiedLibraryFileAlias(
                upload.changesfile, upload, rootsite="mainsite"
            ).http_url

        # Wrap the body in <pre> so its line breaks and indentation
        # survive; structured()/.escapedtext HTML-escapes everything
        # except the changes-file link, which stays a real anchor.
        body = "\n".join(lines)
        if changes_file_url:
            content = FeedTypedData(
                structured(
                    '<pre class="wrap">%s\n\n'
                    'Changes file: <a href="%s">%s</a></pre>',
                    body,
                    changes_file_url,
                    changes_file_url,
                ).escapedtext,
                content_type="html",
            )
        else:
            content = FeedTypedData(
                structured('<pre class="wrap">%s</pre>', body).escapedtext,
                content_type="html",
            )

        return FeedEntry(
            title=title,
            link_alternate=link_alternate,
            date_created=upload.date_created,
            date_updated=upload.date_created,
            date_published=upload.date_created,
            authors=(
                [FeedPerson(spr.creator, rootsite=self.rootsite)]
                if spr and spr.creator
                else []
            ),
            content=content,
            logo=self.logo,
            icon=self.logo,
        )
