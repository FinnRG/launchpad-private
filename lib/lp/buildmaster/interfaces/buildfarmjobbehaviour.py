# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interface for build farm job behaviours."""

__all__ = [
    "BuildArgs",
    "IBuildFarmJobBehaviour",
]

from typing import Any, Dict, Generator, List, Sequence, TypedDict, Union

from zope.interface import Attribute, Interface


# XXX cjwatson 2023-01-04: This should ultimately end up as a protocol
# specification maintained in launchpad-buildd as (probably) pydantic
# models, but this is difficult while Launchpad runs on Python < 3.7.
# XXX cjwatson 2023-01-04: Several of these items are only valid for certain
# build job types; however, TypedDict only supports inheritance if you're
# using the class-based syntax, which isn't available in Python 3.5.  As a
# result, it's difficult to construct a completely accurate type declaration
# on Python 3.5.  In the meantime, job type constraints are noted in the
# comments with the type name (i.e. `IBuildFarmJobBehaviour.builder_type`)
# in brackets.
class BuildArgs(TypedDict, total=False):
    # True if this build should build architecture-independent packages
    # as well as architecture-dependent packages [binarypackage].
    arch_indep: bool
    # The architecture tag to build for. (deprecated in favor of
    # abi_tag and isa_tag)
    arch_tag: str
    # The architecture (i.e. ABI) tag to build for.
    abi_tag: str
    # The ISA tag to build for.
    isa_tag: str
    # Whether this is a build in a private archive.  (This causes URLs
    # in the build log to be sanitized.)
    archive_private: bool
    # The name of the target archive's purpose, e.g. PRIMARY or PPA
    # [binarypackage; required for sourcepackagerecipe].
    archive_purpose: str
    # A list of sources.list lines to use for this build.
    archives: List[str]
    # The email address of the person who requested the recipe build
    # [required for sourcepackagerecipe].
    author_email: str
    # The name of the person who requested the recipe build [required
    # for sourcepackagerecipe].
    author_name: str
    # The URL of the Bazaar branch to build from [charm, ci, oci, snap,
    # translation-templates].
    branch: str
    # The URL of the Bazaar branch to build from
    # [translation-templates].  Deprecated alias for branch.
    branch_url: str
    # ARG variables to pass when building this OCI recipe [oci].
    build_args: Dict[str, str]
    # If True, this build should also build debug symbol packages
    # [binarypackage].
    build_debug_symbols: bool
    # The relative path to the build file within this recipe's branch
    # [oci].
    build_file: str
    # The subdirectory within this recipe's branch containing the build
    # file [charm, oci].
    build_path: str
    # The ID of the build request that prompted this build [snap].
    build_request_id: int
    # The RFC3339-formatted time when the build request that prompted
    # this build was made [snap].
    build_request_timestamp: str
    # If True, also build a tarball containing all source code [snap].
    build_source_tarball: bool
    # The URL of this build.
    build_url: str
    # Builder resource tags required by this build farm job.
    builder_constraints: Sequence[str]
    # Source snap channels to use for this build [charm, ci, snap].
    channels: Dict[str, str]
    # The date stamp to set in the built image [livefs].
    datestamp: str
    # A dictionary of additional environment variables to pass to the CI
    # build runner [ci].
    environment_variables: Dict[str, str]
    # If True, this build is running in an ephemeral environment; skip
    # final cleanup steps.
    fast_cleanup: bool
    # True if this build is for a Git-based source package recipe,
    # otherwise False [sourcepackagerecipe].
    git: bool
    # The Git branch path to build from [charm, ci, oci, snap,
    # translation-templates].
    git_path: str
    # The URL of the Git repository to build from [charm, ci, oci, snap,
    # translation-templates].
    git_repository: str
    # A list of stages in this build's configured pipeline [required for
    # ci].
    jobs: List[str]
    # Dictionary of additional metadata to pass to the build [oci].
    # XXX cjwatson 2023-01-04: This doesn't appear to be used by
    # launchpad-buildd at the moment.
    metadata: Dict[str, Any]
    # The name of the recipe [required for charm, oci, snap].
    name: str
    # The name of the component to build for [required for
    # binarypackage, sourcepackagerecipe].  This argument has a strange
    # name due to a historical in-joke: because components form a sort
    # of layered structure where "outer" components like universe
    # include "inner" components like main, the component structure was
    # at one point referred to as the "ogre model" (from the movie
    # "Shrek": "Ogres have layers.  Onions have layers.  You get it?  We
    # both have layers.").
    ogrecomponent: str
    # A list of sources.list lines for the CI build runner to use [ci].
    package_repositories: List[str]
    # A list of PPA_Reference-pin_priority key value pairs
    pins: List[str]
    # A dictionary of plugin settings to pass to the CI build runner
    # [ci].
    plugin_settings: Dict[str, str]
    # The lower-cased name of the pocket to build from [required for
    # livefs].
    pocket: str
    # If True, the source of this build is private [snap; also passed
    # for charm and ci but currently unused there].
    private: bool
    # The URL of the proxy for internet access [charm, ci, oci, snap].
    proxy_url: str
    # The platform name to build for [charm, snap, sources, rocks].
    craft_platform: str
    # The text of the recipe to build [required for
    # sourcepackagerecipe].
    recipe_text: str
    # The URL for revoking proxy authorization tokens [charm, ci, oci,
    # snap].
    revocation_endpoint: str
    # If True, scan job output for malware [ci].
    scan_malware: bool
    # A dictionary of secrets to pass to the CI build runner [ci].
    secrets: Dict[str, str]
    # The name of the series to build for [required for all types].
    series: str
    # The name of the suite to build for [required for binarypackage,
    # sourcepackagerecipe].
    suite: str
    # A list of target architecture tags to build for [snap].
    target_architectures: List[str]
    # A list of base64-encoded public keys for apt archives used by this
    # build.
    trusted_keys: List[str]
    # Is true, use the fetch service as the proxy within the builder.
    use_fetch_service: bool
    # The URL of the Launchpad server.
    launchpad_server_url: str
    # Launchpad instance: production, qastaging, staging, devel.
    launchpad_instance: str


class IBuildFarmJobBehaviour(Interface):
    builder_type = Attribute(
        "The name of the builder type to use for this build, corresponding "
        "to a launchpad-buildd build manager tag."
    )

    image_types = Attribute(
        "A list of `BuildBaseImageType`s indicating which types of base "
        "images can be used for this build."
    )

    build = Attribute("The `IBuildFarmJob` to build.")

    archive = Attribute("The `Archive` to build against.")

    distro_arch_series = Attribute("The `DistroArchSeries` to build against.")

    pocket = Attribute("The `PackagePublishingPocket` to build against.")

    def setBuilder(builder, worker):
        """Sets the associated builder and worker for this instance."""

    def determineFilesToSend():
        """Work out which files to send to the builder.

        :return: A dict mapping filenames to dicts as follows, or a Deferred
                resulting in the same::
            'sha1': SHA-1 of file content
            'url': URL from which the builder can fetch content
            'username' (optional): username to authenticate as
            'password' (optional): password to authenticate with
        """

    def issueMacaroon():
        """Issue a macaroon to access private resources for this build.

        :raises NotImplementedError: if the build type does not support
            accessing private resources.
        :return: A Deferred that calls back with a serialized macaroon or a
            fault.
        """

    def extraBuildArgs(
        logger=None,
    ) -> Union[BuildArgs, Generator[Any, Any, BuildArgs]]:
        """Return extra arguments required by the builder for this build.

        :param logger: An optional logger.
        :return: A dict of builder arguments, or a Deferred resulting in the
            same.
        """

    def composeBuildRequest(logger):
        """Compose parameters for a worker build request.

        :param logger: A logger to be used to log diagnostic information.
        :return: A tuple of (
            "builder type", `DistroArchSeries` to build against,
            `PackagePublishingPocket` to build against,
            {filename: `sendFileToWorker` arguments}, {extra build arguments}),
            or a Deferred resulting in the same.
        """

    def dispatchBuildToWorker(logger):
        """Dispatch a specific build to the worker.

        :param logger: A logger to be used to log diagnostic information.
        """

    def verifyBuildRequest(logger):
        """Carry out any pre-build checks.

        :param logger: A logger to be used to log diagnostic information.
        """

    def verifySuccessfulBuild():
        """Check that we are allowed to collect this successful build."""

    def handleStatus(bq, worker_status):
        """Update the build from a worker's status response.

        :param bq: The `BuildQueue` currently being processed.
        :param worker_status: Worker status dict from `BuilderWorker.status`.
        """

    def redactXmlrpcArguments(args):
        """Redact arguments before getting logged.

        `args` is a nested data structure which contains secrets in different
        forms. The secrets will be replaced with the string `<redacted>` to
        prevent leaking them while creating log entries.

        :param args: A nested data structure.
        :return: A text representation of the input with redacted secrets.
        """
