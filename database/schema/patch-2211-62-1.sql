-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

-- Needs to be run concurrently on production
CREATE INDEX oauthaccesstoken__refresh_token__idx
    ON OAuthAccessToken (refresh_token);

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 62, 1);
