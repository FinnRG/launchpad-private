#  Copyright 2025 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import yaml
from packaging.version import Version

from lp.app.validators.name import valid_name
from lp.bugs.scripts.svthandler import SVTRecord

__all__ = [
    "SOSSRecord",
]

# From `soss-cve-tracker/git-hooks/check-cve-syntax`
VALID_CHANNEL_REGEX = re.compile(r"^(focal|jammy|noble):[^/]+/stable$")


@dataclass
class SOSSRecord(SVTRecord):

    class PriorityEnum(Enum):
        NEEDS_TRIAGE = "Needs-triage"
        NEGLIGIBLE = "Negligible"
        LOW = "Low"
        MEDIUM = "Medium"
        HIGH = "High"
        CRITICAL = "Critical"

    class PackageStatusEnum(Enum):
        IGNORED = "ignored"
        NEEDS_TRIAGE = "needs-triage"
        RELEASED = "released"
        NOT_AFFECTED = "not-affected"
        DEFERRED = "deferred"
        NEEDED = "needed"

    class PackageTypeEnum(Enum):
        CONDA = "conda"
        PYTHON = "python"
        UNPACKAGED = "unpackaged"
        MAVEN = "maven"
        RUST = "rust"

    @dataclass
    class Channel:
        value: str

        def __post_init__(self):
            if not VALID_CHANNEL_REGEX.match(self.value):
                raise ValueError(f"Invalid channel format: {self.value}")

        def __str__(self) -> str:
            return self.value

        def __lt__(self, other) -> bool:
            try:
                self_ver = self.value.split(":")[-1].split("/")[0]
                other_ver = other.value.split(":")[-1].split("/")[0]
                return Version(self_ver) < Version(other_ver)
            except Exception:
                return self.value < other.value

    @dataclass
    class CVSS:
        source: str
        vector: str
        base_score: float
        base_severity: float

        BASE_SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}

        def __post_init__(self):
            if not (0.0 <= self.base_score <= 10.0):
                raise ValueError(f"Invalid base score: {self.base_score}")
            if self.base_severity not in self.BASE_SEVERITIES:
                raise ValueError(
                    f"Invalid base severity: {self.base_severity}"
                )

        def to_dict(self) -> Dict[str, Any]:
            return {
                "source": self.source,
                "vector": self.vector,
                "baseScore": self.base_score,
                "baseSeverity": self.base_severity,
            }

    @dataclass
    class Package:
        name: str
        channel: "SOSSRecord.Channel"
        repositories: Tuple[str, ...]
        status: "SOSSRecord.PackageStatusEnum"
        note: str

        def __post_init__(self):
            # Normalize to tuple to preserve immutability from
            # ImmutablePgJSON (which returns tuples for JSON arrays).
            if not isinstance(self.repositories, tuple):
                self.repositories = tuple(self.repositories)

        def to_dict(self) -> Dict[str, Any]:
            # Convert tuple back to list for YAML serialization (tuples
            # would serialize as !!python/tuple instead of plain lists).
            return {
                "Name": self.name,
                "Channel": str(self.channel),
                "Repositories": list(self.repositories),
                "Status": self.status.value,
                "Note": self.note,
            }

        def __lt__(self, other: "SOSSRecord.Package") -> bool:
            if self.name == other.name:
                return self.channel < other.channel

            return self.name <= other.name

    references: List[str]
    notes: List[str]
    priority: PriorityEnum
    priority_reason: str
    assigned_to: str
    packages: Dict[PackageTypeEnum, List[Package]]
    candidate: Optional[str] = None
    description: Optional[str] = None
    cvss: Optional[List[CVSS]] = None
    public_date: Optional[datetime] = None
    extra_attrs: Optional[Dict[str, Any]] = None

    @classmethod
    def from_str(cls, string: str) -> "SOSSRecord":
        return cls.from_yaml(string)

    @classmethod
    def from_yaml(cls, yaml_str: str) -> "SOSSRecord":
        raw: Dict[str, Any] = yaml.safe_load(yaml_str)
        if not isinstance(raw, dict):
            raise TypeError(f"{type(raw)} is not a dict")
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SOSSRecord":
        # - Candidate, Description, CVSS and PublicDate are not mandatory.
        # - References, Notes can be empty using [], but they are always shown.
        # - Priority-Reason, Assigned-To can be empty using "", but they are
        #   always shown.
        # - Other fields are mandatory.
        packages = {}
        for enum_key, pkgs in raw.get("Packages", {}).items():
            package_type = SOSSRecord.PackageTypeEnum(enum_key.lower())
            # Use dict comprehension to deduplicate by (name, channel) key
            unique_packages = {
                (package["Name"], package["Channel"]): SOSSRecord.Package(
                    name=package["Name"],
                    channel=SOSSRecord.Channel(package["Channel"]),
                    repositories=package["Repositories"],
                    status=SOSSRecord.PackageStatusEnum(
                        package["Status"].lower()
                    ),
                    note=package["Note"],
                )
                for package in pkgs
                if valid_name(package["Name"])
            }
            packages[package_type] = list(unique_packages.values())

        cvss_list = [
            SOSSRecord.CVSS(
                cvss["source"],
                cvss["vector"],
                cvss["baseScore"],
                cvss["baseSeverity"],
            )
            for cvss in raw.get("CVSS", [])
        ]

        public_date_str = raw.get("PublicDate")
        public_date: datetime = (
            datetime.fromisoformat(public_date_str)
            if public_date_str
            else None
        )

        return cls(
            references=raw.get("References", []),
            notes=raw.get("Notes", []),
            priority=SOSSRecord.PriorityEnum(raw.get("Priority")),
            priority_reason=raw.get("Priority-Reason", ""),
            assigned_to=raw.get("Assigned-To", ""),
            packages=packages,
            candidate=raw.get("Candidate"),
            description=raw.get("Description"),
            cvss=cvss_list,
            public_date=public_date,
            extra_attrs=raw.get("Extra_attrs"),
        )

    def to_dict(self) -> Dict[str, Any]:
        serialized = {
            "References": self.references,
            "Notes": self.notes,
            "Priority": self.priority.value,
            "Priority-Reason": self.priority_reason,
            "Assigned-To": self.assigned_to,
            "Packages": {
                key.value: [p.to_dict() for p in pkg_list]
                for key, pkg_list in self.packages.items()
            },
        }

        # If empty, we don't print the header
        if self.candidate:
            serialized["Candidate"] = self.candidate
        if self.description:
            serialized["Description"] = self.description
        if self.cvss:
            serialized["CVSS"] = [cvss.to_dict() for cvss in self.cvss]
        if self.public_date:
            serialized["PublicDate"] = self.public_date.isoformat(
                sep="T", timespec="milliseconds"
            )
        if self.extra_attrs:
            serialized["Extra_attrs"] = self.extra_attrs

        return serialized

    def to_str(self) -> str:
        return yaml.dump(self.to_dict(), sort_keys=False)
