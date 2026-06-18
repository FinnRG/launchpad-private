#  Copyright 2026 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, NamedTuple

from lp.bugs.scripts.cppa import parser
from lp.bugs.scripts.cppa.parser import PackageStatus
from lp.bugs.scripts.svthandler import SVTRecord

__all__ = [
    "CPPARecord",
]

logger = logging.getLogger(__name__)


@dataclass
class CPPARecord(SVTRecord):
    """
    CPPARecord represents CVE record containing only series package status
    information.

    This is a minimal format compared to UCTRecord, containing just
    the CVE ID and package status information across different series.
    """

    PackageStatus = PackageStatus  # re-exported from parser

    class SeriesPackageStatus(NamedTuple):
        """Status of a package in a specific series."""

        series: str  # e.g., "project1/focal"
        status: "CPPARecord.PackageStatus"
        reason: str  # e.g., "1.2.3-1", "end of life, was needed"

    class Package(NamedTuple):
        """Package with its status across different series."""

        name: str
        statuses: List["CPPARecord.SeriesPackageStatus"]

    candidate: str  # CVE ID, e.g., "CVE-2013-1953"
    packages: List["CPPARecord.Package"]

    @classmethod
    def load(cls, cve_path: Path) -> "CPPARecord":
        """Create a `CPPARecord` from a CPPA CVE file at `cve_path`."""
        data = parser.load(cve_path)
        return cls(
            candidate=data["candidate"],
            packages=[
                cls.Package(
                    name=pkg["name"],
                    statuses=[
                        cls.SeriesPackageStatus(
                            series=s["series"],
                            status=cls.PackageStatus(s["status"]),
                            reason=s["reason"],
                        )
                        for s in pkg["statuses"]
                    ],
                )
                for pkg in data["packages"]
            ],
        )

    @classmethod
    def from_str(cls, content: str) -> "CPPARecord":
        """Create a `CPPARecord` from a string or bytes."""
        data = parser.from_str(content)
        return cls(
            candidate=data["candidate"],
            packages=[
                cls.Package(
                    name=pkg["name"],
                    statuses=[
                        cls.SeriesPackageStatus(
                            series=s["series"],
                            status=cls.PackageStatus(s["status"]),
                            reason=s["reason"],
                        )
                        for s in pkg["statuses"]
                    ],
                )
                for pkg in data["packages"]
            ],
        )

    def to_str(self) -> str:
        """Serialise the record to CPPA file format."""
        lines = []

        for package in self.packages:
            for status in package.statuses:
                line = f"{status.series}_{package.name}: {status.status.value}"
                if status.reason:
                    line += f" ({status.reason})"
                lines.append(line)

        if not lines:
            return ""
        return "\n".join(lines) + "\n"

    def save(self, output_dir: Path) -> Path:
        """Write the record to `output_dir/<candidate>` and return the path."""
        if not output_dir.is_dir():
            raise ValueError(
                f"{output_dir} does not exist or is not a directory"
            )

        output_path = output_dir / self.candidate

        content = self.to_str()
        output_path.write_text(content)

        return output_path
