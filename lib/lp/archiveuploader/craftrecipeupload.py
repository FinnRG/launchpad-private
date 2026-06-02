# Copyright 2024 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Process a craft recipe upload."""

__all__ = [
    "CraftRecipeUpload",
]

import os
import tarfile
import tempfile
from pathlib import Path

import yaml
from zope.component import getUtility

from lp.archiveuploader.utils import (
    SafeTarExtractError,
    UploadError,
    safe_extract_tar,
)
from lp.buildmaster.enums import BuildStatus
from lp.services.helpers import filenameToContentType
from lp.services.librarian.interfaces import ILibraryFileAliasSet


def _upload_to_librarian(path, filename, build, librarian):
    """Upload a single file to the librarian and attach to the build."""
    with open(path, "rb") as f:
        libraryfile = librarian.create(
            filename,
            os.stat(path).st_size,
            f,
            filenameToContentType(str(path)),
            restricted=build.is_private,
        )
    build.addFile(libraryfile)


class ArchiveScanResult:
    """Results of scanning an extracted archive for known artifact types."""

    def __init__(self, tmpdir):
        # safe_extract_tar() has already rejected symlinks, absolute paths,
        # and ".." traversal before we reach this point, so rglob is safe.
        self.crate_files = list(Path(tmpdir).rglob("*.crate"))
        self.jar_files = list(Path(tmpdir).rglob("*.jar"))
        self.pom_files = list(Path(tmpdir).rglob("pom.xml"))
        self.pom_artifact_files = list(Path(tmpdir).rglob("*.pom"))
        self.maven_metadata_files = list(
            Path(tmpdir).rglob("maven-metadata.xml")
        )
        self.metadata_path = Path(tmpdir) / "metadata.yaml"
        self.metadata_exists = self.metadata_path.exists()

    @property
    def has_crate(self):
        return bool(self.crate_files)

    @property
    def has_maven(self):
        return bool(self.jar_files and self.pom_files)

    @property
    def has_maven_staged_repo(self):
        return bool(self.pom_artifact_files and self.maven_metadata_files)


class ArtifactUploadHandler:
    """Base class for artifact-type-specific upload handlers."""

    def can_handle(self, scan):
        """Return True if this handler can process the given scan result."""
        raise NotImplementedError

    def handle(self, scan, build, librarian, logger, craft_path, tmpdir):
        """Process the artifacts and upload them to the librarian."""
        raise NotImplementedError

    def _upload_file(self, path, filename, build, librarian):
        """Upload a single file to the librarian."""
        _upload_to_librarian(path, filename, build, librarian)

    def _upload_metadata(self, scan, build, librarian):
        """Upload metadata.yaml to the librarian."""
        self._upload_file(
            scan.metadata_path, "metadata.yaml", build, librarian
        )


class CrateUploadHandler(ArtifactUploadHandler):
    """Handle Rust crate uploads."""

    def can_handle(self, scan):
        return scan.has_crate and scan.metadata_exists

    def handle(self, scan, build, librarian, logger, craft_path, tmpdir):
        try:
            metadata = yaml.safe_load(scan.metadata_path.read_text())
            crate_name = metadata.get("name")
            crate_version = metadata.get("version")
            logger.debug(
                "Found crate %s version %s", crate_name, crate_version
            )
        except Exception as e:
            logger.warning("Failed to parse metadata.yaml: %s", e)

        crate_path = scan.crate_files[0]
        self._upload_file(
            crate_path, os.path.basename(str(crate_path)), build, librarian
        )
        self._upload_metadata(scan, build, librarian)


class MavenStagedRepoUploadHandler(ArtifactUploadHandler):
    """Handle Maven staged repository uploads (archive preserved as-is)."""

    def can_handle(self, scan):
        return scan.has_maven_staged_repo

    def handle(self, scan, build, librarian, logger, craft_path, tmpdir):
        logger.debug(
            "Found Maven staged repository layout, uploading archive as-is"
        )

        self._upload_file(
            craft_path, os.path.basename(str(craft_path)), build, librarian
        )

        if scan.metadata_exists:
            self._upload_metadata(scan, build, librarian)
        else:
            top_level_entries = sorted(p.name for p in Path(tmpdir).iterdir())
            logger.warning(
                "Maven staged repo archive %s had no root "
                "metadata.yaml. Top-level entries: %s",
                craft_path.name,
                top_level_entries,
            )


class MavenJarUploadHandler(ArtifactUploadHandler):
    """Handle Maven JAR + POM uploads."""

    def can_handle(self, scan):
        return scan.has_maven and scan.metadata_exists

    def handle(self, scan, build, librarian, logger, craft_path, tmpdir):
        try:
            metadata = yaml.safe_load(scan.metadata_path.read_text())
            jar_name = metadata.get("name")
            jar_version = metadata.get("version")
            logger.debug("Found jar %s version %s", jar_name, jar_version)
        except Exception as e:
            logger.warning("Failed to parse metadata.yaml: %s", e)

        logger.debug("Uploading %d JAR file(s)", len(scan.jar_files))
        for jar_path in sorted(scan.jar_files):
            self._upload_file(
                jar_path, os.path.basename(str(jar_path)), build, librarian
            )

        pom_path = scan.pom_files[0]
        self._upload_file(pom_path, "pom.xml", build, librarian)
        self._upload_metadata(scan, build, librarian)


class FallbackUploadHandler(ArtifactUploadHandler):
    """Fallback: upload the original tar.xz archive unchanged."""

    def can_handle(self, scan):
        return True

    def handle(self, scan, build, librarian, logger, craft_path, tmpdir):
        logger.debug("No crate or jar+pom found in archive, uploading tar.xz")
        self._upload_file(
            craft_path, os.path.basename(str(craft_path)), build, librarian
        )


_HANDLERS = [
    CrateUploadHandler(),
    MavenStagedRepoUploadHandler(),
    MavenJarUploadHandler(),
    FallbackUploadHandler(),
]


class CraftRecipeUpload:
    """A craft recipe upload."""

    def __init__(self, upload_path, logger):
        """Create a `CraftRecipeUpload`.

        :param upload_path: A directory containing files to upload.
        :param logger: The logger to be used.
        """
        self.upload_path = upload_path
        self.logger = logger

        self.librarian = getUtility(ILibraryFileAliasSet)

    def process(self, build):
        """Process this upload, loading it into the database."""
        self.logger.debug("Beginning processing.")

        upload_path = Path(self.upload_path)
        craft_paths, other_files = self._collect_files(upload_path)

        if not craft_paths:
            raise UploadError("Build did not produce any tar.xz archives.")

        for path in sorted(other_files):
            self._upload_file_to_build(path, path.name, build)

        for craft_path in sorted(craft_paths):
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    with tarfile.open(craft_path, "r:xz") as tar:
                        safe_extract_tar(tar, tmpdir)
                except SafeTarExtractError as e:
                    raise UploadError("Unsafe archive content: %s" % e) from e

                scan = ArchiveScanResult(tmpdir)
                self._log_scan(craft_path, scan)
                self._validate_scan(scan)

                handler = self._get_handler(scan)
                handler.handle(
                    scan,
                    build,
                    self.librarian,
                    self.logger,
                    craft_path,
                    tmpdir,
                )

        self.logger.debug("Updating %s" % build.title)
        build.updateStatus(BuildStatus.FULLYBUILT)
        self.logger.debug("Finished upload.")

    def _collect_files(self, upload_path):
        """Separate tar.xz archives from other files in subdirectories."""
        craft_paths = []
        other_files = []

        for path in upload_path.rglob("*"):
            if path.parent == upload_path:
                continue
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.endswith(".tar.xz"):
                craft_paths.append(path)
            else:
                other_files.append(path)

        return craft_paths, other_files

    def _upload_file_to_build(self, path, filename, build):
        """Upload a file to the librarian and attach it to the build."""
        _upload_to_librarian(path, filename, build, self.librarian)

    def _log_scan(self, craft_path, scan):
        """Log the archive scan results."""
        self.logger.debug(
            "Archive scan for %s: crates=%d jars=%d pom_xml=%d "
            "pom_artifacts=%d maven_metadata=%d metadata_at_root=%s",
            craft_path.name,
            len(scan.crate_files),
            len(scan.jar_files),
            len(scan.pom_files),
            len(scan.pom_artifact_files),
            len(scan.maven_metadata_files),
            scan.metadata_exists,
        )

    def _validate_scan(self, scan):
        """Validate that the archive doesn't contain conflicting artifacts."""
        if scan.has_crate and (scan.has_maven or scan.has_maven_staged_repo):
            raise UploadError(
                "Archive contains both Rust and Java artifacts. "
                "Only one artifact type is allowed per build."
            )

        if len(scan.crate_files) > 1:
            raise UploadError(
                "Archive contains multiple Rust crate files. "
                "Only one .crate file is allowed per build."
            )

        if len(scan.pom_files) > 1 and not scan.has_maven_staged_repo:
            raise UploadError(
                "Archive contains multiple pom.xml files. "
                "Only one pom.xml file is allowed per build."
            )

    def _get_handler(self, scan):
        """Return the first matching handler for the scan result."""
        for handler in _HANDLERS:
            if handler.can_handle(scan):
                return handler
