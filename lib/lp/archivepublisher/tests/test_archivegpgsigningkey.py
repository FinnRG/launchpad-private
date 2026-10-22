# Copyright 2016-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test ArchiveGPGSigningKey."""

import os
from textwrap import dedent
from unittest import mock

import treq
from testtools.matchers import (
    Equals,
    FileContains,
    Is,
    MatchesStructure,
    Not,
    StartsWith,
)
from testtools.twistedsupport import (
    AsynchronousDeferredRunTest,
    AsynchronousDeferredRunTestForBrokenTwisted,
)
from twisted.internet import defer, reactor
from zope.component import getUtility

from lp.archivepublisher.config import getPubConfig
from lp.archivepublisher.interfaces.archivegpgsigningkey import (
    PUBLISHER_GPG_USES_SIGNING_SERVICE,
    IArchiveGPGSigningKey,
    ISignableArchive,
)
from lp.archivepublisher.interfaces.publisherconfig import IPublisherConfigSet
from lp.archivepublisher.tests.test_run_parts import RunPartsMixin
from lp.registry.interfaces.gpg import IGPGKeySet
from lp.services.features.testing import FeatureFixture
from lp.services.gpg.interfaces import IGPGHandler
from lp.services.gpg.tests.test_gpghandler import FakeGenerateKey
from lp.services.log.logger import BufferLogger
from lp.services.osutils import write_file
from lp.services.signing.enums import SigningKeyType, SigningMode
from lp.services.signing.interfaces.signingkey import (
    IArchiveSigningKeySet,
    ISigningKeySet,
)
from lp.services.signing.tests.helpers import SigningServiceClientFixture
from lp.services.twistedsupport.testing import TReqFixture
from lp.services.twistedsupport.treq import check_status
from lp.soyuz.enums import ArchivePurpose
from lp.testing import TestCaseWithFactory
from lp.testing.gpgkeys import gpgkeysdir, test_pubkey_from_email
from lp.testing.keyserver import InProcessKeyServerFixture
from lp.testing.layers import ZopelessDatabaseLayer


class TestSignableArchiveWithSigningKey(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer
    run_tests_with = AsynchronousDeferredRunTest.make_factory(timeout=30)

    @defer.inlineCallbacks
    def setUp(self):
        super().setUp()
        self.temp_dir = self.makeTemporaryDirectory()
        self.distro = self.factory.makeDistribution()
        db_pubconf = getUtility(IPublisherConfigSet).getByDistribution(
            self.distro
        )
        db_pubconf.root_dir = self.temp_dir
        self.archive = self.factory.makeArchive(
            distribution=self.distro, purpose=ArchivePurpose.PRIMARY
        )
        self.archive_root = getPubConfig(self.archive).archiveroot
        self.suite = "distroseries"

        yield self.useFixture(InProcessKeyServerFixture()).start()
        key_path = os.path.join(gpgkeysdir, "ppa-sample@canonical.com.sec")
        yield IArchiveGPGSigningKey(self.archive).setSigningKey(
            key_path, async_keyserver=True
        )

    def test_signFile_absolute_within_archive(self):
        filename = os.path.join(self.archive_root, "signme")
        write_file(filename, b"sign this")

        signer = ISignableArchive(self.archive)
        self.assertTrue(signer.can_sign)
        signer.signFile(self.suite, filename)

        self.assertTrue(os.path.exists(filename + ".gpg"))

    def test_signFile_absolute_outside_archive(self):
        filename = os.path.join(self.temp_dir, "signme")
        write_file(filename, b"sign this")

        signer = ISignableArchive(self.archive)
        self.assertTrue(signer.can_sign)
        self.assertRaises(
            AssertionError, signer.signFile, self.suite, filename
        )

    def test_signFile_relative_within_archive(self):
        filename_relative = "signme"
        filename = os.path.join(self.archive_root, filename_relative)
        write_file(filename, b"sign this")

        signer = ISignableArchive(self.archive)
        self.assertTrue(signer.can_sign)
        signer.signFile(self.suite, filename_relative)

        self.assertTrue(os.path.exists(filename + ".gpg"))

    def test_signFile_relative_outside_archive(self):
        filename_relative = "../signme"
        filename = os.path.join(self.temp_dir, filename_relative)
        write_file(filename, b"sign this")

        signer = ISignableArchive(self.archive)
        self.assertTrue(signer.can_sign)
        self.assertRaises(
            AssertionError, signer.signFile, self.suite, filename_relative
        )

    def test_signRepository_uses_signing_service(self):
        # If the appropriate feature rule is true, then we use the signing
        # service to sign files.
        self.useFixture(
            FeatureFixture({PUBLISHER_GPG_USES_SIGNING_SERVICE: "on"})
        )
        signing_service_client = self.useFixture(
            SigningServiceClientFixture(self.factory)
        )
        self.factory.makeSigningKey(
            key_type=SigningKeyType.OPENPGP,
            fingerprint=self.archive.signing_key_fingerprint,
        )
        logger = BufferLogger()

        suite_dir = os.path.join(self.archive_root, "dists", self.suite)
        release_path = os.path.join(suite_dir, "Release")
        write_file(release_path, b"Release contents")

        signer = ISignableArchive(self.archive)
        self.assertTrue(signer.can_sign)
        self.assertContentEqual(
            ["Release.gpg", "InRelease"],
            signer.signRepository(self.suite, log=logger),
        )
        self.assertEqual("", logger.getLogBuffer())
        signing_service_client.sign.assert_has_calls(
            [
                mock.call(
                    SigningKeyType.OPENPGP,
                    [self.archive.signing_key_fingerprint],
                    "Release",
                    b"Release contents",
                    SigningMode.DETACHED,
                ),
                mock.call(
                    SigningKeyType.OPENPGP,
                    [self.archive.signing_key_fingerprint],
                    "Release",
                    b"Release contents",
                    SigningMode.CLEAR,
                ),
            ]
        )
        self.assertThat(
            os.path.join(suite_dir, "Release.gpg"),
            FileContains("signed with key_type=OPENPGP mode=DETACHED"),
        )
        self.assertThat(
            os.path.join(suite_dir, "InRelease"),
            FileContains("signed with key_type=OPENPGP mode=CLEAR"),
        )

    def test_signRepository_uses_all_OpenPGPG_keys_of_an_archive(self):
        self.useFixture(
            FeatureFixture({PUBLISHER_GPG_USES_SIGNING_SERVICE: "on"})
        )
        current_key = self.factory.makeSigningKey(
            key_type=SigningKeyType.OPENPGP,
            fingerprint=self.archive.signing_key_fingerprint,
        )
        new_key = self.factory.makeSigningKey(
            key_type=SigningKeyType.OPENPGP,
            # The fingerprint has to be 30 characters long to pass validation.
            fingerprint="ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
        )
        self.archive.signing_key_fingerprint = current_key.fingerprint
        getUtility(IArchiveSigningKeySet).create(
            self.archive, None, current_key
        )
        getUtility(IArchiveSigningKeySet).create(self.archive, None, new_key)
        signing_service_client = self.useFixture(
            SigningServiceClientFixture(self.factory)
        )
        suite_dir = os.path.join(
            self.archive_root,
            "dists",
            self.suite,
        )
        release_path = os.path.join(suite_dir, "Release")
        write_file(release_path, b"Release contents")
        logger = BufferLogger()
        signer = ISignableArchive(self.archive)
        self.assertTrue(signer.can_sign)
        self.assertContentEqual(
            ["Release.gpg", "InRelease"],
            signer.signRepository(self.suite, log=logger),
        )
        signing_service_client.sign.assert_has_calls(
            [
                mock.call(
                    SigningKeyType.OPENPGP,
                    [current_key.fingerprint, new_key.fingerprint],
                    "Release",
                    b"Release contents",
                    SigningMode.DETACHED,
                ),
                mock.call(
                    SigningKeyType.OPENPGP,
                    [current_key.fingerprint, new_key.fingerprint],
                    "Release",
                    b"Release contents",
                    SigningMode.CLEAR,
                ),
            ]
        )

    def test_signRepository_falls_back_from_signing_service(self):
        # If the signing service fails to sign a file, we fall back to
        # making local signatures if possible.
        self.useFixture(
            FeatureFixture({PUBLISHER_GPG_USES_SIGNING_SERVICE: "on"})
        )
        signing_service_client = self.useFixture(
            SigningServiceClientFixture(self.factory)
        )
        self.factory.makeSigningKey(
            key_type=SigningKeyType.OPENPGP,
            fingerprint=self.archive.signing_key_fingerprint,
        )
        logger = BufferLogger()

        suite_dir = os.path.join(self.archive_root, "dists", self.suite)
        release_path = os.path.join(suite_dir, "Release")
        write_file(release_path, b"Release contents")

        signer = ISignableArchive(self.archive)
        self.assertTrue(signer.can_sign)
        signing_service_client.sign.side_effect = Exception("boom")
        self.assertContentEqual(
            ["Release.gpg", "InRelease"],
            signer.signRepository(self.suite, log=logger),
        )
        self.assertEqual(
            "ERROR Failed to sign archive using signing service; falling back "
            "to local key\n",
            logger.getLogBuffer(),
        )
        signing_service_client.sign.assert_called_once_with(
            SigningKeyType.OPENPGP,
            [self.archive.signing_key_fingerprint],
            "Release",
            b"Release contents",
            SigningMode.DETACHED,
        )
        self.assertThat(
            os.path.join(suite_dir, "Release.gpg"),
            FileContains(
                matcher=StartsWith("-----BEGIN PGP SIGNATURE-----\n")
            ),
        )
        self.assertThat(
            os.path.join(suite_dir, "InRelease"),
            FileContains(
                matcher=StartsWith("-----BEGIN PGP SIGNED MESSAGE-----\n")
            ),
        )


class TestSignableArchiveWithRunParts(RunPartsMixin, TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()
        self.temp_dir = self.makeTemporaryDirectory()
        self.distro = self.factory.makeDistribution()
        db_pubconf = getUtility(IPublisherConfigSet).getByDistribution(
            self.distro
        )
        db_pubconf.root_dir = self.temp_dir
        self.archive = self.factory.makeArchive(
            distribution=self.distro, purpose=ArchivePurpose.PRIMARY
        )
        self.archive_root = getPubConfig(self.archive).archiveroot
        self.suite = "distroseries"
        self.enableRunParts(distribution_name=self.distro.name)
        with open(
            os.path.join(
                self.parts_directory, self.distro.name, "sign.d", "10-sign"
            ),
            "w",
        ) as sign_script:
            sign_script.write(
                dedent(
                    """\
                #! /bin/sh
                echo "$MODE signature of $INPUT_PATH" \\
                     "($ARCHIVEROOT, $DISTRIBUTION/$SUITE)" \\
                    >"$OUTPUT_PATH"
                """
                )
            )
            os.fchmod(sign_script.fileno(), 0o755)

    def test_signRepository_runs_parts(self):
        suite_dir = os.path.join(self.archive_root, "dists", self.suite)
        release_path = os.path.join(suite_dir, "Release")
        write_file(release_path, b"Release contents")

        signer = ISignableArchive(self.archive)
        self.assertTrue(signer.can_sign)
        self.assertContentEqual(
            ["Release.gpg", "InRelease"], signer.signRepository(self.suite)
        )

        self.assertThat(
            os.path.join(suite_dir, "Release.gpg"),
            FileContains(
                "detached signature of %s (%s, %s/%s)\n"
                % (
                    release_path,
                    self.archive_root,
                    self.distro.name,
                    self.suite,
                )
            ),
        )
        self.assertThat(
            os.path.join(suite_dir, "InRelease"),
            FileContains(
                "clear signature of %s (%s, %s/%s)\n"
                % (
                    release_path,
                    self.archive_root,
                    self.distro.name,
                    self.suite,
                )
            ),
        )

    def test_signRepository_honours_pubconf(self):
        pubconf = getPubConfig(self.archive)
        pubconf.distsroot = self.makeTemporaryDirectory()
        suite_dir = os.path.join(pubconf.distsroot, self.suite)
        release_path = os.path.join(suite_dir, "Release")
        write_file(release_path, b"Release contents")

        signer = ISignableArchive(self.archive)
        self.assertTrue(signer.can_sign)
        self.assertRaises(AssertionError, signer.signRepository, self.suite)
        self.assertContentEqual(
            ["Release.gpg", "InRelease"],
            signer.signRepository(self.suite, pubconf=pubconf),
        )

        self.assertThat(
            os.path.join(suite_dir, "Release.gpg"),
            FileContains(
                "detached signature of %s (%s, %s/%s)\n"
                % (
                    release_path,
                    self.archive_root,
                    self.distro.name,
                    self.suite,
                )
            ),
        )
        self.assertThat(
            os.path.join(suite_dir, "InRelease"),
            FileContains(
                "clear signature of %s (%s, %s/%s)\n"
                % (
                    release_path,
                    self.archive_root,
                    self.distro.name,
                    self.suite,
                )
            ),
        )

    def test_signFile_runs_parts(self):
        filename = os.path.join(self.archive_root, "signme")
        write_file(filename, b"sign this")

        signer = ISignableArchive(self.archive)
        self.assertTrue(signer.can_sign)
        signer.signFile(self.suite, filename)

        self.assertThat(
            "%s.gpg" % filename,
            FileContains(
                "detached signature of %s (%s, %s/%s)\n"
                % (filename, self.archive_root, self.distro.name, self.suite)
            ),
        )


class TestArchiveGPGSigningKey(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer
    # treq.content doesn't close the connection before yielding control back
    # to the test, so we need to spin the reactor at the end to finish
    # things off.
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=10000
    )

    @defer.inlineCallbacks
    def setUp(self):
        super().setUp()
        self.temp_dir = self.makeTemporaryDirectory()
        self.pushConfig("personalpackagearchive", root=self.temp_dir)
        self.keyserver = self.useFixture(InProcessKeyServerFixture())
        yield self.keyserver.start()

    @defer.inlineCallbacks
    def test_generateSigningKey_local(self):
        # Generating a signing key locally using GPGHandler stores it in the
        # database and pushes it to the keyserver.
        self.useFixture(FakeGenerateKey("ppa-sample@canonical.com.sec"))
        logger = BufferLogger()
        # Use a display name that matches the pregenerated sample key.
        owner = self.factory.makePerson(
            displayname="Celso \xe1\xe9\xed\xf3\xfa Providelo"
        )
        archive = self.factory.makeArchive(owner=owner)
        self.assertIsNone(archive.signing_key_display_name)
        yield IArchiveGPGSigningKey(archive).generateSigningKey(
            log=logger, async_keyserver=True
        )
        # The key is stored in the database.
        self.assertIsNotNone(archive.signing_key_owner)
        self.assertIsNotNone(archive.signing_key_fingerprint)
        self.assertEqual(
            "1024R/0D57E99656BEFB0897606EE9A022DD1F5001B46D",
            archive.signing_key_display_name,
        )
        # The key is stored as a GPGKey, not a SigningKey.
        self.assertIsNotNone(
            getUtility(IGPGKeySet).getByFingerprint(
                archive.signing_key_fingerprint
            )
        )
        self.assertIsNone(
            getUtility(ISigningKeySet).get(
                SigningKeyType.OPENPGP, archive.signing_key_fingerprint
            )
        )
        # The key is uploaded to the keyserver.
        client = self.useFixture(TReqFixture(reactor)).client
        response = yield client.get(
            getUtility(IGPGHandler).getURLForKeyInServer(
                archive.signing_key_fingerprint, "get"
            )
        )
        yield check_status(response)
        content = yield treq.content(response)
        self.assertIn(b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n", content)

    @defer.inlineCallbacks
    def test_generateSigningKey_local_non_default_ppa(self):
        # Generating a signing key locally using GPGHandler for a
        # non-default PPA generates one for the user's default PPA first and
        # then propagates it.
        self.useFixture(FakeGenerateKey("ppa-sample@canonical.com.sec"))
        logger = BufferLogger()
        # Use a display name that matches the pregenerated sample key.
        owner = self.factory.makePerson(
            displayname="Celso \xe1\xe9\xed\xf3\xfa Providelo"
        )
        default_ppa = self.factory.makeArchive(owner=owner)
        another_ppa = self.factory.makeArchive(owner=owner)
        self.assertIsNone(default_ppa.signing_key_display_name)
        self.assertIsNone(another_ppa.signing_key_display_name)
        yield IArchiveGPGSigningKey(another_ppa).generateSigningKey(
            log=logger, async_keyserver=True
        )
        self.assertThat(
            default_ppa,
            MatchesStructure(
                signing_key=Not(Is(None)),
                signing_key_owner=Not(Is(None)),
                signing_key_fingerprint=Not(Is(None)),
                signing_key_display_name=Equals(
                    "1024R/0D57E99656BEFB0897606EE9A022DD1F5001B46D"
                ),
            ),
        )
        self.assertIsNotNone(
            getUtility(IGPGKeySet).getByFingerprint(
                default_ppa.signing_key_fingerprint
            )
        )
        self.assertIsNone(
            getUtility(ISigningKeySet).get(
                SigningKeyType.OPENPGP, default_ppa.signing_key_fingerprint
            )
        )
        self.assertThat(
            another_ppa,
            MatchesStructure.byEquality(
                signing_key=default_ppa.signing_key,
                signing_key_owner=default_ppa.signing_key_owner,
                signing_key_fingerprint=default_ppa.signing_key_fingerprint,
                signing_key_display_name=default_ppa.signing_key_display_name,
            ),
        )

    @defer.inlineCallbacks
    def test_generateSigningKey_signing_service(self):
        # Generating a signing key on the signing service stores it in the
        # database and pushes it to the keyserver.
        self.useFixture(
            FeatureFixture({PUBLISHER_GPG_USES_SIGNING_SERVICE: "on"})
        )
        signing_service_client = self.useFixture(
            SigningServiceClientFixture(self.factory)
        )
        signing_service_client.generate.side_effect = None
        test_key = test_pubkey_from_email("ftpmaster@canonical.com")
        signing_service_client.generate.return_value = {
            "fingerprint": "33C0A61893A5DC5EB325B29E415A12CAC2F30234",
            "public-key": test_key,
        }
        logger = BufferLogger()
        archive = self.factory.makeArchive()
        self.assertIsNone(archive.signing_key_display_name)
        yield IArchiveGPGSigningKey(archive).generateSigningKey(
            log=logger, async_keyserver=True
        )
        # The key is stored in the database.
        self.assertIsNotNone(archive.signing_key_owner)
        self.assertIsNotNone(archive.signing_key_fingerprint)
        self.assertEqual(
            "4096R/33C0A61893A5DC5EB325B29E415A12CAC2F30234",
            archive.signing_key_display_name,
        )
        # The key is stored as a SigningKey, not a GPGKey.
        self.assertIsNone(
            getUtility(IGPGKeySet).getByFingerprint(
                archive.signing_key_fingerprint
            )
        )
        signing_key = getUtility(ISigningKeySet).get(
            SigningKeyType.OPENPGP, archive.signing_key_fingerprint
        )
        self.assertEqual(test_key, signing_key.public_key)
        # The key is uploaded to the keyserver.
        client = self.useFixture(TReqFixture(reactor)).client
        response = yield client.get(
            getUtility(IGPGHandler).getURLForKeyInServer(
                archive.signing_key_fingerprint, "get"
            )
        )
        yield check_status(response)
        content = yield treq.content(response)
        self.assertIn(test_key, content)

    @defer.inlineCallbacks
    def test_generateSigningKey_signing_service_upload_disabled(self):
        # If keyserver uploads are disabled, a key generated on the signing
        # service is stored in the database but not uploaded.
        self.pushConfig("gpghandler", upload_keys=False)
        self.useFixture(
            FeatureFixture({PUBLISHER_GPG_USES_SIGNING_SERVICE: "on"})
        )
        signing_service_client = self.useFixture(
            SigningServiceClientFixture(self.factory)
        )
        signing_service_client.generate.side_effect = None
        test_key = test_pubkey_from_email("ftpmaster@canonical.com")
        signing_service_client.generate.return_value = {
            "fingerprint": "33C0A61893A5DC5EB325B29E415A12CAC2F30234",
            "public-key": test_key,
        }
        archive = self.factory.makeArchive()
        yield IArchiveGPGSigningKey(archive).generateSigningKey(
            log=BufferLogger(), async_keyserver=True
        )
        self.assertEqual(
            "33C0A61893A5DC5EB325B29E415A12CAC2F30234",
            archive.signing_key_fingerprint,
        )
        client = self.useFixture(TReqFixture(reactor)).client
        response = yield client.get(
            getUtility(IGPGHandler).getURLForKeyInServer(
                archive.signing_key_fingerprint, "get"
            )
        )
        yield treq.content(response)
        self.assertEqual(404, response.code)

    @defer.inlineCallbacks
    def test_generateSigningKey_signing_service_non_default_ppa(self):
        # Generating a signing key on the signing service for a non-default
        # PPA generates one for the user's default PPA first and then
        # propagates it.
        self.useFixture(
            FeatureFixture({PUBLISHER_GPG_USES_SIGNING_SERVICE: "on"})
        )
        signing_service_client = self.useFixture(
            SigningServiceClientFixture(self.factory)
        )
        signing_service_client.generate.side_effect = None
        test_key = test_pubkey_from_email("ftpmaster@canonical.com")
        signing_service_client.generate.return_value = {
            "fingerprint": "33C0A61893A5DC5EB325B29E415A12CAC2F30234",
            "public-key": test_key,
        }
        logger = BufferLogger()
        default_ppa = self.factory.makeArchive()
        another_ppa = self.factory.makeArchive(owner=default_ppa.owner)
        self.assertIsNone(default_ppa.signing_key_display_name)
        self.assertIsNone(another_ppa.signing_key_display_name)
        yield IArchiveGPGSigningKey(another_ppa).generateSigningKey(
            log=logger, async_keyserver=True
        )
        self.assertThat(
            default_ppa,
            MatchesStructure(
                signing_key=Is(None),
                signing_key_owner=Not(Is(None)),
                signing_key_fingerprint=Not(Is(None)),
                signing_key_display_name=Equals(
                    "4096R/33C0A61893A5DC5EB325B29E415A12CAC2F30234"
                ),
            ),
        )
        self.assertIsNone(
            getUtility(IGPGKeySet).getByFingerprint(
                default_ppa.signing_key_fingerprint
            )
        )
        signing_key = getUtility(ISigningKeySet).get(
            SigningKeyType.OPENPGP, default_ppa.signing_key_fingerprint
        )
        self.assertEqual(test_key, signing_key.public_key)
        self.assertThat(
            another_ppa,
            MatchesStructure(
                signing_key=Is(None),
                signing_key_owner=Equals(default_ppa.signing_key_owner),
                signing_key_fingerprint=Equals(
                    default_ppa.signing_key_fingerprint
                ),
                signing_key_display_name=Equals(
                    default_ppa.signing_key_display_name
                ),
            ),
        )

    def test_generateSigningKey_ppa_default_ppa_has_1024R_and_4096R_keys(self):
        self.useFixture(
            FeatureFixture({PUBLISHER_GPG_USES_SIGNING_SERVICE: "on"})
        )
        default_ppa = self.factory.makeArchive()
        owner = default_ppa.owner
        another_ppa = self.factory.makeArchive(owner=owner)
        self.assertIsNone(default_ppa.signing_key_fingerprint)
        self.assertIsNone(another_ppa.signing_key_fingerprint)
        # The follow steps simulate the steps taken by the PPA key
        # updater script when it encounters a default PPA with a
        # 1024-bit RSA signing key. We are doing them manually to
        # avoid a dependency on that function which will go away
        # after the key migration is completed. But this logic
        # of propagating the appropriate key from the default PPA
        # has to be present forever.
        fingerprint_1024R = self.factory.getUniqueHexString(digits=40).upper()
        signing_key_1024R = self.factory.makeSigningKey(
            key_type=SigningKeyType.OPENPGP,
            fingerprint=fingerprint_1024R,
        )
        self.factory.makeGPGKey(
            owner=owner,
            keyid=fingerprint_1024R[-8:],
            fingerprint=fingerprint_1024R,
            keysize=1024,
        )
        default_ppa.signing_key_fingerprint = fingerprint_1024R
        fingerprint_4096R = self.factory.getUniqueHexString(digits=40).upper()
        signing_key_4096R = self.factory.makeSigningKey(
            key_type=SigningKeyType.OPENPGP, fingerprint=fingerprint_4096R
        )
        self.factory.makeGPGKey(
            owner=owner,
            keyid=fingerprint_4096R[-8:],
            fingerprint=fingerprint_4096R,
            keysize=4096,
        )
        getUtility(IArchiveSigningKeySet).create(
            default_ppa,
            None,
            signing_key_1024R,
        )
        getUtility(IArchiveSigningKeySet).create(
            default_ppa,
            None,
            signing_key_4096R,
        )
        logger = BufferLogger()
        IArchiveGPGSigningKey(another_ppa).generateSigningKey(log=logger)
        # The 'another_ppa' PPA should now have the fingerprint of the
        # default PPA's 4096-bit RSA signing key as its signing key fingerprint
        self.assertEqual(
            fingerprint_4096R, another_ppa.signing_key_fingerprint
        )
        # `another_ppa` should also have a row in the `archivesigningkey` table
        # with its new signing key propagated from the default PPA.
        self.assertIsNotNone(
            getUtility(IArchiveSigningKeySet).get4096BitRSASigningKey(
                another_ppa
            )
        )
