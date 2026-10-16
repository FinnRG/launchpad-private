# Copyright 2010-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import json
import os.path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import responses
from testtools.matchers import (
    Contains,
    ContainsDict,
    EndsWith,
    Equals,
    MatchesDict,
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
from lp.soyuz.interfaces.publishing import IPublishingSet
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


class PackageListingTestCase(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        """Provide an ordinary API caller."""
        super().setUp()
        self.person = self.factory.makePerson()

    def _make_source_with_binaries(
        self, archive, distroseries, pocket, binary_names
    ):
        """Create a published source and binaries on one architecture."""
        spph = self.factory.makeSourcePackagePublishingHistory(
            archive=archive,
            distroseries=distroseries,
            pocket=pocket,
            status=PackagePublishingStatus.PUBLISHED,
        )
        das = self.factory.makeDistroArchSeries(distroseries=distroseries)
        bpphs = [
            self.factory.makeBinaryPackagePublishingHistory(
                archive=archive,
                distroarchseries=das,
                pocket=pocket,
                source_package_release=spph.sourcepackagerelease,
                binarypackagename=name,
                architecturespecific=True,
                status=PackagePublishingStatus.PUBLISHED,
            )
            for name in binary_names
        ]
        return spph, das, bpphs


class TestSourcePackageList(PackageListingTestCase):
    def test_enrichments_omitted_by_default(self):
        """Omitted enrichment flags do not query or expose related data."""
        with admin_logged_in():
            archive = self.factory.makeArchive()
            series = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            # A nonempty listing ensures the helpers are skipped because
            # enrichment is disabled, not because there are no sources.
            self.factory.makeSourcePackagePublishingHistory(
                archive=archive,
                distroseries=series,
                status=PackagePublishingStatus.PUBLISHED,
            )
            archive_url = api_url(archive)
        ws = webservice_for_person(self.person, default_api_version="devel")
        # Remove the proxy only to install mocks on the real utility, not
        # to change authorization of the API request.
        publishing_set = removeSecurityProxy(getUtility(IPublishingSet))
        # Missing response fields alone would not detect enrichment work
        # whose results were discarded. Fail immediately on either call.
        with patch.object(
            publishing_set,
            "getActiveBinariesForSources",
            side_effect=AssertionError("Unexpected binary query"),
        ) as binary_query, patch.object(
            publishing_set,
            "getBuildArchitecturesForSources",
            side_effect=AssertionError("Unexpected architecture query"),
        ) as architecture_query:
            # Omit both flags to exercise their defaults, rather than
            # explicitly requesting False.
            response = ws.named_get(archive_url, "getSourcePackageList")
            self.assertEqual(200, response.status)
            source = response.jsonBody()["sources"][0]
            # Unrequested enrichments must be absent, including the binary
            # total, rather than appearing as empty lists or a zero count.
            self.assertNotIn("binaries", source)
            self.assertNotIn("binaries_total", source)
            self.assertNotIn("architectures", source)
            # Even an attempted helper call caught by the implementation
            # would still be recorded by these mocks.
            binary_query.assert_not_called()
            architecture_query.assert_not_called()

    def test_enrichment_flags_work_independently(self):
        """Flags independently expose complete or empty enrichment data."""
        with admin_logged_in():
            archive = self.factory.makeArchive()
            series = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            source, das, publications = self._make_source_with_binaries(
                archive, series, PackagePublishingPocket.RELEASE, ["foo"]
            )
            source_name = source.source_package_name
            # Architecture summaries require the binary name to match the
            # source name; this fixture therefore qualifies for both flags.
            removeSecurityProxy(
                publications[0].binarypackagerelease.binarypackagename
            ).name = source_name
            binary_id = publications[0].id
            arch_tag = das.architecturetag
            source_id = source.id
            build_status = publications[0].build.status.title
            build_link_suffix = "/+build/%d" % publications[0].build.id
            # This source has an active binary with a different name, so
            # it qualifies for binaries but has no architecture summary.
            unmatched, _, _ = self._make_source_with_binaries(
                archive,
                series,
                PackagePublishingPocket.RELEASE,
                ["other-bin"],
            )
            unmatched_id = unmatched.id
            archive_url = api_url(archive)
        ws = webservice_for_person(self.person, default_api_version="devel")
        # Exercise neither flag, each flag alone, and both together to catch
        # one enrichment incorrectly depending on or enabling the other.
        for include_binaries, include_architectures in (
            (False, False),
            (True, False),
            (False, True),
            (True, True),
        ):
            response = ws.named_get(
                archive_url,
                "getSourcePackageList",
                include_binaries=include_binaries,
                include_architectures=include_architectures,
            )
            self.assertEqual(200, response.status)
            # Select by source id rather than depending on listing order.
            # Enrichment must not remove the source with no matching summary.
            entries = {
                entry["id"]: entry for entry in response.jsonBody()["sources"]
            }
            self.assertEqual({source_id, unmatched_id}, set(entries))
            # Unrequested keys must be omitted, not returned as empty lists.
            # The binary list and its total are controlled by the same flag.
            for entry in entries.values():
                self.assertEqual(include_binaries, "binaries" in entry)
                self.assertEqual(include_binaries, "binaries_total" in entry)
                self.assertEqual(
                    include_architectures, "architectures" in entry
                )
            entry = entries[source_id]
            if include_binaries:
                # Binary metadata stays available with architectures either
                # enabled or disabled, and retains the publication id.
                self.assertEqual(1, entry["binaries_total"])
                self.assertEqual(
                    [{"name": source_name, "id": binary_id}],
                    entry["binaries"],
                )
            if include_architectures:
                # Check the complete architecture payload, including the
                # build status and link to the fixture's build.
                self.assertThat(
                    entry["architectures"],
                    MatchesListwise(
                        [
                            MatchesDict(
                                {
                                    "architecture_tag": Equals(arch_tag),
                                    "build_status": Equals(build_status),
                                    "build_web_link": EndsWith(
                                        build_link_suffix
                                    ),
                                }
                            )
                        ]
                    ),
                )
                # Requested but unmatched enrichment is an empty list,
                # unlike the omitted key when the flag is disabled.
                self.assertEqual([], entries[unmatched_id]["architectures"])

    def test_response_contains_source_and_binary_metadata(self):
        """Return source metadata, pagination totals and sorted binary ids."""
        with admin_logged_in():
            archive = self.factory.makeArchive()
            distroseries = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            pocket = PackagePublishingPocket.RELEASE
            # Create binary names in reverse alphabetical order so the
            # response must sort them rather than preserve insertion order.
            spph, _, bpphs = self._make_source_with_binaries(
                archive, distroseries, pocket, ["foo-dev", "foo-bin"]
            )
            # Remember publication ids by name to check that sorting keeps
            # each name paired with the correct id.
            expected_ids = {
                bpph.binary_package_name: removeSecurityProxy(bpph).id
                for bpph in bpphs
            }
            archive_url = api_url(archive)
            expected_source_id = spph.id
            source_name = spph.source_package_name
            source_version = spph.source_package_version
            series_name = distroseries.name
        ws = webservice_for_person(self.person, default_api_version="devel")
        # Request binary enrichment only; pagination retains its defaults.
        response = ws.named_get(
            archive_url, "getSourcePackageList", include_binaries=True
        )
        self.assertEqual(200, response.status)
        result = response.jsonBody()
        # These totals count source publications, not their binary names.
        # One source fits on page 1 with the default page size of 50.
        self.assertEqual(1, result["total"])
        self.assertEqual(1, result["total_pages"])
        self.assertEqual(1, result["page"])
        self.assertEqual(50, result["page_size"])
        source = result["sources"][0]
        # Source fields identify the publication, release and series;
        # pocket and status are returned as titles, not numeric enum values.
        self.assertEqual(expected_source_id, source["id"])
        self.assertEqual(source_name, source["source_package_name"])
        self.assertEqual(source_version, source["source_package_version"])
        self.assertEqual(series_name, source["series"])
        self.assertEqual("Release", source["pocket"])
        self.assertEqual("Published", source["status"])
        # Binary enrichment must not implicitly enable architecture data.
        self.assertNotIn("architectures", source)
        # Both unique binary names fit under the default per-source limit.
        self.assertEqual(2, source["binaries_total"])
        entries = source["binaries"]
        self.assertEqual(
            ["foo-bin", "foo-dev"],
            [entry["name"] for entry in entries],
        )
        self.assertEqual(
            [expected_ids["foo-bin"], expected_ids["foo-dev"]],
            [entry["id"] for entry in entries],
        )

    def test_binaries_capped_per_source_with_uncapped_totals(self):
        """Cap names, count all unique matches and keep the newest id."""
        with admin_logged_in():
            archive = self.factory.makeArchive()
            series = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            # Three distinct names exceed the requested two-name limit.
            first, das, publications = self._make_source_with_binaries(
                archive,
                series,
                PackagePublishingPocket.RELEASE,
                ["a-bin", "b-bin", "c-bin"],
            )
            first_b_id = publications[1].id
            # Publish a-bin on another architecture. It must count once and
            # use this higher publication id as its representative.
            duplicate = self.factory.makeBinaryPackagePublishingHistory(
                archive=archive,
                distroarchseries=self.factory.makeDistroArchSeries(
                    distroseries=series
                ),
                pocket=first.pocket,
                source_package_release=first.sourcepackagerelease,
                binarypackagename="a-bin",
                architecturespecific=True,
                status=PackagePublishingStatus.PUBLISHED,
            )
            # A second source also exceeds the limit, distinguishing a
            # per-source cap from a limit shared across the whole response.
            second, das, publications = self._make_source_with_binaries(
                archive,
                series,
                PackagePublishingPocket.RELEASE,
                ["d-bin", "e-bin", "f-bin"],
            )
            first_id, second_id, duplicate_id = (
                first.id,
                second.id,
                duplicate.id,
            )
            # A source without binaries must still appear with an empty
            # list and a zero total when binary enrichment is requested.
            empty = self.factory.makeSourcePackagePublishingHistory(
                archive=archive,
                distroseries=series,
                pocket=PackagePublishingPocket.RELEASE,
                status=PackagePublishingStatus.PUBLISHED,
            )
            empty_id = empty.id
            archive_url = api_url(archive)
        ws = webservice_for_person(self.person, default_api_version="devel")
        # Limit displayed names, not the total number of matching names.
        response = ws.named_get(
            archive_url,
            "getSourcePackageList",
            binary_names_limit=2,
            include_binaries=True,
        )
        self.assertEqual(200, response.status)
        by_source = {
            source["id"]: source for source in response.jsonBody()["sources"]
        }
        # Totals remain three despite truncation; the extra architecture
        # must not inflate the first source's count to four.
        self.assertEqual(3, by_source[first_id]["binaries_total"])
        self.assertEqual(3, by_source[second_id]["binaries_total"])
        self.assertEqual(0, by_source[empty_id]["binaries_total"])
        self.assertEqual([], by_source[empty_id]["binaries"])
        # Alphabetical ordering keeps a-bin and b-bin. The duplicate a-bin
        # publication neither consumes another slot nor keeps the older id.
        self.assertEqual(
            [
                {"name": "a-bin", "id": duplicate_id},
                {"name": "b-bin", "id": first_b_id},
            ],
            by_source[first_id]["binaries"],
        )
        # The second source gets its own two slots rather than losing them
        # to the first source's results.
        self.assertEqual(
            ["d-bin", "e-bin"],
            [binary["name"] for binary in by_source[second_id]["binaries"]],
        )

    def test_binaries_exclude_inactive_publications(self):
        """Superseded binary names are absent from the requested enrichment."""
        with admin_logged_in():
            archive = self.factory.makeArchive()
            distroseries = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            pocket = PackagePublishingPocket.RELEASE
            spph, das, _ = self._make_source_with_binaries(
                archive, distroseries, pocket, ["active-bin"]
            )
            self.factory.makeBinaryPackagePublishingHistory(
                archive=archive,
                distroarchseries=das,
                pocket=pocket,
                source_package_release=spph.sourcepackagerelease,
                binarypackagename="superseded-bin",
                architecturespecific=True,
                status=PackagePublishingStatus.SUPERSEDED,
            )
            archive_url = api_url(archive)
        ws = webservice_for_person(self.person, default_api_version="devel")
        response = ws.named_get(
            archive_url, "getSourcePackageList", include_binaries=True
        )
        self.assertEqual(200, response.status)
        self.assertEqual(
            1, response.jsonBody()["sources"][0]["binaries_total"]
        )
        self.assertEqual(
            ["active-bin"],
            [
                entry["name"]
                for entry in response.jsonBody()["sources"][0]["binaries"]
            ],
        )

    def test_access_allowed_for_private_archive_subscriber(self):
        """A private-archive subscriber can request source binary metadata."""
        with admin_logged_in():
            archive = self.factory.makeArchive(private=True)
            distroseries = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            pocket = PackagePublishingPocket.RELEASE
            spph, _, _ = self._make_source_with_binaries(
                archive, distroseries, pocket, ["foo-bin"]
            )
            subscriber = self.factory.makePerson()
            with person_logged_in(archive.owner):
                archive.newSubscription(subscriber, archive.owner)
            archive_url = api_url(archive)
        ws = webservice_for_person(
            subscriber,
            permission=OAuthPermission.READ_PRIVATE,
            default_api_version="devel",
        )
        response = ws.named_get(
            archive_url, "getSourcePackageList", include_binaries=True
        )
        self.assertEqual(200, response.status)
        self.assertEqual(
            ["foo-bin"],
            [
                entry["name"]
                for entry in response.jsonBody()["sources"][0]["binaries"]
            ],
        )

    def test_sources_exclude_other_archives(self):
        """List only source publications belonging to the target archive."""
        with admin_logged_in():
            archive = self.factory.makeArchive()
            other_archive = self.factory.makeArchive(
                distribution=archive.distribution
            )
            distroseries = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            spph = self.factory.makeSourcePackagePublishingHistory(
                archive=archive,
                distroseries=distroseries,
                status=PackagePublishingStatus.PUBLISHED,
            )
            self.factory.makeSourcePackagePublishingHistory(
                archive=other_archive,
                distroseries=distroseries,
                status=PackagePublishingStatus.PUBLISHED,
            )
            good_id = removeSecurityProxy(spph).id
            archive_url = api_url(archive)
        ws = webservice_for_person(self.person, default_api_version="devel")
        response = ws.named_get(
            archive_url,
            "getSourcePackageList",
        )
        self.assertEqual(200, response.status)
        returned_ids = [
            entry["id"] for entry in response.jsonBody()["sources"]
        ]
        self.assertEqual([good_id], returned_ids)

    def test_pagination_uses_requested_order(self):
        """Paginate newest-first by default; honor explicit name ordering."""
        with admin_logged_in():
            archive = self.factory.makeArchive()
            series = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            # Three matching publications with a page size of two leave
            # one result on the last page.
            sources = [
                self.factory.makeSourcePackagePublishingHistory(
                    archive=archive,
                    distroseries=series,
                    status=PackagePublishingStatus.PUBLISHED,
                )
                for day in (1, 2, 3)
            ]
            # Distinct dates make the expected newest-first order explicit,
            # independent of the factory's creation timestamps.
            for day, source in enumerate(sources, 1):
                removeSecurityProxy(source).datecreated = datetime(
                    2026, 1, day, tzinfo=timezone.utc
                )
            ids = [source.id for source in sources]
            expected_names = sorted(
                source.source_package_name for source in sources
            )
            archive_url = api_url(archive)
        ws = webservice_for_person(self.person, default_api_version="devel")
        # Omitting the page and sort parameters selects page 1 and the two
        # newest publications: the January 3 and January 2 fixtures.
        response = ws.named_get(
            archive_url, "getSourcePackageList", page_size=2
        )
        self.assertEqual(200, response.status)
        result = response.jsonBody()
        # Totals describe the complete matching set, not just this page.
        self.assertEqual(3, result["total"])
        self.assertEqual(2, result["total_pages"])
        self.assertEqual(ids[:0:-1], [s["id"] for s in result["sources"]])
        # The next page contains only the oldest publication, proving the
        # offset neither repeats nor skips results from the first page.
        result = ws.named_get(
            archive_url, "getSourcePackageList", page=2, page_size=2
        ).jsonBody()
        self.assertEqual([ids[0]], [s["id"] for s in result["sources"]])
        # An explicit name sort overrides newest-first ordering. The default
        # page size is large enough to return all three names together.
        result = ws.named_get(
            archive_url,
            "getSourcePackageList",
            order_by=["source_package_name"],
        ).jsonBody()
        self.assertEqual(
            expected_names,
            [s["source_package_name"] for s in result["sources"]],
        )
        # There are only two pages; requesting a third must return 404
        # rather than silently returning an empty page.
        self.assertEqual(
            404,
            ws.named_get(
                archive_url, "getSourcePackageList", page=3, page_size=2
            ).status,
        )

    def test_ordering_supports_sort_keys_and_directions(self):
        """Sort by series versions and enum titles; reject unknown keys."""
        with admin_logged_in():
            archive = self.factory.makeArchive()
            older = self.factory.makeDistroSeries(
                distribution=archive.distribution, version="9.10"
            )
            newer = self.factory.makeDistroSeries(
                distribution=archive.distribution, version="10.04"
            )
            first = self.factory.makeSourcePackagePublishingHistory(
                archive=archive,
                distroseries=older,
                pocket=PackagePublishingPocket.UPDATES,
                status=PackagePublishingStatus.SUPERSEDED,
            )
            second = self.factory.makeSourcePackagePublishingHistory(
                archive=archive,
                distroseries=newer,
                pocket=PackagePublishingPocket.BACKPORTS,
                status=PackagePublishingStatus.PUBLISHED,
            )
            third = self.factory.makeSourcePackagePublishingHistory(
                archive=archive,
                distroseries=older,
                pocket=PackagePublishingPocket.RELEASE,
                status=PackagePublishingStatus.DELETED,
            )
            first_id, second_id, third_id = first.id, second.id, third.id
            archive_url = api_url(archive)
        ws = webservice_for_person(self.person, default_api_version="devel")
        for keys, expected in (
            (["series"], [third_id, first_id, second_id]),
            (["-series"], [second_id, third_id, first_id]),
            (["pocket"], [second_id, third_id, first_id]),
            (["status"], [third_id, second_id, first_id]),
            (["-status", "series"], [first_id, second_id, third_id]),
        ):
            response = ws.named_get(
                archive_url,
                "getSourcePackageList",
                status=["Published", "Superseded", "Deleted"],
                order_by=keys,
            )
            self.assertEqual(200, response.status)
            self.assertEqual(
                expected,
                [source["id"] for source in response.jsonBody()["sources"]],
            )
        self.assertEqual(
            400,
            ws.named_get(
                archive_url, "getSourcePackageList", order_by=["unknown"]
            ).status,
        )

    def test_ordering_applies_secondary_keys_and_id_tiebreak(self):
        """Equal primary keys use secondary ordering, then descending ids."""
        with admin_logged_in():
            archive = self.factory.makeArchive()
            older = self.factory.makeDistroSeries(
                distribution=archive.distribution, version="9.10"
            )
            newer = self.factory.makeDistroSeries(
                distribution=archive.distribution, version="10.04"
            )
            publications = [
                self.factory.makeSourcePackagePublishingHistory(
                    archive=archive,
                    distroseries=series,
                    status=PackagePublishingStatus.PUBLISHED,
                )
                for series in (older, newer, older)
            ]
            first_id, newer_id, last_id = (pub.id for pub in publications)
            archive_url = api_url(archive)
        ws = webservice_for_person(self.person, default_api_version="devel")
        for ordering, expected in (
            (["status", "series"], [last_id, first_id, newer_id]),
            (["status", "-series"], [newer_id, last_id, first_id]),
        ):
            response = ws.named_get(
                archive_url, "getSourcePackageList", order_by=ordering
            )
            self.assertEqual(200, response.status)
            self.assertEqual(
                expected,
                [entry["id"] for entry in response.jsonBody()["sources"]],
            )

    def test_filters_select_matching_publications(self):
        """Each filter distinguishes matching and nonmatching sources."""
        with admin_logged_in():
            archive = self.factory.makeArchive()
            series = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            # This source matches each positive filter below. Both fixtures
            # are Published so status filtering cannot distinguish them.
            source = self.factory.makeSourcePackagePublishingHistory(
                archive=archive,
                distroseries=series,
                pocket=PackagePublishingPocket.UPDATES,
                status=PackagePublishingStatus.PUBLISHED,
                sourcepackagename=self.factory.makeSourcePackageName(
                    name="filter-match"
                ),
            )
            release = removeSecurityProxy(source.sourcepackagerelease)
            # Ubuntu-change filtering checks the version marker, not the
            # changelog or whether the source belongs to Ubuntu.
            release.version = "1.0-1ubuntu1"
            release.signing_key_owner = self.factory.makePerson()
            # Keep the archive the same, but change the name, series, pocket
            # and version marker so ignoring any tested filter returns both.
            other = self.factory.makeSourcePackagePublishingHistory(
                archive=archive,
                distroseries=self.factory.makeDistroSeries(
                    distribution=archive.distribution
                ),
                pocket=PackagePublishingPocket.RELEASE,
                status=PackagePublishingStatus.PUBLISHED,
                sourcepackagename=self.factory.makeSourcePackageName(
                    name="unrelated-package"
                ),
            )
            other_release = removeSecurityProxy(other.sourcepackagerelease)
            other_release.version = "1.0-1"
            # Swapping signer and maintainer makes a filter on the wrong
            # person field return the other source instead of this one.
            other_release.maintainer = release.signing_key_owner
            other_release.signing_key_owner = release.maintainer
            source_id, other_id = source.id, other.id
            maintainer_url = api_url(release.maintainer)
            signer_url = api_url(release.signing_key_owner)
            series_url = api_url(series)
            name = source.source_package_name
            archive_url = api_url(archive)
        ws = webservice_for_person(self.person, default_api_version="devel")
        # Apply each filter separately so another filter cannot conceal a
        # missing condition. The returned id proves which fixture matched.
        for params in (
            # Exact names and substring names must both find this source.
            {"name": name, "exact_match": True},
            {"name": name[:-1]},
            {"ubuntu_change": True},
            # Reference parameters use API URLs rather than model objects.
            {"signed_by": signer_url},
            {"maintained_by": maintainer_url},
            {"distroseries": series_url},
            {"pocket": "Updates"},
        ):
            response = ws.named_get(
                archive_url, "getSourcePackageList", **params
            )
            self.assertEqual(200, response.status)
            self.assertEqual(1, response.jsonBody()["total"])
            self.assertEqual(
                [source_id],
                [entry["id"] for entry in response.jsonBody()["sources"]],
            )
        # False selects the unmodified version without the Ubuntu marker,
        # rather than disabling the version filter.
        response = ws.named_get(
            archive_url, "getSourcePackageList", ubuntu_change=False
        )
        self.assertEqual(200, response.status)
        self.assertEqual(
            [other_id],
            [entry["id"] for entry in response.jsonBody()["sources"]],
        )
        # The substring that matched above must not match in exact mode.
        self.assertEqual(
            0,
            ws.named_get(
                archive_url,
                "getSourcePackageList",
                name=name[:-1],
                exact_match=True,
            ).jsonBody()["total"],
        )

    def test_status_controls_selection(self):
        """Select active, inactive or empty sets of publication statuses."""
        with admin_logged_in():
            archive = self.factory.makeArchive()
            series = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            publications = {
                status.title: self.factory.makeSourcePackagePublishingHistory(
                    archive=archive, distroseries=series, status=status
                ).id
                for status in PackagePublishingStatus.items
            }
            archive_url = api_url(archive)
        ws = webservice_for_person(self.person, default_api_version="devel")
        response = ws.named_get(archive_url, "getSourcePackageList")
        self.assertEqual(200, response.status)
        result = response.jsonBody()
        self.assertEqual(2, result["total"])
        self.assertContentEqual(
            [publications["Pending"], publications["Published"]],
            [entry["id"] for entry in result["sources"]],
        )
        for statuses in (
            ["Published"],
            ["Superseded", "Deleted"],
            list(publications),
            [],
        ):
            response = ws.named_get(
                archive_url, "getSourcePackageList", status=statuses
            )
            self.assertEqual(200, response.status)
            result = response.jsonBody()
            self.assertEqual(len(statuses), result["total"])
            self.assertContentEqual(
                [publications[status] for status in statuses],
                [entry["id"] for entry in result["sources"]],
            )
        result = ws.named_get(
            archive_url,
            "getSourcePackageList",
            status=["Superseded", "Deleted"],
            page_size=1,
            page=2,
        ).jsonBody()
        self.assertEqual(2, result["total"])
        self.assertEqual(2, result["total_pages"])
        self.assertEqual(
            [min(publications["Superseded"], publications["Deleted"])],
            [entry["id"] for entry in result["sources"]],
        )

    def test_parameters_handle_empty_results_and_reject_invalid_values(self):
        """Empty archives have no pages; invalid limits and statuses fail."""
        archive_url = api_url(self.factory.makeArchive())
        ws = webservice_for_person(self.person, default_api_version="devel")
        result = ws.named_get(archive_url, "getSourcePackageList").jsonBody()
        self.assertEqual([], result["sources"])
        self.assertEqual(0, result["total_pages"])
        for params in (
            {"page": 0},
            {"page_size": 0},
            {"page_size": 101},
            {"binary_names_limit": 0},
            {"status": ["Unknown"]},
            {"status": ["Published", "Unknown"]},
        ):
            self.assertEqual(
                400,
                ws.named_get(
                    archive_url, "getSourcePackageList", **params
                ).status,
            )

    def test_query_count_constant_with_source_count(self):
        """Enrichment query counts remain constant as sources grow."""
        with admin_logged_in():
            archive = self.factory.makeArchive()
            series = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            archive_url = api_url(archive)
        ws = webservice_for_person(self.person, default_api_version="devel")

        def create_source():
            """Add a source whose binary also qualifies for architectures."""
            with admin_logged_in():
                source, das, binaries = self._make_source_with_binaries(
                    archive,
                    series,
                    PackagePublishingPocket.RELEASE,
                    [self.factory.getUniqueString()],
                )
                removeSecurityProxy(
                    binaries[0].binarypackagerelease.binarypackagename
                ).name = source.source_package_name

        for include_binaries, include_architectures in (
            (False, False),
            (True, False),
            (False, True),
            (True, True),
        ):

            def get_sources():
                """Request the current enrichment combination successfully."""
                response = ws.named_get(
                    archive_url,
                    "getSourcePackageList",
                    include_binaries=include_binaries,
                    include_architectures=include_architectures,
                )
                self.assertEqual(200, response.status)

            recorder1, recorder2 = record_two_runs(
                get_sources, create_source, 2
            )
            self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))


class TestBinaryPackageDetails(PackageListingTestCase):

    def test_public_copy_preserves_private_origin_permissions(self):
        """A public copy grants neither private archive nor build access."""
        # Admin rights are used only to create the private origin and copy.
        with admin_logged_in():
            private_archive = self.factory.makeArchive(private=True)
            original_series = self.factory.makeDistroSeries(
                distribution=private_archive.distribution
            )
            source, das, publications = self._make_source_with_binaries(
                private_archive,
                original_series,
                PackagePublishingPocket.RELEASE,
                ["copied-private"],
            )
            release = publications[0].binarypackagerelease
            # An architecture-independent release can be copied to the
            # target series without requiring the original architecture.
            removeSecurityProxy(release).architecturespecific = False
            public_archive = self.factory.makeArchive(
                distribution=private_archive.distribution
            )
            target_series = self.factory.makeDistroSeries(
                distribution=private_archive.distribution
            )
            target_das = self.factory.makeDistroArchSeries(
                distroseries=target_series
            )
            # Reuse the release rather than rebuilding it: the public copy
            # still references the build belonging to the private archive.
            copied = self.factory.makeBinaryPackagePublishingHistory(
                archive=public_archive,
                distroarchseries=target_das,
                binarypackagerelease=release,
                pocket=source.pocket,
                status=PackagePublishingStatus.PUBLISHED,
            )
            copied_id = copied.id
            original_id = publications[0].id
            public_url = api_url(public_archive)
            private_url = api_url(private_archive)
            private_build_url = api_url(release.build)
        # Neither caller has access to the private origin; both should be
        # able to read the independently published public copy.
        for person in (None, self.person):
            ws = webservice_for_person(person, default_api_version="devel")
            # Establish that the original build is inaccessible before
            # fetching any metadata through the new operation.
            self.assertEqual(401, ws.get(private_build_url).status)
            # Querying the original publication in its private archive
            # must be denied, even though its release also has a public copy.
            self.assertEqual(
                401,
                ws.named_get(
                    private_url,
                    "getBinaryPackageDetails",
                    binary_id=original_id,
                ).status,
            )
            # The private publication id cannot be looked up through the
            # public archive; only the copy's publication belongs there.
            self.assertEqual(
                404,
                ws.named_get(
                    public_url,
                    "getBinaryPackageDetails",
                    binary_id=original_id,
                ).status,
            )
            # Reading the public copy must succeed without requiring access
            # to the original private build.
            response = ws.named_get(
                public_url, "getBinaryPackageDetails", binary_id=copied_id
            )
            self.assertEqual(200, response.status)
            details = response.jsonBody()
            self.assertEqual("copied-private", details["binary_package_name"])
            # The response exposes package metadata, not extra fields for
            # the original archive or build.
            self.assertEqual(
                {
                    "id",
                    "binary_package_name",
                    "binary_package_version",
                    "summary",
                    "description",
                    "files",
                },
                set(details),
            )
            # Fetching public metadata must not grant private build access.
            self.assertEqual(401, ws.get(private_build_url).status)

    def test_access_denied_without_private_archive_subscription(self):
        """Anonymous and unrelated callers cannot read private publications."""
        with admin_logged_in():
            archive = self.factory.makeArchive(private=True)
            publication = self.factory.makeBinaryPackagePublishingHistory(
                archive=archive
            )
            binary_id = publication.id
            archive_url = api_url(archive)
        for person in (None, self.person):
            ws = webservice_for_person(
                person,
                permission=OAuthPermission.READ_PRIVATE,
                default_api_version="devel",
            )
            response = ws.named_get(
                archive_url, "getBinaryPackageDetails", binary_id=binary_id
            )
            self.assertEqual(401, response.status)

    def test_subscriber_gets_artifact_metadata_without_build_access(self):
        """Subscribers get artifact links without gaining build access."""
        with admin_logged_in():
            archive = self.factory.makeArchive(private=True)
            publication = self.factory.makeBinaryPackagePublishingHistory(
                archive=archive
            )
            summary = publication.binarypackagerelease.summary
            description = publication.binarypackagerelease.description
            library_file = self.factory.makeLibraryFileAlias(
                filename="private.deb",
                content=b"private binary",
                restricted=True,
                db_only=True,
            )
            self.factory.makeBinaryPackageFile(
                binarypackagerelease=publication.binarypackagerelease,
                library_file=library_file,
            )
            subscriber = self.factory.makePerson()
            with person_logged_in(archive.owner):
                archive.newSubscription(subscriber, archive.owner)
            binary_id = publication.id
            archive_url = api_url(archive)
            private_build_url = api_url(publication.build)
        ws = webservice_for_person(
            subscriber,
            permission=OAuthPermission.READ_PRIVATE,
            default_api_version="devel",
        )
        response = ws.named_get(
            archive_url, "getBinaryPackageDetails", binary_id=binary_id
        )
        self.assertEqual(200, response.status)
        details = response.jsonBody()
        self.assertEqual(summary, details["summary"])
        self.assertEqual(description, details["description"])
        self.assertEqual(1, len(details["files"]))
        artifact = details["files"][0]
        self.assertEqual("private.deb", artifact["filename"])
        self.assertEqual(14, artifact["size"])
        self.assertThat(artifact["url"], EndsWith("/+files/private.deb"))
        self.assertEqual(401, ws.get(private_build_url).status)

    def test_metadata_lookup_rejects_unknown_and_foreign_ids(self):
        """Return metadata; reject unknown and cross-archive publications."""
        # Create fixtures as admin, but fetch public metadata as a normal user.
        with admin_logged_in():
            archive = self.factory.makeArchive()
            publication = self.factory.makeBinaryPackagePublishingHistory(
                archive=archive
            )
            summary = "Short package summary"
            removeSecurityProxy(publication.binarypackagerelease).summary = (
                summary
            )
            # Attach a known artifact to the selected binary release.
            library_file = self.factory.makeLibraryFileAlias(
                filename="example.deb",
                content=b"binary contents",
                db_only=True,
            )
            self.factory.makeBinaryPackageFile(
                binarypackagerelease=publication.binarypackagerelease,
                library_file=library_file,
            )
            binary_id = publication.id
            name = publication.binary_package_name
            version = publication.binary_package_version
            description = publication.binarypackagerelease.description
            # A separate public archive tests publication ownership, not
            # denial of access to a private archive.
            other_archive_url = api_url(self.factory.makeArchive())
            archive_url = api_url(archive)
        ws = webservice_for_person(self.person, default_api_version="devel")
        response = ws.named_get(
            archive_url, "getBinaryPackageDetails", binary_id=binary_id
        )
        self.assertEqual(200, response.status)
        details = response.jsonBody()
        # Metadata must describe the requested publication and its release.
        self.assertEqual(binary_id, details["id"])
        self.assertEqual(name, details["binary_package_name"])
        self.assertEqual(version, details["binary_package_version"])
        self.assertEqual(summary, details["summary"])
        self.assertEqual(description, details["description"])
        # The 15-byte fixture determines the size; check the artifact URL
        # without downloading its contents.
        self.assertEqual(1, len(details["files"]))
        self.assertEqual("example.deb", details["files"][0]["filename"])
        self.assertEqual(15, details["files"][0]["size"])
        self.assertTrue(details["files"][0]["url"].endswith("example.deb"))
        # An id with no publication must return 404 in the correct archive.
        self.assertEqual(
            404,
            ws.named_get(
                archive_url, "getBinaryPackageDetails", binary_id=-1
            ).status,
        )
        # An existing id must also return 404 through a different archive;
        # knowing the id must not bypass the operation's archive scope.
        self.assertEqual(
            404,
            ws.named_get(
                other_archive_url,
                "getBinaryPackageDetails",
                binary_id=binary_id,
            ).status,
        )

    def test_artifacts_aggregate_only_matching_publications(self):
        """Return only matching artifacts across binary architectures.

        Requesting one binary publication must return both its artifact and
        the artifact of the same binary built from the same source release
        on another architecture in the same archive, series and pocket.

        Each unrelated publication differs in one scope condition: archive,
        series, pocket, source release, active status or binary name. Its
        artifact must not appear in the response. The exact filename list
        checks both inclusion of matching files and exclusion of unrelated
        files, in filename order.

        The request is made by an ordinary user against a public archive.
        This test checks artifact metadata, not file downloads or private
        archive authorization.
        """
        # Use admin rights for setup, then query as an ordinary API caller.
        with admin_logged_in():
            archive = self.factory.makeArchive()
            series = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            source, das, binaries = self._make_source_with_binaries(
                archive, series, PackagePublishingPocket.RELEASE, ["foo"]
            )
            first = binaries[0]
            # The same binary from the same source release on another
            # architecture must contribute its files to the response.
            other = self.factory.makeBinaryPackagePublishingHistory(
                archive=archive,
                distroarchseries=self.factory.makeDistroArchSeries(
                    distroseries=series
                ),
                pocket=source.pocket,
                source_package_release=source.sourcepackagerelease,
                binarypackagename=first.binarypackagerelease.binarypackagename,
                architecturespecific=True,
                status=PackagePublishingStatus.PUBLISHED,
            )
            # Give each matching release a recognizable artifact. Database
            # records suffice because this test does not download the files.
            for publication, filename in (
                (first, "foo-first.deb"),
                (other, "foo-other.deb"),
            ):
                self.factory.makeBinaryPackageFile(
                    binarypackagerelease=publication.binarypackagerelease,
                    library_file=self.factory.makeLibraryFileAlias(
                        filename=filename, db_only=True
                    ),
                )
            other_archive = self.factory.makeArchive(
                distribution=archive.distribution
            )
            other_series = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            other_das = self.factory.makeDistroArchSeries(
                distroseries=other_series
            )
            other_source = self.factory.makeSourcePackageRelease()
            package_name = first.binarypackagerelease.binarypackagename
            # Vary each scope dimension while keeping the other values valid.
            for counter, (
                label,
                target_archive,
                target_das,
                pocket,
                source_release,
                status,
                name,
            ) in enumerate(
                (
                    # A publication in another archive must not contribute.
                    (
                        "archive",
                        other_archive,
                        das,
                        source.pocket,
                        source.sourcepackagerelease,
                        PackagePublishingStatus.PUBLISHED,
                        package_name,
                    ),
                    # The distribution matches, but the series does not.
                    (
                        "series",
                        archive,
                        other_das,
                        source.pocket,
                        source.sourcepackagerelease,
                        PackagePublishingStatus.PUBLISHED,
                        package_name,
                    ),
                    # Updates-pocket files must not enter Release results.
                    (
                        "pocket",
                        archive,
                        das,
                        PackagePublishingPocket.UPDATES,
                        source.sourcepackagerelease,
                        PackagePublishingStatus.PUBLISHED,
                        package_name,
                    ),
                    # Matching binary names from another source release
                    # must not be merged with the selected release's files.
                    (
                        "source",
                        archive,
                        das,
                        source.pocket,
                        other_source,
                        PackagePublishingStatus.PUBLISHED,
                        package_name,
                    ),
                    # Inactive sibling publications are excluded even when
                    # the binary name and publication location match.
                    (
                        "inactive",
                        archive,
                        das,
                        source.pocket,
                        source.sourcepackagerelease,
                        PackagePublishingStatus.SUPERSEDED,
                        package_name,
                    ),
                    # Another binary produced by this source is unrelated
                    # to the selected binary's artifact list.
                    (
                        "name",
                        archive,
                        das,
                        source.pocket,
                        source.sourcepackagerelease,
                        PackagePublishingStatus.PUBLISHED,
                        "other-binary",
                    ),
                ),
                1,
            ):
                # Distinct versions prevent the factory from treating these
                # fixtures as already-existing binary publications.
                unrelated = self.factory.makeBinaryPackagePublishingHistory(
                    archive=target_archive,
                    distroarchseries=target_das,
                    pocket=pocket,
                    source_package_release=source_release,
                    binarypackagename=name,
                    version="2.0-%d" % counter,
                    architecturespecific=True,
                    status=status,
                )
                # Every excluded fixture needs a file, otherwise dropping
                # its filter could leave the response unchanged.
                self.factory.makeBinaryPackageFile(
                    binarypackagerelease=unrelated.binarypackagerelease,
                    library_file=self.factory.makeLibraryFileAlias(
                        filename="excluded-%s.deb" % label, db_only=True
                    ),
                )
            archive_url = api_url(archive)
            binary_id = first.id
        # One publication id must retrieve both matching architectures.
        ws = webservice_for_person(self.person, default_api_version="devel")
        response = ws.named_get(
            archive_url, "getBinaryPackageDetails", binary_id=binary_id
        )
        self.assertEqual(200, response.status)
        # Check the exact list to catch missing matches and unrelated files.
        self.assertEqual(
            ["foo-first.deb", "foo-other.deb"],
            [file["filename"] for file in response.jsonBody()["files"]],
        )


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
