# Copyright 2010-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import json
import os.path
from datetime import datetime, timedelta, timezone

import responses
from testtools.matchers import (
    Contains,
    ContainsDict,
    EndsWith,
    Equals,
    MatchesListwise,
    MatchesRegex,
    MatchesStructure,
)
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.registry.interfaces.person import PersonVisibility
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.gpg.interfaces import IGPGHandler
from lp.services.webapp.authorization import check_permission
from lp.services.webapp.interfaces import OAuthPermission
from lp.soyuz.enums import (
    ArchivePermissionType,
    ArchivePurpose,
    PackagePublishingStatus,
)
from lp.soyuz.interfaces.component import IComponentSet
from lp.soyuz.interfaces.packagecopyjob import IPlainPackageCopyJobSource
from lp.soyuz.model.archivepermission import ArchivePermission
from lp.testing import (
    ANONYMOUS,
    TestCaseWithFactory,
    admin_logged_in,
    api_url,
    celebrity_logged_in,
    login,
    person_logged_in,
    record_two_runs,
)
from lp.testing.gpgkeys import gpgkeysdir
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.matchers import HasQueryCount
from lp.testing.pages import webservice_for_person


class TestArchiveWebservice(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        with admin_logged_in() as _admin:
            admin = _admin
            self.archive = self.factory.makeArchive(
                purpose=ArchivePurpose.PRIMARY
            )
            distroseries = self.factory.makeDistroSeries(
                distribution=self.archive.distribution
            )
            person = self.factory.makePerson()
        self.main_archive_url = api_url(self.archive)
        self.distroseries_url = api_url(distroseries)
        self.person_url = api_url(person)
        self.ws = webservice_for_person(
            admin,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )

    def test_checkUpload_bad_pocket(self):
        # Make sure a 403 error and not an OOPS is returned when
        # CannotUploadToPocket is raised when calling checkUpload.
        response = self.ws.named_get(
            self.main_archive_url,
            "checkUpload",
            distroseries=self.distroseries_url,
            sourcepackagename="mozilla-firefox",
            pocket="Updates",
            component="restricted",
            person=self.person_url,
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=403,
                body=(
                    b"Not permitted to upload to the UPDATES pocket in a "
                    b"series in the 'DEVELOPMENT' state."
                ),
            ),
        )

    def test_getAllPermissions_constant_query_count(self):
        # getAllPermissions has a query count constant in the number of
        # permissions and people.
        def create_permission():
            with admin_logged_in():
                ArchivePermission(
                    archive=self.archive,
                    person=self.factory.makePerson(),
                    component=getUtility(IComponentSet)["main"],
                    permission=ArchivePermissionType.UPLOAD,
                )

        def get_permissions():
            self.ws.named_get(self.main_archive_url, "getAllPermissions")

        recorder1, recorder2 = record_two_runs(
            get_permissions, create_permission, 1
        )
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    def test_delete(self):
        with admin_logged_in():
            ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
            ppa_url = api_url(ppa)
            ws = webservice_for_person(
                ppa.owner,
                permission=OAuthPermission.WRITE_PRIVATE,
                default_api_version="devel",
            )

        # DELETE on an archive resource doesn't actually remove it
        # immediately, but it asks the publisher to delete it later.
        self.assertEqual(
            "Active", self.getWebserviceJSON(ws, ppa_url)["status"]
        )
        self.assertEqual(200, ws.delete(ppa_url).status)
        self.assertEqual(
            "Deleting", self.getWebserviceJSON(ws, ppa_url)["status"]
        )

        # Deleting the PPA again fails.
        self.assertThat(
            ws.delete(ppa_url),
            MatchesStructure.byEquality(
                status=400, body=b"Archive already deleted."
            ),
        )

    def test_delete_is_restricted(self):
        with admin_logged_in():
            ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
            ppa_url = api_url(ppa)
            ws = webservice_for_person(
                self.factory.makePerson(),
                permission=OAuthPermission.WRITE_PRIVATE,
                default_api_version="devel",
            )

        # A random user can't delete someone else's PPA.
        self.assertEqual(401, ws.delete(ppa_url).status)

    def test_publishing_enabled_exposed(self):
        with admin_logged_in():
            archive = self.factory.makeArchive()
            archive_url = api_url(archive)
            ws = webservice_for_person(
                archive.owner,
                permission=OAuthPermission.WRITE_PRIVATE,
                default_api_version="devel",
            )

        ws_archive = self.getWebserviceJSON(ws, archive_url)

        # It's exposed via API.
        self.assertTrue(ws_archive["publish"])

    def test_publishing_enabled_false(self):
        with admin_logged_in():
            archive = self.factory.makeArchive()
            archive_url = api_url(archive)
            ws = webservice_for_person(
                archive.owner,
                permission=OAuthPermission.WRITE_PRIVATE,
                default_api_version="devel",
            )

        # Setting it to False works.
        response = ws.patch(
            archive_url, "application/json", json.dumps({"publish": False})
        )
        self.assertEqual(209, response.status)

        ws_archive = self.getWebserviceJSON(ws, archive_url)
        self.assertFalse(ws_archive["publish"])

    def test_publishing_enabled_true_active(self):
        with admin_logged_in():
            archive = self.factory.makeArchive()
            archive_url = api_url(archive)
            ws = webservice_for_person(
                archive.owner,
                permission=OAuthPermission.WRITE_PRIVATE,
                default_api_version="devel",
            )
            response = ws.patch(
                archive_url, "application/json", json.dumps({"publish": False})
            )
            self.assertEqual(209, response.status)

        # Setting it back to True works because archive is Active.
        ws.patch(
            archive_url, "application/json", json.dumps({"publish": True})
        )

        ws_archive = self.getWebserviceJSON(ws, archive_url)
        self.assertTrue(ws_archive["publish"])

    def test_publishing_enabled_true_not_active(self):
        with admin_logged_in():
            archive = self.factory.makeArchive()
            archive_url = api_url(archive)
            ws = webservice_for_person(
                archive.owner,
                permission=OAuthPermission.WRITE_PRIVATE,
                default_api_version="devel",
            )
            response = ws.patch(
                archive_url, "application/json", json.dumps({"publish": False})
            )
            self.assertEqual(209, response.status)
            response = ws.delete(archive_url)
            self.assertEqual(200, response.status)
            ws_archive = self.getWebserviceJSON(ws, archive_url)
            self.assertEqual("Deleting", ws_archive["status"])

            # Setting it to True with archive status
            # different from Active won't work.
            response = ws.patch(
                archive_url, "application/json", json.dumps({"publish": True})
            )

            self.assertEqual(400, response.status)
            self.assertEqual(b"Deleted PPAs can't be enabled.", response.body)


class TestSigningKey(TestCaseWithFactory):
    """Test signing-key-related information for archives.

    We just use `responses` to mock the keyserver here; the details of its
    implementation aren't especially important, we can't use
    `InProcessKeyServerFixture` because the keyserver operations are
    synchronous, and `responses` is much faster than `KeyServerTac`.
    """

    layer = DatabaseFunctionalLayer

    def _setUpSigningKey(self, archive):
        key_path = os.path.join(gpgkeysdir, "ppa-sample@canonical.com.sec")
        gpghandler = getUtility(IGPGHandler)
        with open(key_path, "rb") as key_file:
            secret_key = gpghandler.importSecretKey(key_file.read())
        public_key = gpghandler.retrieveKey(secret_key.fingerprint)
        public_key_data = public_key.export()
        removeSecurityProxy(archive).signing_key_fingerprint = (
            public_key.fingerprint
        )
        key_url = gpghandler.getURLForKeyInServer(
            public_key.fingerprint, action="get"
        )
        responses.add("GET", key_url, body=public_key_data)
        gpghandler.resetLocalState()
        return public_key.fingerprint, public_key_data

    @responses.activate
    def test_signing_key_public(self):
        # Anyone can read signing key information for public archives.
        archive = self.factory.makeArchive()
        fingerprint, public_key_data = self._setUpSigningKey(archive)
        archive_url = api_url(archive)
        ws = webservice_for_person(
            self.factory.makePerson(), default_api_version="devel"
        )
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        self.assertEqual(fingerprint, ws_archive["signing_key_fingerprint"])
        response = ws.named_get(archive_url, "getSigningKeyData")
        self.assertEqual(200, response.status)
        self.assertEqual(public_key_data.decode("ASCII"), response.jsonBody())

    @responses.activate
    def test_signing_key_private_subscriber(self):
        # Subscribers can read signing key information for private archives.
        archive = self.factory.makeArchive(private=True)
        fingerprint, public_key_data = self._setUpSigningKey(archive)
        subscriber = self.factory.makePerson()
        with person_logged_in(archive.owner):
            archive.newSubscription(subscriber, archive.owner)
        archive_url = api_url(archive)
        ws = webservice_for_person(
            subscriber,
            permission=OAuthPermission.READ_PRIVATE,
            default_api_version="devel",
        )
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        self.assertEqual(fingerprint, ws_archive["signing_key_fingerprint"])
        response = ws.named_get(archive_url, "getSigningKeyData")
        self.assertEqual(200, response.status)
        self.assertEqual(public_key_data.decode("ASCII"), response.jsonBody())

    @responses.activate
    def test_signing_key_private_non_subscriber(self):
        # Non-subscribers cannot read signing key information (or indeed
        # anything else) for private archives.
        archive = self.factory.makeArchive(private=True)
        fingerprint, public_key_data = self._setUpSigningKey(archive)
        archive_url = api_url(archive)
        ws = webservice_for_person(
            self.factory.makePerson(),
            permission=OAuthPermission.READ_PRIVATE,
            default_api_version="devel",
        )
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        self.assertEqual(
            "tag:launchpad.net:2008:redacted",
            ws_archive["signing_key_fingerprint"],
        )
        response = ws.named_get(archive_url, "getSigningKeyData")
        self.assertEqual(401, response.status)


class TestExternalDependencies(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_external_dependencies_random_user(self):
        """Normal users can look but not touch."""
        archive = self.factory.makeArchive()
        archive_url = api_url(archive)
        ws = webservice_for_person(
            self.factory.makePerson(), permission=OAuthPermission.WRITE_PUBLIC
        )
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        self.assertIsNone(ws_archive["external_dependencies"])
        response = ws.patch(
            archive_url,
            "application/json",
            json.dumps({"external_dependencies": "random"}),
        )
        self.assertEqual(401, response.status)

    def test_external_dependencies_owner(self):
        """Normal archive owners can look but not touch."""
        archive = self.factory.makeArchive()
        archive_url = api_url(archive)
        ws = webservice_for_person(
            archive.owner, permission=OAuthPermission.WRITE_PUBLIC
        )
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        self.assertIsNone(ws_archive["external_dependencies"])
        response = ws.patch(
            archive_url,
            "application/json",
            json.dumps({"external_dependencies": "random"}),
        )
        self.assertEqual(401, response.status)

    def test_external_dependencies_ppa_owner_invalid(self):
        """PPA admins can look and touch."""
        ppa_admin_team = getUtility(ILaunchpadCelebrities).ppa_admin
        ppa_admin = self.factory.makePerson(member_of=[ppa_admin_team])
        archive = self.factory.makeArchive(owner=ppa_admin)
        archive_url = api_url(archive)
        ws = webservice_for_person(
            archive.owner, permission=OAuthPermission.WRITE_PUBLIC
        )
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        self.assertIsNone(ws_archive["external_dependencies"])
        response = ws.patch(
            archive_url,
            "application/json",
            json.dumps({"external_dependencies": "random"}),
        )
        self.assertThat(
            response,
            MatchesStructure(
                status=Equals(400),
                body=Contains(b"Invalid external dependencies"),
            ),
        )

    def test_external_dependencies_ppa_owner_valid(self):
        """PPA admins can look and touch."""
        ppa_admin_team = getUtility(ILaunchpadCelebrities).ppa_admin
        ppa_admin = self.factory.makePerson(member_of=[ppa_admin_team])
        archive = self.factory.makeArchive(owner=ppa_admin)
        archive_url = api_url(archive)
        ws = webservice_for_person(
            archive.owner, permission=OAuthPermission.WRITE_PUBLIC
        )
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        self.assertIsNone(ws_archive["external_dependencies"])
        response = ws.patch(
            archive_url,
            "application/json",
            json.dumps(
                {
                    "external_dependencies": (
                        "deb http://example.org suite components"
                    ),
                }
            ),
        )
        self.assertEqual(209, response.status)


class TestArchiveDependencies(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_addArchiveDependency_random_user(self):
        """Normal users cannot add archive dependencies."""
        archive = self.factory.makeArchive()
        dependency = self.factory.makeArchive()
        archive_url = api_url(archive)
        dependency_url = api_url(dependency)
        ws = webservice_for_person(
            self.factory.makePerson(),
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        ws_dependencies = self.getWebserviceJSON(
            ws, ws_archive["dependencies_collection_link"]
        )
        self.assertEqual([], ws_dependencies["entries"])
        response = ws.named_post(
            archive_url,
            "addArchiveDependency",
            dependency=dependency_url,
            pocket="Release",
            component="main",
        )
        self.assertThat(
            response,
            MatchesStructure(
                status=Equals(401),
                body=MatchesRegex(
                    rb".*addArchiveDependency.*launchpad.Edit.*"
                ),
            ),
        )

    def test_addArchiveDependency_owner(self):
        """Archive owners can add archive dependencies."""
        archive = self.factory.makeArchive()
        dependency = self.factory.makeArchive()
        archive_url = api_url(archive)
        dependency_url = api_url(dependency)
        ws = webservice_for_person(
            archive.owner,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        ws_dependencies = self.getWebserviceJSON(
            ws, ws_archive["dependencies_collection_link"]
        )
        self.assertEqual([], ws_dependencies["entries"])
        response = ws.named_post(
            archive_url,
            "addArchiveDependency",
            dependency=dependency_url,
            pocket="Release",
            component="asdf",
        )
        self.assertThat(
            response,
            MatchesStructure(status=Equals(404), body=Contains(b"asdf")),
        )
        response = ws.named_post(
            archive_url,
            "addArchiveDependency",
            dependency=dependency_url,
            pocket="Release",
            component="main",
        )
        self.assertEqual(201, response.status)
        archive_dependency_url = response.getHeader("Location")
        ws_dependencies = self.getWebserviceJSON(
            ws, ws_archive["dependencies_collection_link"]
        )
        self.assertThat(
            ws_dependencies["entries"],
            MatchesListwise(
                [
                    ContainsDict(
                        {"self_link": Equals(archive_dependency_url)}
                    ),
                ]
            ),
        )

    def test_addArchiveDependency_invalid(self):
        """Invalid requests generate a BadRequest error."""
        archive = self.factory.makeArchive()
        dependency = self.factory.makeArchive()
        with person_logged_in(archive.owner):
            archive.addArchiveDependency(
                dependency, PackagePublishingPocket.RELEASE
            )
        archive_url = api_url(archive)
        dependency_url = api_url(dependency)
        ws = webservice_for_person(
            archive.owner,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            archive_url,
            "addArchiveDependency",
            dependency=dependency_url,
            pocket="Release",
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=400, body=b"This dependency is already registered."
            ),
        )

    def test_removeArchiveDependency_random_user(self):
        """Normal users cannot remove archive dependencies."""
        archive = self.factory.makeArchive()
        dependency = self.factory.makeArchive()
        with person_logged_in(archive.owner):
            archive.addArchiveDependency(
                dependency, PackagePublishingPocket.RELEASE
            )
        archive_url = api_url(archive)
        dependency_url = api_url(dependency)
        ws = webservice_for_person(
            self.factory.makePerson(),
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            archive_url, "removeArchiveDependency", dependency=dependency_url
        )
        self.assertThat(
            response,
            MatchesStructure(
                status=Equals(401),
                body=MatchesRegex(
                    rb".*removeArchiveDependency.*launchpad.Edit.*"
                ),
            ),
        )

    def test_removeArchiveDependency_owner(self):
        """Archive owners can remove archive dependencies."""
        archive = self.factory.makeArchive()
        dependency = self.factory.makeArchive()
        with person_logged_in(archive.owner):
            archive.addArchiveDependency(
                dependency, PackagePublishingPocket.RELEASE
            )
        archive_url = api_url(archive)
        dependency_url = api_url(dependency)
        ws = webservice_for_person(
            archive.owner,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            archive_url, "removeArchiveDependency", dependency=dependency_url
        )
        self.assertEqual(200, response.status)
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        ws_dependencies = self.getWebserviceJSON(
            ws, ws_archive["dependencies_collection_link"]
        )
        self.assertEqual([], ws_dependencies["entries"])


class TestProcessors(TestCaseWithFactory):
    """Test the enabled_restricted_processors property and methods."""

    layer = DatabaseFunctionalLayer

    def test_erpNotAvailableInBeta(self):
        """The enabled_restricted_processors property is not in beta."""
        archive = self.factory.makeArchive()
        ppa_admin_team = getUtility(ILaunchpadCelebrities).ppa_admin
        ppa_admin = self.factory.makePerson(member_of=[ppa_admin_team])
        archive_url = api_url(archive)
        ws = webservice_for_person(ppa_admin, default_api_version="beta")
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        self.assertNotIn(
            "enabled_restricted_processors_collection_link", ws_archive
        )

    def test_erpAvailableInDevel(self):
        """The enabled_restricted_processors property is in devel."""
        archive = self.factory.makeArchive()
        ppa_admin_team = getUtility(ILaunchpadCelebrities).ppa_admin
        ppa_admin = self.factory.makePerson(member_of=[ppa_admin_team])
        archive_url = api_url(archive)
        ws = webservice_for_person(ppa_admin, default_api_version="devel")
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        ws_erp = self.getWebserviceJSON(
            ws, ws_archive["enabled_restricted_processors_collection_link"]
        )
        self.assertEqual([], ws_erp["entries"])

    def test_processors(self):
        """Attributes about processors are available."""
        self.factory.makeProcessor(
            "new-arm", "New ARM Title", "New ARM Description"
        )
        ws = webservice_for_person(
            self.factory.makePerson(), default_api_version="devel"
        )
        response = ws.named_get("/+processors", "getByName", name="new-arm")
        self.assertEqual(200, response.status)
        self.assertThat(
            response.jsonBody(),
            ContainsDict(
                {
                    "name": Equals("new-arm"),
                    "title": Equals("New ARM Title"),
                    "description": Equals("New ARM Description"),
                }
            ),
        )

    def setProcessors(self, user, archive_url, names):
        ws = webservice_for_person(
            user, permission=OAuthPermission.WRITE_PUBLIC
        )
        return ws.named_post(
            archive_url,
            "setProcessors",
            processors=["/+processors/%s" % name for name in names],
            api_version="devel",
        )

    def assertProcessors(self, user, archive_url, names):
        body = (
            webservice_for_person(user)
            .get(archive_url + "/processors", api_version="devel")
            .jsonBody()
        )
        self.assertContentEqual(
            names, [entry["name"] for entry in body["entries"]]
        )

    def test_setProcessors_admin(self):
        """An admin can add a new processor to the enabled restricted set."""
        ppa_admin_team = getUtility(ILaunchpadCelebrities).ppa_admin
        ppa_admin = self.factory.makePerson(member_of=[ppa_admin_team])
        self.factory.makeProcessor(
            "arm", "ARM", "ARM", restricted=True, build_by_default=False
        )
        ppa_url = api_url(self.factory.makeArchive(purpose=ArchivePurpose.PPA))
        self.assertProcessors(ppa_admin, ppa_url, ["386", "hppa", "amd64"])

        response = self.setProcessors(ppa_admin, ppa_url, ["386", "arm"])
        self.assertEqual(200, response.status)
        self.assertProcessors(ppa_admin, ppa_url, ["386", "arm"])

    def test_setProcessors_non_owner_forbidden(self):
        """Only PPA admins and archive owners can call setProcessors."""
        self.factory.makeProcessor(
            "unrestricted",
            "Unrestricted",
            "Unrestricted",
            restricted=False,
            build_by_default=False,
        )
        ppa_url = api_url(self.factory.makeArchive(purpose=ArchivePurpose.PPA))

        response = self.setProcessors(
            self.factory.makePerson(), ppa_url, ["386", "unrestricted"]
        )
        self.assertEqual(401, response.status)

    def test_setProcessors_owner(self):
        """The archive owner can enable/disable unrestricted processors."""
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        ppa_url = api_url(archive)
        owner = archive.owner
        self.assertProcessors(owner, ppa_url, ["386", "hppa", "amd64"])

        response = self.setProcessors(owner, ppa_url, ["386"])
        self.assertEqual(200, response.status)
        self.assertProcessors(owner, ppa_url, ["386"])

        response = self.setProcessors(owner, ppa_url, ["386", "amd64"])
        self.assertEqual(200, response.status)
        self.assertProcessors(owner, ppa_url, ["386", "amd64"])

    def test_setProcessors_owner_restricted_forbidden(self):
        """The archive owner cannot enable/disable restricted processors."""
        ppa_admin_team = getUtility(ILaunchpadCelebrities).ppa_admin
        ppa_admin = self.factory.makePerson(member_of=[ppa_admin_team])
        self.factory.makeProcessor(
            "arm", "ARM", "ARM", restricted=True, build_by_default=False
        )
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        ppa_url = api_url(archive)
        owner = archive.owner

        response = self.setProcessors(owner, ppa_url, ["386", "arm"])
        self.assertEqual(403, response.status)

        # If a PPA admin enables arm, the owner cannot disable it.
        response = self.setProcessors(ppa_admin, ppa_url, ["386", "arm"])
        self.assertEqual(200, response.status)
        self.assertProcessors(owner, ppa_url, ["386", "arm"])

        response = self.setProcessors(owner, ppa_url, ["386"])
        self.assertEqual(403, response.status)

    def test_enableRestrictedProcessor(self):
        """A new processor can be added to the enabled restricted set."""
        archive = self.factory.makeArchive()
        arm = self.factory.makeProcessor(
            name="arm", restricted=True, build_by_default=False
        )
        ppa_admin_team = getUtility(ILaunchpadCelebrities).ppa_admin
        ppa_admin = self.factory.makePerson(member_of=[ppa_admin_team])
        archive_url = api_url(archive)
        arm_url = api_url(arm)
        ws = webservice_for_person(
            ppa_admin,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        ws_erp = self.getWebserviceJSON(
            ws, ws_archive["enabled_restricted_processors_collection_link"]
        )
        self.assertEqual([], ws_erp["entries"])
        response = ws.named_post(
            archive_url, "enableRestrictedProcessor", processor=arm_url
        )
        self.assertEqual(200, response.status)
        ws_erp = self.getWebserviceJSON(
            ws, ws_archive["enabled_restricted_processors_collection_link"]
        )
        self.assertThat(
            ws_erp["entries"],
            MatchesListwise(
                [
                    ContainsDict({"self_link": EndsWith(arm_url)}),
                ]
            ),
        )

    def test_enableRestrictedProcessor_owner(self):
        """A new processor can be added to the enabled restricted set.

        An unauthorized user, even the archive owner, is not allowed.
        """
        archive = self.factory.makeArchive()
        arm = self.factory.makeProcessor(
            name="arm", restricted=True, build_by_default=False
        )
        archive_url = api_url(archive)
        arm_url = api_url(arm)
        ws = webservice_for_person(
            archive.owner,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        ws_erp = self.getWebserviceJSON(
            ws, ws_archive["enabled_restricted_processors_collection_link"]
        )
        self.assertEqual([], ws_erp["entries"])
        response = ws.named_post(
            archive_url, "enableRestrictedProcessor", processor=arm_url
        )
        self.assertThat(
            response,
            MatchesStructure(
                status=Equals(401), body=Contains(b"'launchpad.Admin'")
            ),
        )

    def test_enableRestrictedProcessor_nonPrivUser(self):
        """A new processor can be added to the enabled restricted set.

        An unauthorized user, some regular user, is not allowed.
        """
        archive = self.factory.makeArchive()
        arm = self.factory.makeProcessor(
            name="arm", restricted=True, build_by_default=False
        )
        archive_url = api_url(archive)
        arm_url = api_url(arm)
        ws = webservice_for_person(
            self.factory.makePerson(),
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        ws_archive = self.getWebserviceJSON(ws, archive_url)
        ws_erp = self.getWebserviceJSON(
            ws, ws_archive["enabled_restricted_processors_collection_link"]
        )
        self.assertEqual([], ws_erp["entries"])
        response = ws.named_post(
            archive_url, "enableRestrictedProcessor", processor=arm_url
        )
        self.assertThat(
            response,
            MatchesStructure(
                status=Equals(401), body=Contains(b"'launchpad.Admin'")
            ),
        )


class TestCopyPackage(TestCaseWithFactory):
    """Webservice test cases for the copyPackage/copyPackages methods"""

    layer = DatabaseFunctionalLayer

    def setup_data(self):
        uploader_dude = self.factory.makePerson()
        sponsored_dude = self.factory.makePerson()
        source_archive = self.factory.makeArchive()
        target_archive = self.factory.makeArchive(
            purpose=ArchivePurpose.PRIMARY
        )
        source = self.factory.makeSourcePackagePublishingHistory(
            archive=source_archive, status=PackagePublishingStatus.PUBLISHED
        )
        source_name = source.source_package_name
        version = source.source_package_version
        to_pocket = PackagePublishingPocket.RELEASE
        to_series = self.factory.makeDistroSeries(
            distribution=target_archive.distribution
        )
        with person_logged_in(target_archive.owner):
            target_archive.newComponentUploader(uploader_dude, "universe")
        return (
            source,
            source_archive,
            source_name,
            target_archive,
            to_pocket,
            to_series,
            uploader_dude,
            sponsored_dude,
            version,
        )

    def test_copyPackage(self):
        """Basic smoke test"""
        (
            source,
            source_archive,
            source_name,
            target_archive,
            to_pocket,
            to_series,
            uploader_dude,
            sponsored_dude,
            version,
        ) = self.setup_data()

        target_archive_url = api_url(target_archive)
        source_archive_url = api_url(source_archive)
        sponsored_dude_url = api_url(sponsored_dude)
        ws = webservice_for_person(
            uploader_dude,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )

        response = ws.named_post(
            target_archive_url,
            "copyPackage",
            source_name=source_name,
            version=version,
            from_archive=source_archive_url,
            to_pocket=to_pocket.name,
            to_series=to_series.name,
            include_binaries=False,
            sponsored=sponsored_dude_url,
        )
        self.assertEqual(200, response.status)

        login(ANONYMOUS)
        job_source = getUtility(IPlainPackageCopyJobSource)
        copy_job = job_source.getActiveJobs(target_archive).one()
        self.assertEqual(target_archive, copy_job.target_archive)
        self.assertFalse(copy_job.move)

    def test_copyPackage_move(self):
        (
            source,
            source_archive,
            source_name,
            target_archive,
            to_pocket,
            to_series,
            uploader,
            _,
            version,
        ) = self.setup_data()
        with person_logged_in(source_archive.owner):
            source_archive.newComponentUploader(uploader, "main")

        target_archive_url = api_url(target_archive)
        source_archive_url = api_url(source_archive)
        ws = webservice_for_person(
            uploader,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )

        response = ws.named_post(
            target_archive_url,
            "copyPackage",
            source_name=source_name,
            version=version,
            from_archive=source_archive_url,
            to_pocket=to_pocket.name,
            to_series=to_series.name,
            include_binaries=False,
            move=True,
        )
        self.assertEqual(200, response.status)

        login(ANONYMOUS)
        job_source = getUtility(IPlainPackageCopyJobSource)
        copy_job = job_source.getActiveJobs(target_archive).one()
        self.assertEqual(target_archive, copy_job.target_archive)
        self.assertTrue(copy_job.move)

    def test_copyPackages(self):
        """Basic smoke test"""
        (
            source,
            source_archive,
            source_name,
            target_archive,
            to_pocket,
            to_series,
            uploader_dude,
            sponsored_dude,
            version,
        ) = self.setup_data()
        from_series = source.distroseries

        target_archive_url = api_url(target_archive)
        source_archive_url = api_url(source_archive)
        sponsored_dude_url = api_url(sponsored_dude)
        ws = webservice_for_person(
            uploader_dude,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )

        response = ws.named_post(
            target_archive_url,
            "copyPackages",
            source_names=[source_name],
            from_archive=source_archive_url,
            to_pocket=to_pocket.name,
            to_series=to_series.name,
            from_series=from_series.name,
            include_binaries=False,
            sponsored=sponsored_dude_url,
        )
        self.assertEqual(200, response.status)

        login(ANONYMOUS)
        job_source = getUtility(IPlainPackageCopyJobSource)
        copy_job = job_source.getActiveJobs(target_archive).one()
        self.assertEqual(target_archive, copy_job.target_archive)
        self.assertFalse(copy_job.move)


class TestGetPublishedBinaries(TestCaseWithFactory):
    """Test getPublishedBinaries."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()
        self.archive = self.factory.makeArchive()
        self.person_url = api_url(self.person)
        self.archive_url = api_url(self.archive)

    def test_getPublishedBinaries(self):
        self.factory.makeBinaryPackagePublishingHistory(archive=self.archive)
        ws = webservice_for_person(self.person, default_api_version="beta")
        response = ws.named_get(self.archive_url, "getPublishedBinaries")
        self.assertEqual(200, response.status)
        self.assertEqual(1, response.jsonBody()["total_size"])

    def test_getPublishedBinaries_private_subscriber(self):
        # Subscribers can see published binaries for private archives.
        private_archive = self.factory.makeArchive(private=True)
        with admin_logged_in():
            self.factory.makeBinaryPackagePublishingHistory(
                archive=private_archive
            )
        subscriber = self.factory.makePerson()
        with person_logged_in(private_archive.owner):
            private_archive.newSubscription(subscriber, private_archive.owner)
        archive_url = api_url(private_archive)
        ws = webservice_for_person(
            subscriber,
            permission=OAuthPermission.READ_PRIVATE,
            default_api_version="devel",
        )
        response = ws.named_get(archive_url, "getPublishedBinaries")
        self.assertEqual(200, response.status)
        self.assertEqual(1, response.jsonBody()["total_size"])

    def test_getPublishedBinaries_private_non_subscriber(self):
        private_archive = self.factory.makeArchive(private=True)
        archive_url = api_url(private_archive)
        ws = webservice_for_person(
            self.factory.makePerson(),
            permission=OAuthPermission.READ_PRIVATE,
            default_api_version="devel",
        )
        response = ws.named_get(archive_url, "getPublishedBinaries")
        self.assertEqual(401, response.status)

    def test_getPublishedBinaries_created_since_date(self):
        datecreated = self.factory.getUniqueDate()
        later_date = datecreated + timedelta(minutes=1)
        self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive, datecreated=datecreated
        )
        ws = webservice_for_person(self.person, default_api_version="beta")
        response = ws.named_get(
            self.archive_url,
            "getPublishedBinaries",
            created_since_date=later_date.isoformat(),
        )
        self.assertEqual(200, response.status)
        self.assertEqual(0, response.jsonBody()["total_size"])

    def test_getPublishedBinaries_filter_by_distroseries(self):
        with admin_logged_in():
            distroseries1 = self.factory.makeDistroSeries(
                distribution=self.archive.distribution
            )
            distroseries2 = self.factory.makeDistroSeries(
                distribution=self.archive.distribution
            )
            das1 = self.factory.makeDistroArchSeries(
                distroseries=distroseries1
            )
            das2 = self.factory.makeDistroArchSeries(
                distroseries=distroseries2
            )
            pub1 = self.factory.makeBinaryPackagePublishingHistory(
                archive=self.archive, distroarchseries=das1
            )
            self.factory.makeBinaryPackagePublishingHistory(
                archive=self.archive, distroarchseries=das2
            )
            distro_series_url = api_url(distroseries1)
            pub1_url = api_url(pub1)

        ws = webservice_for_person(self.person, default_api_version="beta")
        response = ws.named_get(
            self.archive_url,
            "getPublishedBinaries",
            distro_series=distro_series_url,
        )
        self.assertEqual(200, response.status)
        self.assertEqual(1, response.jsonBody()["total_size"])
        self.assertIn(pub1_url, response.jsonBody()["entries"][0]["self_link"])

    def test_getPublishedBinaries_no_ordering(self):
        self.factory.makeBinaryPackagePublishingHistory(archive=self.archive)
        self.factory.makeBinaryPackagePublishingHistory(archive=self.archive)
        ws = webservice_for_person(self.person, default_api_version="beta")
        response = ws.named_get(
            self.archive_url, "getPublishedBinaries", ordered=False
        )
        self.assertEqual(200, response.status)
        self.assertEqual(2, response.jsonBody()["total_size"])

    def test_getPublishedBinaries_order_by_published_descending(self):
        now = datetime.now(timezone.utc)
        pub0 = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive, status=PackagePublishingStatus.PUBLISHED
        )
        removeSecurityProxy(pub0).datepublished = now - timedelta(days=2)
        pub1 = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive, status=PackagePublishingStatus.PUBLISHED
        )
        removeSecurityProxy(pub1).datepublished = now - timedelta(days=1)
        pub2 = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive, status=PackagePublishingStatus.PUBLISHED
        )
        removeSecurityProxy(pub2).datepublished = now

        pub0_id = removeSecurityProxy(pub0).id
        pub1_id = removeSecurityProxy(pub1).id
        pub2_id = removeSecurityProxy(pub2).id

        ws = webservice_for_person(self.person, default_api_version="beta")
        response = ws.named_get(
            self.archive_url,
            "getPublishedBinaries",
            order_by="published_date_desc",
        )
        self.assertEqual(200, response.status)
        self.assertEqual(3, response.jsonBody()["total_size"])
        entries = response.jsonBody()["entries"]
        self.assertEqual(3, len(entries))
        self.assertTrue(
            entries[0]["self_link"].endswith(f"+binarypub/{pub2_id}")
        )
        self.assertTrue(
            entries[1]["self_link"].endswith(f"+binarypub/{pub1_id}")
        )
        self.assertTrue(
            entries[2]["self_link"].endswith(f"+binarypub/{pub0_id}")
        )

    def test_getPublishedBinaries_order_by_published_ascending(self):
        now = datetime.now(timezone.utc)
        pub0 = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive, status=PackagePublishingStatus.PUBLISHED
        )
        removeSecurityProxy(pub0).datepublished = now - timedelta(days=2)
        pub1 = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive, status=PackagePublishingStatus.PUBLISHED
        )
        removeSecurityProxy(pub1).datepublished = now - timedelta(days=1)
        pub2 = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive, status=PackagePublishingStatus.PUBLISHED
        )
        removeSecurityProxy(pub2).datepublished = now

        pub0_id = removeSecurityProxy(pub0).id
        pub1_id = removeSecurityProxy(pub1).id
        pub2_id = removeSecurityProxy(pub2).id

        ws = webservice_for_person(self.person, default_api_version="beta")
        response_asc = ws.named_get(
            self.archive_url,
            "getPublishedBinaries",
            order_by="published_date_asc",
        )
        self.assertEqual(200, response_asc.status)
        self.assertEqual(3, response_asc.jsonBody()["total_size"])
        entries_asc = response_asc.jsonBody()["entries"]
        self.assertEqual(3, len(entries_asc))
        self.assertTrue(
            entries_asc[0]["self_link"].endswith(f"+binarypub/{pub0_id}")
        )
        self.assertTrue(
            entries_asc[1]["self_link"].endswith(f"+binarypub/{pub1_id}")
        )
        self.assertTrue(
            entries_asc[2]["self_link"].endswith(f"+binarypub/{pub2_id}")
        )

    def test_getPublishedBinaries_query_count(self):
        # getPublishedBinaries has a query count constant in the number of
        # packages returned.
        archive_url = api_url(self.archive)
        webservice = webservice_for_person(None)

        def create_bpph():
            with admin_logged_in():
                self.factory.makeBinaryPackagePublishingHistory(
                    archive=self.archive
                )

        def get_binaries():
            webservice.named_get(
                archive_url, "getPublishedBinaries"
            ).jsonBody()

        recorder1, recorder2 = record_two_runs(get_binaries, create_bpph, 1)
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    def test_getPublishedBinaries_query_count_private_archive(self):
        # getPublishedBinaries has a query count (almost) constant in the
        # number of packages returned, even for private archives.
        archive = self.factory.makeArchive(private=True)
        uploader = self.factory.makePerson()
        with person_logged_in(archive.owner):
            archive.newComponentUploader(uploader, archive.default_component)
        archive_url = api_url(archive)
        ws = webservice_for_person(
            uploader, permission=OAuthPermission.READ_PRIVATE
        )

        def create_bpph():
            with admin_logged_in():
                self.factory.makeBinaryPackagePublishingHistory(
                    archive=archive
                )

        def get_binaries():
            ws.named_get(archive_url, "getPublishedBinaries").jsonBody()

        recorder1, recorder2 = record_two_runs(get_binaries, create_bpph, 1)
        # XXX cjwatson 2019-07-01: There are still some O(n) queries from
        # security adapters (e.g. ViewSourcePackageRelease) that are
        # currently hard to avoid.  To fix this properly, I think we somehow
        # need to arrange for AuthorizationBase.forwardCheckAuthenticated to
        # be able to use iter_authorization's cache.
        self.assertThat(
            recorder2, HasQueryCount(Equals(recorder1.count + 3), recorder1)
        )

    def test_getPublishedBinaries_filter_by_component(self):
        # self.archive cannot be used, as this is a PPA, which only
        # supports a single "main" component
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PRIMARY)
        archive_url = api_url(archive)
        for component in ("main", "main", "universe"):
            self.factory.makeBinaryPackagePublishingHistory(
                archive=archive, component=component
            )
        ws = webservice_for_person(self.person, default_api_version="devel")

        for component, expected_count in (
            ("main", 2),
            ("universe", 1),
            ("restricted", 0),
        ):
            response = ws.named_get(
                archive_url, "getPublishedBinaries", component_name=component
            )

            self.assertEqual(200, response.status)
            self.assertEqual(expected_count, response.jsonBody()["total_size"])
            for entry in response.jsonBody()["entries"]:
                self.assertEqual(component, entry["component_name"])

    def test_getPublishedBinaries_published_since_until(self):
        now = datetime.now(timezone.utc)
        pub0 = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive, status=PackagePublishingStatus.PUBLISHED
        )
        removeSecurityProxy(pub0).datepublished = now - timedelta(days=2)
        pub1 = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive, status=PackagePublishingStatus.PUBLISHED
        )
        removeSecurityProxy(pub1).datepublished = now - timedelta(days=1)
        pub2 = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive, status=PackagePublishingStatus.PUBLISHED
        )
        removeSecurityProxy(pub2).datepublished = now

        ws = webservice_for_person(self.person, default_api_version="beta")

        # Query with published_since
        since_date = now - timedelta(hours=12)
        response = ws.named_get(
            self.archive_url,
            "getPublishedBinaries",
            published_since=since_date.isoformat(),
        )
        self.assertEqual(200, response.status)
        self.assertEqual(1, response.jsonBody()["total_size"])

        # Query with published_until
        until_date = now - timedelta(hours=12)
        response = ws.named_get(
            self.archive_url,
            "getPublishedBinaries",
            published_until=until_date.isoformat(),
        )
        self.assertEqual(200, response.status)
        self.assertEqual(2, response.jsonBody()["total_size"])

        # Query with both (range)
        response = ws.named_get(
            self.archive_url,
            "getPublishedBinaries",
            published_since=(now - timedelta(days=1, hours=12)).isoformat(),
            published_until=(now - timedelta(hours=12)).isoformat(),
        )
        self.assertEqual(200, response.status)
        self.assertEqual(1, response.jsonBody()["total_size"])

    def test_getPublishedSources_published_since_until(self):
        now = datetime.now(timezone.utc)
        pub0 = self.factory.makeSourcePackagePublishingHistory(
            archive=self.archive, status=PackagePublishingStatus.PUBLISHED
        )
        removeSecurityProxy(pub0).datepublished = now - timedelta(days=2)
        pub1 = self.factory.makeSourcePackagePublishingHistory(
            archive=self.archive, status=PackagePublishingStatus.PUBLISHED
        )
        removeSecurityProxy(pub1).datepublished = now - timedelta(days=1)
        pub2 = self.factory.makeSourcePackagePublishingHistory(
            archive=self.archive, status=PackagePublishingStatus.PUBLISHED
        )
        removeSecurityProxy(pub2).datepublished = now

        ws = webservice_for_person(self.person, default_api_version="beta")

        # Query with published_since
        since_date = now - timedelta(hours=12)
        response = ws.named_get(
            self.archive_url,
            "getPublishedSources",
            published_since=since_date.isoformat(),
        )
        self.assertEqual(200, response.status)
        self.assertEqual(1, response.jsonBody()["total_size"])

        # Query with published_until
        until_date = now - timedelta(hours=12)
        response = ws.named_get(
            self.archive_url,
            "getPublishedSources",
            published_until=until_date.isoformat(),
        )
        self.assertEqual(200, response.status)
        self.assertEqual(2, response.jsonBody()["total_size"])

        # Query with both (range)
        response = ws.named_get(
            self.archive_url,
            "getPublishedSources",
            published_since=(now - timedelta(days=1, hours=12)).isoformat(),
            published_until=(now - timedelta(hours=12)).isoformat(),
        )
        self.assertEqual(200, response.status)
        self.assertEqual(1, response.jsonBody()["total_size"])


class TestRemoveCopyNotification(TestCaseWithFactory):
    """Test removeCopyNotification."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()
        self.archive = self.factory.makeArchive(owner=self.person)
        self.archive_url = api_url(self.archive)

    def test_removeCopyNotification(self):
        distroseries = self.factory.makeDistroSeries()
        source_archive = self.factory.makeArchive(distroseries.distribution)
        requester = self.factory.makePerson()
        source = getUtility(IPlainPackageCopyJobSource)
        job = source.create(
            package_name="foo",
            source_archive=source_archive,
            target_archive=self.archive,
            target_distroseries=distroseries,
            target_pocket=PackagePublishingPocket.RELEASE,
            package_version="1.0-1",
            include_binaries=True,
            requester=requester,
        )
        job.start()
        job.fail()

        ws = webservice_for_person(
            self.person,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = ws.named_post(
            self.archive_url, "removeCopyNotification", job_id=job.id
        )
        self.assertEqual(200, response.status)

        login(ANONYMOUS)
        source = getUtility(IPlainPackageCopyJobSource)
        self.assertEqual(
            None, source.getIncompleteJobsForArchive(self.archive).any()
        )


class TestArchiveSet(TestCaseWithFactory):
    """Test ArchiveSet.getByReference."""

    layer = DatabaseFunctionalLayer

    def test_getByReference(self):
        random = self.factory.makePerson()
        body = (
            webservice_for_person(None)
            .named_get(
                "/archives",
                "getByReference",
                reference="ubuntu",
                api_version="devel",
            )
            .jsonBody()
        )
        self.assertEqual(body["reference"], "ubuntu")
        body = (
            webservice_for_person(random)
            .named_get(
                "/archives",
                "getByReference",
                reference="ubuntu",
                api_version="devel",
            )
            .jsonBody()
        )
        self.assertEqual(body["reference"], "ubuntu")

    def test_getByReference_ppa(self):
        body = (
            webservice_for_person(None)
            .named_get(
                "/archives",
                "getByReference",
                reference="~cprov/ubuntu/ppa",
                api_version="devel",
            )
            .jsonBody()
        )
        self.assertEqual(body["reference"], "~cprov/ubuntu/ppa")

    def test_getByReference_invalid(self):
        body = (
            webservice_for_person(None)
            .named_get(
                "/archives",
                "getByReference",
                reference="~cprov/ubuntu",
                api_version="devel",
            )
            .jsonBody()
        )
        self.assertIs(None, body)

    def test_getByReference_private(self):
        with admin_logged_in():
            archive = self.factory.makeArchive(private=True)
            owner = archive.owner
            reference = archive.reference
            random = self.factory.makePerson()
        body = (
            webservice_for_person(None)
            .named_get(
                "/archives",
                "getByReference",
                reference=reference,
                api_version="devel",
            )
            .jsonBody()
        )
        self.assertIs(None, body)
        body = (
            webservice_for_person(random)
            .named_get(
                "/archives",
                "getByReference",
                reference=reference,
                api_version="devel",
            )
            .jsonBody()
        )
        self.assertIs(None, body)
        body = (
            webservice_for_person(owner)
            .named_get(
                "/archives",
                "getByReference",
                reference=reference,
                api_version="devel",
            )
            .jsonBody()
        )
        self.assertEqual(body["reference"], reference)


class TestArchiveMetadataOverridesWebService(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.default_overrides = {
            "Origin": "default_origin",
            "Label": "default_label",
            "Suite": "default_suite",
            "Snapshots": "default_snapshots",
        }

    def create_archive(self, owner=None, private=False, primary=False):
        if primary:
            distribution = self.factory.makeDistribution(owner=owner)
            archive = self.factory.makeArchive(
                owner=owner,
                distribution=distribution,
                purpose=ArchivePurpose.PRIMARY,
            )
            with celebrity_logged_in("admin"):
                archive.setMetadataOverrides(self.default_overrides)
            return archive

        return self.factory.makeArchive(
            owner=owner,
            private=private,
            metadata_overrides=self.default_overrides,
        )

    def get_and_check_response(self, person, archive, expected_body=None):
        archive_url = api_url(archive)
        webservice = webservice_for_person(
            person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.get(archive_url)
        self.assertEqual(200, response.status)
        if expected_body:
            self.assertEqual(
                expected_body,
                response.jsonBody()["metadata_overrides"],
            )

    def patch_and_check_response(
        self, person, archive, overrides, expected_status, expected_body=None
    ):
        archive_url = api_url(archive)
        webservice = webservice_for_person(
            person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.patch(
            archive_url,
            "application/json",
            json.dumps({"metadata_overrides": overrides}),
        )
        self.assertEqual(expected_status, response.status)
        if expected_body:
            with person_logged_in(person):
                self.assertEqual(expected_body, archive.metadata_overrides)

    def patch_and_check_structure(
        self, webservice, achive_url, data, expected_status, expected_body
    ):
        response = webservice.patch(
            achive_url, "application/json", json.dumps(data)
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=expected_status,
                body=expected_body,
            ),
        )

    def test_cannot_set_invalid_metadata_keys(self):
        owner = self.factory.makePerson()
        archive = self.create_archive(owner=owner)
        archive_url = api_url(archive)
        webservice = webservice_for_person(
            owner,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        invalid_overrides = {"metadata_overrides": {"Invalid": "test_invalid"}}
        self.patch_and_check_structure(
            webservice,
            archive_url,
            invalid_overrides,
            400,
            (
                b"Invalid metadata override key. Allowed keys are "
                b"{'Label', 'Origin', 'Snapshots', 'Suite'}."
            ),
        )

    def test_cannot_set_non_string_values_for_metadata(self):
        owner = self.factory.makePerson()
        archive = self.create_archive(owner=owner)
        archive_url = api_url(archive)
        webservice = webservice_for_person(
            owner,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        invalid_values = ["", None, True, 1, [], {}]
        for value in invalid_values:
            data = {"metadata_overrides": {"Origin": value}}
            self.patch_and_check_structure(
                webservice,
                archive_url,
                data,
                400,
                b"Value for 'Origin' must be a non-empty string.",
            )

    def test_non_owner_can_view_public_archive_metadata_overrides(self):
        user = self.factory.makePerson()
        archive = self.create_archive()
        self.get_and_check_response(user, archive, self.default_overrides)

    def test_owner_can_view_own_public_archive_metadata_overrides(self):
        owner = self.factory.makePerson()
        archive = self.create_archive(owner=owner)
        self.get_and_check_response(owner, archive, self.default_overrides)

    def test_admin_can_view_metadata_overrides_of_any_public_archive(self):
        admin = self.factory.makeAdministrator()
        archive = self.create_archive()
        self.get_and_check_response(admin, archive, self.default_overrides)

    def test_owner_can_view_own_private_archive_metadata_overrides(self):
        owner = self.factory.makePerson()
        private_archive = self.create_archive(owner=owner, private=True)
        self.get_and_check_response(
            owner, private_archive, self.default_overrides
        )

    def test_admin_can_view_metadata_overrides_of_any_private_archive(self):
        admin = self.factory.makeAdministrator()
        private_archive = self.create_archive(private=True)
        self.get_and_check_response(
            admin, private_archive, self.default_overrides
        )

    def test_non_owner_cannot_view_private_archive_metadata_overrides(self):
        owner = self.factory.makePerson()
        user = self.factory.makePerson()
        private_archive = self.create_archive(owner=owner, private=True)
        self.get_and_check_response(
            user,
            private_archive,
            "tag:launchpad.net:2008:redacted",
        )

    def test_subscriber_cannot_view_private_archive_metadata_overrides(self):
        owner = self.factory.makePerson()
        user = self.factory.makePerson()
        private_archive = self.create_archive(owner=owner, private=True)
        with person_logged_in(owner):
            private_archive.newSubscription(user, owner)
        self.get_and_check_response(
            user,
            private_archive,
            "tag:launchpad.net:2008:redacted",
        )

    def test_owner_can_set_metadata_overrides_on_own_public_archive(self):
        owner = self.factory.makePerson()
        archive = self.create_archive(owner=owner)
        with person_logged_in(owner):
            self.assertEqual(
                archive.metadata_overrides, self.default_overrides
            )
        overrides = {"Origin": "test_origin"}
        self.patch_and_check_response(
            owner, archive, overrides, 209, overrides
        )

    def test_admin_can_set_metadata_overrides_on_any_public_archive(self):
        admin = self.factory.makeAdministrator()
        archive = self.create_archive()
        with celebrity_logged_in("admin"):
            self.assertEqual(
                archive.metadata_overrides, self.default_overrides
            )
        overrides = {"Origin": "test_origin"}
        self.patch_and_check_response(
            admin, archive, overrides, 209, overrides
        )

    def test_non_owner_cannot_set_metadata_overrides_on_public_archive(self):
        owner = self.factory.makePerson()
        user = self.factory.makePerson()
        archive = self.create_archive(owner=owner)
        archive_url = api_url(archive)
        webservice = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        overrides = {"metadata_overrides": {"Origin": "test_origin"}}
        self.patch_and_check_structure(
            webservice,
            archive_url,
            overrides,
            401,
            b"(<Archive object>, 'setMetadataOverrides', 'launchpad.Edit')",
        )

    def test_owner_can_set_metadata_overrides_on_private_archive(self):
        owner = self.factory.makePerson()
        private_archive = self.create_archive(owner=owner, private=True)
        with person_logged_in(owner):
            self.assertEqual(
                private_archive.metadata_overrides, self.default_overrides
            )
        overrides = {"Origin": "test_origin"}
        self.patch_and_check_response(
            owner, private_archive, overrides, 209, overrides
        )

    def test_admin_can_set_metadata_overrides_on_any_private_archive(self):
        admin = self.factory.makeAdministrator()
        private_archive = self.create_archive(private=True)
        with celebrity_logged_in("admin"):
            self.assertEqual(
                private_archive.metadata_overrides, self.default_overrides
            )
        overrides = {"Origin": "test_origin"}
        self.patch_and_check_response(
            admin, private_archive, overrides, 209, overrides
        )

    def test_non_owner_cannot_set_metadata_overrides_on_private_archive(self):
        user = self.factory.makePerson()
        private_archive = self.create_archive(primary=True)
        archive_url = api_url(private_archive)
        webservice = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        overrides = {"metadata_overrides": {"Origin": "test_origin"}}
        self.patch_and_check_structure(
            webservice,
            archive_url,
            overrides,
            401,
            b"(<Archive object>, 'setMetadataOverrides', 'launchpad.Edit')",
        )

    def test_subscriber_cannot_set_metadata_overrides_on_private_archive(self):
        owner = self.factory.makePerson()
        user = self.factory.makePerson()
        private_archive = self.create_archive(owner=owner, private=True)
        with person_logged_in(owner):
            private_archive.newSubscription(user, owner)
        overrides = {"Origin": "test_origin"}
        self.patch_and_check_response(user, private_archive, overrides, 401)

    def test_owner_can_set_metadata_overrides_on_own_primary_archive(self):
        owner = self.factory.makePerson()
        primary_archive = self.create_archive(owner=owner, primary=True)
        with person_logged_in(owner):
            self.assertEqual(
                primary_archive.metadata_overrides, self.default_overrides
            )
        overrides = {"Origin": "test_origin"}
        self.patch_and_check_response(
            owner, primary_archive, overrides, 209, overrides
        )

    def test_admin_can_set_metadata_overrides_on_any_primary_archive(self):
        admin = self.factory.makeAdministrator()
        primary_archive = self.create_archive(primary=True)
        with celebrity_logged_in("admin"):
            self.assertEqual(
                primary_archive.metadata_overrides, self.default_overrides
            )
        overrides = {"Origin": "test_origin"}
        self.patch_and_check_response(
            admin, primary_archive, overrides, 209, overrides
        )

    def test_non_owner_cannot_set_metadata_overrides_on_primary_archive(self):
        user = self.factory.makePerson()
        primary_archive = self.create_archive(primary=True)
        archive_url = api_url(primary_archive)
        webservice = webservice_for_person(
            user,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        overrides = {"metadata_overrides": {"Origin": "test_origin"}}
        self.patch_and_check_structure(
            webservice,
            archive_url,
            overrides,
            401,
            b"(<Archive object>, 'setMetadataOverrides', 'launchpad.Edit')",
        )


class TestArchiveSubscriptionWebservice(TestCaseWithFactory):
    """Tests for archive subscription operations via the webservice."""

    layer = DatabaseFunctionalLayer

    def test_newSubscription_allows_private_team(self):
        """Subscribing a private team to a PPA via webservice succeeds.

        This tests the basic functionality that private teams can be
        subscribed to PPAs via the webservice API.
        """

        owner = self.factory.makePerson()

        with person_logged_in(owner):
            # Create PPA owner team
            ppa_owner_team = self.factory.makeTeam(owner=owner)

            # Create private team to subscribe
            private_team = self.factory.makeTeam(
                owner=owner,
                visibility=PersonVisibility.PRIVATE,
            )
            private_team_url = api_url(private_team)

            # Create private PPA
            ppa = self.factory.makeArchive(
                private=True,
                owner=ppa_owner_team,
            )
            ppa_url = api_url(ppa)

        # Subscribe private team via webservice
        ws = webservice_for_person(
            owner,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="beta",
        )

        response = ws.named_post(
            ppa_url,
            "newSubscription",
            subscriber=private_team_url,
        )

        self.assertEqual(201, response.status)
        subscription = ws.get(response.getHeader("Location")).jsonBody()
        self.assertIn(private_team_url, subscription["subscriber_link"])
        self.assertIn(ppa_url, subscription["archive_link"])

    def test_newSubscription_private_team_grants_member_access(self):
        """Subscribing a private team grants PPA access to all team members.

        This tests the real-world use case: a PPA owner subscribes a private
        team to give all team members access to the PPA.
        """

        # PPA owner
        ppa_owner = self.factory.makePerson()

        # Team member who will gain access through subscription
        team_member = self.factory.makePerson()

        with person_logged_in(ppa_owner):
            # Create team that owns the PPA
            ppa_owner_team = self.factory.makeTeam(owner=ppa_owner)

            # Create private team with a member
            private_team = self.factory.makeTeam(
                owner=ppa_owner,
                visibility=PersonVisibility.PRIVATE,
            )
            private_team.addMember(team_member, ppa_owner)
            private_team_url = api_url(private_team)

            # Create private PPA
            ppa = self.factory.makeArchive(
                private=True,
                owner=ppa_owner_team,
            )
            ppa_url = api_url(ppa)

        # Subscribe the private team via webservice
        ws = webservice_for_person(
            ppa_owner,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="beta",
        )

        response = ws.named_post(
            ppa_url,
            "newSubscription",
            subscriber=private_team_url,
        )

        # Subscription succeeds
        self.assertEqual(201, response.status)
        subscription = ws.get(response.getHeader("Location")).jsonBody()
        self.assertIn(private_team_url, subscription["subscriber_link"])

        # Verify the team member can now view the private archive
        with person_logged_in(team_member):
            self.assertTrue(check_permission("launchpad.SubscriberView", ppa))

    def test_newSubscription_private_team_requires_access(self):
        """Cannot subscribe a private team without access to that team.

        This verifies the security constraint: you must have permission to
        view/use a private team in order to subscribe it to a PPA.
        """

        # PPA owner
        ppa_owner = self.factory.makePerson()

        # Private team owner (different person)
        private_team_owner = self.factory.makePerson()

        with person_logged_in(ppa_owner):
            # Create team that owns the PPA
            ppa_owner_team = self.factory.makeTeam(owner=ppa_owner)

            # Create private PPA
            ppa = self.factory.makeArchive(
                private=True,
                owner=ppa_owner_team,
            )
            ppa_url = api_url(ppa)

        # Create private team owned by someone else
        with person_logged_in(private_team_owner):
            private_team = self.factory.makeTeam(
                owner=private_team_owner,
                visibility=PersonVisibility.PRIVATE,
            )
            private_team_url = api_url(private_team)

        # PPA owner tries to subscribe the private team via webservice
        # This should fail because ppa_owner cannot access private_team
        ws = webservice_for_person(
            ppa_owner,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="beta",
        )

        response = ws.named_post(
            ppa_url,
            "newSubscription",
            subscriber=private_team_url,
        )

        # Should fail with 400 Bad Request due to security constraint
        self.assertEqual(400, response.status)
        self.assertIn(b"subscriber", response.body)
