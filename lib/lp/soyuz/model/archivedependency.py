# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Database class for ArchiveDependency."""

__all__ = ["ArchiveDependency"]

from datetime import timezone

from storm.locals import DateTime, Int, Reference, Store
from zope.interface import implementer

from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.database.constants import UTC_NOW
from lp.services.database.enumcol import DBEnum
from lp.services.database.stormbase import StormBase
from lp.snappy.interfaces.snapbase import ISnapBase
from lp.soyuz.adapters.archivedependencies import get_components_for_context
from lp.soyuz.interfaces.archive import IArchive
from lp.soyuz.interfaces.archivedependency import (
    IArchiveDependency,
    validate_pin_priority,
)


def storm_validate_pin_priority(archive_dependency, attr, value):
    """Storm validator for pin_priority."""
    return validate_pin_priority(archive_dependency.dependency, value)


@implementer(IArchiveDependency)
class ArchiveDependency(StormBase):
    """See `IArchiveDependency`."""

    __storm_table__ = "ArchiveDependency"
    __storm_order__ = "id"

    id = Int(primary=True)

    date_created = DateTime(
        name="date_created",
        tzinfo=timezone.utc,
        allow_none=False,
        default=UTC_NOW,
    )

    archive_id = Int(name="archive", allow_none=True)
    archive = Reference(archive_id, "Archive.id")

    snap_base_id = Int(name="snap_base", allow_none=True)
    snap_base = Reference(snap_base_id, "SnapBase.id")

    dependency_id = Int(name="dependency", allow_none=False)
    dependency = Reference(dependency_id, "Archive.id")

    pocket = DBEnum(
        name="pocket", allow_none=False, enum=PackagePublishingPocket
    )

    component_id = Int(name="component", allow_none=True)
    component = Reference(component_id, "Component.id")

    pin_priority = Int(
        name="pin_priority",
        allow_none=False,
        validator=storm_validate_pin_priority,
    )

    def __init__(
        self, parent, dependency, pocket, component=None, pin_priority=500
    ):
        super().__init__()
        self.parent = parent
        self.dependency = dependency
        self.pocket = pocket
        self.component = component
        self.pin_priority = pin_priority

    @property
    def parent(self):
        if self.archive is not None:
            return self.archive
        else:
            return self.snap_base

    @parent.setter
    def parent(self, value):
        if IArchive.providedBy(value):
            self.archive = value
            self.snap_base = None
        elif ISnapBase.providedBy(value):
            self.archive = None
            self.snap_base = value
        else:
            raise AssertionError(
                "Unknown archive dependency parent %s" % value
            )

    @property
    def component_name(self):
        """See `IArchiveDependency`"""
        if self.component:
            return self.component.name
        else:
            return None

    @property
    def title(self):
        """See `IArchiveDependency`."""
        if self.dependency.is_ppa:
            return self.dependency.displayname

        pocket_title = "%s - %s" % (
            self.dependency.displayname,
            self.pocket.name,
        )

        if self.component is None:
            return pocket_title

        if self.archive is not None:
            # XXX cjwatson 2016-03-31: This may be inaccurate, but we can't
            # do much better since this ArchiveDependency applies to
            # multiple series which may each resolve component dependencies
            # in different ways.
            distroseries = self.archive.distribution.currentseries
        else:
            distroseries = self.snap_base.distro_series

        component_part = ", ".join(
            get_components_for_context(
                self.component, distroseries, self.pocket
            )
        )

        return "%s (%s)" % (pocket_title, component_part)

    def destroySelf(self):
        Store.of(self).remove(self)
