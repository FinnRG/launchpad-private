# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interfaces for requesting validation-run autopkgtests."""

__all__ = [
    "IRequestAutopkgtestJob",
    "IRequestAutopkgtestJobSource",
    "IRequestAutopkgtestBackend",
    "IAutopkgtestAdapter",
]

from zope.interface import Interface

from lp.services.job.interfaces.job import IRunnableJob


class IRequestAutopkgtestJob(IRunnableJob):
    """A persistent request for one validation package and architecture."""


class IRequestAutopkgtestJobSource(Interface):
    def create(validation, publication, attempt=0):
        """Create or return a request and its waiting report.

        None allocates an explicit new attempt.  The caller owns the
        transaction; submission state is created later by the worker.
        """

    def retry(job):
        """Create a new attempt after its preceding report is terminal.

        :raises ValueError: if the attempt is not the latest, its report is
            not terminal, or its validation or publication can no longer be
            tested.
        """


class IRequestAutopkgtestBackend(Interface):
    """The separately configured execution side of a request job."""

    def requestTests(job):
        """Request autopkgtests for the published package described by job.

        Use the archive, source version, commit, architecture, and test
        options stored in job.metadata. Check that the package is available
        to download before submitting it to the test service.

        Save the service response needed to identify the submission on
        retry, and update the attempt's report and create a polling watch
        to track its result. Raise AutopkgtestRequestNotReady when packages
        or the test service are temporarily unavailable.
        """

    def cancelTests(job):
        """Cancel local reports and watches for this request job."""

    def cancelForValidation(validation):
        """Stop local tracking in a dedicated lifecycle worker."""

    def cancelRemoteTests(job):
        """Delegate remote cancellation separately from local cleanup."""


class IAutopkgtestAdapter(Interface):
    """Translate the internal contract to the external service protocol.

    Bound network operations and translate transport failures into
    AutopkgtestAdapterUnavailable, TimeoutError, or ConnectionError. Verify
    remote correlation before returning a receipt or result. Authentication
    belongs in transport configuration, never saved metadata.
    """

    def checkReadiness(request):
        """Return AutopkgtestReadiness after checking package availability."""

    def reconcile(request, service_metadata=None):
        """Recover a submission or a verified equivalent exact-build result.

        ABSENT means a previous attempt cannot subsequently be accepted.
        ACCEPTED requires a receipt identifying the submission or explicit
        build tracking. Otherwise return UNKNOWN, not permission to submit
        again. Resume from service_metadata saved by the previous lookup,
        and return updated metadata so bounded searches can make progress.
        """

    def submit(request):
        """Return a verified receipt, possibly a labelled local build ID."""

    def poll(request, receipt):
        """Return a normalized result with verified request identity."""

    def cancel(request, receipt):
        """Request remote cancellation and report whether it was confirmed."""
