# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import re

cveseq_regexp = r"(19|20)\d{2}\-\d{4,}"
CVEREF_PATTERN = re.compile(r"(CVE|CAN)-(%s)" % cveseq_regexp)
GHSAREF_PATTERN = re.compile(
    r"\bGHSA-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}\b"
)


def valid_cve(name):
    """Validate CVE identification.

    Until 2014 CVE sequence had to be smaller 4 digits (<= 9999):

    >>> valid_cve("1999-1234")
    True
    >>> valid_cve("2014-9999")
    True

    And leading zeros were required for sequence in [1-999]:

    >>> valid_cve("2014-999")
    False
    >>> valid_cve("2014-0999")
    True

    From 2014 and on, sequence can be any sequence of digits greater or
    equal to 4 digits:

    >>> valid_cve("2014-19999")
    True
    >>> valid_cve("2014-99999999")
    True
    """
    pat = r"^%s" % cveseq_regexp
    if re.match(pat, name):
        return True
    return False


def valid_vulnerability_id(name):
    """Validate a vulnerability identifier.

    Accepts any identifier with an uppercase prefix such as ``CVE-2024-1234``,
    ``GHSA-2c6m-6gqh-6qg3`` or ``ABD-sdshjkdnas-dkdjnsak``.

    >>> valid_vulnerability_id("CVE-2024-1234")
    True
    >>> valid_vulnerability_id("GHSA-2c6m-6gqh-6qg3")
    True
    >>> valid_vulnerability_id("ABD-sdshjkdnas-dkdjnsak")
    True
    >>> valid_vulnerability_id("2024-1234")
    False
    >>> valid_vulnerability_id("")
    False
    >>> valid_vulnerability_id("no spaces allowed")
    False
    """
    # Any uppercase-prefixed identifier: LETTERS-something
    # (e.g. CVE-..., GHSA-..., ABD-...)
    return bool(re.match(r"^[A-Z][A-Z0-9]*(-[A-Za-z0-9._]+)+$", name))


# Maps known vulnerability identifier prefixes to their canonical URL
# template. `seq` expands to the sequence as stored in the database.
#
# To add a new scheme, add an entry here AND extend VULNERABILITY_TEXT_PATTERN
# below with the scheme's pattern string.
VULNERABILITY_URL_PREFIXES = {
    "CVE-": "https://cve.org/CVERecord?id={seq}",
    "GHSA-": "https://github.com/advisories/{seq}",
}

# Single combined pattern used by CveSet.inText() to detect all known
# vulnerability IDs in free text.  match.group(0) always yields the full ID.
# To add a new scheme, add its pattern here AND an entry in
# VULNERABILITY_URL_PREFIXES above.
VULNERABILITY_TEXT_PATTERN = re.compile(
    "|".join(p.pattern for p in [CVEREF_PATTERN, GHSAREF_PATTERN])
)
