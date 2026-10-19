# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Persist autopkgtest submission intent, receipts, reports, and watches."""

__all__ = ["AutopkgtestRequestBackend"]

from copy import deepcopy
from datetime import datetime, timezone

import transaction
from zope.component import getUtility, queryUtility
from zope.interface import implementer
from zope.security.proxy import removeSecurityProxy

from lp.buildmaster.enums import BuildStatus
from lp.code.autopkgtest import (
    AutopkgtestAdapterUnavailable,
    AutopkgtestCancellation,
    AutopkgtestLookup,
    AutopkgtestLookupStatus,
    AutopkgtestReadiness,
    AutopkgtestReceipt,
    AutopkgtestRequest,
    AutopkgtestWatchReference,
    UnconfiguredAutopkgtestAdapter,
)
from lp.code.enums import RevisionStatusResult
from lp.code.interfaces.autopkgtest import (
    IAutopkgtestAdapter,
    IRequestAutopkgtestBackend,
)
from lp.code.interfaces.revisionstatuswatch import IRevisionStatusWatchSet
from lp.code.model.branchmergeproposaljob import (
    AutopkgtestRequestNotReady,
    BranchMergeProposalJob,
    BranchMergeProposalJobType,
    RequestAutopkgtestJob,
)
from lp.code.model.revisionstatus import RevisionStatusReport
from lp.code.scripts.revisionstatuswatch import FINAL_RESULTS
from lp.services.database.interfaces import IStore
from lp.services.features import getFeatureFlag
from lp.soyuz.enums import PackagePublishingStatus
from lp.soyuz.model.binarypackagebuild import BinaryPackageBuild
from lp.soyuz.model.publishing import BinaryPackagePublishingHistory


@implementer(IRequestAutopkgtestBackend)
class AutopkgtestRequestBackend:
    """Submit validation autopkgtests and manage their reports and watches."""

    remote_system = "autopkgtest"

    def __init__(self, adapter=None, transaction_manager=None):
        self.adapter = adapter
        self.txn = (
            transaction.manager
            if transaction_manager is None
            else transaction_manager
        )

    def _getAdapter(self):
        if self.adapter is not None:
            return self.adapter
        return queryUtility(IAutopkgtestAdapter) or (
            UnconfiguredAutopkgtestAdapter()
        )

    def _lock(self, job):
        store = IStore(job.context)
        store.flush()
        store.execute(
            "SELECT id FROM BranchMergeProposalJob WHERE id = %s FOR UPDATE",
            (job.id,),
        )
        store.autoreload(job.context)
        return job.metadata.get("autopkgtest")

    def _getReport(self, state):
        report = IStore(RevisionStatusReport).get(
            RevisionStatusReport, state["report_id"]
        )
        if report is None:
            raise AutopkgtestRequestNotReady("Autopkgtest report is missing")
        return report

    def _getBuilds(self, publication):
        """Include other architectures that may provide arch-all binaries."""
        source_package_release = publication.build.source_package_release
        return [
            build
            for build in IStore(BinaryPackageBuild).find(
                BinaryPackageBuild,
                source_package_release=source_package_release,
                archive=publication.archive,
            )
            if build.distro_arch_series.distroseries
            == publication.distroarchseries.distroseries
        ]

    def _getExpectedBinaries(self, publication):
        return sorted(
            {
                binary.binarypackagename.name
                for build in self._getBuilds(publication)
                for binary in build.binarypackages
                if build.distro_arch_series == publication.distroarchseries
                or not binary.architecturespecific
            }
        )

    def _checkPublication(self, job, state):
        context = job.getAutopkgtestContext()
        if context is None:
            self._cancelLocked(job, state)
            self.txn.commit()
            return False
        validation, publication = context
        builds = self._getBuilds(publication)
        for build in builds:
            if build.distro_arch_series != publication.distroarchseries:
                continue
            if build.status in (
                BuildStatus.FAILEDTOBUILD,
                BuildStatus.FAILEDTOUPLOAD,
            ):
                self._fail(
                    job, "Autopkgtest error: a binary build failed", state
                )
                return False
            if build.status != BuildStatus.FULLYBUILT:
                raise AutopkgtestRequestNotReady(
                    "Not all binary builds are complete"
                )
            binaries = list(build.binarypackages)
            if not binaries:
                self._fail(
                    job, "Autopkgtest error: build produced no binaries", state
                )
                return False
            for binary in binaries:
                published = (
                    IStore(BinaryPackagePublishingHistory)
                    .find(
                        BinaryPackagePublishingHistory,
                        binarypackagerelease=binary,
                        archive=publication.archive,
                        distroarchseries=build.distro_arch_series,
                        pocket=publication.pocket,
                        status=PackagePublishingStatus.PUBLISHED,
                    )
                    .any()
                )
                if published is None:
                    raise AutopkgtestRequestNotReady(
                        "Not all binaries are published"
                    )
        self.txn.commit()
        return True

    def _prepare(self, job):
        state = self._lock(job)
        if state is None:
            context = job.getAutopkgtestContext()
            if context is None:
                self.txn.commit()
                return None
            validation, publication = context
            report = job.getOrCreateRevisionStatusReport(
                validation, publication
            )
            if report.result in FINAL_RESULTS:
                self.txn.commit()
                return None
            metadata = job.metadata
            arch = publication.distroarchseries
            request = AutopkgtestRequest(
                key=report.properties["request_key"],
                validation_id=validation.id,
                proposal_id=job.branch_merge_proposal.id,
                archive_id=publication.archive.id,
                archive_reference=publication.archive.archive_url,
                distribution=publication.archive.distribution.name,
                series=arch.distroseries.name,
                architecture=arch.architecturetag,
                pocket=publication.pocket.name.lower(),
                source_package_name=metadata["source_package_name"],
                source_package_version=metadata["source_package_version"],
                commit_sha1=metadata["commit_sha1"],
                source_package_git_build_id=(
                    metadata["source_package_git_build_id"]
                ),
                parameters=metadata["parameters"],
                expected_binary_packages=self._getExpectedBinaries(
                    publication
                ),
                date_requested=datetime.now(timezone.utc).strftime(
                    "%Y%m%d_%H%M%S"
                ),
            )
            state = {
                "schema_version": 1,
                "phase": "prepared",
                "request": request.to_dict(),
                "report_id": report.id,
                "receipt": None,
            }
            job.updateAutopkgtestState(state)
        if state.get("schema_version") != 1:
            raise AutopkgtestRequestNotReady("Unsupported submission version")
        if state.get("phase") not in (
            "prepared",
            "submitting",
            "accepted",
            "cancelled",
            "skipped",
            "failed",
        ):
            raise AutopkgtestRequestNotReady("Invalid submission phase")
        if state["phase"] == "prepared":
            context = job.getAutopkgtestContext()
            if context is not None:
                state["request"]["expected_binary_packages"] = (
                    self._getExpectedBinaries(context[1])
                )
                job.updateAutopkgtestState(state)
        request = AutopkgtestRequest.from_dict(state["request"])
        self._getReport(state)
        if state.get("receipt") is not None:
            AutopkgtestReceipt.from_dict(state["receipt"]).validate(request)
        elif state["phase"] == "accepted":
            raise AutopkgtestRequestNotReady(
                "Accepted submission has no receipt"
            )
        self.txn.commit()
        return state

    def _cancelLocked(self, job, state):
        report = self._getReport(state)
        if report.result not in FINAL_RESULTS:
            report.update(result=RevisionStatusResult.CANCELLED)
        watch_set = removeSecurityProxy(getUtility(IRevisionStatusWatchSet))
        watch = watch_set.findByReport(report)
        if watch is not None:
            watch.destroySelf()
        state["phase"] = "cancelled"
        job.updateAutopkgtestState(state)

    def cancelTests(self, job):
        """Mark an unfinished report cancelled and remove its polling watch."""
        state = self._lock(job)
        if state is not None:
            self._cancelLocked(job, state)
        elif job.metadata.get("report_id") is not None:
            report = self._getReport(job.metadata)
            if report.result not in FINAL_RESULTS:
                report.update(result=RevisionStatusResult.CANCELLED)
        self.txn.commit()

    def cancelForValidation(self, validation):
        jobs = list(
            IStore(BranchMergeProposalJob).find(
                BranchMergeProposalJob,
                job_type=BranchMergeProposalJobType.REQUEST_AUTOPKGTEST,
                branch_merge_proposal=validation.branch_merge_proposal,
            )
        )
        for base_job in jobs:
            if base_job.metadata.get("validation_id") == validation.id:
                self.cancelTests(RequestAutopkgtestJob(base_job))
        self.txn.commit()

    def cancelRemoteTests(self, job):
        state = self._lock(job)
        self.txn.commit()
        if state is None or state.get("receipt") is None:
            return None
        request = AutopkgtestRequest.from_dict(state["request"])
        receipt = AutopkgtestReceipt.from_dict(state["receipt"])
        receipt.validate(request)
        response = self._getAdapter().cancel(request, receipt)
        if not isinstance(response, AutopkgtestCancellation):
            raise ValueError("Adapter must return a cancellation response")
        state = self._lock(job)
        state["remote_cancellation"] = {
            "acknowledged": response.acknowledged,
            "supported": response.supported,
            "reason": response.reason,
        }
        job.updateAutopkgtestState(state)
        self.txn.commit()
        return response

    def _checkGeneration(self, state, expected):
        if (
            state.get("generation", 0) != expected.get("generation", 0)
            or state["phase"] != expected["phase"]
            or state["request"] != expected["request"]
        ):
            raise AutopkgtestRequestNotReady("Submission state changed")

    def _markSubmitting(self, job, expected):
        state = self._lock(job)
        if state["phase"] in ("cancelled", "skipped", "failed", "accepted"):
            self.txn.commit()
            return False
        self._checkGeneration(state, expected)
        if job.getAutopkgtestContext() is None:
            self._cancelLocked(job, state)
            self.txn.commit()
            return False
        if not getFeatureFlag("autopkgtest.request.enabled"):
            raise AutopkgtestRequestNotReady("Autopkgtest requests disabled")
        state["phase"] = "submitting"
        state["generation"] = state.get("generation", 0) + 1
        job.updateAutopkgtestState(state)
        self.txn.commit()
        return True

    def _fail(self, job, reason, expected):
        state = self._lock(job)
        self._checkGeneration(state, expected)
        if job.getAutopkgtestContext() is None:
            self._cancelLocked(job, state)
        else:
            self._getReport(state).update(
                result=RevisionStatusResult.FAILED,
                result_summary=reason,
            )
            state["phase"] = "failed"
            job.updateAutopkgtestState(state)
        self.txn.commit()

    def _accept(self, job, request, receipt):
        if not isinstance(receipt, AutopkgtestReceipt):
            raise AutopkgtestRequestNotReady("Adapter returned no receipt")
        try:
            receipt.validate(request)
        except (TypeError, ValueError) as error:
            raise AutopkgtestRequestNotReady(
                "Adapter returned an invalid receipt"
            ) from error
        state = self._lock(job)
        saved = AutopkgtestRequest.from_dict(state["request"])
        if saved.fingerprint != request.fingerprint:
            raise AutopkgtestRequestNotReady("Submission identity changed")
        previous = state.get("receipt")
        if (
            previous is not None
            and previous["request_id"] != receipt.request_id
        ):
            raise AutopkgtestRequestNotReady("Conflicting submission receipts")
        state["receipt"] = receipt.to_dict()
        if (
            state["phase"] == "cancelled"
            or job.getAutopkgtestContext() is None
        ):
            self._cancelLocked(job, state)
        else:
            report = self._getReport(state)
            report.update(url=receipt.url)
            watch_set = removeSecurityProxy(
                getUtility(IRevisionStatusWatchSet)
            )
            if report.result not in FINAL_RESULTS:
                if watch_set.findByReport(report) is None:
                    watch_set.new(
                        report,
                        self.remote_system,
                        AutopkgtestWatchReference(
                            job.id, request, receipt
                        ).to_dict(),
                    )
            state["phase"] = "accepted"
            job.updateAutopkgtestState(state)
        self.txn.commit()

    def requestTests(self, job):
        try:
            state = self._prepare(job)
            if state is None or state["phase"] in (
                "accepted",
                "cancelled",
                "skipped",
                "failed",
            ):
                return
            request = AutopkgtestRequest.from_dict(state["request"])
            adapter = self._getAdapter()
            if state["phase"] == "submitting":
                lookup = adapter.reconcile(
                    AutopkgtestRequest.from_dict(request.to_dict()),
                    service_metadata=deepcopy(state.get("reconciliation")),
                )
                if not isinstance(lookup, AutopkgtestLookup):
                    raise AutopkgtestRequestNotReady("Invalid request lookup")
                if lookup.status == AutopkgtestLookupStatus.ACCEPTED:
                    self._accept(job, request, lookup.receipt)
                    return
                if lookup.status != AutopkgtestLookupStatus.ABSENT:
                    if lookup.service_metadata:
                        current = self._lock(job)
                        self._checkGeneration(current, state)
                        current["reconciliation"] = deepcopy(
                            lookup.service_metadata
                        )
                        job.updateAutopkgtestState(current)
                        self.txn.commit()
                    raise AutopkgtestRequestNotReady(
                        "Submission is uncertain; it will not be repeated"
                    )
            if not self._checkPublication(job, state):
                return
            readiness = adapter.checkReadiness(
                AutopkgtestRequest.from_dict(request.to_dict())
            )
            if not isinstance(readiness, AutopkgtestReadiness):
                raise AutopkgtestRequestNotReady("Invalid readiness response")
            if not readiness.eligible:
                self._fail(
                    job, readiness.reason or "Package is ineligible", state
                )
                return
            if not readiness.ready:
                raise AutopkgtestRequestNotReady("Packages are not ready")
            if self._markSubmitting(job, state):
                receipt = adapter.submit(
                    AutopkgtestRequest.from_dict(request.to_dict())
                )
                self._accept(job, request, receipt)
        except AutopkgtestAdapterUnavailable as error:
            self.txn.abort()
            raise AutopkgtestRequestNotReady(str(error)) from error
        except (TimeoutError, ConnectionError) as error:
            self.txn.abort()
            raise AutopkgtestRequestNotReady(
                "Autopkgtest service request failed"
            ) from error
        except Exception:
            self.txn.abort()
            raise
