# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Read-only revision check selections shared by policy and presentation."""

__all__ = ["CheckSelection"]

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

from lp.code.enums import RevisionStatusResult

if TYPE_CHECKING:
    from lp.code.model.revisionstatus import RevisionStatusReport
    from lp.soyuz.model.mergeproposalvalidation import MergeProposalValidation


@dataclass
class CheckSelection:
    current: List["RevisionStatusReport"] = field(default_factory=list)
    history: List["RevisionStatusReport"] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    validation: Optional["MergeProposalValidation"] = None

    @property
    def result(self):
        results = [report.result for report in self.current]
        for result in (
            RevisionStatusResult.FAILED,
            RevisionStatusResult.CANCELLED,
            RevisionStatusResult.RUNNING,
        ):
            if result in results:
                return result
        if self.issues or any(
            result in (None, RevisionStatusResult.WAITING)
            for result in results
        ):
            return RevisionStatusResult.WAITING
        if not results:
            return None
        if all(result == RevisionStatusResult.SKIPPED for result in results):
            return RevisionStatusResult.SKIPPED
        if all(
            result
            in (RevisionStatusResult.SUCCEEDED, RevisionStatusResult.SKIPPED)
            for result in results
        ):
            return RevisionStatusResult.SUCCEEDED
        return None
