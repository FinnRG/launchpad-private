-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

-- To be done CONCURRENTLY
CREATE INDEX archivefile__archive__container__date_created__index_files__idx
    ON ArchiveFile (archive, container, date_created)
    WHERE path ~ '/(Packages|Sources)(\.(gz|bz2|xz))?$';

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 67, 0);
