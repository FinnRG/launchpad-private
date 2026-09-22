-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

CREATE TABLE RevisionStatusWatch (
    id serial PRIMARY KEY,
    report integer NOT NULL REFERENCES revisionstatusreport ON DELETE CASCADE,
    remote_system text NOT NULL,
    remote_ref jsonb NOT NULL,
    next_check timestamp without time zone,
    lastchecked timestamp without time zone,
    failure_count integer DEFAULT 0 NOT NULL,
    date_created timestamp without time zone
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL
);

CREATE INDEX revisionstatuswatch__report__idx
    ON RevisionStatusWatch (report);
CREATE INDEX revisionstatuswatch__next_check__idx
    ON RevisionStatusWatch (next_check);

COMMENT ON TABLE RevisionStatusWatch IS
    'An external check whose result updates a revision status report.';
COMMENT ON COLUMN RevisionStatusWatch.report IS
    'The revision status report updated by this watch.';
COMMENT ON COLUMN RevisionStatusWatch.remote_system IS
    'The external system to poll, such as autopkgtest.';
COMMENT ON COLUMN RevisionStatusWatch.remote_ref IS
    'Handler data whose schema is specific to the remote system.';
COMMENT ON COLUMN RevisionStatusWatch.next_check IS
    'The time after which the watch should next be checked.';
COMMENT ON COLUMN RevisionStatusWatch.lastchecked IS
    'When the watch was last checked, or NULL if it has not been checked.';
COMMENT ON COLUMN RevisionStatusWatch.failure_count IS
    'The number of consecutive failures while checking this watch.';
COMMENT ON COLUMN RevisionStatusWatch.date_created IS
    'When this watch was created.';

CREATE TABLE MergeProposalValidation (
    id serial PRIMARY KEY,
    branch_merge_proposal integer NOT NULL REFERENCES branchmergeproposal,
    commit_sha1 character(40) NOT NULL,
    archive integer NOT NULL REFERENCES archive,
    requester integer NOT NULL REFERENCES person,
    parameters jsonb NOT NULL,
    status integer NOT NULL,
    date_created timestamp without time zone
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL,
    date_finished timestamp without time zone
);

CREATE INDEX mergeproposalvalidation__proposal__commit__idx
    ON MergeProposalValidation (branch_merge_proposal, commit_sha1);
CREATE INDEX mergeproposalvalidation__archive__idx
    ON MergeProposalValidation (archive);
CREATE INDEX mergeproposalvalidation__requester__idx
    ON MergeProposalValidation (requester);

COMMENT ON TABLE MergeProposalValidation IS
    'A validation workflow run for a merge proposal and Git commit.';
COMMENT ON COLUMN MergeProposalValidation.branch_merge_proposal IS
    'The merge proposal being validated.';
COMMENT ON COLUMN MergeProposalValidation.commit_sha1 IS
    'The Git commit ID being validated.';
COMMENT ON COLUMN MergeProposalValidation.archive IS
    'The PPA used for this validation run.';
COMMENT ON COLUMN MergeProposalValidation.requester IS
    'The person who requested this validation run.';
COMMENT ON COLUMN MergeProposalValidation.parameters IS
    'The parsed workflow parameters for this validation run.';
COMMENT ON COLUMN MergeProposalValidation.status IS
    'The overall result, as a RevisionStatusResult value: 0 = waiting, 1 = running, 2 = succeeded, 3 = failed, 4 = skipped, 5 = cancelled.';
COMMENT ON COLUMN MergeProposalValidation.date_created IS
    'When this validation run was created.';
COMMENT ON COLUMN MergeProposalValidation.date_finished IS
    'When this validation run finished.';

CREATE TABLE SourcePackageGitBuild (
    id serial PRIMARY KEY,
    build_farm_job integer NOT NULL REFERENCES buildfarmjob,
    git_repository integer NOT NULL REFERENCES gitrepository,
    commit_sha1 character(40) NOT NULL,
    archive integer NOT NULL REFERENCES archive,
    distroseries integer NOT NULL REFERENCES distroseries,
    pocket integer NOT NULL,
    requester integer NOT NULL REFERENCES person,
    version_suffix text NOT NULL,
    processor integer NOT NULL REFERENCES processor,
    virtualized boolean NOT NULL,
    date_created timestamp without time zone
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL,
    date_started timestamp without time zone,
    date_finished timestamp without time zone,
    date_first_dispatched timestamp without time zone,
    builder integer REFERENCES builder,
    status integer NOT NULL,
    log integer REFERENCES libraryfilealias,
    upload_log integer REFERENCES libraryfilealias,
    dependencies text,
    failure_count integer DEFAULT 0 NOT NULL
);

CREATE INDEX sourcepackagegitbuild__build_farm_job__idx
    ON SourcePackageGitBuild (build_farm_job);
CREATE INDEX sourcepackagegitbuild__repository__commit__idx
    ON SourcePackageGitBuild (git_repository, commit_sha1);
CREATE INDEX sourcepackagegitbuild__archive__idx
    ON SourcePackageGitBuild (archive);
CREATE INDEX sourcepackagegitbuild__distroseries__idx
    ON SourcePackageGitBuild (distroseries);
CREATE INDEX sourcepackagegitbuild__requester__idx
    ON SourcePackageGitBuild (requester);
CREATE INDEX sourcepackagegitbuild__processor__idx
    ON SourcePackageGitBuild (processor);
CREATE INDEX sourcepackagegitbuild__builder__idx
    ON SourcePackageGitBuild (builder);
CREATE INDEX sourcepackagegitbuild__log__idx
    ON SourcePackageGitBuild (log);
CREATE INDEX sourcepackagegitbuild__upload_log__idx
    ON SourcePackageGitBuild (upload_log);

COMMENT ON TABLE SourcePackageGitBuild IS
    'A build record for a source package built from a Git commit.';
COMMENT ON COLUMN SourcePackageGitBuild.build_farm_job IS
    'The build farm job with the base information.';
COMMENT ON COLUMN SourcePackageGitBuild.git_repository IS
    'The Git repository containing the source package.';
COMMENT ON COLUMN SourcePackageGitBuild.commit_sha1 IS
    'The Git commit ID to build.';
COMMENT ON COLUMN SourcePackageGitBuild.archive IS
    'The validation PPA to upload the source package to.';
COMMENT ON COLUMN SourcePackageGitBuild.distroseries IS
    'The distribution series to build for.';
COMMENT ON COLUMN SourcePackageGitBuild.pocket IS
    'The target pocket, as a PackagePublishingPocket value.';
COMMENT ON COLUMN SourcePackageGitBuild.requester IS
    'The person who requested the build.';
COMMENT ON COLUMN SourcePackageGitBuild.version_suffix IS
    'The suffix to append to the source package version.';
COMMENT ON COLUMN SourcePackageGitBuild.processor IS
    'The processor required by this build farm job.';
COMMENT ON COLUMN SourcePackageGitBuild.virtualized IS
    'The virtualization setting required by this build farm job.';
COMMENT ON COLUMN SourcePackageGitBuild.date_created IS
    'When the build record was created.';
COMMENT ON COLUMN SourcePackageGitBuild.date_started IS
    'When the build started being processed.';
COMMENT ON COLUMN SourcePackageGitBuild.date_finished IS
    'When the build finished being processed.';
COMMENT ON COLUMN SourcePackageGitBuild.date_first_dispatched IS
    'When the build was first dispatched, preserved across retries.';
COMMENT ON COLUMN SourcePackageGitBuild.builder IS
    'The builder that processed this build farm job.';
COMMENT ON COLUMN SourcePackageGitBuild.status IS
    'The current build status, as a BuildStatus value.';
COMMENT ON COLUMN SourcePackageGitBuild.log IS
    'The build log stored in the librarian.';
COMMENT ON COLUMN SourcePackageGitBuild.upload_log IS
    'The upload log stored in the librarian.';
COMMENT ON COLUMN SourcePackageGitBuild.dependencies IS
    'The dependencies installed for this build.';
COMMENT ON COLUMN SourcePackageGitBuild.failure_count IS
    'The number of consecutive failures on this build farm job.';

ALTER TABLE RevisionStatusReport
    ADD COLUMN group_name text,
    ADD COLUMN source_package_git_build integer;

ALTER TABLE RevisionStatusReport
    ADD CONSTRAINT revisionstatusreport__source_package_git_build__fk
    FOREIGN KEY (source_package_git_build) REFERENCES SourcePackageGitBuild
    NOT VALID;

COMMENT ON COLUMN RevisionStatusReport.group_name IS
    'An optional name used to group related status reports.';
COMMENT ON COLUMN RevisionStatusReport.source_package_git_build IS
    'The source package Git build that produced this report, if any.';

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 64, 0);
