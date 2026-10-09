# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interfaces for revision status reports and artifacts."""

__all__ = [
    "IRevisionStatusArtifact",
    "IRevisionStatusArtifactSet",
    "IRevisionStatusReport",
    "IRevisionStatusReportSet",
    "RevisionStatusReportsFeatureDisabled",
]


import http.client

from lazr.restful.declarations import (
    error_status,
    export_read_operation,
    export_write_operation,
    exported,
    exported_as_webservice_entry,
    mutator_for,
    operation_for_version,
    operation_parameters,
    scoped,
)
from lazr.restful.fields import Reference
from lazr.restful.interface import copy_field
from zope.interface import Attribute, Interface
from zope.schema import Bytes, Choice, Datetime, Dict, Int, TextLine
from zope.security.interfaces import Unauthorized

from lp import _
from lp.app.validators.attachment import attachment_size_constraint
from lp.code.enums import RevisionStatusArtifactType, RevisionStatusResult
from lp.services.auth.enums import AccessTokenScope
from lp.services.fields import PublicPersonChoice, URIField
from lp.soyuz.interfaces.distroarchseries import IDistroArchSeries


@error_status(http.client.UNAUTHORIZED)
class RevisionStatusReportsFeatureDisabled(Unauthorized):
    """Only certain users can access APIs for revision status reports."""

    def __init__(self):
        super().__init__(
            "You do not have permission to create revision status reports"
        )


class IRevisionStatusReportView(Interface):
    """`IRevisionStatusReport` attributes that require launchpad.View."""

    id = Int(title=_("ID"), required=True, readonly=True)

    creator = PublicPersonChoice(
        title=_("Creator"),
        required=True,
        readonly=True,
        vocabulary="ValidPersonOrTeam",
        description=_("The person who created this report."),
    )

    date_created = exported(
        Datetime(
            title=_("When the report was created."),
            required=True,
            readonly=True,
        )
    )
    date_started = exported(
        Datetime(title=_("When the report was started.")), readonly=False
    )
    date_finished = exported(
        Datetime(title=_("When the report has finished.")), readonly=False
    )

    latest_log = Attribute("The most recent log for this report.")

    group_name = exported(
        TextLine(title=_("Report group"), required=False, readonly=True)
    )
    source_package_git_build = Attribute(
        "The source package Git build that produced this report, if any."
    )

    @operation_parameters(
        artifact_type=Choice(
            vocabulary=RevisionStatusArtifactType, required=False
        )
    )
    @scoped(AccessTokenScope.REPOSITORY_BUILD_STATUS.title)
    @export_read_operation()
    @operation_for_version("devel")
    def getArtifactURLs(artifact_type):
        """Retrieves the list of URLs for artifacts that exist for this report.

        :param artifact_type: The type of artifact for the report.
        """


class IRevisionStatusReportEditableAttributes(Interface):
    """`IRevisionStatusReport` attributes that can be edited.

    These attributes need launchpad.View to see, and launchpad.Edit to change.
    """

    title = exported(
        TextLine(
            title=_("A short title for the report."),
            required=True,
            readonly=False,
        )
    )

    git_repository = exported(
        Reference(
            title=_("The Git repository for which this report is built."),
            # Really IGitRepository, patched in _schema_circular_imports.py.
            schema=Interface,
            required=True,
            readonly=True,
        )
    )

    commit_sha1 = exported(
        TextLine(
            title=_("The Git commit for which this report is built."),
            required=True,
            readonly=True,
        )
    )

    url = exported(
        URIField(
            title=_("URL"),
            required=False,
            readonly=False,
            description=_("The external url of the report."),
        )
    )

    result_summary = exported(
        TextLine(
            title=_("A short summary of the result."),
            required=False,
            readonly=False,
        )
    )

    result = exported(
        Choice(
            title=_("Result of the report"),
            readonly=True,
            required=False,
            vocabulary=RevisionStatusResult,
        )
    )

    ci_build = exported(
        Reference(
            title=_("The CI build that produced this report."),
            # Really ICIBuild, patched in _schema_circular_imports.py.
            schema=Interface,
            required=False,
            readonly=True,
        )
    )

    distro_arch_series = exported(
        Reference(
            title=_(
                "The series and architecture for the CI build job that "
                "produced this report."
            ),
            schema=IDistroArchSeries,
            required=False,
            readonly=True,
        )
    )

    properties = exported(
        Dict(
            title=_("Metadata for artifacts attached to this report"),
            key_type=TextLine(),
            required=False,
            readonly=True,
        )
    )

    @mutator_for(result)
    @operation_parameters(result=copy_field(result))
    @export_write_operation()
    @operation_for_version("devel")
    def transitionToNewResult(result):
        """Set the RevisionStatusReport result.

        Set the revision status report result."""


class IRevisionStatusReportEdit(Interface):
    """`IRevisionStatusReport` attributes that require launchpad.Edit."""

    @operation_parameters(
        log_data=Bytes(
            title=_("The content of the artifact in bytes."),
            constraint=attachment_size_constraint,
        )
    )
    @scoped(AccessTokenScope.REPOSITORY_BUILD_STATUS.title)
    @export_write_operation()
    @operation_for_version("devel")
    def setLog(log_data):
        """Set a new log on an existing status report.

        :param log_data: The contents of the log, either as bytes or as a file
            object.
        """

    # XXX cjwatson 2022-01-14: artifact_type isn't currently exported, but
    # if RevisionStatusArtifactType gains more items (e.g. detailed test
    # output in subunit format or similar?) then it may make sense to do so.
    @operation_parameters(
        name=TextLine(title=_("The name of the artifact.")),
        data=Bytes(
            title=_("The content of the artifact in bytes."),
            constraint=attachment_size_constraint,
        ),
    )
    @scoped(AccessTokenScope.REPOSITORY_BUILD_STATUS.title)
    @export_write_operation()
    @operation_for_version("devel")
    def attach(name, data, artifact_type=RevisionStatusArtifactType.BINARY):
        """Attach a new artifact to an existing status report.

        :param data: The contents of the artifact, either as bytes or as a file
            object.
        :param artifact_type: The type of the artifact.  This may currently
            only be `RevisionStatusArtifactType.BINARY`, but more types may
            be added in future.
        """

    @operation_parameters(
        title=TextLine(
            title=_("A short title for the report."), required=False
        ),
        url=TextLine(
            title=_("The external link of the status report."), required=False
        ),
        result_summary=TextLine(
            title=_("A short summary of the result."), required=False
        ),
        result=Choice(vocabulary=RevisionStatusResult, required=False),
        properties=Dict(
            title=_("Properties dictionary"),
            required=False,
        ),
    )
    @scoped(AccessTokenScope.REPOSITORY_BUILD_STATUS.title)
    @export_write_operation()
    @operation_for_version("devel")
    def update(
        title=None, url=None, result_summary=None, result=None, properties=None
    ):
        """Updates a status report.

        :param title: A short title for the report.
        :param url: The external url of the report.
        :param result_summary: A short summary of the result.
        :param result: The result of the report.
        :param properties: A dictionary of general-purpose metadata.
        """


@exported_as_webservice_entry(as_of="beta")
class IRevisionStatusReport(
    IRevisionStatusReportView,
    IRevisionStatusReportEditableAttributes,
    IRevisionStatusReportEdit,
):
    """An revision status report for a Git commit."""


class IRevisionStatusReportSet(Interface):
    """The set of all revision status reports."""

    def new(
        creator,
        title,
        git_repository,
        commit_sha1,
        url=None,
        result_summary=None,
        result=None,
        date_started=None,
        date_finished=None,
        log=None,
        ci_build=None,
        distro_arch_series=None,
        properties=None,
        group_name=None,
        source_package_git_build=None,
    ):
        """Return a new revision status report.

        :param title: A text string.
        :param git_repository: An `IGitRepository` for which the report
            is being created.
        :param commit_sha1: The sha1 of the commit for which the report
            is being created.
        :param url: External URL to view result of report.
        :param result_summary: A short summary of the result.
        :param result: The result of the check job for this revision.
        :param date_started: DateTime that report was started.
        :param date_finished: DateTime that report was completed.
        :param log: Stores the content of the artifact for this report.
        :param ci_build: The `ICIBuild` that produced this report, if any.
        :param distro_arch_series: The series and architecture for the build
            that produced this report, if any.
        :param properties: Metadata for artifacts attached to this report.
        """

    def getByID(id):
        """Returns the RevisionStatusReport for a given ID."""

    def findByRepository(repository):
        """Returns all `RevisionStatusReport` for a repository."""

    def findByCommit(repository, commit_sha1):
        """Returns all `RevisionStatusReport` for a repository and commit."""

    def getByCIBuildAndTitle(ci_build, title):
        """Return the `RevisionStatusReport` for a given CI build and title."""

    def deleteForRepository(repository):
        """Delete all `RevisionStatusReport` for a repository."""


class IRevisionStatusArtifactSet(Interface):
    """The set of all revision status artifacts."""

    def new(lfa, report, artifact_type, date_created=None):
        """Return a new revision status artifact.

        :param lfa: An `ILibraryFileAlias`.
        :param report: An `IRevisionStatusReport` for which the
            artifact is being created.
        :param artifact_type: A `RevisionStatusArtifactType`.
        """

    def getByID(id):
        """Returns the RevisionStatusArtifact for a given ID."""

    def findByReport(report):
        """Returns the set of artifacts for a given report."""

    def findByCIBuild(ci_build):
        """Return all `RevisionStatusArtifact`s for a CI build."""

    def getByRepositoryAndID(repository, id):
        """Returns the artifact for a given repository and ID."""


class IRevisionStatusArtifact(Interface):
    id = Int(title=_("ID"), required=True, readonly=True)

    report = Attribute(
        "The `RevisionStatusReport` that this artifact is linked to."
    )

    library_file_id = Int(
        title=_("LibraryFileAlias ID"), required=True, readonly=True
    )
    library_file = Attribute(
        "The `LibraryFileAlias` object containing information for "
        "a revision status report."
    )

    artifact_type = Choice(
        title=_("The type of artifact, only log for now."),
        vocabulary=RevisionStatusArtifactType,
    )

    repository = Attribute("The repository for this artifact.")

    download_url = Attribute("The download url for this artifact.")

    date_created = Datetime(
        title=_("When the artifact was created."), readonly=True
    )

    def getFileByName(filename):
        """Returns an artifact by name."""
