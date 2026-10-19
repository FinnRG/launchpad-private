# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Update merge proposal validation reports from build events."""

__all__ = [
    "binary_build_created",
    "binary_build_status_changed",
    "source_build_status_changed",
    "source_upload_status_changed",
]

from lp.soyuz.enums import PackageUploadStatus
from lp.soyuz.model.mergeproposalvalidation import MergeProposalValidation


def _status_changed(event):
    return event.edited_fields is not None and "status" in event.edited_fields


def source_build_status_changed(build, event):
    if _status_changed(event):
        MergeProposalValidation.updateSourceBuildReport(build)


def source_upload_status_changed(upload, event):
    """Mark a source build as successful once its upload is accepted."""
    if (
        not _status_changed(event)
        or upload.status != PackageUploadStatus.ACCEPTED
        or not upload.archive.is_ppa
    ):
        return
    for source in upload.sources:
        build = source.sourcepackagerelease.source_package_git_build
        if build is not None and build.archive == upload.archive:
            MergeProposalValidation.updateSourceBuildReport(build)


def binary_build_created(build, event):
    MergeProposalValidation.updateBinaryBuildReport(build)


def binary_build_status_changed(build, event):
    if _status_changed(event):
        MergeProposalValidation.updateBinaryBuildReport(build)
