# Copyright 2009-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for deathrow class."""

import shutil
import tempfile
from pathlib import Path

from zope.component import getUtility

from lp.archivepublisher.artifactory import ArtifactoryPool
from lp.archivepublisher.deathrow import DeathRow
from lp.archivepublisher.diskpool import DiskPool
from lp.registry.interfaces.distribution import IDistributionSet
from lp.registry.interfaces.sourcepackage import SourcePackageType
from lp.services.log.logger import BufferLogger
from lp.soyuz.enums import (
    ArchivePublishingMethod,
    ArchivePurpose,
    ArchiveRepositoryFormat,
)
from lp.soyuz.interfaces.component import IComponentSet
from lp.soyuz.tests.test_publishing import SoyuzTestPublisher
from lp.testing import TestCaseWithFactory
from lp.testing.layers import LaunchpadZopelessLayer


class TestDeathRow(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def getTestPublisher(self, distroseries):
        """Return an `SoyuzTestPublisher`instance."""
        stp = SoyuzTestPublisher()
        stp.addFakeChroots(distroseries)
        stp.setUpDefaultDistroSeries(distroseries)
        return stp

    def getDeathRow(self, archive):
        """Return an `DeathRow` for the given archive.

        Created the temporary 'pool' and 'temp' directories and register
        a 'cleanup' to purge them after the test runs.
        """
        pool_path = tempfile.mkdtemp("-pool")
        temp_path = tempfile.mkdtemp("-pool-tmp")

        def clean_pool(pool_path, temp_path):
            shutil.rmtree(pool_path)
            shutil.rmtree(temp_path)

        self.addCleanup(clean_pool, pool_path, temp_path)

        logger = BufferLogger()
        diskpool = DiskPool(archive, pool_path, temp_path, logger)
        return DeathRow(archive, diskpool, logger)

    def getDiskPoolPath(self, pub, pub_file, diskpool):
        """Return the absolute path to a published file in the disk pool/."""
        return diskpool.pathFor(
            pub.component.name, pub.pool_name, pub.pool_version, pub_file
        )

    def assertIsFile(self, path: Path) -> None:
        """Assert the path exists and is a regular file."""
        self.assertTrue(path.exists(), "File %s does not exist" % path.name)
        self.assertFalse(
            path.is_symlink(), "File %s is a symbolic link" % path.name
        )

    def assertIsLink(self, path: Path) -> None:
        """Assert the path exists and is a symbolic link."""
        self.assertTrue(path.exists(), "File %s does not exist" % path.name)
        self.assertTrue(
            path.is_symlink(), "File %s is a not symbolic link" % path.name
        )

    def assertDoesNotExist(self, path: Path) -> None:
        """Assert the path does not exit."""
        self.assertFalse(path.exists(), "File %s exists" % path.name)

    def test_MissingSymLinkInPool(self):
        # When a publication is promoted from 'universe' to 'main' and
        # the symbolic links expected in 'universe' are not present,
        # a `MissingSymlinkInPool` error is generated and immediately
        # ignored by the `DeathRow` processor. Even in this adverse
        # circumstances the database record (removal candidate) is
        # updated to match the disk status.

        # Setup an `SoyuzTestPublisher` and a `DeathRow` instance.
        ubuntu = getUtility(IDistributionSet).getByName("ubuntu")
        hoary = ubuntu.getSeries("hoary")
        stp = self.getTestPublisher(hoary)
        deathrow = self.getDeathRow(hoary.main_archive)

        # Create a source publication with a since file (DSC) in
        # 'universe' and promote it to 'main'.
        source_universe = stp.getPubSource(component="universe")
        source_main = source_universe.changeOverride(
            new_component=getUtility(IComponentSet)["main"]
        )
        test_publications = (source_universe, source_main)

        # Commit for exposing the just-created librarian files.
        self.layer.commit()

        # Publish the testing publication on disk, the file for the
        # 'universe' component will be a symbolic link to the one
        # in 'main'.
        for pub in test_publications:
            pub.publish(deathrow.diskpool, deathrow.logger)
        [main_dsc_path] = [
            self.getDiskPoolPath(source_main, pub_file, deathrow.diskpool)
            for pub_file in source_main.files
        ]
        [universe_dsc_path] = [
            self.getDiskPoolPath(source_universe, pub_file, deathrow.diskpool)
            for pub_file in source_universe.files
        ]
        self.assertIsFile(main_dsc_path)
        self.assertIsLink(universe_dsc_path)

        # Remove the symbolic link to emulate MissingSymlinkInPool scenario.
        universe_dsc_path.unlink()

        # Remove the testing publications.
        for pub in test_publications:
            pub.requestObsolescence()

        # Commit for exposing the just-created removal candidates.
        self.layer.commit()

        # Due to the MissingSymlinkInPool scenario, it takes 2 iteration to
        # remove both references to the shared file in pool/.
        deathrow.reap()
        deathrow.reap()

        for pub in test_publications:
            self.assertTrue(
                pub.dateremoved is not None,
                "%s (%s) is not marked as removed."
                % (pub.displayname, pub.component.name),
            )

        self.assertDoesNotExist(main_dsc_path)
        self.assertDoesNotExist(universe_dsc_path)

    def test_skips_conda_source_packages(self):
        root_url = "https://foo.example.com/artifactory/repository"
        distroseries = self.factory.makeDistroSeries()
        archive = self.factory.makeArchive(
            distribution=distroseries.distribution,
            purpose=ArchivePurpose.PPA,
            publishing_method=ArchivePublishingMethod.ARTIFACTORY,
            repository_format=ArchiveRepositoryFormat.CONDA,
        )
        stp = self.getTestPublisher(distroseries)
        logger = BufferLogger()
        pool = ArtifactoryPool(archive, root_url, logger)
        deathrow = DeathRow(archive, pool, logger)
        pub_source = stp.getPubSource(
            filecontent=b"Hello world",
            archive=archive,
            format=SourcePackageType.CI_BUILD,
            user_defined_fields=[("bogus_field", "instead_of_subdir")],
        )
        pub_source.publish(pool, logger)
        pub_source.requestObsolescence()
        self.layer.commit()

        self.assertIsNone(pub_source.dateremoved)
        deathrow.reap()
        self.assertIsNotNone(pub_source.dateremoved)
