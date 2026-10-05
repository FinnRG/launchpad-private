# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Visibility of archive permission records and their webservice entries."""

from zope.component import getUtility
from zope.security.interfaces import Unauthorized

from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.privacy.interfaces import IObjectPrivacy
from lp.services.webapp.authorization import check_permission
from lp.services.webapp.interfaces import OAuthPermission
from lp.soyuz.enums import ArchivePurpose
from lp.soyuz.interfaces.archivepermission import IArchivePermissionSet
from lp.testing import (
    ANONYMOUS,
    TestCaseWithFactory,
    admin_logged_in,
    api_url,
    login,
    login_person,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.pages import webservice_for_person


class TestArchivePermissionVisibility(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def makePermission(self, **kwargs):
        archive = self.factory.makeArchive(**kwargs)
        with admin_logged_in():
            permissions = getUtility(IArchivePermissionSet)
            permission = permissions.newComponentUploader(
                archive, self.factory.makePerson(), "main"
            )
        return archive, permission

    def assertVisibility(self, expected, permission):
        for name in ("launchpad.View", "launchpad.LimitedView"):
            self.assertEqual(
                expected, check_permission(name, permission), name
            )
        if expected:
            self.assertIsNotNone(permission.person)
        else:
            self.assertRaises(Unauthorized, getattr, permission, "person")

    def test_privacy_follows_archive(self):
        archive, permission = self.makePermission()
        self.assertFalse(IObjectPrivacy(permission).is_private)
        with admin_logged_in():
            archive.private = True
        self.assertTrue(IObjectPrivacy(permission).is_private)

    def test_anonymous_public(self):
        archive, permission = self.makePermission()
        login(ANONYMOUS)
        self.assertVisibility(True, permission)

    def test_anonymous_private(self):
        archive, permission = self.makePermission(private=True)
        login(ANONYMOUS)
        self.assertVisibility(False, permission)

    def test_anonymous_disabled(self):
        archive, permission = self.makePermission(enabled=False)
        login(ANONYMOUS)
        self.assertVisibility(False, permission)

    def test_unrelated_user_private(self):
        archive, permission = self.makePermission(private=True)
        login_person(self.factory.makePerson())
        self.assertVisibility(False, permission)

    def test_owner_private(self):
        owner = self.factory.makePerson()
        archive, permission = self.makePermission(private=True, owner=owner)
        login_person(owner)
        self.assertVisibility(True, permission)

    def test_subscriber_private(self):
        owner = self.factory.makePerson()
        subscriber = self.factory.makePerson()
        archive, permission = self.makePermission(private=True, owner=owner)
        with person_logged_in(owner):
            archive.newSubscription(subscriber, registrant=owner)
        login_person(subscriber)
        self.assertTrue(check_permission("launchpad.LimitedView", archive))
        self.assertFalse(check_permission("launchpad.View", archive))
        self.assertVisibility(False, permission)


class TestArchivePermissionWebService(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def makePermission(self, person=None, **kwargs):
        archive = self.factory.makeArchive(**kwargs)
        with admin_logged_in():
            if person is None:
                person = self.factory.makePerson()
            permissions = getUtility(IArchivePermissionSet)
            permission = permissions.newComponentUploader(
                archive, person, "main"
            )
            urls = api_url(archive), api_url(permission), api_url(person)
        return urls

    def assertCollection(self, webservice, response, expected):
        self.assertEqual(200, response.status, response.body)
        body = response.jsonBody()
        if "total_size" in body:
            total = body["total_size"]
        else:
            size_response = webservice.get(body["total_size_link"])
            self.assertEqual(200, size_response.status)
            total = size_response.jsonBody()
        self.assertEqual(expected, total)
        self.assertEqual(expected, len(body["entries"]))
        return body["entries"]

    def checkAllOperations(self, authenticated=False):
        with admin_logged_in():
            archive = self.factory.makeArchive(purpose=ArchivePurpose.PRIMARY)
            person = self.factory.makePerson()
            reader = self.factory.makePerson() if authenticated else None
            source = self.factory.makeSourcePackageName()
            series = self.factory.makeDistroSeries(
                distribution=archive.distribution
            )
            packageset = self.factory.makePackageset(
                distroseries=series, packages=[source]
            )
            permissions = getUtility(IArchivePermissionSet)
            permissions.newComponentUploader(archive, person, "main")
            permissions.newPackageUploader(archive, person, source)
            permissions.newPackagesetUploader(archive, person, packageset)
            permissions.newPocketUploader(
                archive, person, PackagePublishingPocket.PROPOSED
            )
            permissions.newQueueAdmin(archive, person, "main")
            permissions.newPocketQueueAdmin(
                archive, person, PackagePublishingPocket.PROPOSED
            )
            archive_url = api_url(archive)
            person_url = api_url(person)
            source_name = source.name
            packageset_url = api_url(packageset)
        operations = [
            ("getAllPermissions", {}, 6),
            ("getPermissionsForPerson", {"person": person_url}, 6),
            (
                "getUploadersForPackage",
                {"source_package_name": source_name},
                1,
            ),
            ("getUploadersForComponent", {"component_name": "main"}, 1),
            ("getQueueAdminsForComponent", {"component_name": "main"}, 1),
            ("getComponentsForQueueAdmin", {"person": person_url}, 1),
            ("getUploadersForPocket", {"pocket": "Proposed"}, 1),
            ("getPocketsForUploader", {"person": person_url}, 1),
            ("getQueueAdminsForPocket", {"pocket": "Proposed"}, 1),
            ("getPocketsForQueueAdmin", {"person": person_url}, 1),
            (
                "getUploadersForPackageset",
                {"packageset": packageset_url},
                1,
            ),
            ("getPackagesetsForUploader", {"person": person_url}, 1),
            (
                "getPackagesetsForSourceUploader",
                {"sourcepackagename": source_name, "person": person_url},
                1,
            ),
            (
                "getPackagesetsForSource",
                {"sourcepackagename": source_name},
                1,
            ),
        ]
        webservice = webservice_for_person(reader, default_api_version="devel")
        for operation, parameters, expected in operations:
            with self.subTest(operation=operation):
                entries = self.assertCollection(
                    webservice,
                    webservice.named_get(
                        archive_url,
                        operation,
                        **parameters,
                        **{"ws.size": expected},
                    ),
                    expected,
                )
                for entry in entries:
                    self.assertTrue(entry["person_link"].endswith(person_url))
                    self.assertTrue(
                        entry["archive_link"].endswith(archive_url)
                    )
                    direct = webservice.get(entry["self_link"])
                    self.assertEqual(200, direct.status, direct.body)
                    for field in (
                        "self_link",
                        "archive_link",
                        "person_link",
                        "permission",
                    ):
                        self.assertEqual(
                            entry[field], direct.jsonBody()[field]
                        )

    def test_all_operations_anonymous(self):
        self.checkAllOperations()

    def test_all_operations_authenticated(self):
        self.checkAllOperations(authenticated=True)

    def test_public_team_stable_api(self):
        team = self.factory.makeTeam()
        archive_url, permission_url, person_url = self.makePermission(team)
        webservice = webservice_for_person(None, default_api_version="1.0")
        entries = self.assertCollection(
            webservice,
            webservice.named_get(
                archive_url, "getUploadersForComponent", component_name="main"
            ),
            1,
        )
        self.assertTrue(entries[0]["person_link"].endswith(person_url))
        self.assertEqual("main", entries[0]["component_name"])
        self.assertEqual(200, webservice.get(permission_url).status)

    def test_pagination(self):
        with admin_logged_in():
            archive = self.factory.makeArchive()
            expected_people = set()
            for _ in range(3):
                person = self.factory.makePerson()
                getUtility(IArchivePermissionSet).newComponentUploader(
                    archive, person, "main"
                )
                expected_people.add(api_url(person))
            archive_url = api_url(archive)
        webservice = webservice_for_person(None, default_api_version="1.0")
        for size in (1, 2, 3):
            with self.subTest(size=size):
                response = webservice.named_get(
                    archive_url,
                    "getUploadersForComponent",
                    component_name="main",
                    **{"ws.size": size},
                )
                people = []
                visited = set()
                while True:
                    self.assertEqual(200, response.status, response.body)
                    body = response.jsonBody()
                    self.assertEqual(3, body["total_size"])
                    self.assertGreater(len(body["entries"]), 0)
                    self.assertLessEqual(len(body["entries"]), size)
                    people.extend(e["person_link"] for e in body["entries"])
                    next_url = body.get("next_collection_link")
                    if next_url is None:
                        break
                    self.assertNotIn(next_url, visited)
                    visited.add(next_url)
                    response = webservice.get(next_url)
                self.assertEqual(3, len(people))
                self.assertEqual(3, len(set(people)))
                for person_url in expected_people:
                    self.assertTrue(
                        any(p.endswith(person_url) for p in people)
                    )

    def test_empty_collection(self):
        archive_url = api_url(self.factory.makeArchive())
        webservice = webservice_for_person(None, default_api_version="devel")
        self.assertCollection(
            webservice,
            webservice.named_get(archive_url, "getAllPermissions"),
            0,
        )

    def assertRestrictedResponses(
        self, webservice, archive_url, permission_url
    ):
        """Check status and error body for collection and direct access."""
        collection_response = webservice.named_get(
            archive_url, "getAllPermissions"
        )
        direct_response = webservice.get(permission_url)
        self.assertEqual(
            401, collection_response.status, collection_response.body
        )
        self.assertEqual(
            b"(<Archive object>, 'getAllPermissions', 'launchpad.View')",
            collection_response.body,
        )
        self.assertEqual(401, direct_response.status, direct_response.body)
        self.assertEqual(
            b"(<ArchivePermission object>, 'permission', 'launchpad.View')",
            direct_response.body,
        )

    def checkRestricted(self, private=False, enabled=True, reader=None):
        archive_url, permission_url, person_url = self.makePermission(
            private=private, enabled=enabled
        )
        webservice = webservice_for_person(
            reader,
            permission=OAuthPermission.READ_PRIVATE,
            default_api_version="devel",
        )
        self.assertRestrictedResponses(webservice, archive_url, permission_url)

    def test_private_anonymous(self):
        self.checkRestricted(private=True)

    def test_private_unrelated_user(self):
        self.checkRestricted(private=True, reader=self.factory.makePerson())

    def test_disabled_anonymous(self):
        self.checkRestricted(enabled=False)

    def test_private_owner(self):
        owner = self.factory.makePerson()
        archive_url, permission_url, person_url = self.makePermission(
            private=True, owner=owner
        )
        webservice = webservice_for_person(
            owner,
            permission=OAuthPermission.READ_PRIVATE,
            default_api_version="devel",
        )
        self.assertCollection(
            webservice,
            webservice.named_get(archive_url, "getAllPermissions"),
            1,
        )
        self.assertEqual(200, webservice.get(permission_url).status)

    def test_private_owner_public_token(self):
        owner = self.factory.makePerson()
        archive_url, permission_url, person_url = self.makePermission(
            private=True, owner=owner
        )
        webservice = webservice_for_person(
            owner,
            permission=OAuthPermission.READ_PUBLIC,
            default_api_version="devel",
        )
        self.assertRestrictedResponses(webservice, archive_url, permission_url)

    def test_private_subscriber(self):
        owner = self.factory.makePerson()
        subscriber = self.factory.makePerson()
        with admin_logged_in():
            archive = self.factory.makeArchive(private=True, owner=owner)
            permissions = getUtility(IArchivePermissionSet)
            permission = permissions.newComponentUploader(
                archive, self.factory.makePerson(), "main"
            )
            archive_url = api_url(archive)
            permission_url = api_url(permission)
        with person_logged_in(owner):
            archive.newSubscription(subscriber, registrant=owner)
        webservice = webservice_for_person(
            subscriber,
            permission=OAuthPermission.READ_PRIVATE,
            default_api_version="devel",
        )
        self.assertEqual(200, webservice.get(archive_url).status)
        self.assertRestrictedResponses(webservice, archive_url, permission_url)
