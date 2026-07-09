-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

CREATE INDEX sourcepackagepublishinghistory__archive__datepublished__id__idx
    ON sourcepackagepublishinghistory (archive, datepublished, id);

CREATE INDEX binarypackagepublishinghistory__archive__datepublished__id__idx
    ON binarypackagepublishinghistory (archive, datepublished, id);

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 58, 0);
