#  Copyright 2026 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

import logging
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "PackageStatus",
    "load",
    "from_str",
]

logger = logging.getLogger(__name__)


class PackageStatus(Enum):
    """Status values for package series entries."""

    IGNORED = "ignored"
    NEEDS_TRIAGE = "needs-triage"
    DOES_NOT_EXIST = "DNE"
    RELEASED = "released"
    NOT_AFFECTED = "not-affected"
    DEFERRED = "deferred"
    NEEDED = "needed"
    PENDING = "pending"
    IN_PROGRESS = "in-progress"


def _parse_status_line(
    cve_name: str, line_num: int, line: str
) -> Optional[Tuple[str, str, str, str]]:
    """Parse one CPPA status line; return (series, package, status, reason) or
    None."""
    # Split into field and value on the first ':'
    if ":" not in line:
        logger.warning(f"{cve_name}:{line_num}: Could not parse line: {line}")
        return None
    field, _, value = line.partition(":")
    field = field.strip()
    value = value.strip()

    # Split field into series and package on the first '_'
    if "_" not in field:
        logger.warning(f"{cve_name}:{line_num}: Could not parse line: {line}")
        return None
    series, package = field.split("_", 1)

    # Series must be a PPA subproject entry (contains '/'), not a plain distro
    # series (e.g. 'focal')
    if "/" not in series:
        logger.warning(
            f"{cve_name}:{line_num}: Skipping plain distro series "
            f"'{series}' \u2014 CPPA files must only contain "
            f"PPA subproject entries (e.g. 'project1/focal')"
        )
        return None

    # Split value into state and details on the first space
    info = value.split(" ", 1)
    status_str = info[0]
    details = info[1].strip() if len(info) > 1 else ""

    # Strip surrounding parentheses from details
    if details.startswith("("):
        details = details[1:]
    if details.endswith(")"):
        details = details[:-1]

    # Old-style entries recorded only a version number with no explicit state,
    # implying "released".
    if details == "" and status_str and status_str[0] in "0123456789":
        details = status_str
        status_str = "released"

    # Validate status
    try:
        PackageStatus(status_str)
    except ValueError:
        logger.warning(
            f"{cve_name}:{line_num}: Unknown status '{status_str}' "
            f"for {package}, skipping"
        )
        return None

    return series, package, status_str, details


def load(cve_path: Path) -> Dict[str, Any]:
    """
    Parse a CPPA CVE file into a plain dict.

    The CVE ID is taken from the filename (must start with 'CVE-').
    """
    packages_by_name: Dict[str, List] = {}
    seen: set = set()  # tracks (package, series) pairs

    candidate = cve_path.name
    if not candidate.startswith("CVE-"):
        raise ValueError(
            f"{cve_path}: Filename must start with 'CVE-', got: {candidate}"
        )

    with open(cve_path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue

            result = _parse_status_line(candidate, line_num, line)
            if result is None:
                continue
            series, package, status_str, reason = result

            if (package, series) in seen:
                raise ValueError(
                    f"{candidate}:{line_num}: Duplicate entry for "
                    f"{series}_{package}"
                )
            seen.add((package, series))

            if package not in packages_by_name:
                packages_by_name[package] = []
            packages_by_name[package].append(
                {"series": series, "status": status_str, "reason": reason}
            )

    return {
        "candidate": candidate,
        "packages": [
            {"name": name, "statuses": statuses}
            for name, statuses in packages_by_name.items()
        ],
    }


def from_str(content, candidate: str) -> Dict[str, Any]:
    """Parse a CPPA record from a string or bytes, returning a plain dict.

    Unlike :func:`load`, which derives the CVE ID from the filename on disk,
    this function accepts the content directly.  A temporary file named after
    *candidate* is created so that :func:`load` can extract the CVE ID from
    the filename as usual.

    :param content: Raw file content as ``str`` or ``bytes``.
    :param candidate: CVE identifier used as the temporary filename
        (e.g. ``"CVE-2024-0001"``).  Must be a plain filename with no
        directory components.
    """
    # candidate is used as a filename; reject path separators to prevent
    # writing outside the temporary directory.
    if Path(candidate).name != candidate:
        raise ValueError(
            f"candidate must be a plain filename with no path components, "
            f"got: {candidate!r}"
        )
    with tempfile.TemporaryDirectory() as tmp_dir:
        named_path = Path(tmp_dir) / candidate
        with open(named_path, "wb") as f:
            if isinstance(content, str):
                f.write(content.encode("utf-8"))
            else:
                f.write(content)
        return load(named_path)
