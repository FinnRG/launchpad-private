-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

-- Extend valid_cve to accept any well-formed vulnerability identifier.
-- Legacy CVE sequences are stored without prefix: YYYY-NNNN (e.g. 2024-12345).
-- All other advisories (GHSA, etc.) are stored with their full prefix
-- (e.g. GHSA-2c6m-6gqh-6qg3, ABD-sdshjkdnas-dkdjnsak).
CREATE OR REPLACE FUNCTION public.valid_cve(text) RETURNS boolean
    LANGUAGE plpython3u IMMUTABLE STRICT
    AS $_$
    import re
    name = args[0]
    # Legacy CVE: bare YYYY-NNNN sequence without prefix
    if re.match(r"^(19|20)\d{2}-\d{4,}$", name):
        return True
    # Prefixed identifier: LETTERS-something (e.g. CVE-..., GHSA-..., ABD-...)
    if re.match(r"^[A-Z][A-Z0-9]*(-[A-Za-z0-9._]+)+$", name):
        return True
    return False
$_$;

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 59, 0);
