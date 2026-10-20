# Copyright 2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from heapq import nlargest

from zope.component import getUtility

from lp.code.enums import RevisionStatusResult
from lp.code.interfaces.revisionstatus import (
    IRevisionStatusArtifact,
    IRevisionStatusReportSet,
)
from lp.code.revisionstatus import CheckSelection
from lp.services.librarian.browser import FileNavigationMixin
from lp.services.propertycache import cachedproperty
from lp.services.webapp import Navigation
from lp.services.webapp.authorization import check_permission

_RESULT_ICONS = {
    RevisionStatusResult.SUCCEEDED: ("Succeeded", "/@@/yes"),
    RevisionStatusResult.SKIPPED: ("Skipped", "/@@/yes-gray"),
    RevisionStatusResult.FAILED: ("Failed", "/@@/no"),
    RevisionStatusResult.CANCELLED: ("Cancelled", "/@@/no"),
    RevisionStatusResult.RUNNING: ("Running", "/@@/processing"),
    RevisionStatusResult.WAITING: ("Waiting", "/@@/processing"),
    None: ("No checks", "/@@/info"),
}


def presentChecks(selection):
    """Present current checks and up to five newest visible history reports."""
    phases = {}
    for report in selection.current:
        if not check_permission("launchpad.View", report):
            continue
        if selection.validation is None:
            phase = report.group_name or "CI checks"
        elif report.title == "source-build":
            phase = "Source build"
        elif report.properties.get("binary_build_id") is not None:
            phase = "Binary builds"
        else:
            phase = "Autopkgtests"
        phases.setdefault(phase, []).append(report)
    order = {"Source build": 0, "Binary builds": 1, "Autopkgtests": 2}
    groups = []
    for title, reports in sorted(
        phases.items(), key=lambda item: (order.get(item[0], 3), item[0])
    ):
        reports.sort(
            key=lambda report: (
                (
                    report.distro_arch_series.architecturetag
                    if report.distro_arch_series is not None
                    else ""
                ),
                report.title,
            )
        )
        result = CheckSelection(current=reports).result
        groups.append(
            {
                "title": title,
                "reports": reports,
                "status": _RESULT_ICONS[result][0],
                "icon": _RESULT_ICONS[result][1],
                "count": len(reports),
                "passed": sum(
                    report.result == RevisionStatusResult.SUCCEEDED
                    for report in reports
                ),
                # Validation phases can be long, so they start collapsed.
                "open": None if selection.validation is not None else "open",
            }
        )
    source_build = next(
        (
            report.source_package_git_build
            for report in selection.current
            if report.title == "source-build"
            and report.source_package_git_build is not None
        ),
        None,
    )
    release = (
        source_build.source_package_release
        if source_build is not None
        and check_permission("launchpad.View", source_build)
        else None
    )
    display_result = selection.result if selection.current else None
    history = nlargest(
        6,
        (
            report
            for report in selection.history
            if check_permission("launchpad.View", report)
        ),
        key=lambda report: (report.date_created, report.id),
    )
    return {
        "groups": groups,
        "history": history[:5],
        "history_truncated": len(history) > 5,
        "issues": selection.issues,
        "validation": selection.validation,
        "status": _RESULT_ICONS[display_result][0],
        "icon": _RESULT_ICONS[display_result][1],
        "count": len(selection.current),
        "version": str(release.version) if release is not None else None,
    }


class RevisionStatusArtifactNavigation(Navigation, FileNavigationMixin):
    """Traversal to +files/${filename}."""

    usedfor = IRevisionStatusArtifact


class HasRevisionStatusReportsMixin:
    def canViewCheckBuild(self, build):
        return build is not None and check_permission("launchpad.View", build)

    def _getCheckSelections(self, repository, commit_sha1):
        key = (repository.id, commit_sha1)
        if key not in self._check_selections:
            self._check_selections[key] = getUtility(
                IRevisionStatusReportSet
            ).getCommitChecks(repository, commit_sha1)
        return self._check_selections[key]

    @cachedproperty
    def _check_selections(self):
        return {}

    def getGroupedStatusReports(self, repository, commit_sha1):
        return [
            presentChecks(selection)
            for selection in self._getCheckSelections(repository, commit_sha1)
        ]

    def getValidationStatus(self, validation):
        reports = getUtility(IRevisionStatusReportSet)
        return presentChecks(reports.getValidationChecks(validation))

    def getOverallIcon(self, repository, commit_sha1):
        """Show an appropriate icon at the top of the report."""
        icon_template = (
            '<img width="14" height="14" alt="%(title)s" '
            'title="%(title)s" src="%(src)s" />'
        )
        selections = self._getCheckSelections(repository, commit_sha1)
        combined = CheckSelection(
            current=[report for s in selections for report in s.current],
            issues=[issue for s in selections for issue in s.issues],
        )
        title, source = _RESULT_ICONS[combined.result]
        if combined.result in (
            RevisionStatusResult.RUNNING,
            RevisionStatusResult.WAITING,
        ):
            title = "In progress"
        elif combined.result == RevisionStatusResult.CANCELLED:
            title = "Failed"
        return icon_template % {"title": title, "src": source}
