# Copyright 2010-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test uploads of SourcePackageRecipeBuilds."""

import os

from storm.store import Store
from zope.component import getUtility

from lp.archiveuploader.tests.test_uploadprocessor import (
    TestUploadProcessorBase,
)
from lp.archiveuploader.uploadprocessor import UploadHandler, UploadStatusEnum
from lp.buildmaster.enums import BuildStatus
from lp.code.interfaces.sourcepackagerecipebuild import (
    ISourcePackageRecipeBuildSource,
)


class TestSourcePackageRecipeBuildUploads(TestUploadProcessorBase):
    def setUp(self):
        super().setUp()

        self.setupBreezy()

        self.switchToAdmin()
        self.recipe = self.factory.makeSourcePackageRecipe()
        self.build = getUtility(ISourcePackageRecipeBuildSource).new(
            distroseries=self.breezy,
            recipe=self.recipe,
            archive=self.factory.makeArchive(
                distribution=self.ubuntu, owner=self.recipe.owner
            ),
            requester=self.recipe.owner,
        )
        Store.of(self.build).flush()
        self.switchToUploader()
        self.options.context = "buildd"

        self.uploadprocessor = self.getUploadProcessor(
            self.layer.txn, builds=True
        )

    def testSetsBuildAndState(self):
        # Ensure that the upload processor correctly links the SPR to
        # the SPRB, and that the status is set properly.
        # This test depends on write access being granted to anybody
        # (it does not matter who) on SPRB.{status,upload_log}.
        self.assertIs(None, self.build.source_package_release)
        self.assertEqual(False, self.build.verifySuccessfulUpload())
        self.queueUpload("bar_1.0-1", "%d/ubuntu" % self.build.archive.id)
        fsroot = os.path.join(self.queue_folder, "incoming")
        handler = UploadHandler.forProcessor(
            self.uploadprocessor, fsroot, "bar_1.0-1", self.build
        )
        result = handler.processChangesFile(
            "%d/ubuntu/bar_1.0-1_source.changes" % self.build.archive.id
        )
        self.layer.txn.commit()

        self.assertEqual(
            UploadStatusEnum.ACCEPTED,
            result,
            "Source upload failed\nGot: %s" % self.log.getLogBuffer(),
        )

        self.assertEqual(BuildStatus.FULLYBUILT, self.build.status)
        self.assertEqual(True, self.build.verifySuccessfulUpload())


class TestSourcePackageGitBuildUploads(TestUploadProcessorBase):
    def setUp(self):
        super().setUp()
        self.setupBreezy()
        self.switchToAdmin()
        repository = self.factory.makeGitRepository()
        self.build = getUtility(ISourcePackageGitBuildSource).new(
            git_repository=repository,
            commit_sha1="a" * 40,
            archive=self.factory.makeArchive(
                distribution=self.ubuntu, owner=repository.owner
            ),
            distroseries=self.breezy,
            requester=repository.owner,
            version_suffix="~ppa1",
        )
        self.build.updateStatus(BuildStatus.UPLOADING)
        self.switchToUploader()
        self.options.context = "buildd"
        self.uploadprocessor = self.getUploadProcessor(
            self.layer.txn, builds=True
        )

    def test_source_upload(self):
        self.assertIsNone(self.build.source_package_release)
        self.queueUpload("bar_1.0-1", "%d/ubuntu" % self.build.archive.id)
        handler = UploadHandler.forProcessor(
            self.uploadprocessor,
            self.incoming_folder,
            "bar_1.0-1",
            self.build,
        )

        handler.process()

        self.assertEqual(BuildStatus.FULLYBUILT, self.build.status)
        spr = self.build.source_package_release
        self.assertEqual("bar", spr.name)
        self.assertIsNone(spr.source_package_recipe_build)
        self.assertEqual(self.build, spr.source_package_git_build)
        self.assertFalse(os.path.exists(handler.upload_path))
