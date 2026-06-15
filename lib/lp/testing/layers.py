# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Layers used by Launchpad tests.

Layers are the mechanism used by the Zope3 test runner to efficiently
provide environments for tests and are documented in the lib/zope/testing.

Note that every Layer should define all of setUp, tearDown, testSetUp
and testTearDown. If you don't do this, a base class' method will be called
instead probably breaking something.

Preferred style is to not use the 'cls' argument to Layer class methods,
as this is unambiguous.

TODO: Make the Zope3 test runner handle multiple layers per test instead
of one, forcing us to attempt to make some sort of layer tree.
-- StuartBishop 20060619
"""

__all__ = [
    "AppServerLayer",
    "BaseLayer",
    "BingLaunchpadFunctionalLayer",
    "BingServiceLayer",
    "DatabaseFunctionalLayer",
    "DatabaseLayer",
    "FunctionalLayer",
    "LaunchpadFunctionalLayer",
    "LaunchpadLayer",
    "LaunchpadScriptLayer",
    "LaunchpadTestSetup",
    "LaunchpadZopelessLayer",
    "LayerInvariantError",
    "LayerIsolationError",
    "LibrarianLayer",
    "PageTestLayer",
    "RabbitMQLayer",
    "TwistedLayer",
    "YUITestLayer",
    "YUIAppServerLayer",
    "ZopelessAppServerLayer",
    "ZopelessDatabaseLayer",
    "ZopelessLayer",
    "reconnect_stores",
]

import base64
import datetime
import gc
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from cProfile import Profile
from functools import partial
from textwrap import dedent
from unittest import TestCase, TestResult
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import psycopg2
import transaction
import wsgi_intercept
import zope.testbrowser.wsgi
from fixtures import Fixture, MonkeyPatch
from requests import Session
from requests.adapters import HTTPAdapter
from storm.uri import URI
from talisker.context import Context
from webob.request import environ_from_url as orig_environ_from_url
from wsgi_intercept import httplib2_intercept
from zope.component import getUtility, globalregistry, provideUtility
from zope.component.testlayer import ZCMLFileLayer
from zope.event import notify
from zope.interface.interfaces import ComponentLookupError
from zope.processlifetime import DatabaseOpened
from zope.security.management import endInteraction, getSecurityPolicy
from zope.testbrowser.browser import HostNotAllowed
from zope.testbrowser.wsgi import AuthorizationMiddleware

import lp.services.mail.stub
import lp.services.webapp.session
import zcml
from lp.services import pidfile
from lp.services.config import LaunchpadConfig, config, dbconfig
from lp.services.config.fixture import ConfigFixture, ConfigUseFixture
from lp.services.database.interfaces import IStore
from lp.services.database.sqlbase import disconnect_stores, session_store
from lp.services.encoding import wsgi_native_string
from lp.services.job.tests import celery_worker
from lp.services.librarian.model import LibraryFileAlias
from lp.services.librarianserver.testing.server import LibrarianServerFixture
from lp.services.mail.mailbox import IMailBox, TestMailBox
from lp.services.mail.sendmail import set_immediate_mail_delivery
from lp.services.memcache.client import memcache_client_factory
from lp.services.osutils import kill_by_pidfile
from lp.services.rabbit.server import RabbitServer
from lp.services.scripts import execute_zcml_for_scripts
from lp.services.sitesearch.tests.bingserviceharness import (
    BingServiceTestSetup,
)
from lp.services.testing.profiled import profiled
from lp.services.timeout import (
    get_default_timeout_function,
    set_default_timeout_function,
)
from lp.services.webapp.authorization import LaunchpadPermissiveSecurityPolicy
from lp.services.webapp.interfaces import IOpenLaunchBag
from lp.services.webapp.servers import (
    register_launchpad_request_publication_factories,
)
from lp.services.webapp.wsgi import WSGIPublisherApplication
from lp.testing import ANONYMOUS, login, logout, reset_logging
from lp.testing.html5browser import Browser
from lp.testing.pgsql import PgTestSetup

WAIT_INTERVAL = datetime.timedelta(seconds=180)


class LayerError(Exception):
    pass


class LayerInvariantError(LayerError):
    """Layer self checks have detected a fault. Invariant has been violated.

    This indicates the Layer infrastructure has messed up. The test run
    should be aborted.
    """

    pass


class LayerIsolationError(LayerError):
    """Test isolation has been broken, probably by the test we just ran.

    This generally indicates a test has screwed up by not resetting
    something correctly to the default state.

    The test suite should abort if it cannot clean up the mess as further
    test failures may well be spurious.
    """


def is_ca_available():
    """Returns true if the component architecture has been loaded"""
    try:
        getUtility(IOpenLaunchBag)
    except ComponentLookupError:
        return False
    else:
        return True


def reconnect_stores(reset=False):
    """Reconnect Storm stores, resetting the dbconfig to its defaults.

    After reconnecting, the database revision will be checked to make
    sure the right data is available.
    """
    disconnect_stores()
    if reset:
        dbconfig.reset()

    main_store = IStore(LibraryFileAlias)
    assert main_store is not None, "Failed to reconnect"

    # Confirm that Storm is talking to the database again (it connects
    # as soon as any query is executed).
    r = main_store.execute("SELECT count(*) FROM LaunchpadDatabaseRevision")
    assert r.get_one()[0] > 0, "Storm is not talking to the database"
    assert session_store() is not None, "Failed to reconnect"


class BaseLayer:
    """Base layer.

    All our layers should subclass Base, as this is where we will put
    test isolation checks to ensure that tests to not leave global
    resources in a mess.

    XXX: StuartBishop 2006-07-12: Unit tests (tests with no layer) will not
    get these checks. The Z3 test runner should be updated so that a layer
    can be specified to use for unit tests.
    """

    # Set to True when we are running tests in this layer.
    isSetUp = False

    # The name of this test - this is the same output that the testrunner
    # displays. It is probably unique, but not guaranteed to be so.
    test_name = None

    # A flag to disable a check for threads still running after test
    # completion.  This is hopefully a temporary measure; see the comment
    # in tearTestDown.
    disable_thread_check = False

    # A flag to make services like Librarian and Memcached to persist
    # between test runs. This flag is set in setUp() by looking at the
    # LP_PERSISTENT_TEST_SERVICES environment variable.
    persist_test_services = False

    # Things we need to cleanup.
    fixture = None

    # ConfigFixtures for the configs generated for this layer. Set to None
    # if the layer is not setUp, or if persistent tests services are in use.
    config_fixture = None
    appserver_config_fixture = None

    # The config names that are generated for this layer. Set to None when
    # the layer is not setUp.
    config_name = None
    appserver_config_name = None

    @classmethod
    def make_config(cls, config_name, clone_from, attr_name):
        """Create a temporary config and link it into the layer cleanup."""
        cfg_fixture = ConfigFixture(config_name, clone_from)
        cls.fixture.addCleanup(cfg_fixture.cleanUp)
        cfg_fixture.setUp()
        cls.fixture.addCleanup(setattr, cls, attr_name, None)
        setattr(cls, attr_name, cfg_fixture)

    @classmethod
    @profiled
    def setUp(cls):
        # Set the default appserver config instance name.
        # May be changed as required eg when running parallel tests.
        cls.appserver_config_name = "testrunner-appserver"
        BaseLayer.isSetUp = True
        cls.fixture = Fixture()
        cls.fixture.setUp()
        cls.fixture.addCleanup(setattr, cls, "fixture", None)
        BaseLayer.persist_test_services = (
            os.environ.get("LP_PERSISTENT_TEST_SERVICES") is not None
        )
        # We can only do unique test allocation and parallelisation if
        # LP_PERSISTENT_TEST_SERVICES is off.
        if not BaseLayer.persist_test_services:
            # This should be at most 38 characters long, otherwise
            # 'launchpad_ftest_template_{test_instance}' won't fit within
            # PostgreSQL's 63-character limit for identifiers.  Linux
            # currently allows up to 2^22 PIDs, so PIDs may be up to seven
            # digits long.
            test_instance = "%d_%s" % (
                os.getpid(),
                base64.b16encode(os.urandom(12)).decode().lower(),
            )
            os.environ["LP_TEST_INSTANCE"] = test_instance
            cls.fixture.addCleanup(os.environ.pop, "LP_TEST_INSTANCE", "")
            # Kill any Memcached or Librarian left running from a previous
            # test run, or from the parent test process if the current
            # layer is being run in a subprocess. No need to be polite
            # about killing memcached - just do it quickly.
            kill_by_pidfile(MemcachedLayer.getPidFile(), num_polls=0)
            config_name = "testrunner_%s" % test_instance
            cls.make_config(config_name, "testrunner", "config_fixture")
            app_config_name = "testrunner-appserver_%s" % test_instance
            cls.make_config(
                app_config_name,
                "testrunner-appserver",
                "appserver_config_fixture",
            )
            cls.appserver_config_name = app_config_name
        else:
            config_name = "testrunner"
            app_config_name = "testrunner-appserver"
        cls.config_name = config_name
        cls.fixture.addCleanup(setattr, cls, "config_name", None)
        cls.appserver_config_name = app_config_name
        cls.fixture.addCleanup(setattr, cls, "appserver_config_name", None)
        use_fixture = ConfigUseFixture(config_name)
        cls.fixture.addCleanup(use_fixture.cleanUp)
        use_fixture.setUp()
        # Kill any database left lying around from a previous test run.
        db_fixture = LaunchpadTestSetup()
        try:
            db_fixture.connect().close()
        except psycopg2.Error:
            # We assume this means 'no test database exists.'
            pass
        else:
            db_fixture.dropDb()

    @classmethod
    @profiled
    def tearDown(cls):
        cls.fixture.cleanUp()
        BaseLayer.isSetUp = False

    @classmethod
    @profiled
    def testSetUp(cls):
        # Store currently running threads so we can detect if a test
        # leaves new threads running.
        BaseLayer._threads = threading.enumerate()
        BaseLayer.check()
        BaseLayer.original_working_directory = os.getcwd()

        # Tests and test infrastructure sometimes needs to know the test
        # name.  The testrunner doesn't provide this, so we have to do
        # some snooping.
        import inspect

        frame = inspect.currentframe()
        try:
            while frame.f_code.co_name != "startTest":
                frame = frame.f_back
            BaseLayer.test_name = str(frame.f_locals["test"])
        finally:
            del frame  # As per no-leak stack inspection in Python reference.

    @classmethod
    @profiled
    def testTearDown(cls):
        # Get our current working directory, handling the case where it no
        # longer exists (!).
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = None

        # Handle a changed working directory. If the test succeeded,
        # add an error. Then restore the working directory so the test
        # run can continue.
        if cwd != BaseLayer.original_working_directory:
            BaseLayer.flagTestIsolationFailure(
                "Test failed to restore working directory."
            )
            os.chdir(BaseLayer.original_working_directory)

        BaseLayer.original_working_directory = None
        reset_logging()
        del lp.services.mail.stub.test_emails[:]
        BaseLayer.test_name = None
        BaseLayer.check()

        def new_live_threads():
            return [
                thread
                for thread in threading.enumerate()
                if thread not in BaseLayer._threads and thread.is_alive()
            ]

        if BaseLayer.disable_thread_check:
            new_threads = None
        else:
            for _ in range(0, 100):
                # Check for tests that leave live threads around early.
                # A live thread may be the cause of other failures, such as
                # uncollectable garbage.
                new_threads = new_live_threads()
                has_live_threads = False
                for new_thread in new_threads:
                    new_thread.join(0.1)
                    if new_thread.is_alive():
                        has_live_threads = True
                if has_live_threads:
                    # Trigger full garbage collection that might be
                    # blocking threads from exiting.
                    gc.collect()
                else:
                    break
            new_threads = new_live_threads()

        if new_threads:
            # BaseLayer.disable_thread_check is a mechanism to stop
            # tests that leave threads behind from failing. Its use
            # should only ever be temporary.
            if BaseLayer.disable_thread_check:
                print(
                    ("ERROR DISABLED: " "Test left new live threads: %s")
                    % repr(new_threads)
                )
            else:
                BaseLayer.flagTestIsolationFailure(
                    "Test left new live threads: %s" % repr(new_threads)
                )

        BaseLayer.disable_thread_check = False
        del BaseLayer._threads

        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            BaseLayer.flagTestIsolationFailure("Test left SIGCHLD handler.")

        # Objects with __del__ methods cannot participate in reference cycles.
        # Fail tests with memory leaks now rather than when Launchpad crashes
        # due to a leak because someone ignored the warnings.
        if gc.garbage:
            del gc.garbage[:]
            gc.collect()  # Expensive, so only do if there might be garbage.
            if gc.garbage:
                BaseLayer.flagTestIsolationFailure(
                    "Test left uncollectable garbage\n"
                    "%s (referenced from %s; referencing %s)"
                    % (
                        gc.garbage,
                        gc.get_referrers(*gc.garbage),
                        gc.get_referents(*gc.garbage),
                    )
                )

    @classmethod
    @profiled
    def check(cls):
        """Check that the environment is working as expected.

        We check here so we can detect tests that, for example,
        initialize the Zopeless or Functional environments and
        are using the incorrect layer.
        """
        if FunctionalLayer.isSetUp and ZopelessLayer.isSetUp:
            raise LayerInvariantError(
                "Both Zopefull and Zopeless CA environments setup"
            )

        # Detect a test that causes the component architecture to be loaded.
        # This breaks test isolation, as it cannot be torn down.
        if (
            is_ca_available()
            and not FunctionalLayer.isSetUp
            and not ZopelessLayer.isSetUp
        ):
            raise LayerIsolationError(
                "Component architecture should not be loaded by tests. "
                "This should only be loaded by the Layer."
            )

        # Detect a test that forgot to reset the default socket timeout.
        # This safety belt is cheap and protects us from very nasty
        # intermittent test failures: see bug #140068 for an example.
        if socket.getdefaulttimeout() is not None:
            raise LayerIsolationError(
                "Test didn't reset the socket default timeout."
            )

    @classmethod
    def flagTestIsolationFailure(cls, message):
        """Handle a breakdown in test isolation.

        If the test that broke isolation thinks it succeeded,
        add an error. If the test failed, don't add a notification
        as the isolation breakdown is probably just fallout.

        The layer that detected the isolation failure still needs to
        repair the damage, or in the worst case abort the test run.
        """
        test_result = BaseLayer.getCurrentTestResult()
        if test_result.wasSuccessful():
            test_case = BaseLayer.getCurrentTestCase()
            try:
                raise LayerIsolationError(message)
            except LayerIsolationError:
                test_result.addError(test_case, sys.exc_info())

    @classmethod
    def getCurrentTestResult(cls):
        """Return the TestResult currently in play."""
        import inspect

        frame = inspect.currentframe()
        try:
            while True:
                f_self = frame.f_locals.get("self", None)
                if isinstance(f_self, TestResult):
                    return frame.f_locals["self"]
                frame = frame.f_back
        finally:
            del frame  # As per no-leak stack inspection in Python reference.

    @classmethod
    def getCurrentTestCase(cls):
        """Return the test currently in play."""
        import inspect

        frame = inspect.currentframe()
        try:
            while True:
                f_self = frame.f_locals.get("self", None)
                if isinstance(f_self, TestCase):
                    return f_self
                f_test = frame.f_locals.get("test", None)
                if isinstance(f_test, TestCase):
                    return f_test
                frame = frame.f_back
            return frame.f_locals["test"]
        finally:
            del frame  # As per no-leak stack inspection in Python reference.

    @classmethod
    def appserver_config(cls):
        """Return a config suitable for AppServer tests."""
        return LaunchpadConfig(cls.appserver_config_name)

    @classmethod
    def appserver_root_url(cls, facet="mainsite", ensureSlash=False):
        """Return the correct app server root url for the given facet."""
        return cls.appserver_config().appserver_root_url(facet, ensureSlash)


class MemcachedLayer(BaseLayer):
    """Provides tests access to a memcached.

    Most tests needing memcache access will actually need to use
    ZopelessLayer, FunctionalLayer or sublayer as they will be accessing
    memcached using a utility.
    """

    # A memcache.Client instance.
    client = None

    # A subprocess.Popen instance if this process spawned the test
    # memcached.
    _memcached_process = None

    _is_setup = False

    @classmethod
    @profiled
    def setUp(cls):
        cls._is_setup = True
        # Create a client
        MemcachedLayer.client = memcache_client_factory()
        if BaseLayer.persist_test_services and os.path.exists(
            MemcachedLayer.getPidFile()
        ):
            return

        # First, check to see if there is a memcached already running.
        # This happens when new layers are run as a subprocess.
        test_key = "MemcachedLayer__live_test"
        try:
            if MemcachedLayer.client.set(test_key, "live"):
                return
        except OSError:
            pass

        # memcached >= 1.4.29 requires the item size to be at most a quarter
        # of the memory size; 1.5.4 lifts this restriction to at most half
        # the memory size, but we take the more conservative value.  We cap
        # the item size at a megabyte.  Note that the argument to -m is in
        # megabytes.
        item_size = min(
            config.memcached.memory_size * 1024 * 1024 / 4, 1024 * 1024
        )
        cmd = [
            "memcached",
            "-m",
            str(config.memcached.memory_size),
            "-I",
            str(item_size),
            "-l",
            str(config.memcached.address),
            "-p",
            str(config.memcached.port),
            "-U",
            str(config.memcached.port),
        ]
        if config.memcached.verbose:
            cmd.append("-vv")
            stdout = sys.stdout
            stderr = sys.stderr
        else:
            stdout = tempfile.NamedTemporaryFile()
            stderr = tempfile.NamedTemporaryFile()
        MemcachedLayer._memcached_process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr
        )
        MemcachedLayer._memcached_process.stdin.close()

        # Wait for the memcached to become operational.
        while True:
            try:
                if MemcachedLayer.client.set(test_key, "live"):
                    break
            except OSError:
                if MemcachedLayer._memcached_process.returncode is not None:
                    raise LayerInvariantError(
                        "memcached never started or has died.",
                        MemcachedLayer._memcached_process.stdout.read(),
                    )
                time.sleep(0.1)

        # Store the pidfile for other processes to kill.
        pid_file = MemcachedLayer.getPidFile()
        with open(pid_file, "w") as f:
            f.write(str(MemcachedLayer._memcached_process.pid))

    @classmethod
    @profiled
    def tearDown(cls):
        if not cls._is_setup:
            return
        cls._is_setup = False
        MemcachedLayer.client.disconnect_all()
        MemcachedLayer.client = None
        if not BaseLayer.persist_test_services:
            # Kill our memcached, and there is no reason to be nice about it.
            kill_by_pidfile(MemcachedLayer.getPidFile())
            # Also try killing the subprocess just in case it's different
            # from what's recorded in the pid file, to avoid deadlocking on
            # wait().
            try:
                MemcachedLayer._memcached_process.kill()
            except OSError:
                pass
            # Clean up the resulting zombie.
            MemcachedLayer._memcached_process.communicate()
            MemcachedLayer._memcached_process = None

    @classmethod
    @profiled
    def testSetUp(cls):
        MemcachedLayer.client.flush_all()

    @classmethod
    @profiled
    def testTearDown(cls):
        pass

    @classmethod
    def getPidFile(cls):
        return os.path.join(config.root, ".memcache.pid")

    @classmethod
    def purge(cls):
        "Purge everything from our memcached."
        MemcachedLayer.client.flush_all()  # Only do this in tests!


class RabbitMQLayer(BaseLayer):
    """Provides tests access to a rabbitMQ instance."""

    # The default timeout is 15 seconds, but increase this a bit to allow
    # some more leeway for slow test environments.
    rabbit = RabbitServer(ctltimeout=150)

    _is_setup = False

    @classmethod
    @profiled
    def setUp(cls):
        cls.rabbit.setUp()
        cls.config_fixture.add_section(cls.rabbit.config.service_config)
        cls.appserver_config_fixture.add_section(
            cls.rabbit.config.service_config
        )
        # The celeryconfig module reads broker_url from LP config at import
        # time. If it was imported before RabbitMQ was started (e.g. during
        # test collection, when broker_urls is 'none'), the stale value is
        # stuck in the module globals. Celery reads broker_url dynamically
        # via DictAttribute, so refreshing the module globals here is enough
        # to make celery_app.conf.broker_url reflect the real broker URL.
        import sys as _sys

        if "lp.services.job.celeryconfig" in _sys.modules:
            import lp.services.job.celeryconfig as _celeryconfig

            _celeryconfig.__dict__.update(_celeryconfig.configure(_sys.argv))
        cls._is_setup = True

    @classmethod
    @profiled
    def tearDown(cls):
        if not cls._is_setup:
            return
        cls.appserver_config_fixture.remove_section(
            cls.rabbit.config.service_config
        )
        cls.config_fixture.remove_section(cls.rabbit.config.service_config)
        cls.rabbit.cleanUp()
        cls._is_setup = False

    @classmethod
    @profiled
    def testSetUp(cls):
        pass

    @classmethod
    @profiled
    def testTearDown(cls):
        pass


# We store a reference to the DB-API connect method here when we
# put a proxy in its place.
_org_connect = None


class DatabaseLayer(BaseLayer):
    """Provides tests access to the Launchpad sample database."""

    _is_setup = False
    _db_fixture = None
    # For parallel testing, we allocate a temporary template to prevent worker
    # contention.
    _db_template_fixture = None

    @classmethod
    @profiled
    def setUp(cls):
        cls._is_setup = True
        # Allocate a template for this test instance
        if os.environ.get("LP_TEST_INSTANCE"):
            template_name = "_".join(
                [
                    LaunchpadTestSetup.template,
                    os.environ.get("LP_TEST_INSTANCE"),
                ]
            )
            cls._db_template_fixture = LaunchpadTestSetup(dbname=template_name)
            cls._db_template_fixture.setUp()
        else:
            template_name = LaunchpadTestSetup.template
        cls._db_fixture = LaunchpadTestSetup(template=template_name)
        cls.force_dirty_database()
        # Nuke any existing DB (for persistent-test-services) [though they
        # prevent this !?]
        cls._db_fixture.tearDown()
        # Force a db creation for unique db names - needed at layer init
        # because appserver using layers run things at layer setup, not
        # test setup.
        cls._db_fixture.setUp()
        # And take it 'down' again to be in the right state for testSetUp
        # - note that this conflicts in principle with layers whose setUp
        # needs the db working, but this is a conceptually cleaner starting
        # point for addressing that mismatch.
        cls._db_fixture.tearDown()
        # Bring up the db, so that it is available for other layers.
        cls._db_fixture.setUp()

    @classmethod
    @profiled
    def tearDown(cls):
        if not cls._is_setup:
            return
        cls._is_setup = False
        # Don't leave the DB lying around or it might break tests
        # that depend on it not being there on startup, such as found
        # in test_layers.py
        cls.force_dirty_database()
        cls._db_fixture.tearDown()
        cls._db_fixture = None
        if os.environ.get("LP_TEST_INSTANCE"):
            cls._db_template_fixture.tearDown()
            cls._db_template_fixture = None

    @classmethod
    @profiled
    def testSetUp(cls):
        pass

    @classmethod
    @profiled
    def testTearDown(cls):
        cls._db_fixture.tearDown()

        # Fail tests that forget to uninstall their database policies.
        from lp.services.webapp.adapter import StoreSelector

        while StoreSelector.get_current() is not None:
            BaseLayer.flagTestIsolationFailure(
                "Database policy %s still installed"
                % repr(StoreSelector.pop())
            )
        # Reset/bring up the db - makes it available for either the next test,
        # or a subordinate layer which builds on the db. This wastes one setup
        # per db layer teardown per run, but that's tolerable.
        cls._db_fixture.setUp()

    @classmethod
    @profiled
    def force_dirty_database(cls):
        cls._db_fixture.force_dirty_database()

    @classmethod
    @profiled
    def connect(cls):
        return cls._db_fixture.connect()


class LibrarianLayer(DatabaseLayer):
    """Provides tests access to a Librarian instance.

    Calls to the Librarian will fail unless there is also a Launchpad
    database available.
    """

    librarian_fixture = None

    @classmethod
    @profiled
    def setUp(cls):
        cls.librarian_fixture = LibrarianServerFixture(
            BaseLayer.config_fixture
        )
        cls.librarian_fixture.setUp()
        cls._check_and_reset()

        # Make sure things using the appserver config know the
        # correct Librarian port numbers.
        cls.appserver_service_config = cls.librarian_fixture.service_config
        cls.appserver_config_fixture.add_section(cls.appserver_service_config)

    @classmethod
    @profiled
    def tearDown(cls):
        # Permit multiple teardowns while we sort out the layering
        # responsibilities : not desirable though.
        if cls.librarian_fixture is None:
            return
        cls.appserver_config_fixture.remove_section(
            cls.appserver_service_config
        )
        try:
            cls._check_and_reset()
        finally:
            librarian = cls.librarian_fixture
            cls.librarian_fixture = None
            librarian.cleanUp()

    @classmethod
    @profiled
    def _check_and_reset(cls):
        """Raise an exception if the Librarian has been killed, else reset."""
        try:
            with Session() as session:
                session.mount(
                    config.librarian.download_url, HTTPAdapter(max_retries=3)
                )
                session.get(config.librarian.download_url).content
        except Exception as e:
            raise LayerIsolationError(
                "Librarian has been killed or has hung."
                "Tests should use LibrarianLayer.hide() and "
                "LibrarianLayer.reveal() where possible, and ensure "
                "the Librarian is restarted if it absolutely must be "
                "shutdown: " + str(e)
            )
        else:
            cls.librarian_fixture.reset()

    @classmethod
    @profiled
    def testSetUp(cls):
        cls._check_and_reset()

    @classmethod
    @profiled
    def testTearDown(cls):
        if cls._hidden:
            cls.reveal()
        cls._check_and_reset()

    # Flag maintaining state of hide()/reveal() calls
    _hidden = False

    # Fake upload socket used when the librarian is hidden
    _fake_upload_socket = None

    @classmethod
    @profiled
    def hide(cls):
        """Hide the Librarian so nothing can find it. We don't want to
        actually shut it down because starting it up again is expensive.

        We do this by altering the configuration so the Librarian client
        looks for the Librarian server on the wrong port.
        """
        cls._hidden = True
        if cls._fake_upload_socket is None:
            # Bind to a socket, but don't listen to it.  This way we
            # guarantee that connections to the given port will fail.
            cls._fake_upload_socket = socket.socket(
                socket.AF_INET, socket.SOCK_STREAM
            )
            assert (
                config.librarian.upload_host == "localhost"
            ), "Can only hide librarian if it is running locally"
            cls._fake_upload_socket.bind(("127.0.0.1", 0))

        host, port = cls._fake_upload_socket.getsockname()
        librarian_data = dedent(
            """
            [librarian]
            upload_port: %s
            """
            % port
        )
        config.push("hide_librarian", librarian_data)

    @classmethod
    @profiled
    def reveal(cls):
        """Reveal a hidden Librarian.

        This just involves restoring the config to the original value.
        """
        cls._hidden = False
        config.pop("hide_librarian")


def test_default_timeout():
    """Don't timeout by default in tests."""
    return None


class LaunchpadLayer(LibrarianLayer, MemcachedLayer, RabbitMQLayer):
    """Provides access to the Launchpad database and daemons.

    We need to ensure that the database setup runs before the daemon
    setup, or the database setup will fail because the daemons are
    already connected to the database.

    This layer is mainly used by tests that call initZopeless() themselves.
    Most tests will use a sublayer such as LaunchpadFunctionalLayer that
    provides access to the Component Architecture.
    """

    @classmethod
    @profiled
    def setUp(cls):
        pass

    @classmethod
    @profiled
    def tearDown(cls):
        pass

    @classmethod
    @profiled
    def testSetUp(cls):
        # By default, don't make external service tests timeout.
        if get_default_timeout_function() is not None:
            raise LayerIsolationError(
                "Global default timeout function should be None."
            )
        set_default_timeout_function(test_default_timeout)

    @classmethod
    @profiled
    def testTearDown(cls):
        if get_default_timeout_function() is not test_default_timeout:
            raise LayerIsolationError(
                "Test didn't reset default timeout function."
            )
        set_default_timeout_function(None)

    # A database connection to the session database, created by the first
    # call to resetSessionDb.
    _raw_sessiondb_connection = None

    @classmethod
    @profiled
    def resetSessionDb(cls):
        """Reset the session database.

        Layers that need session database isolation call this explicitly
        in the testSetUp().
        """
        if LaunchpadLayer._raw_sessiondb_connection is None:
            from lp.services.webapp.adapter import LaunchpadSessionDatabase

            launchpad_session_database = LaunchpadSessionDatabase(
                URI("launchpad-session:")
            )
            LaunchpadLayer._raw_sessiondb_connection = (
                launchpad_session_database.raw_connect()
            )
        LaunchpadLayer._raw_sessiondb_connection.cursor().execute(
            "DELETE FROM SessionData"
        )


class BasicTaliskerMiddleware:
    """Middleware to set up a Talisker context.

    The full `talisker.wsgi.TaliskerMiddleware` does a lot of things we
    don't need in our tests, but it's useful to at least have a context so
    that we can test logging behaviour.
    """

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        Context.new()
        return self.app(environ, start_response)


class TransactionMiddleware:
    """Middleware to commit the current transaction before the test.

    This is like `zope.app.wsgi.testlayer.TransactionMiddleware`, but avoids
    ZODB.
    """

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        transaction.commit()
        yield from self.app(environ, start_response)


class RemoteAddrMiddleware:
    """Middleware to set a default for `REMOTE_ADDR`.

    zope.app.testing.functional.HTTPCaller used to set this, but WebTest
    doesn't.  However, some tests rely on it.
    """

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        environ.setdefault("REMOTE_ADDR", wsgi_native_string("127.0.0.1"))
        return self.app(environ, start_response)


class SortHeadersMiddleware:
    """Middleware to sort response headers.

    This makes it easier to write reliable tests.
    """

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        def wrap_start_response(status, response_headers, exc_info=None):
            return start_response(status, sorted(response_headers), exc_info)

        return self.app(environ, wrap_start_response)


class _FunctionalBrowserLayer(zope.testbrowser.wsgi.Layer, ZCMLFileLayer):
    """A variant of zope.app.wsgi.testlayer.BrowserLayer for FunctionalLayer.

    This is not a layer for use in Launchpad tests (hence the leading
    underscore), as zope.component's layer composition strategy is different
    from the one zope.testrunner expects.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.middlewares = [
            AuthorizationMiddleware,
            RemoteAddrMiddleware,
            SortHeadersMiddleware,
            TransactionMiddleware,
            BasicTaliskerMiddleware,
        ]

    def setUp(self):
        super().setUp()
        # We don't use ZODB, but the webapp subscribes to IDatabaseOpened to
        # perform some post-configuration tasks, so emit that event
        # manually.
        notify(DatabaseOpened(None))

    def _resetWSGIApp(self):
        """Reset `zope.testbrowser.wsgi.Layer.get_app`'s cache.

        `zope.testbrowser.wsgi.Layer` sets up a cached WSGI application in
        `setUp` and assumes that it won't change for the lifetime of the
        layer.  This assumption doesn't hold in Launchpad, but
        `zope.testbrowser.wsgi.Layer` doesn't provide a straightforward way
        to avoid making it.  We do our best.
        """
        zope.testbrowser.wsgi._APP_UNDER_TEST = self.make_wsgi_app()

    def addMiddlewares(self, *middlewares):
        self.middlewares.extend(middlewares)
        self._resetWSGIApp()

    def removeMiddlewares(self, *middlewares):
        for middleware in middlewares:
            self.middlewares.remove(middleware)
        self._resetWSGIApp()

    def setupMiddleware(self, app):
        for middleware in self.middlewares:
            app = middleware(app)
        return app

    def make_wsgi_app(self):
        """See `zope.testbrowser.wsgi.Layer`."""
        return self.setupMiddleware(WSGIPublisherApplication())


class FunctionalLayer(BaseLayer):
    """Loads the Zope3 component architecture in appserver mode."""

    # Set to True if tests using the Functional layer are currently being run.
    isSetUp = False

    @classmethod
    @profiled
    def setUp(cls):
        FunctionalLayer.isSetUp = True

        # zope.component.testlayer.LayerBase has a different strategy for
        # layer composition that doesn't play well with zope.testrunner's
        # approach to setting up and tearing down individual layers.  Work
        # around this by creating a BrowserLayer instance here rather than
        # having this layer subclass it.
        FunctionalLayer.browser_layer = _FunctionalBrowserLayer(
            zcml, zcml_file="ftesting.zcml"
        )
        FunctionalLayer.browser_layer.setUp()

        # Assert that ZCMLFileLayer did what it says it does
        if not is_ca_available():
            raise LayerInvariantError("Component architecture failed to load")

        # Access the cookie manager's secret to get the cache populated.
        # If we don't, it may issue extra queries depending on test order.
        lp.services.webapp.session.idmanager.secret
        # If our request publication factories were defined using ZCML,
        # they'd be set up by ZCMLFileLayer. Since they're defined by Python
        # code, we need to call that code here.
        register_launchpad_request_publication_factories()

        # Most tests use the WSGI application directly via
        # zope.testbrowser.wsgi.Layer.get_app, but some (especially those
        # that use lazr.restfulclient or launchpadlib) still talk to the app
        # server over HTTP and need to be intercepted.
        wsgi_intercept.add_wsgi_intercept(
            "localhost", 80, _FunctionalBrowserLayer.get_app
        )
        wsgi_intercept.add_wsgi_intercept(
            "api.launchpad.test", 80, _FunctionalBrowserLayer.get_app
        )
        httplib2_intercept.install()

        # webob.request.environ_from_url defaults to HTTP/1.0, which is
        # somewhat unhelpful and breaks some tests (due to e.g. differences
        # in status codes used for redirections).  Patch this to default to
        # HTTP/1.1 instead.
        def environ_from_url_http11(path):
            env = orig_environ_from_url(path)
            env["SERVER_PROTOCOL"] = "HTTP/1.1"
            return env

        FunctionalLayer._environ_from_url_http11 = MonkeyPatch(
            "webob.request.environ_from_url", environ_from_url_http11
        )
        FunctionalLayer._environ_from_url_http11.setUp()

    @classmethod
    @profiled
    def tearDown(cls):
        FunctionalLayer.isSetUp = False
        FunctionalLayer._environ_from_url_http11.cleanUp()
        wsgi_intercept.remove_wsgi_intercept("localhost", 80)
        wsgi_intercept.remove_wsgi_intercept("api.launchpad.test", 80)
        httplib2_intercept.uninstall()
        FunctionalLayer.browser_layer.tearDown()
        # Signal Layer cannot be torn down fully
        raise NotImplementedError

    @classmethod
    @profiled
    def testSetUp(cls):
        transaction.abort()
        transaction.begin()

        # Allow the WSGI test browser to talk to our various test hosts.
        def _assertAllowed(self, url):
            parsed = urlparse(url)
            host = parsed.netloc.partition(":")[0]
            if host == "localhost" or host.endswith(".test"):
                return
            raise HostNotAllowed(url)

        FunctionalLayer._testbrowser_allowed = MonkeyPatch(
            "zope.testbrowser.browser.TestbrowserApp._assertAllowed",
            _assertAllowed,
        )
        FunctionalLayer._testbrowser_allowed.setUp()
        FunctionalLayer.browser_layer.testSetUp()

        # Should be impossible, as the CA cannot be unloaded. Something
        # mighty nasty has happened if this is triggered.
        if not is_ca_available():
            raise LayerInvariantError(
                "Component architecture not loaded or totally screwed"
            )

    @classmethod
    @profiled
    def testTearDown(cls):
        FunctionalLayer.browser_layer.testTearDown()
        FunctionalLayer._testbrowser_allowed.cleanUp()

        # Should be impossible, as the CA cannot be unloaded. Something
        # mighty nasty has happened if this is triggered.
        if not is_ca_available():
            raise LayerInvariantError(
                "Component architecture not loaded or totally screwed"
            )

        transaction.abort()


class ZopelessLayer(BaseLayer):
    """Layer for tests that need the Zopeless component architecture
    loaded using execute_zcml_for_scripts().
    """

    # Set to True if tests in the Zopeless layer are currently being run.
    isSetUp = False

    @classmethod
    @profiled
    def setUp(cls):
        ZopelessLayer.isSetUp = True
        execute_zcml_for_scripts()

        # Assert that execute_zcml_for_scripts did what it says it does.
        if not is_ca_available():
            raise LayerInvariantError(
                "Component architecture not loaded by "
                "execute_zcml_for_scripts"
            )

        # If our request publication factories were defined using
        # ZCML, they'd be set up by execute_zcml_for_scripts(). Since
        # they're defined by Python code, we need to call that code
        # here.
        register_launchpad_request_publication_factories()

    @classmethod
    @profiled
    def tearDown(cls):
        ZopelessLayer.isSetUp = False
        # Signal Layer cannot be torn down fully
        raise NotImplementedError

    @classmethod
    @profiled
    def testSetUp(cls):
        # Should be impossible, as the CA cannot be unloaded. Something
        # mighty nasty has happened if this is triggered.
        if not is_ca_available():
            raise LayerInvariantError(
                "Component architecture not loaded or totally screwed"
            )
        # This should not happen here, it should be caught by the
        # testTearDown() method. If it does, something very nasty
        # happened.
        if getSecurityPolicy() != LaunchpadPermissiveSecurityPolicy:
            raise LayerInvariantError(
                "Previous test removed the LaunchpadPermissiveSecurityPolicy."
            )

        # execute_zcml_for_scripts() sets up an interaction for the
        # anonymous user. A previous script may have changed or removed
        # the interaction, so set it up again
        login(ANONYMOUS)

    @classmethod
    @profiled
    def testTearDown(cls):
        # Should be impossible, as the CA cannot be unloaded. Something
        # mighty nasty has happened if this is triggered.
        if not is_ca_available():
            raise LayerInvariantError(
                "Component architecture not loaded or totally screwed"
            )
        # Make sure that a test that changed the security policy, reset it
        # back to its default value.
        if getSecurityPolicy() != LaunchpadPermissiveSecurityPolicy:
            raise LayerInvariantError(
                "This test removed the LaunchpadPermissiveSecurityPolicy and "
                "didn't restore it."
            )
        logout()


class TwistedLayer(BaseLayer):
    """A layer for cleaning up the Twisted thread pool."""

    @classmethod
    @profiled
    def setUp(cls):
        pass

    @classmethod
    @profiled
    def tearDown(cls):
        pass

    @classmethod
    def _save_signals(cls):
        """Save the current signal handlers."""
        TwistedLayer._original_sigint = signal.getsignal(signal.SIGINT)
        TwistedLayer._original_sigterm = signal.getsignal(signal.SIGTERM)
        TwistedLayer._original_sigchld = signal.getsignal(signal.SIGCHLD)
        # XXX MichaelHudson, 2009-07-14, bug=399118: If a test case in this
        # layer launches a process with spawnProcess, there should really be a
        # SIGCHLD handler installed to avoid PotentialZombieWarnings.  But
        # some tests in this layer use tachandler and it is fragile when a
        # SIGCHLD handler is installed.  tachandler needs to be fixed.
        # from twisted.internet import reactor
        # signal.signal(signal.SIGCHLD, reactor._handleSigchld)

    @classmethod
    def _restore_signals(cls):
        """Restore the signal handlers."""
        signal.signal(signal.SIGINT, TwistedLayer._original_sigint)
        signal.signal(signal.SIGTERM, TwistedLayer._original_sigterm)
        signal.signal(signal.SIGCHLD, TwistedLayer._original_sigchld)

    @classmethod
    @profiled
    def testSetUp(cls):
        TwistedLayer._save_signals()
        from twisted.internet import interfaces, reactor
        from twisted.python import threadpool

        # zope.exception demands more of frame objects than
        # twisted.python.failure provides in its fake frames.  This is enough
        # to make it work with them as of 2009-09-16.  See
        # https://bugs.launchpad.net/bugs/425113.
        cls._patch = MonkeyPatch(
            "twisted.python.failure._Frame.f_locals", property(lambda self: {})
        )
        cls._patch.setUp()
        if interfaces.IReactorThreads.providedBy(reactor):
            pool = getattr(reactor, "threadpool", None)
            # If the Twisted threadpool has been obliterated (probably by
            # testTearDown), then re-build it using the values that Twisted
            # uses.
            if pool is None:
                reactor.threadpool = threadpool.ThreadPool(0, 10)
                reactor.threadpool.start()

    @classmethod
    @profiled
    def testTearDown(cls):
        # Shutdown and obliterate the Twisted threadpool, to plug up leaking
        # threads.
        from twisted.internet import interfaces, reactor

        if interfaces.IReactorThreads.providedBy(reactor):
            reactor.suggestThreadPoolSize(0)
            pool = getattr(reactor, "threadpool", None)
            if pool is not None:
                reactor.threadpool.stop()
                reactor.threadpool = None
        cls._patch.cleanUp()
        TwistedLayer._restore_signals()


class BingServiceLayer(BaseLayer):
    """Tests for Bing web service integration."""

    @classmethod
    def setUp(cls):
        bing = BingServiceTestSetup()
        bing.setUp()

    @classmethod
    def tearDown(cls):
        BingServiceTestSetup().tearDown()

    @classmethod
    def testSetUp(self):
        # We need to override BaseLayer.testSetUp(), or else we will
        # get a LayerIsolationError.
        pass

    @classmethod
    def testTearDown(self):
        # We need to override BaseLayer.testTearDown(), or else we will
        # get a LayerIsolationError.
        pass


class DatabaseFunctionalLayer(DatabaseLayer, FunctionalLayer):
    """Provides the database and the Zope3 application server environment."""

    @classmethod
    @profiled
    def setUp(cls):
        pass

    @classmethod
    @profiled
    def tearDown(cls):
        pass

    @classmethod
    @profiled
    def testSetUp(cls):
        # Connect Storm
        reconnect_stores(reset=True)

    @classmethod
    @profiled
    def testTearDown(cls):
        getUtility(IOpenLaunchBag).clear()

        endInteraction()

        # Disconnect Storm so it doesn't get in the way of database resets
        disconnect_stores()


class LaunchpadFunctionalLayer(LaunchpadLayer, FunctionalLayer):
    """Provides the Launchpad Zope3 application server environment."""

    @classmethod
    @profiled
    def setUp(cls):
        pass

    @classmethod
    @profiled
    def testSetUp(cls):
        # Reset any statistics
        from lp.services.webapp.opstats import OpStats

        OpStats.resetStats()

        # Connect Storm
        reconnect_stores(reset=True)

    @classmethod
    @profiled
    def testTearDown(cls):
        getUtility(IOpenLaunchBag).clear()

        endInteraction()

        # Reset any statistics
        from lp.services.webapp.opstats import OpStats

        OpStats.resetStats()

        # Disconnect Storm so it doesn't get in the way of database resets
        disconnect_stores()


class BingLaunchpadFunctionalLayer(LaunchpadFunctionalLayer, BingServiceLayer):
    """Provides Bing service in addition to LaunchpadFunctionalLayer."""

    @classmethod
    @profiled
    def setUp(cls):
        pass

    @classmethod
    @profiled
    def tearDown(cls):
        pass

    @classmethod
    @profiled
    def testSetUp(cls):
        pass

    @classmethod
    @profiled
    def testTearDown(cls):
        pass


class ZopelessDatabaseLayer(ZopelessLayer, DatabaseLayer):
    """Testing layer for unit tests with no need for librarian.

    Can be used wherever you're accustomed to using LaunchpadZopeless
    or LaunchpadScript layers, but there is no need for librarian.
    """

    @classmethod
    @profiled
    def setUp(cls):
        pass

    @classmethod
    @profiled
    def tearDown(cls):
        # Signal Layer cannot be torn down fully
        raise NotImplementedError

    @classmethod
    @profiled
    def testSetUp(cls):
        # LaunchpadZopelessLayer takes care of reconnecting the stores
        if not LaunchpadZopelessLayer.isSetUp:
            reconnect_stores(reset=True)

    @classmethod
    @profiled
    def testTearDown(cls):
        disconnect_stores()


class LaunchpadScriptLayer(ZopelessLayer, LaunchpadLayer):
    """Testing layer for scripts using the main Launchpad database adapter"""

    @classmethod
    @profiled
    def setUp(cls):
        # Make a TestMailBox available
        # This is registered via ZCML in the LaunchpadFunctionalLayer
        # XXX flacoste 2006-10-25 bug=68189: This should be configured from
        # ZCML but execute_zcml_for_scripts() doesn't cannot support a
        # different testing configuration.
        cls._mailbox = TestMailBox()
        provideUtility(cls._mailbox, IMailBox)

    @classmethod
    @profiled
    def tearDown(cls):
        if not globalregistry.base.unregisterUtility(cls._mailbox):
            raise NotImplementedError("failed to unregister mailbox")

    @classmethod
    @profiled
    def testSetUp(cls):
        # LaunchpadZopelessLayer takes care of reconnecting the stores
        if not LaunchpadZopelessLayer.isSetUp:
            reconnect_stores(reset=True)

    @classmethod
    @profiled
    def testTearDown(cls):
        disconnect_stores()


class LaunchpadTestSetup(PgTestSetup):
    template = "launchpad_ftest_template"
    dbuser = "launchpad"
    host = "localhost"


class LaunchpadZopelessLayer(LaunchpadScriptLayer):
    """Full Zopeless environment including Component Architecture and
    database connections initialized.
    """

    isSetUp = False
    txn = transaction

    @classmethod
    @profiled
    def setUp(cls):
        LaunchpadZopelessLayer.isSetUp = True

    @classmethod
    @profiled
    def tearDown(cls):
        LaunchpadZopelessLayer.isSetUp = False

    @classmethod
    @profiled
    def testSetUp(cls):
        dbconfig.override(isolation_level="read_committed")
        # XXX wgrant 2011-09-24 bug=29744: initZopeless used to do this.
        # Tests that still need it should eventually set this directly,
        # so the whole layer is not polluted.
        set_immediate_mail_delivery(True)

        # Connect Storm
        reconnect_stores()

    @classmethod
    @profiled
    def testTearDown(cls):
        dbconfig.reset()
        # LaunchpadScriptLayer will disconnect the stores for us.

        # XXX wgrant 2011-09-24 bug=29744: uninstall used to do this.
        # Tests that still need immediate delivery should eventually do
        # this directly.
        set_immediate_mail_delivery(False)

    @classmethod
    @profiled
    def commit(cls):
        transaction.commit()

    @classmethod
    @profiled
    def abort(cls):
        transaction.abort()


class ProfilingMiddleware:
    """Middleware to profile WSGI responses."""

    def __init__(self, app, profiler=None):
        self.app = app
        self.profiler = profiler

    def __call__(self, environ, start_response):
        if self.profiler is not None:
            start_response = partial(self.profiler.runcall, start_response)
        return self.app(environ, start_response)


class PageTestLayer(LaunchpadFunctionalLayer, BingServiceLayer):
    """Environment for page tests."""

    @classmethod
    @profiled
    def setUp(cls):
        if os.environ.get("PROFILE_PAGETESTS_REQUESTS"):
            PageTestLayer.profiler = Profile()
        else:
            PageTestLayer.profiler = None

        PageTestLayer._profiling_middleware = partial(
            ProfilingMiddleware, profiler=PageTestLayer.profiler
        )
        FunctionalLayer.browser_layer.addMiddlewares(
            PageTestLayer._profiling_middleware
        )

    @classmethod
    @profiled
    def tearDown(cls):
        FunctionalLayer.browser_layer.removeMiddlewares(
            PageTestLayer._profiling_middleware
        )
        if PageTestLayer.profiler:
            PageTestLayer.profiler.dump_stats(
                os.environ.get("PROFILE_PAGETESTS_REQUESTS")
            )

    @classmethod
    @profiled
    def testSetUp(cls):
        LaunchpadLayer.resetSessionDb()

    @classmethod
    @profiled
    def testTearDown(cls):
        pass


class LayerProcessController:
    """Controller for starting and stopping subprocesses.

    Layers which need to start and stop a child process appserver should
    call the methods in this class, but should NOT inherit from this class.
    """

    # Holds the Popen instance of the spawned app server.
    appserver = None

    # The config used by the spawned app server.
    appserver_config = None

    @classmethod
    def setConfig(cls):
        """Stash a config for use."""
        cls.appserver_config = LaunchpadConfig(
            BaseLayer.appserver_config_name, "runlaunchpad"
        )

    @classmethod
    def setUp(cls):
        cls.setConfig()
        cls.startAppServer()

    @classmethod
    @profiled
    def startAppServer(cls, run_name="run"):
        """Start the app server if it hasn't already been started."""
        if cls.appserver is not None:
            raise LayerInvariantError("App server already running")
        cls._cleanUpStaleAppServer()
        cls._runAppServer(run_name)
        cls._waitUntilAppServerIsReady()

    @classmethod
    def _kill(cls, sig):
        """Kill the appserver with `sig`.

        :param sig: the signal to kill with
        :type sig: int
        :return: True if the signal was delivered, otherwise False.
        :rtype: bool
        """
        try:
            os.kill(cls.appserver.pid, sig)
        except ProcessLookupError:
            # The child process doesn't exist.  Maybe it went away by the
            # time we got here.
            cls.appserver.communicate()
            cls.appserver = None
            return False
        else:
            return True

    @classmethod
    @profiled
    def stopAppServer(cls):
        """Kill the appserver and wait until it's exited."""
        if cls.appserver is not None:
            # Unfortunately, Popen.wait() does not support a timeout, so poll
            # for a little while, then SIGKILL the process if it refuses to
            # exit.  test_on_merge.py will barf if we hang here for too long.
            until = datetime.datetime.now() + WAIT_INTERVAL
            last_chance = False
            if not cls._kill(signal.SIGTERM):
                # The process is already gone.
                return
            while True:
                # Sleep and poll for process exit.
                if cls.appserver.poll() is not None:
                    break
                time.sleep(0.5)
                # If we slept long enough, send a harder kill and wait again.
                # If we already had our last chance, raise an exception.
                if datetime.datetime.now() > until:
                    if last_chance:
                        raise RuntimeError("The appserver just wouldn't die")
                    last_chance = True
                    if not cls._kill(signal.SIGKILL):
                        # The process is already gone.
                        return
                    until = datetime.datetime.now() + WAIT_INTERVAL
            cls.appserver.communicate()
            cls.appserver = None

    @classmethod
    @profiled
    def postTestInvariants(cls):
        """Enforce some invariants after each test.

        Must be called in your layer class's `testTearDown()`.
        """
        if cls.appserver.poll() is not None:
            raise LayerIsolationError(
                "App server died in this test (status=%s):\n%s"
                % (cls.appserver.returncode, cls.appserver.stdout.read())
            )
        # Cleanup the app server's output buffer between tests.
        while True:
            # Read while we have something available at the stdout.
            r, w, e = select.select([cls.appserver.stdout], [], [], 0)
            if cls.appserver.stdout in r:
                cls.appserver.stdout.readline()
            else:
                break
        DatabaseLayer.force_dirty_database()

    @classmethod
    def _cleanUpStaleAppServer(cls):
        """Kill any stale app server or pid file."""
        pid = pidfile.get_pid("launchpad", cls.appserver_config)
        if pid is not None:
            # Don't worry if the process no longer exists.
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            pidfile.remove_pidfile("launchpad", cls.appserver_config)

    @classmethod
    def _runAppServer(cls, run_name):
        """Start the app server using runlaunchpad.py"""
        _config = cls.appserver_config
        cmd = [os.path.join(_config.root, "bin", run_name)]
        environ = dict(os.environ)
        environ["LPCONFIG"] = _config.instance_name
        cls.appserver = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environ,
            cwd=_config.root,
        )

    @classmethod
    def appserver_root_url(cls):
        return cls.appserver_config.vhost.mainsite.rooturl

    @classmethod
    def _waitUntilAppServerIsReady(cls):
        """Wait until the app server accepts connection."""
        assert cls.appserver is not None, "App server isn't started."
        root_url = cls.appserver_root_url()
        until = datetime.datetime.now() + WAIT_INTERVAL
        while until > datetime.datetime.now():
            try:
                connection = urlopen(root_url)
                connection.read()
            except HTTPError as error:
                if error.code == 503:
                    raise RuntimeError(
                        "App server is returning unknown error code %s. Is "
                        "there another instance running in the same port?"
                        % error.code
                    )
            except URLError as error:
                # We are interested in a wrapped ConnectionRefusedError.
                if not isinstance(error.reason, ConnectionRefusedError):
                    raise
                returncode = cls.appserver.poll()
                if returncode is not None:
                    raise RuntimeError(
                        "App server failed to start (status=%d):\n%s"
                        % (returncode, cls.appserver.stdout.read())
                    )
                time.sleep(0.5)
            else:
                connection.close()
                break
        else:
            os.kill(cls.appserver.pid, signal.SIGTERM)
            cls.appserver.communicate()
            cls.appserver = None
            # Go no further.
            raise AssertionError("App server startup timed out.")


class AppServerLayer(LaunchpadFunctionalLayer):
    """Layer for tests that run in a webapp environment with an app server."""

    @classmethod
    @profiled
    def setUp(cls):
        LayerProcessController.setUp()

    @classmethod
    @profiled
    def tearDown(cls):
        LayerProcessController.stopAppServer()

    @classmethod
    @profiled
    def testSetUp(cls):
        LaunchpadLayer.resetSessionDb()

    @classmethod
    @profiled
    def testTearDown(cls):
        LayerProcessController.postTestInvariants()


class CeleryJobLayer(AppServerLayer):
    """Layer for tests that run jobs via Celery."""

    celery_worker = None

    @classmethod
    @profiled
    def setUp(cls):
        cls.celery_worker = celery_worker("launchpad_job")
        cls.celery_worker.__enter__()

    @classmethod
    @profiled
    def tearDown(cls):
        cls.celery_worker.__exit__(None, None, None)
        cls.celery_worker = None


class CelerySlowJobLayer(AppServerLayer):
    """Layer for tests that run jobs via Celery."""

    celery_worker = None

    @classmethod
    @profiled
    def setUp(cls):
        cls.celery_worker = celery_worker("launchpad_job_slow")
        cls.celery_worker.__enter__()

    @classmethod
    @profiled
    def tearDown(cls):
        cls.celery_worker.__exit__(None, None, None)
        cls.celery_worker = None


class CeleryBzrsyncdJobLayer(AppServerLayer):
    """Layer for tests that run jobs that read from branches via Celery."""

    celery_worker = None

    @classmethod
    @profiled
    def setUp(cls):
        cls.celery_worker = celery_worker("bzrsyncd_job")
        cls.celery_worker.__enter__()

    @classmethod
    @profiled
    def tearDown(cls):
        cls.celery_worker.__exit__(None, None, None)
        cls.celery_worker = None


class CeleryBranchWriteJobLayer(AppServerLayer):
    """Layer for tests that run jobs which write to branches via Celery."""

    celery_worker = None

    @classmethod
    @profiled
    def setUp(cls):
        cls.celery_worker = celery_worker("branch_write_job")
        cls.celery_worker.__enter__()

    @classmethod
    @profiled
    def tearDown(cls):
        cls.celery_worker.__exit__(None, None, None)
        cls.celery_worker = None


class ZopelessAppServerLayer(LaunchpadZopelessLayer):
    """Layer for Zopeless tests with an appserver."""

    @classmethod
    @profiled
    def setUp(cls):
        LayerProcessController.setUp()

    @classmethod
    @profiled
    def tearDown(cls):
        LayerProcessController.stopAppServer()

    @classmethod
    @profiled
    def testSetUp(cls):
        LaunchpadLayer.resetSessionDb()

    @classmethod
    @profiled
    def testTearDown(cls):
        LayerProcessController.postTestInvariants()


class WebBrowserLayer(BaseLayer):
    """A layer that runs a web browser."""

    browser = None

    @classmethod
    @profiled
    def setUp(cls):
        cls.browser = Browser()

    @classmethod
    @profiled
    def tearDown(cls):
        if cls.browser:
            cls.browser.close()
            cls.browser = None

    @classmethod
    @profiled
    def testSetUp(cls):
        pass

    @classmethod
    @profiled
    def testTearDown(cls):
        pass


class YUITestLayer(FunctionalLayer, WebBrowserLayer):
    """The layer for all YUI test cases."""

    browser = None

    @classmethod
    @profiled
    def setUp(cls):
        pass

    @classmethod
    @profiled
    def tearDown(cls):
        pass

    @classmethod
    @profiled
    def testSetUp(cls):
        pass

    @classmethod
    @profiled
    def testTearDown(cls):
        pass


class YUIAppServerLayer(MemcachedLayer):
    """The layer for all YUIAppServer test cases."""

    browser = None

    @classmethod
    @profiled
    def setUp(cls):
        LayerProcessController.setConfig()
        LayerProcessController.startAppServer("run-testapp")
        cls.browser = Browser()

    @classmethod
    @profiled
    def tearDown(cls):
        LayerProcessController.stopAppServer()
        if cls.browser:
            cls.browser.close()
            cls.browser = None

    @classmethod
    @profiled
    def testSetUp(cls):
        LaunchpadLayer.resetSessionDb()
