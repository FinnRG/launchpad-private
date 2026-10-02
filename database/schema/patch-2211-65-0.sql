-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

ALTER TABLE SourcePackageRelease
    ADD COLUMN source_package_git_build integer,
    ADD CONSTRAINT sourcepackagerelease_source_package_git_build_fkey
        FOREIGN KEY (source_package_git_build)
        REFERENCES SourcePackageGitBuild NOT VALID,
    DROP CONSTRAINT at_most_one_build,
    ADD CONSTRAINT at_most_one_build CHECK (
        null_count(ARRAY[
            sourcepackage_recipe_build, ci_build, source_package_git_build])
        >= 2) NOT VALID;

COMMENT ON COLUMN SourcePackageRelease.source_package_git_build IS
    'The source package Git build that produced this source package release, if any.';

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 65, 0);
