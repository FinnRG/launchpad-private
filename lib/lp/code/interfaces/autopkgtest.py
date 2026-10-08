# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interfaces for requesting validation-run autopkgtests."""

__all__ = [
    "IRequestAutopkgtestJob",
    "IRequestAutopkgtestJobSource",
    "IRequestAutopkgtestBackend",
]

from zope.interface import Interface

from lp.services.job.interfaces.job import IRunnableJob


class IRequestAutopkgtestJob(IRunnableJob):
    """A persistent request for one validation package and architecture."""


class IRequestAutopkgtestJobSource(Interface):
    def create(validation, publication):
        """Create or return a package-and-architecture request job."""


class IRequestAutopkgtestBackend(Interface):
    """The separately configured execution side of a request job."""

    def requestTests(job):
        """Request autopkgtests for the published package described by job.

        Use the archive, source version, commit, architecture, and test
        options stored in job.metadata. Check that the package is available
        to download before submitting it to the test service.

        Save the service response needed to identify the submission on
        retry, and create a revision status report and polling watch to
        track its result. Raise AutopkgtestRequestNotReady when packages
        or the test service are temporarily unavailable.
        """
