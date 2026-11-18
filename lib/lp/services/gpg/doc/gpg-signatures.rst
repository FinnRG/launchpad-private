OpenPGP Signature Verification
==============================

    >>> from lp.testing.gpgkeys import import_public_test_keys
    >>> import transaction
    >>> import_public_test_keys()

    >>> transaction.commit()

    >>> from lp.testing.keyserver import KeyServerTac
    >>> keyserver = KeyServerTac()
    >>> keyserver.setUp()

    >>> from lp.testing import login
    >>> from lp.services.webapp.interfaces import ILaunchBag

    >>> bag = getUtility(ILaunchBag)

    >>> bag.user is None
    True

    >>> login("test@canonical.com")
    >>> print(bag.user.name)
    name12

    >>> from zope.component import getUtility
    >>> from lp.services.gpg.interfaces import GPGVerificationError, IGPGHandler
    >>> gpghandler = getUtility(IGPGHandler)

The text below was "clear signed" by 0xDFD20543 master key:

    >>> content = b"""-----BEGIN PGP SIGNED MESSAGE-----
    ... Hash: SHA1
    ...
    ... Test Message.
    ... -----BEGIN PGP SIGNATURE-----
    ... Version: GnuPG v1.2.5 (GNU/Linux)
    ...
    ... iD8DBQFC7ZYY2yWXVgK6XvYRAgcwAJ43g/8X6DguixRucoXN86No67/W2wCgjFDj
    ... jLeauuXDPTcnzDmDzCaQLXo=
    ... =ettP
    ... -----END PGP SIGNATURE-----
    ... """

    >>> master_sig = gpghandler.getVerifiedSignature(content)
    >>> print(master_sig.fingerprint)
    A419AE861E88BC9E04B9C26FBA2B9389DFD20543

The text below was "clear signed" by a 0x02BA5EF6, a subkey of 0xDFD20543

    >>> content = b"""-----BEGIN PGP SIGNED MESSAGE-----
    ... Hash: SHA1
    ...
    ... Test Message.
    ... -----BEGIN PGP SIGNATURE-----
    ... Version: GnuPG v1.2.5 (GNU/Linux)
    ...
    ... iD8DBQFC7ZVH2yWXVgK6XvYRAqWEAJsF9I1MK2tRPPcxjXR2QclSPiQEsgCgtwst
    ... dZ7wZYeW68bk6GuuadabsSY=
    ... =ioKn
    ... -----END PGP SIGNATURE-----
    ... """
    >>>

    >>> subkey_sig = gpghandler.getVerifiedSignature(content)
    >>> print(subkey_sig.fingerprint)
    A419AE861E88BC9E04B9C26FBA2B9389DFD20543


    >>> master_sig.fingerprint == subkey_sig.fingerprint
    True

The text below was "clear signed" by 0xDFD20543 master key but tampered with:

    >>> content = b"""-----BEGIN PGP SIGNED MESSAGE-----
    ... Hash: SHA1
    ...
    ... Test Message
    ... -----BEGIN PGP SIGNATURE-----
    ... Version: GnuPG v1.2.5 (GNU/Linux)
    ...
    ... iD8DBQFC7ZYY2yWXVgK6XvYRAgcwAJ43g/8X6DguixRucoXN86No67/W2wCgjFDj
    ... jLeauuXDPTcnzDmDzCaQLXo=
    ... =ettP
    ... -----END PGP SIGNATURE-----
    ... """

    >>> try:
    ...     gpghandler.getVerifiedSignature(content)
    ... except GPGVerificationError as e:
    ...     source, code, message = e.args[0]
    ...     print(int(source), int(code), message)
    ... else:
    ...     raise AssertionError("Tampered signature was accepted")
    7 8 Bad signature

If no signed content is found, an exception is raised:

    >>> content = b"""-----BEGIN PGP SIGNED MESSAGE-----
    ... Hash: SHA1
    ...
    ... Test Message
    ... -----BEGIN PGP SIGNATURE-----
    ... -----END PGP SIGNATURE-----
    ... """

    >>> master_sig = gpghandler.getVerifiedSignature(content)
    Traceback (most recent call last):
    ...
    lp.services.gpg.interfaces.GPGVerificationError: No signatures found


The text below contains two clear signed sections.  As there are two
signing keys involved here, we raise a verification error, since the
signed text can not be attributed solely to either key:

    >>> content = b"""
    ... -----BEGIN PGP SIGNED MESSAGE-----
    ... Hash: SHA1
    ...
    ... Test Message.
    ... -----BEGIN PGP SIGNATURE-----
    ... Version: GnuPG v1.4.1 (GNU/Linux)
    ...
    ... iD8DBQFD3xV52yWXVgK6XvYRAtJQAJ4ojuLC4aap4R9T0og17RkPYoND+ACfbCA3
    ... yrZD6MZcqzyaGNy1s28Co2Q=
    ... =5QGd
    ... -----END PGP SIGNATURE-----
    ... -----BEGIN PGP SIGNED MESSAGE-----
    ... Hash: SHA1
    ...
    ... Some data appended by foo.bar@canonical.com
    ... -----BEGIN PGP SIGNATURE-----
    ... Version: GnuPG v1.4.1 (GNU/Linux)
    ...
    ... iD8DBQFD3xWpjn63CGxkqMURAmi6AJ4yHAnhIpt49VlYDG1uxpGy9BmHwwCeKbFM
    ... aHIJLqhWVf8bGLHZBIH5odw=
    ... =iUSC
    ... -----END PGP SIGNATURE-----
    ... """

Originally we could test for the exception text "Single signature expected,
found multiple signatures", but this stopped working as of
https://ubuntu.com/security/notices/USN-432-2
(https://launchpad.net/ubuntu/+source/gpgme1.0/1.1.0-1ubuntu0.1), and GPGME
now only gives us a rather less informative "Bad data" exception.  We don't
care too much about the details as long as it fails.

    >>> master_sig = gpghandler.getVerifiedSignature(content)
    Traceback (most recent call last):
    ...
    lp.services.gpg.interfaces.GPGVerificationError: ...

The text below was signed by  key that's is not part of the
imported keyring. Note that we have extra debug information containing
the GPGME error codes (they may be helpful).

    >>> content = b"""-----BEGIN PGP SIGNED MESSAGE-----
    ... Hash: SHA1
    ...
    ... Text Message
    ... -----BEGIN PGP SIGNATURE-----
    ... Version: GnuPG v1.4.2.2 (GNU/Linux)
    ...
    ... iD8DBQFFo+jp4ZLAVDsbsusRAhcYAJ9OOo7+tAxK94xGDu5yIUQG1LEY+wCeJvxr
    ... bOpYlIQD8vo7f9Y6LGqJbCc=
    ... =ds3K
    ... -----END PGP SIGNATURE-----
    ... """
    >>> gpghandler.getVerifiedSignature(content)
    Traceback (most recent call last):
    ...
    lp.services.gpg.interfaces.GPGKeyDoesNotExistOnServer:
    GPG key E192C0543B1BB2EB does not exist on the keyserver.

Due to unpredictable behaviour between the production system and
the external keyserver, we have a resilient signature verifier,
encapsulated in 'getVerifiedSignatureResilient'.

It retries the failed verification 2 other times before raising an
exception. The exception raised by this method will contain debug
information for the 3 failures.

    >>> gpghandler.getVerifiedSignatureResilient(content)
    Traceback (most recent call last):
    ...
    lp.services.gpg.interfaces.GPGVerificationError:
    Verification failed 3 times:
    ['GPG key E192C0543B1BB2EB does not exist on the keyserver.',
     'GPG key E192C0543B1BB2EB does not exist on the keyserver.',
     'GPG key E192C0543B1BB2EB does not exist on the keyserver.']


Debugging exceptions
--------------------

The GPGVerificationError exception object has some additional attributes
with information about the error.  These come directly from the gpgme module
itself.

    >>> content = b"""
    ... -----BEGIN PGP SIGNED MESSAGE-----
    ... Hash: SHA1
    ...
    ... Test Message.
    ... -----BEGIN PGP SIGNATURE-----
    ... Version: GnuPG v1.4.1 (GNU/Linux)
    ...
    ... iD8DBQFD3xV52yWXVgK6XvYRAtJQAJ4ojuLC4aap4R9T0og17RkPYoND+ACfbCA3
    ... yrZD6MZcqzyaGNy1s28Co2Q=
    ... =5QGd
    ... -----END PGP SIGNATURE-----
    ... -----BEGIN PGP SIGNED MESSAGE-----
    ... Hash: SHA1
    ...
    ... Some data appended by foo.bar@canonical.com
    ... -----BEGIN PGP SIGNATURE-----
    ... Version: GnuPG v1.4.1 (GNU/Linux)
    ...
    ... iD8DBQFD3xWpjn63CGxkqMURAmi6AJ4yHAnhIpt49VlYDG1uxpGy9BmHwwCeKbFM
    ... aHIJLqhWVf8bGLHZBIH5odw=
    ... =iUSC
    ... -----END PGP SIGNATURE-----
    ... """
    >>> try:
    ...     gpghandler.getVerifiedSignature(content)
    ... except GPGVerificationError as e:
    ...     source, code, message = e.args
    ...     print(int(source), int(code), message)
    ...     print(e.code)
    ...     print(e.signatures)
    ...     print(e.source)
    ... else:
    ...     raise AssertionError("Multiple signed sections were accepted")
    ...
    7 89 Bad data
    89
    [<gpgme.Signature object at ...>]
    7
    >>> gpghandler.resetLocalState()
    >>> keyserver.tearDown()
