# Copyright 2010-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import hashlib
import io

from testtools.matchers import (
    ContainsDict,
    EndsWith,
    Equals,
    MatchesDict,
    MatchesStructure,
)

from lp.buildmaster.enums import BuildBaseImageType
from lp.registry.enums import PersonVisibility
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.features.testing import FeatureFixture
from lp.services.webapp.interfaces import OAuthPermission
from lp.soyuz.interfaces.livefs import LIVEFS_FEATURE_FLAG
from lp.testing import (
    ANONYMOUS,
    TestCaseWithFactory,
    api_url,
    login,
    login_as,
    person_logged_in,
)
from lp.testing.layers import LaunchpadFunctionalLayer
from lp.testing.pages import webservice_for_person


class TestDistroArchSeriesWebservice(TestCaseWithFactory):
    """Unit Tests for 'DistroArchSeries' Webservice."""

    layer = LaunchpadFunctionalLayer

    def test_distroseries_id_accessible_but_not_exported(self):
        """The foreign key is readable in Python, but absent from API JSON."""
        architecture = self.factory.makeDistroArchSeries()
        series_id = architecture.distroseries.id
        user = self.factory.makePerson()
        with person_logged_in(user):
            self.assertEqual(series_id, architecture.distroseries_id)
            architecture_url = api_url(architecture)
        ws = webservice_for_person(user, default_api_version="devel")
        representation = self.getWebserviceJSON(ws, architecture_url)
        self.assertNotIn("distroseries_id", representation)

    def test_distroseries_architectures_anonymous(self):
        """Test anonymous DistroArchSeries API Access."""
        distroseries = self.factory.makeDistroArchSeries().distroseries
        distroseries_url = api_url(distroseries)
        ws = webservice_for_person(None, default_api_version="devel")
        ws_distroseries = self.getWebserviceJSON(ws, distroseries_url)
        ws_architectures = self.getWebserviceJSON(
            ws, ws_distroseries["architectures_collection_link"]
        )
        self.assertEqual(1, len(ws_architectures["entries"]))

    def test_distroseries_architectures_authenticated(self):
        """Test authenticated DistroArchSeries API Access."""
        distroseries = self.factory.makeDistroArchSeries().distroseries
        distroseries_url = api_url(distroseries)
        # Create a user to use the authenticated API
        accessor = self.factory.makePerson()
        ws = webservice_for_person(accessor, default_api_version="devel")
        ws_distroseries = self.getWebserviceJSON(ws, distroseries_url)
        ws_architectures = self.getWebserviceJSON(
            ws, ws_distroseries["architectures_collection_link"]
        )
        self.assertEqual(1, len(ws_architectures["entries"]))

    def test_getBuildRecords(self):
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        build = self.factory.makeBinaryPackageBuild(distroarchseries=das)
        build_title = build.title
        user = self.factory.makePerson()
        ws = webservice_for_person(user)
        response = ws.named_get(das_url, "getBuildRecords")
        self.assertEqual(200, response.status)
        self.assertEqual(
            [build_title],
            [entry["title"] for entry in response.jsonBody()["entries"]],
        )

    def test_setChroot_removeChroot_random_user(self):
        # Random users are not allowed to set or remove chroots.
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        user = self.factory.makePerson()
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            das_url, "setChroot", data=io.BytesIO(b"xyz"), sha1sum="0"
        )
        self.assertEqual(401, response.status)
        response = ws.named_post(das_url, "removeChroot")
        self.assertEqual(401, response.status)

    def test_setChroot_wrong_sha1sum(self):
        # If the sha1sum calculated is different, the chroot is not set.
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        user = das.distroseries.distribution.main_archive.owner
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            das_url, "setChroot", data=io.BytesIO(b"zyx"), sha1sum="x"
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=400, body=b"Chroot upload checksums do not match"
            ),
        )

    def test_setChroot_missing_trailing_cr(self):
        # Due to http://bugs.python.org/issue1349106 launchpadlib sends
        # MIME with \n line endings, which is illegal. lazr.restful
        # parses each ending as \r\n, resulting in a binary that ends
        # with \r getting the last byte chopped off. To cope with this
        # on the server side we try to append \r if the SHA-1 doesn't
        # match.
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        user = das.distroseries.distribution.main_archive.owner
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        sha1 = "95e0c0e09be59e04eb0e312e5daa11a2a830e526"
        response = ws.named_post(
            das_url,
            "setChroot",
            data=io.BytesIO(b"foo\r"),
            sha1sum="95e0c0e09be59e04eb0e312e5daa11a2a830e526",
        )
        self.assertEqual(200, response.status)
        login(ANONYMOUS)
        self.assertEqual(sha1, das.getChroot().content.sha1)

    def test_getChrootHash(self):
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        user = das.distroseries.distribution.main_archive.owner
        sha1 = hashlib.sha1(b"abcxyz").hexdigest()
        sha256 = hashlib.sha256(b"abcxyz").hexdigest()
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            das_url, "setChroot", data=io.BytesIO(b"abcxyz"), sha1sum=sha1
        )
        self.assertEqual(200, response.status)
        login(ANONYMOUS)
        self.assertThat(
            das.getChrootHash(
                PackagePublishingPocket.RELEASE, BuildBaseImageType.CHROOT
            ),
            MatchesDict({"sha256": Equals(sha256)}),
        )

    def test_setChroot_removeChroot(self):
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        user = das.distroseries.distribution.main_archive.owner
        expected_file = "chroot-%s-%s-%s.tar.gz" % (
            das.distroseries.distribution.name,
            das.distroseries.name,
            das.architecturetag,
        )
        sha1 = hashlib.sha1(b"abcxyz").hexdigest()
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            das_url, "setChroot", data=io.BytesIO(b"abcxyz"), sha1sum=sha1
        )
        self.assertEqual(200, response.status)
        self.assertEndsWith(
            self.getWebserviceJSON(ws, das_url)["chroot_url"], expected_file
        )
        response = ws.named_post(das_url, "removeChroot")
        self.assertEqual(200, response.status)
        self.assertIsNone(self.getWebserviceJSON(ws, das_url)["chroot_url"])
        response = ws.named_post(
            das_url, "setChroot", data=io.BytesIO(b"abcxyz"), sha1sum=sha1
        )
        self.assertEqual(200, response.status)
        self.assertEndsWith(
            self.getWebserviceJSON(ws, das_url)["chroot_url"], expected_file
        )

    def test_setChroot_removeChroot_pocket(self):
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        user = das.distroseries.distribution.main_archive.owner
        sha1_1 = hashlib.sha1(b"abcxyz").hexdigest()
        sha1_2 = hashlib.sha1(b"123456").hexdigest()
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            das_url, "setChroot", data=io.BytesIO(b"abcxyz"), sha1sum=sha1_1
        )
        self.assertEqual(200, response.status)
        response = ws.named_post(
            das_url,
            "setChroot",
            data=io.BytesIO(b"123456"),
            sha1sum=sha1_2,
            pocket="Updates",
        )
        self.assertEqual(200, response.status)
        with person_logged_in(user):
            release_chroot = das.getChroot(
                pocket=PackagePublishingPocket.RELEASE
            )
            self.assertEqual(sha1_1, release_chroot.content.sha1)
            updates_chroot = das.getChroot(
                pocket=PackagePublishingPocket.UPDATES
            )
            self.assertEqual(sha1_2, updates_chroot.content.sha1)
            release_chroot_url = release_chroot.getURL()
            updates_chroot_url = updates_chroot.getURL()
        response = ws.named_get(das_url, "getChrootURL", pocket="Release")
        self.assertEqual(200, response.status)
        self.assertEqual(release_chroot_url, response.jsonBody())
        response = ws.named_get(das_url, "getChrootURL", pocket="Updates")
        self.assertEqual(200, response.status)
        self.assertEqual(updates_chroot_url, response.jsonBody())
        response = ws.named_get(das_url, "getChrootURL", pocket="Proposed")
        self.assertEqual(200, response.status)
        self.assertEqual(updates_chroot_url, response.jsonBody())
        response = ws.named_post(das_url, "removeChroot", pocket="Updates")
        response = ws.named_get(das_url, "getChrootURL", pocket="Release")
        self.assertEqual(200, response.status)
        self.assertEqual(release_chroot_url, response.jsonBody())
        response = ws.named_get(das_url, "getChrootURL", pocket="Updates")
        self.assertEqual(200, response.status)
        self.assertEqual(release_chroot_url, response.jsonBody())
        response = ws.named_get(das_url, "getChrootURL", pocket="Proposed")
        self.assertEqual(200, response.status)
        self.assertEqual(release_chroot_url, response.jsonBody())
        response = ws.named_post(
            das_url,
            "setChroot",
            data=io.BytesIO(b"123456"),
            sha1sum=sha1_2,
            pocket="Updates",
        )
        with person_logged_in(user):
            updates_chroot = das.getChroot(
                pocket=PackagePublishingPocket.UPDATES
            )
            self.assertEqual(sha1_2, updates_chroot.content.sha1)
            updates_chroot_url = updates_chroot.getURL()
        response = ws.named_get(das_url, "getChrootURL", pocket="Release")
        self.assertEqual(200, response.status)
        self.assertEqual(release_chroot_url, response.jsonBody())
        response = ws.named_get(das_url, "getChrootURL", pocket="Updates")
        self.assertEqual(200, response.status)
        self.assertEqual(updates_chroot_url, response.jsonBody())
        response = ws.named_get(das_url, "getChrootURL", pocket="Proposed")
        self.assertEqual(200, response.status)
        self.assertEqual(updates_chroot_url, response.jsonBody())

    def test_setChroot_removeChroot_image_type(self):
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        user = das.distroseries.distribution.main_archive.owner
        sha1_1 = hashlib.sha1(b"abcxyz").hexdigest()
        sha1_2 = hashlib.sha1(b"123456").hexdigest()
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            das_url, "setChroot", data=io.BytesIO(b"abcxyz"), sha1sum=sha1_1
        )
        self.assertEqual(200, response.status)
        response = ws.named_post(
            das_url,
            "setChroot",
            data=io.BytesIO(b"123456"),
            sha1sum=sha1_2,
            image_type="LXD image",
        )
        self.assertEqual(200, response.status)
        with person_logged_in(user):
            chroot_image = das.getChroot(image_type=BuildBaseImageType.CHROOT)
            self.assertEqual(sha1_1, chroot_image.content.sha1)
            lxd_image = das.getChroot(image_type=BuildBaseImageType.LXD)
            self.assertEqual(sha1_2, lxd_image.content.sha1)
            chroot_image_url = chroot_image.getURL()
            lxd_image_url = lxd_image.getURL()
        response = ws.named_get(
            das_url, "getChrootURL", image_type="Chroot tarball"
        )
        self.assertEqual(200, response.status)
        self.assertEqual(chroot_image_url, response.jsonBody())
        response = ws.named_get(
            das_url, "getChrootURL", image_type="LXD image"
        )
        self.assertEqual(200, response.status)
        self.assertEqual(lxd_image_url, response.jsonBody())
        response = ws.named_post(
            das_url, "removeChroot", image_type="LXD image"
        )
        response = ws.named_get(
            das_url, "getChrootURL", image_type="Chroot tarball"
        )
        self.assertEqual(200, response.status)
        self.assertEqual(chroot_image_url, response.jsonBody())
        response = ws.named_get(
            das_url, "getChrootURL", image_type="LXD image"
        )
        self.assertEqual(200, response.status)
        self.assertIsNone(response.jsonBody())

    def test_setChrootFromBuild(self):
        self.useFixture(FeatureFixture({LIVEFS_FEATURE_FLAG: "on"}))
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        build = self.factory.makeLiveFSBuild()
        build_url = api_url(build)
        login_as(build.livefs.owner)
        lfas = []
        for filename in (
            "livecd.ubuntu-base.rootfs.tar.gz",
            "livecd.ubuntu-base.manifest",
        ):
            lfa = self.factory.makeLibraryFileAlias(filename=filename)
            lfas.append(lfa)
            build.addFile(lfa)
        user = das.distroseries.distribution.main_archive.owner
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        ws.named_post(
            das_url,
            "setChrootFromBuild",
            livefsbuild=build_url,
            filename="livecd.ubuntu-base.rootfs.tar.gz",
        )
        login(ANONYMOUS)
        self.assertEqual(lfas[0], das.getChroot())

    def test_setChrootFromBuild_random_user(self):
        # Random users are not allowed to set chroots from a livefs build.
        self.useFixture(FeatureFixture({LIVEFS_FEATURE_FLAG: "on"}))
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        build = self.factory.makeLiveFSBuild()
        build_url = api_url(build)
        login_as(build.livefs.owner)
        build.addFile(
            self.factory.makeLibraryFileAlias(
                filename="livecd.ubuntu-base.rootfs.tar.gz"
            )
        )
        user = self.factory.makePerson()
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            das_url,
            "setChrootFromBuild",
            livefsbuild=build_url,
            filename="livecd.ubuntu-base.rootfs.tar.gz",
        )
        self.assertEqual(401, response.status)

    def test_setChrootFromBuild_private(self):
        # Chroots may not be set to the output of a private livefs build.
        self.useFixture(FeatureFixture({LIVEFS_FEATURE_FLAG: "on"}))
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        owner = self.factory.makePerson()
        private_team = self.factory.makeTeam(
            owner=owner, visibility=PersonVisibility.PRIVATE
        )
        login_as(owner)
        build = self.factory.makeLiveFSBuild(
            requester=owner, owner=private_team
        )
        build_url = api_url(build)
        build.addFile(
            self.factory.makeLibraryFileAlias(
                filename="livecd.ubuntu-base.rootfs.tar.gz"
            )
        )
        user = das.distroseries.distribution.main_archive.owner
        private_team.addMember(user, owner)
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = ws.named_post(
            das_url,
            "setChrootFromBuild",
            livefsbuild=build_url,
            filename="livecd.ubuntu-base.rootfs.tar.gz",
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=400, body=b"Cannot set chroot from a private build."
            ),
        )

    def test_setChrootFromBuild_pocket(self):
        self.useFixture(FeatureFixture({LIVEFS_FEATURE_FLAG: "on"}))
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        build = self.factory.makeLiveFSBuild()
        build_url = api_url(build)
        login_as(build.livefs.owner)
        lfa = self.factory.makeLibraryFileAlias(
            filename="livecd.ubuntu-base.rootfs.tar.gz"
        )
        build.addFile(lfa)
        user = das.distroseries.distribution.main_archive.owner
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            das_url,
            "setChrootFromBuild",
            livefsbuild=build_url,
            filename="livecd.ubuntu-base.rootfs.tar.gz",
            pocket="Updates",
        )
        self.assertEqual(200, response.status)
        login(ANONYMOUS)
        self.assertIsNone(
            das.getChroot(pocket=PackagePublishingPocket.RELEASE)
        )
        self.assertEqual(
            lfa, das.getChroot(pocket=PackagePublishingPocket.UPDATES)
        )

    def test_setChrootFromBuild_image_type(self):
        self.useFixture(FeatureFixture({LIVEFS_FEATURE_FLAG: "on"}))
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        build = self.factory.makeLiveFSBuild()
        build_url = api_url(build)
        login_as(build.livefs.owner)
        lfa = self.factory.makeLibraryFileAlias(
            filename="livecd.ubuntu-base.lxd.tar.gz"
        )
        build.addFile(lfa)
        user = das.distroseries.distribution.main_archive.owner
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            das_url,
            "setChrootFromBuild",
            livefsbuild=build_url,
            filename="livecd.ubuntu-base.lxd.tar.gz",
            image_type="LXD image",
        )
        self.assertEqual(200, response.status)
        login(ANONYMOUS)
        self.assertIsNone(das.getChroot(image_type=BuildBaseImageType.CHROOT))
        self.assertEqual(lfa, das.getChroot(image_type=BuildBaseImageType.LXD))

    def test_setSourceFilter_removeSourceFilter_random_user(self):
        # Random users are not allowed to set or remove filters.
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        packageset = self.factory.makePackageset(distroseries=das.distroseries)
        user = self.factory.makePerson()
        packageset_url = api_url(packageset)
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            das_url,
            "setSourceFilter",
            packageset=packageset_url,
            sense="Include",
        )
        self.assertEqual(401, response.status)
        response = ws.named_post(das_url, "removeSourceFilter")
        self.assertEqual(401, response.status)

    def test_setSourceFilter_wrong_distroseries(self):
        # Trying to set a filter using a packageset for the wrong
        # distroseries returns an error.
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        packageset = self.factory.makePackageset()
        user = das.distroseries.distribution.main_archive.owner
        packageset_url = api_url(packageset)
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            das_url,
            "setSourceFilter",
            packageset=packageset_url,
            sense="Include",
        )
        expected_error = (
            "The requested package set is for %s and cannot be set as a "
            "filter for %s %s."
            % (
                packageset.distroseries.fullseriesname,
                das.distroseries.fullseriesname,
                das.architecturetag,
            )
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=400, body=expected_error.encode()
            ),
        )

    def test_setSourceFilter_removeSourceFilter(self):
        das = self.factory.makeDistroArchSeries()
        das_url = api_url(das)
        packageset = self.factory.makePackageset(distroseries=das.distroseries)
        user = das.distroseries.distribution.main_archive.owner
        packageset_url = api_url(packageset)
        ws = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            das_url,
            "setSourceFilter",
            packageset=packageset_url,
            sense="Include",
        )
        self.assertEqual(200, response.status)
        response = ws.named_get(das_url, "getSourceFilter")
        self.assertEqual(200, response.status)
        self.assertThat(
            response.jsonBody(),
            ContainsDict(
                {
                    "packageset_link": EndsWith(packageset_url),
                    "sense": Equals("Include"),
                }
            ),
        )
        response = ws.named_post(
            das_url,
            "setSourceFilter",
            packageset=packageset_url,
            sense="Exclude",
        )
        self.assertEqual(200, response.status)
        response = ws.named_get(das_url, "getSourceFilter")
        self.assertEqual(200, response.status)
        self.assertThat(
            response.jsonBody(),
            ContainsDict(
                {
                    "packageset_link": EndsWith(packageset_url),
                    "sense": Equals("Exclude"),
                }
            ),
        )
        response = ws.named_post(das_url, "removeSourceFilter")
        self.assertEqual(200, response.status)
        response = ws.named_get(das_url, "getSourceFilter")
        self.assertEqual(200, response.status)
        self.assertIsNone(response.jsonBody())
