#!/usr/bin/python3 -S
#
# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Verify that Librarian files referenced in the db exist in Swift."""

import _pythonpath  # noqa: F401

from lp.services.librarian.scripts.verify_swift import VerifyLibrarianSwift

if __name__ == "__main__":
    script = VerifyLibrarianSwift(
        "librarian-verify-swift", dbuser="librarianverifyswift"
    )
    script.lock_and_run()
