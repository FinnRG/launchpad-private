-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

-- The range [-100, 2000] covers APT’s pin-priority behaviors,
-- such as preventing a version from being installed (< 0),
-- preferring a version and allowing downgrades (>= 1000), and
-- leaves room for custom priority levels.

-- See APT_PREFERENCES(5) for additional information.

ALTER TABLE ArchiveDependency
    ADD COLUMN pin_priority integer DEFAULT 500 NOT NULL,
    ADD CONSTRAINT valid_pin_priority
        CHECK (pin_priority >= -100 AND pin_priority <= 2000)
;

COMMENT ON COLUMN ArchiveDependency.pin_priority
    IS 'APT pin priority applied to this dependency when generating'
        ' build sources.';

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 57, 0);
