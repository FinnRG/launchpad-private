-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

-- Needs to be run concurrently on production
CREATE INDEX packageupload__archive__distroseries__date_created__idx
    ON PackageUpload (archive, distroseries, date_created);

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 61, 0);
