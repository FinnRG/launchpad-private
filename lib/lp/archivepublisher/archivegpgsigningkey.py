# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""ArchiveGPGSigningKey implementation."""

__all__ = [
    "ArchiveGPGSigningKey",
    "SignableArchive",
]


import os

import gpgme
from twisted.internet import defer
from twisted.internet.threads import deferToThread
from zope.component import getUtility
from zope.interface import implementer
from zope.security.proxy import ProxyFactory, removeSecurityProxy

from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.archivepublisher.config import getPubConfig
from lp.archivepublisher.interfaces.archivegpgsigningkey import (
    PUBLISHER_GPG_USES_SIGNING_SERVICE,
    CannotSignArchive,
    IArchiveGPGSigningKey,
    ISignableArchive,
)
from lp.archivepublisher.run_parts import find_run_parts_dir, run_parts
from lp.registry.interfaces.gpg import IGPGKeySet
from lp.services.config import config
from lp.services.features import getFeatureFlag
from lp.services.gpg.interfaces import GPGKeyAlgorithm, IGPGHandler, IPymeKey
from lp.services.osutils import remove_if_exists
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.services.signing.enums import (
    OpenPGPKeyAlgorithm,
    SigningKeyType,
    SigningMode,
)
from lp.services.signing.interfaces.signingkey import (
    IArchiveSigningKeySet,
    ISigningKey,
    ISigningKeySet,
)


@implementer(ISignableArchive)
class SignableArchive:
    """`IArchive` adapter for operations that involve signing files."""

    gpgme_modes = {
        SigningMode.DETACHED: gpgme.SIG_MODE_DETACH,
        SigningMode.CLEAR: gpgme.SIG_MODE_CLEAR,
    }

    def __init__(self, archive):
        self.archive = archive
        self.pubconf = getPubConfig(self.archive)

    @cachedproperty
    def _run_parts_dir(self):
        """This distribution's sign.d run-parts directory, if any."""
        return find_run_parts_dir(self.archive.distribution.name, "sign.d")

    @property
    def can_sign(self):
        """See `ISignableArchive`."""
        return (
            self.archive.signing_key_fingerprint is not None
            or self._run_parts_dir is not None
        )

    @cachedproperty
    def _signing_keys(self):
        """This archive's signing keys on the signing service, if any."""
        if not getFeatureFlag(PUBLISHER_GPG_USES_SIGNING_SERVICE):
            return []

        signing_keys = []
        signing_keys.extend(
            getUtility(IArchiveSigningKeySet).getOpenPGPSigningKeysForArchive(
                self.archive,
            )
        )
        if self.archive.signing_key_fingerprint is not None:
            if not any(
                key.fingerprint == self.archive.signing_key_fingerprint
                for key in signing_keys
            ):
                signing_key = getUtility(ISigningKeySet).get(
                    SigningKeyType.OPENPGP,
                    self.archive.signing_key_fingerprint,
                )
                if signing_key:
                    signing_keys.append(signing_key)
        return signing_keys

    @cachedproperty
    def _secret_key(self):
        """This archive's signing key as a local GPG key."""
        if self.archive.signing_key is not None:
            secret_key_path = self.getPathForSecretKey(
                self.archive.signing_key
            )
            with open(secret_key_path, "rb") as secret_key_file:
                secret_key_export = secret_key_file.read()
            gpghandler = getUtility(IGPGHandler)
            return gpghandler.importSecretKey(secret_key_export)
        else:
            return None

    def _makeSignatures(self, signatures, log=None):
        """Make a sequence of signatures.

        This abstraction is useful in the case where we're using an
        in-process `GPGHandler`, since it avoids having to import the secret
        key more than once.

        :param signatures: A sequence of (input path, output path,
            `SigningMode`, suite) tuples.  Note that some backends may make
            a policy decision not to produce all the requested output paths.
        :param log: An optional logger.
        :return: A list of output paths that were produced.
        """
        if not self.can_sign:
            raise CannotSignArchive(
                "No signing key available for %s" % self.archive.displayname
            )

        output_paths = []
        for input_path, output_path, mode, suite in signatures:
            if mode not in {SigningMode.DETACHED, SigningMode.CLEAR}:
                raise ValueError("Invalid signature mode for GPG: %s" % mode)
            signed = False

            if self._signing_keys or self._secret_key is not None:
                with open(input_path, "rb") as input_file:
                    input_content = input_file.read()
                if self._signing_keys:
                    try:
                        signature = getUtility(ISigningKeySet).sign(
                            self._signing_keys,
                            input_content,
                            os.path.basename(input_path),
                            mode=mode,
                        )
                        signed = True
                    except Exception:
                        if log is not None:
                            log.exception(
                                "Failed to sign archive using signing "
                                "service; falling back to local key"
                            )
                        get_property_cache(self)._signing_keys = None
                if not signed and self._secret_key is not None:
                    signature = getUtility(IGPGHandler).signContent(
                        input_content,
                        self._secret_key,
                        mode=self.gpgme_modes[mode],
                    )
                    signed = True
                if signed:
                    with open(output_path, "wb") as output_file:
                        output_file.write(signature)
                    output_paths.append(output_path)

            if not signed and self._run_parts_dir is not None:
                remove_if_exists(output_path)
                env = {
                    "ARCHIVEROOT": self.pubconf.archiveroot,
                    "DISTRIBUTION": self.archive.distribution.name,
                    "INPUT_PATH": input_path,
                    "MODE": mode.name.lower(),
                    "OUTPUT_PATH": output_path,
                    # Allow parts to detect whether they're running on
                    # production.
                    "SITE_NAME": config.vhost.mainsite.hostname,
                    "SUITE": suite,
                }
                run_parts(
                    self.archive.distribution.name, "sign.d", log=log, env=env
                )
                signed = True
                if os.path.exists(output_path):
                    output_paths.append(output_path)

            if not signed:
                raise AssertionError(
                    "No signing key available for %s"
                    % self.archive.displayname
                )
        return output_paths

    def signRepository(self, suite, pubconf=None, suffix="", log=None):
        """See `ISignableArchive`."""
        if pubconf is None:
            pubconf = self.pubconf
        suite_path = os.path.join(pubconf.distsroot, suite)
        release_file_path = os.path.join(suite_path, "Release" + suffix)
        if not os.path.exists(release_file_path):
            raise AssertionError(
                "Release file doesn't exist in the repository: %s"
                % release_file_path
            )

        output_names = []
        for output_path in self._makeSignatures(
            [
                (
                    release_file_path,
                    os.path.join(suite_path, "Release.gpg" + suffix),
                    SigningMode.DETACHED,
                    suite,
                ),
                (
                    release_file_path,
                    os.path.join(suite_path, "InRelease" + suffix),
                    SigningMode.CLEAR,
                    suite,
                ),
            ],
            log=log,
        ):
            output_name = os.path.basename(output_path)
            if suffix:
                output_name = output_name[: -len(suffix)]
            assert (
                os.path.join(suite_path, output_name + suffix) == output_path
            )
            output_names.append(output_name)
        return output_names

    def signFile(self, suite, path, log=None):
        """See `ISignableArchive`."""
        # Allow the passed path to be relative to the archive root.
        path = os.path.realpath(os.path.join(self.pubconf.archiveroot, path))

        # Ensure the resulting path is within the archive root after
        # normalisation.
        # NOTE: uses os.sep to prevent /var/tmp/../tmpFOO attacks.
        archive_root = self.pubconf.archiveroot + os.sep
        if not path.startswith(archive_root):
            raise AssertionError(
                "Attempting to sign file (%s) outside archive_root for %s"
                % (path, self.archive.displayname)
            )

        self._makeSignatures(
            [(path, "%s.gpg" % path, SigningMode.DETACHED, suite)], log=log
        )


@implementer(IArchiveGPGSigningKey)
class ArchiveGPGSigningKey(SignableArchive):
    """`IArchive` adapter for manipulating its GPG key."""

    def getPathForSecretKey(self, key):
        """See `IArchiveGPGSigningKey`."""
        return os.path.join(
            config.personalpackagearchive.signing_keys_root,
            "%s.gpg" % key.fingerprint,
        )

    def exportSecretKey(self, key):
        """See `IArchiveGPGSigningKey`."""
        assert key.secret, "Only secret keys should be exported."
        export_path = self.getPathForSecretKey(key)

        if not os.path.exists(os.path.dirname(export_path)):
            os.makedirs(os.path.dirname(export_path))

        with open(export_path, "wb") as export_file:
            export_file.write(key.export())

    def generateSigningKey(self, log=None, async_keyserver=False):
        """See `IArchiveGPGSigningKey`."""
        assert (
            self.archive.signing_key_fingerprint is None
        ), "Cannot override signing_keys."

        # Always generate signing keys for the default PPA, even if it
        # was not specifically requested. The default PPA signing key
        # is then propagated to the context named-ppa.
        default_ppa = (
            self.archive.owner.archive if self.archive.is_ppa else self.archive
        )
        if self.archive != default_ppa:

            def propagate_key(_):
                self.archive.signing_key_owner = default_ppa.signing_key_owner
                default_ppa_signing_key = (
                    # The signing key table is used only when a key was
                    # generated using the signing service.
                    # Has a replacement key generated using the signing
                    # service.
                    getUtility(IArchiveSigningKeySet).get4096BitRSASigningKey(
                        default_ppa
                    )
                    or
                    # Has a single 4096-bit RSA signing key generated using
                    # the signing service.
                    getUtility(ISigningKeySet).get(
                        SigningKeyType.OPENPGP,
                        default_ppa.signing_key_fingerprint,
                    )
                    or
                    # Has a key generated using local key generation.
                    default_ppa.signing_key
                )
                self.archive.signing_key_fingerprint = (
                    default_ppa_signing_key.fingerprint
                )
                if getFeatureFlag(PUBLISHER_GPG_USES_SIGNING_SERVICE):
                    assert ISigningKey.providedBy(default_ppa_signing_key)
                    getUtility(IArchiveSigningKeySet).create(
                        self.archive, None, default_ppa_signing_key
                    )
                del get_property_cache(self.archive).signing_key
                del get_property_cache(self.archive).signing_key_display_name

            if default_ppa.signing_key_fingerprint is None:
                d = IArchiveGPGSigningKey(default_ppa).generateSigningKey(
                    log=log, async_keyserver=async_keyserver
                )
            else:
                d = defer.succeed(None)
            # generateSigningKey is only asynchronous if async_keyserver is
            # true; we need some contortions to keep it synchronous
            # otherwise.
            if async_keyserver:
                d.addCallback(propagate_key)
                return d
            else:
                propagate_key(None)
                return

        # XXX cjwatson 2021-12-17: If we need key generation for other
        # archive purposes (PRIMARY/PARTNER) then we should extend this, and
        # perhaps push it down to a property of the archive.
        if self.archive.is_copy:
            key_displayname = (
                "Launchpad copy archive %s" % self.archive.reference
            )
        else:
            key_displayname = (
                "Launchpad PPA for %s" % self.archive.owner.displayname
            )
        if getFeatureFlag(PUBLISHER_GPG_USES_SIGNING_SERVICE):
            try:
                signing_key = getUtility(ISigningKeySet).generate(
                    SigningKeyType.OPENPGP,
                    key_displayname,
                    openpgp_key_algorithm=OpenPGPKeyAlgorithm.RSA,
                    length=4096,
                )
            except Exception as e:
                if log is not None:
                    log.exception(
                        "Error generating signing key for %s: %s %s"
                        % (self.archive.reference, e.__class__.__name__, e)
                    )
                raise
        else:
            signing_key = getUtility(IGPGHandler).generateKey(
                key_displayname, logger=log
            )
        return self._setupSigningKey(
            signing_key, async_keyserver=async_keyserver
        )

    def generate4096BitRSASigningKey(self, log=None):
        """See `IArchiveGPGSigningKey`."""
        assert getFeatureFlag(
            PUBLISHER_GPG_USES_SIGNING_SERVICE
        ), "Signing service should be enabled to use this feature."
        assert (
            self.archive.signing_key_fingerprint is not None
        ), "Archive doesn't have an existing signing key to update."
        current_gpg_key = getUtility(IGPGKeySet).getByFingerprint(
            self.archive.signing_key_fingerprint
        )
        if current_gpg_key:
            assert (
                current_gpg_key.keysize == 1024
            ), "Archive already has a 4096-bit RSA signing key."

        default_ppa = self.archive.owner.archive

        # If the current signing key is not in the 'archivesigningkey' table,
        # add it.

        current_archive_signing_key = getUtility(
            IArchiveSigningKeySet
        ).getByArchiveAndFingerprint(
            self.archive, self.archive.signing_key_fingerprint
        )
        if not current_archive_signing_key:
            current_signing_key = getUtility(ISigningKeySet).get(
                SigningKeyType.OPENPGP, self.archive.signing_key_fingerprint
            )
            getUtility(IArchiveSigningKeySet).create(
                self.archive, None, current_signing_key
            )
            if not current_gpg_key:
                # The archive already has a 4096-bit RSA key which is missing
                # an entry in the `gpgkey` table. Add it and return.
                getUtility(IGPGKeySet).new(
                    getUtility(ILaunchpadCelebrities).ppa_key_guard,
                    current_signing_key.fingerprint[-8:],
                    current_signing_key.fingerprint,
                    4096,
                    GPGKeyAlgorithm.R,
                )
                return

        if self.archive != default_ppa:
            if default_ppa.signing_key_fingerprint is None:
                IArchiveGPGSigningKey(default_ppa).generateSigningKey(log=log)

            default_ppa_new_signing_key = getUtility(
                IArchiveSigningKeySet
            ).get4096BitRSASigningKey(default_ppa)
            if default_ppa_new_signing_key is None:
                try:
                    # Recursively update default_ppa key
                    IArchiveGPGSigningKey(
                        default_ppa
                    ).generate4096BitRSASigningKey(log=log)
                except Exception as e:
                    log.error(
                        "Error generating 4096-bit RSA signing key "
                        "for %s - %s",
                        default_ppa.reference,
                        e,
                    )
                    return
                # Refresh the default_ppa_new_signing_key with
                # the newly created one.
                default_ppa_new_signing_key = getUtility(
                    IArchiveSigningKeySet
                ).get4096BitRSASigningKey(default_ppa)
            # Propagate the default PPA 4096-bit RSA signing key
            # to non-default PPAs and return.
            getUtility(IArchiveSigningKeySet).create(
                self.archive, None, default_ppa_new_signing_key
            )
            return

        key_displayname = (
            "Launchpad PPA for %s" % self.archive.owner.displayname
        )
        key_owner = getUtility(ILaunchpadCelebrities).ppa_key_guard
        try:
            signing_key = getUtility(ISigningKeySet).generate(
                SigningKeyType.OPENPGP,
                key_displayname,
                openpgp_key_algorithm=OpenPGPKeyAlgorithm.RSA,
                length=4096,
            )
        except Exception as e:
            if log is not None:
                log.exception(
                    "Error generating signing key for %s: %s %s"
                    % (self.archive.reference, e.__class__.__name__, e)
                )
            raise
        getUtility(IArchiveSigningKeySet).create(
            self.archive, None, signing_key
        )
        getUtility(IGPGKeySet).new(
            key_owner,
            signing_key.fingerprint[-8:],
            signing_key.fingerprint,
            4096,
            GPGKeyAlgorithm.R,
        )
        self._uploadPublicSigningKey(signing_key)

    def setSigningKey(self, key_path, async_keyserver=False):
        """See `IArchiveGPGSigningKey`."""
        assert (
            self.archive.signing_key_fingerprint is None
        ), "Cannot override signing_keys."
        assert os.path.exists(key_path), "%s does not exist" % key_path

        with open(key_path, "rb") as key_file:
            secret_key_export = key_file.read()
        secret_key = getUtility(IGPGHandler).importSecretKey(secret_key_export)
        return self._setupSigningKey(
            secret_key, async_keyserver=async_keyserver
        )

    def _uploadPublicSigningKey(self, signing_key):
        """Upload the public half of a signing key to the keyserver."""
        # The handler's security proxying doesn't protect anything useful
        # here, and when we're running in a thread we don't have an
        # interaction.
        gpghandler = removeSecurityProxy(getUtility(IGPGHandler))
        if IPymeKey.providedBy(signing_key):
            pub_key = gpghandler.retrieveKey(signing_key.fingerprint)
            gpghandler.uploadPublicKey(pub_key.fingerprint)
            return pub_key
        else:
            assert ISigningKey.providedBy(signing_key)
            if config.gpghandler.upload_keys:
                gpghandler.submitKey(
                    removeSecurityProxy(signing_key).public_key
                )
            return signing_key

    def _storeSigningKey(self, pub_key):
        """Store signing key reference in the database."""
        key_owner = getUtility(ILaunchpadCelebrities).ppa_key_guard
        if IPymeKey.providedBy(pub_key):
            key, _ = getUtility(IGPGKeySet).activate(
                key_owner, pub_key, pub_key.can_encrypt
            )
        else:
            assert ISigningKey.providedBy(pub_key)
            key = pub_key
        self.archive.signing_key_owner = key_owner
        self.archive.signing_key_fingerprint = key.fingerprint
        del get_property_cache(self.archive).signing_key
        del get_property_cache(self.archive).signing_key_display_name

    def _setupSigningKey(self, signing_key, async_keyserver=False):
        """Mandatory setup for signing keys.

        * Export the secret key into the protected disk location (for
          locally-generated keys).
        * Upload public key to the keyserver, if enabled in configuration.
        * Store the public GPGKey reference in the database (for
          locally-generated keys) and update the context
          archive.signing_key.
        """
        if IPymeKey.providedBy(signing_key):
            self.exportSecretKey(signing_key)
        if async_keyserver:
            # If we have an asynchronous keyserver running in the current
            # thread using Twisted, then we need some contortions to ensure
            # that the GPG handler doesn't deadlock.  This is most easily
            # done by deferring the GPG handler work to another thread.
            # Since that thread won't have a Zope interaction, we need to
            # unwrap the security proxy for it.
            d = deferToThread(
                self._uploadPublicSigningKey, removeSecurityProxy(signing_key)
            )
            d.addCallback(ProxyFactory)
            d.addCallback(self._storeSigningKey)
            return d
        else:
            pub_key = self._uploadPublicSigningKey(signing_key)
            self._storeSigningKey(pub_key)
