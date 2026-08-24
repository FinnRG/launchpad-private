# Copyright 2011-2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for BugTaskSet."""

from zope.component import getUtility

from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.bugs.interfaces.bug import CreateBugParams, IBugSet
from lp.bugs.interfaces.bugtask import (
    BugTaskStatus,
    IBugTaskSet,
    IllegalTarget,
)
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.product import IProductSet
from lp.services.webapp.interfaces import ILaunchBag
from lp.soyuz.enums import PackagePublishingStatus
from lp.testing import TestCase, TestCaseWithFactory, login, person_logged_in
from lp.testing.layers import DatabaseFunctionalLayer


class TestCountsForProducts(TestCase):
    """Test BugTaskSet.getOpenBugTasksPerProduct"""

    layer = DatabaseFunctionalLayer

    def test_open_product_counts(self):
        # IBugTaskSet.getOpenBugTasksPerProduct() will return a dictionary
        # of product_id:count entries for bugs in an open status that
        # the user given as a parameter is allowed to see. If a product,
        # such as id=3 does not have any open bugs, it will not appear
        # in the result.
        launchbag = getUtility(ILaunchBag)
        login("foo.bar@canonical.com")
        foobar = launchbag.user

        productset = getUtility(IProductSet)
        products = [productset.get(id) for id in (3, 5, 20)]
        sample_person = getUtility(IPersonSet).getByEmail("test@canonical.com")
        bugtask_counts = getUtility(IBugTaskSet).getOpenBugTasksPerProduct(
            sample_person, products
        )
        res = sorted(bugtask_counts.items())
        self.assertEqual(
            "product_id=%d count=%d" % tuple(res[0]), "product_id=5 count=1"
        )
        self.assertEqual(
            "product_id=%d count=%d" % tuple(res[1]), "product_id=20 count=2"
        )

        # A Launchpad admin will get a higher count for the product with id=20
        # because they can see the private bug.
        bugtask_counts = getUtility(IBugTaskSet).getOpenBugTasksPerProduct(
            foobar, products
        )
        res = sorted(bugtask_counts.items())
        self.assertEqual(
            "product_id=%d count=%d" % tuple(res[0]), "product_id=5 count=1"
        )
        self.assertEqual(
            "product_id=%d count=%d" % tuple(res[1]), "product_id=20 count=3"
        )

        # Someone subscribed to the private bug on the product with id=20
        # will also have it added to the count.
        karl = getUtility(IPersonSet).getByName("karl")
        bugtask_counts = getUtility(IBugTaskSet).getOpenBugTasksPerProduct(
            karl, products
        )
        res = sorted(bugtask_counts.items())
        self.assertEqual(
            "product_id=%d count=%d" % tuple(res[0]), "product_id=5 count=1"
        )
        self.assertEqual(
            "product_id=%d count=%d" % tuple(res[1]), "product_id=20 count=3"
        )


class TestSortingBugTasks(TestCase):
    """Bug tasks need to sort in a very particular order."""

    layer = DatabaseFunctionalLayer

    def test_sortingorder(self):
        """We want product tasks, then ubuntu, then distro-related.

        In the distro-related tasks we want a distribution-task first, then
        distroseries-tasks for that same distribution. The distroseries tasks
        should be sorted by distroseries version.
        """
        login("foo.bar@canonical.com")
        bug_one = getUtility(IBugSet).get(1)
        tasks = bug_one.bugtasks
        task_names = [task.bugtargetdisplayname for task in tasks]
        self.assertEqual(
            task_names,
            [
                "Mozilla Firefox",
                "mozilla-firefox (Ubuntu)",
                "mozilla-firefox (Debian)",
            ],
        )


class TestFindSimilar(TestCaseWithFactory):
    """Tests for BugTaskSet.findSimilar."""

    layer = DatabaseFunctionalLayer

    def test_excludes_invalid_bugtasks(self):
        product = self.factory.makeProduct()
        user = self.factory.makePerson()

        valid_bug = self.factory.makeBug(
            target=product,
            title="Wireless adapter does not work",
        )
        invalid_bug = self.factory.makeBug(
            target=product,
            title="Wireless adapter does not work",
        )

        valid_task = valid_bug.bugtasks[0]
        invalid_task = invalid_bug.bugtasks[0]

        with person_logged_in(product.owner):
            invalid_task.transitionToStatus(
                BugTaskStatus.INVALID,
                product.owner,
            )

        results = list(
            getUtility(IBugTaskSet).findSimilar(
                user,
                "Wireless adapter does not work",
                product=product,
            )
        )

        self.assertIn(valid_task, results)
        self.assertNotIn(invalid_task, results)


class TestEolDistributionPackage(TestCaseWithFactory):
    """Tests for bugtask operations that accept EOL distribution packages."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        series = self.factory.makeDistroSeries(distribution=self.ubuntu)
        self.eol_pkg = self.factory.makeDistributionSourcePackage(
            distribution=self.ubuntu
        )
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=series,
            sourcepackagename=self.eol_pkg.sourcepackagename,
            archive=self.ubuntu.main_archive,
            status=PackagePublishingStatus.OBSOLETE,
        )
        self.owner = self.factory.makePerson()
        product = self.factory.makeProduct()
        with person_logged_in(self.owner):
            params = CreateBugParams(
                owner=self.owner,
                title="Test bug",
                comment="Test",
                target=product,
            )
            self.bug = getUtility(IBugSet).createBug(params)

    def test_createTask_accepts_eol_package_with_flag(self):
        # createTask() succeeds for an EOL-only package when
        # include_eol_dist_package=True is passed.
        task = getUtility(IBugTaskSet).createTask(
            self.bug,
            self.owner,
            self.eol_pkg,
            include_eol_dist_package=True,
        )
        self.assertEqual(self.eol_pkg, task.target)

    def test_createTask_rejects_eol_package_without_flag(self):
        # createTask() raises IllegalTarget for an EOL-only package
        # when include_eol_dist_package is not set.
        self.assertRaises(
            IllegalTarget,
            getUtility(IBugTaskSet).createTask,
            self.bug,
            self.owner,
            self.eol_pkg,
        )

    def test_transitionToTarget_accepts_eol_package_with_flag(self):
        # transitionToTarget() accepts an EOL-only package when
        # include_eol_dist_package=True is passed.
        ubuntu_task = getUtility(IBugTaskSet).createTask(
            self.bug, self.owner, self.ubuntu
        )
        with person_logged_in(self.owner):
            ubuntu_task.transitionToTarget(
                self.eol_pkg,
                self.owner,
                include_eol_dist_package=True,
            )
        self.assertEqual(self.eol_pkg, ubuntu_task.target)

    def test_transitionToTarget_rejects_eol_package_without_flag(self):
        # transitionToTarget() raises IllegalTarget for an EOL-only
        # package when include_eol_dist_package is not set.
        ubuntu_task = getUtility(IBugTaskSet).createTask(
            self.bug, self.owner, self.ubuntu
        )
        with person_logged_in(self.owner):
            self.assertRaises(
                IllegalTarget,
                ubuntu_task.transitionToTarget,
                self.eol_pkg,
                self.owner,
            )
