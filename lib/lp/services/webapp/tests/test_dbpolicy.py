# Copyright 2009-2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the DBPolicy."""

__all__ = []

from textwrap import dedent

import psycopg2
import transaction
from lazr.restful.interfaces import IWebServiceConfiguration
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from storm.exceptions import DisconnectionError
from zope.component import getAdapter, getUtility
from zope.publisher.interfaces.xmlrpc import IXMLRPCRequest
from zope.security.management import endInteraction, newInteraction

from lp.layers import FeedsLayer, WebServiceLayer, setFirstLayer
from lp.registry.model.person import Person
from lp.services.config import config, dbconfig
from lp.services.database.interfaces import (
    ALL_STORES,
    DEFAULT_FLAVOR,
    MAIN_STORE,
    PRIMARY_FLAVOR,
    STANDBY_FLAVOR,
    DisallowedStore,
    IDatabasePolicy,
    IPrimaryStore,
    IStandbyStore,
    IStoreSelector,
)
from lp.services.database.policy import (
    BaseDatabasePolicy,
    LaunchpadDatabasePolicy,
    PrimaryDatabasePolicy,
    StandbyDatabasePolicy,
    StandbyOnlyDatabasePolicy,
)
from lp.services.database.sqlbase import disconnect_stores
from lp.services.propertycache import get_property_cache
from lp.services.webapp.interfaces import ISession
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.testing import TestCase
from lp.testing.fixture import PGBouncerFixture
from lp.testing.layers import (
    DatabaseFunctionalLayer,
    DatabaseLayer,
    FunctionalLayer,
)


class ImplicitDatabasePolicyTestCase(TestCase):
    """Tests for when there is no policy installed."""

    layer = DatabaseFunctionalLayer

    def test_defaults(self):
        for store in ALL_STORES:
            self.assertProvides(
                getUtility(IStoreSelector).get(store, DEFAULT_FLAVOR),
                IPrimaryStore,
            )

    def test_dbusers(self):
        store_selector = getUtility(IStoreSelector)
        main_store = store_selector.get(MAIN_STORE, DEFAULT_FLAVOR)
        self.assertEqual(self.getDBUser(main_store), "launchpad_main")

    def getDBUser(self, store):
        return store.execute("SHOW session_authorization").get_one()[0]


class BaseDatabasePolicyTestCase(ImplicitDatabasePolicyTestCase):
    """Base tests for DatabasePolicy implementation."""

    policy = None

    def setUp(self):
        super().setUp()
        if self.policy is None:
            self.policy = BaseDatabasePolicy()
        getUtility(IStoreSelector).push(self.policy)

    def tearDown(self):
        getUtility(IStoreSelector).pop()
        super().tearDown()

    def test_correctly_implements_IDatabasePolicy(self):
        self.assertProvides(self.policy, IDatabasePolicy)


class StandbyDatabasePolicyTestCase(BaseDatabasePolicyTestCase):
    """Tests for the `StandbyDatabasePolicy`."""

    def setUp(self):
        if self.policy is None:
            self.policy = StandbyDatabasePolicy()
        super().setUp()

    def test_defaults(self):
        for store in ALL_STORES:
            self.assertProvides(
                getUtility(IStoreSelector).get(store, DEFAULT_FLAVOR),
                IStandbyStore,
            )

    def test_primary_allowed(self):
        for store in ALL_STORES:
            self.assertProvides(
                getUtility(IStoreSelector).get(store, PRIMARY_FLAVOR),
                IPrimaryStore,
            )


class StandbyOnlyDatabasePolicyTestCase(StandbyDatabasePolicyTestCase):
    """Tests for the `StandbyOnlyDatabasePolicy`."""

    def setUp(self):
        self.policy = StandbyOnlyDatabasePolicy()
        super().setUp()

    def test_primary_allowed(self):
        for store in ALL_STORES:
            self.assertRaises(
                DisallowedStore,
                getUtility(IStoreSelector).get,
                store,
                PRIMARY_FLAVOR,
            )


class PrimaryDatabasePolicyTestCase(BaseDatabasePolicyTestCase):
    """Tests for the `PrimaryDatabasePolicy`."""

    def setUp(self):
        self.policy = PrimaryDatabasePolicy()
        super().setUp()

    def test_XMLRPCRequest_uses_PrimaryDatabasePolicy(self):
        """XMLRPC should always use the primary flavor, since they always
        use POST and do not support session cookies.
        """
        request = LaunchpadTestRequest(
            SERVER_URL="http://xmlrpc-private.launchpad.test"
        )
        setFirstLayer(request, IXMLRPCRequest)
        policy = getAdapter(request, IDatabasePolicy)
        self.assertTrue(
            isinstance(policy, PrimaryDatabasePolicy),
            "Expected PrimaryDatabasePolicy, not %s." % policy,
        )

    def test_standby_allowed(self):
        # We get the primary store even if the standby was requested.
        for store in ALL_STORES:
            self.assertProvides(
                getUtility(IStoreSelector).get(store, STANDBY_FLAVOR),
                IStandbyStore,
            )


class LaunchpadDatabasePolicyTestCase(StandbyDatabasePolicyTestCase):
    """Fuller LaunchpadDatabasePolicy tests are in the page tests.

    This test just checks the defaults, which is the same as the
    standby policy for unauthenticated requests.
    """

    def setUp(self):
        request = LaunchpadTestRequest(SERVER_URL="http://launchpad.test")
        self.policy = LaunchpadDatabasePolicy(request)
        super().setUp()


class LayerDatabasePolicyTestCase(TestCase):
    layer = FunctionalLayer

    def test_FeedsLayer_uses_StandbyOnlyDatabasePolicy(self):
        """FeedsRequest should use the StandbyOnlyDatabasePolicy since they
        are read-only in nature. Also we don't want to send session cookies
        over them.
        """
        request = LaunchpadTestRequest(
            SERVER_URL="http://feeds.launchpad.test"
        )
        setFirstLayer(request, FeedsLayer)
        policy = IDatabasePolicy(request)
        self.assertIsInstance(policy, StandbyOnlyDatabasePolicy)

    def test_WebServiceRequest_uses_PrimaryDatabasePolicy(self):
        """WebService requests should always use the primary flavor, since
        it's likely that clients won't support cookies and thus mixing read
        and write requests will result in incoherent views of the data.

        XXX 20090320 Stuart Bishop bug=297052: This doesn't scale of course
            and will meltdown when the API becomes popular.
        """
        api_prefix = getUtility(IWebServiceConfiguration).active_versions[0]
        server_url = "http://api.launchpad.test/%s" % api_prefix
        request = LaunchpadTestRequest(SERVER_URL=server_url)
        setFirstLayer(request, WebServiceLayer)
        policy = IDatabasePolicy(request)
        self.assertIsInstance(policy, PrimaryDatabasePolicy)

    def test_WebServiceRequest_uses_LaunchpadDatabasePolicy(self):
        """WebService requests with a session cookie will use the
        standard LaunchpadDatabasePolicy so their database queries
        can be outsourced to a standby database when possible.
        """
        api_prefix = getUtility(IWebServiceConfiguration).active_versions[0]
        server_url = "http://api.launchpad.test/%s" % api_prefix
        request = LaunchpadTestRequest(SERVER_URL=server_url)
        newInteraction(request)
        try:
            # First, generate a valid session cookie.
            ISession(request)["whatever"]["whatever"] = "whatever"
            # Then stuff it into the request where we expect to
            # find it. The database policy is only interested if
            # a session cookie was sent with the request, not it
            # one has subsequently been set in the response.
            request._cookies = request.response._cookies
            setFirstLayer(request, WebServiceLayer)
            policy = IDatabasePolicy(request)
            self.assertIsInstance(policy, LaunchpadDatabasePolicy)
        finally:
            endInteraction()

    def test_other_request_uses_LaunchpadDatabasePolicy(self):
        """By default, requests should use the LaunchpadDatabasePolicy."""
        server_url = "http://launchpad.test/"
        request = LaunchpadTestRequest(SERVER_URL=server_url)
        policy = IDatabasePolicy(request)
        self.assertIsInstance(policy, LaunchpadDatabasePolicy)


class PrimaryFallbackTestCase(TestCase):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()

        self.pgbouncer_fixture = PGBouncerFixture()

        # The PGBouncerFixture will set the PGPORT environment variable,
        # causing all DB connections to go via pgbouncer unless an
        # explicit port is provided.
        dbname = DatabaseLayer._db_fixture.dbname
        # Pull the direct db connection string, including explicit port.
        conn_str_direct = self.pgbouncer_fixture.databases[dbname]
        # Generate a db connection string that will go via pgbouncer.
        conn_str_pgbouncer = "dbname=%s host=localhost" % dbname

        # Configure standby connections via pgbouncer, so we can shut them
        # down. Primary connections direct so they are unaffected.
        config_key = "primary-standby-separation"
        config.push(
            config_key,
            dedent(
                """\
            [database]
            rw_main_primary: %s
            rw_main_standby: %s
            """
                % (conn_str_direct, conn_str_pgbouncer)
            ),
        )
        self.addCleanup(lambda: config.pop(config_key))

        self.useFixture(self.pgbouncer_fixture)

    def test_can_shutdown_standby_only(self):
        """Confirm that this TestCase's test infrastructure works as needed."""
        primary_store = IPrimaryStore(Person)
        standby_store = IStandbyStore(Person)

        # Both Stores work when pgbouncer is up.
        primary_store.get(Person, 1)
        standby_store.get(Person, 1)

        # Standby Store breaks when pgbouncer is torn down.  Primary Store
        # is fine.
        self.pgbouncer_fixture.stop()
        primary_store.get(Person, 2)
        self.assertRaises(DisconnectionError, standby_store.get, Person, 2)

    def test_startup_with_no_standby(self):
        """An attempt is made for the first time to connect to a standby."""
        self.pgbouncer_fixture.stop()

        primary_store = IPrimaryStore(Person)
        standby_store = IStandbyStore(Person)

        # The primary and standby Stores are the same object.
        self.assertIs(primary_store, standby_store)

    def test_standby_shutdown_during_transaction(self):
        """Standby is shutdown while running, but we can recover."""
        primary_store = IPrimaryStore(Person)
        standby_store = IStandbyStore(Person)

        self.assertIsNot(primary_store, standby_store)

        self.pgbouncer_fixture.stop()

        # The transaction fails if the standby store is used. Robust
        # processes will handle this and retry (even if just means exit
        # and wait for the next scheduled invocation).
        self.assertRaises(DisconnectionError, standby_store.get, Person, 1)

        transaction.abort()

        # But in the next transaction, we get the primary Store if we ask
        # for the standby Store so we can continue.
        primary_store = IPrimaryStore(Person)
        standby_store = IStandbyStore(Person)

        self.assertIs(primary_store, standby_store)

    def test_standby_shutdown_between_transactions(self):
        """Standby is shutdown in between transactions."""
        primary_store = IPrimaryStore(Person)
        standby_store = IStandbyStore(Person)
        self.assertIsNot(primary_store, standby_store)

        transaction.abort()
        self.pgbouncer_fixture.stop()

        # The process doesn't notice the standby going down, and things
        # will fail the next time the standby is used.
        primary_store = IPrimaryStore(Person)
        standby_store = IStandbyStore(Person)
        self.assertIsNot(primary_store, standby_store)
        self.assertRaises(DisconnectionError, standby_store.get, Person, 1)

        # But now it has been discovered the socket is no longer
        # connected to anything, next transaction we get a primary
        # Store when we ask for a standby.
        primary_store = IPrimaryStore(Person)
        standby_store = IStandbyStore(Person)
        self.assertIs(primary_store, standby_store)

    def test_standby_reconnect_after_outage(self):
        """The standby is again used once it becomes available."""
        self.pgbouncer_fixture.stop()

        primary_store = IPrimaryStore(Person)
        standby_store = IStandbyStore(Person)
        self.assertIs(primary_store, standby_store)

        self.pgbouncer_fixture.start()
        transaction.abort()

        primary_store = IPrimaryStore(Person)
        standby_store = IStandbyStore(Person)
        self.assertIsNot(primary_store, standby_store)


class TestFastDowntimeRollout(TestCase):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()

        self.primary_dbname = DatabaseLayer._db_fixture.dbname
        self.standby_dbname = self.primary_dbname + "_standby"

        self.pgbouncer_fixture = PGBouncerFixture()
        self.pgbouncer_fixture.databases[self.standby_dbname] = (
            self.pgbouncer_fixture.databases[self.primary_dbname]
        )

        # Configure primary and standby connections to go via different
        # pgbouncer aliases.
        config_key = "primary-standby-separation"
        config.push(
            config_key,
            dedent(
                """\
            [database]
            rw_main_primary: dbname=%s host=localhost
            rw_main_standby: dbname=%s host=localhost
            """
                % (self.primary_dbname, self.standby_dbname)
            ),
        )
        self.addCleanup(lambda: config.pop(config_key))

        self.useFixture(self.pgbouncer_fixture)

        self.pgbouncer_con = psycopg2.connect(
            "dbname=pgbouncer user=pgbouncer host=localhost"
        )
        self.pgbouncer_con.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        self.pgbouncer_cur = self.pgbouncer_con.cursor()

        transaction.abort()

    def store_is_working(self, store):
        try:
            store.execute("SELECT TRUE")
            return True
        except DisconnectionError:
            return False

    def store_is_standby(self, store):
        return store.get_database().name.startswith("main-standby")

    def store_is_primary(self, store):
        return not self.store_is_standby(store)

    def test_standby_only_fast_downtime_rollout(self):
        """You can always access a working standby during fast downtime."""
        # Everything is running happily.
        store = IStandbyStore(Person)
        original_store = store
        self.assertTrue(self.store_is_working(store))
        self.assertTrue(self.store_is_standby(store))

        # But fast downtime is about to happen.

        # Replication is stopped on the standby, and lag starts
        # increasing.

        # All connections to the primary are killed so database schema
        # updates can be applied.
        self.pgbouncer_cur.execute("DISABLE %s" % self.primary_dbname)
        self.pgbouncer_cur.execute("KILL %s" % self.primary_dbname)

        # Of course, standby connections are unaffected.
        self.assertTrue(self.store_is_working(store))

        # After schema updates have been made to the primary, it is
        # re-enabled.
        self.pgbouncer_cur.execute("RESUME %s" % self.primary_dbname)
        self.pgbouncer_cur.execute("ENABLE %s" % self.primary_dbname)

        # And the standbys taken down, and replication re-enabled so the
        # schema updates can replicate.
        self.pgbouncer_cur.execute("DISABLE %s" % self.standby_dbname)
        self.pgbouncer_cur.execute("KILL %s" % self.standby_dbname)

        # The next attempt at accessing the standby store will fail
        # with a DisconnectionError.
        self.assertRaises(DisconnectionError, store.execute, "SELECT TRUE")
        transaction.abort()

        # But if we handle that and retry, we can continue.
        # Now the failed connection has been detected, the next Store
        # we are handed is a primary Store instead of a standby.
        store = IStandbyStore(Person)
        self.assertTrue(self.store_is_primary(store))
        self.assertIsNot(IStandbyStore(Person), original_store)

        # But alas, it might not work the first transaction. If it has
        # been earlier, its connection was killed by pgbouncer earlier
        # but it hasn't noticed yet.
        self.assertFalse(self.store_is_working(store))
        transaction.abort()

        # Next retry attempt, everything is fine using the primary
        # connection, even though our code only asked for a standby.
        store = IStandbyStore(Person)
        self.assertTrue(self.store_is_primary(store))
        self.assertTrue(self.store_is_working(store))

        # The original Store is busted though. You cannot reuse Stores
        # across transaction boundaries because you might end up using
        # the wrong Store.
        self.assertFalse(self.store_is_working(original_store))
        transaction.abort()

        # Once replication has caught up, the standby is re-enabled.
        self.pgbouncer_cur.execute("RESUME %s" % self.standby_dbname)
        self.pgbouncer_cur.execute("ENABLE %s" % self.standby_dbname)

        # And next transaction, we are back to normal.
        store = IStandbyStore(Person)
        self.assertTrue(self.store_is_working(store))
        self.assertTrue(self.store_is_standby(store))
        self.assertIs(original_store, store)

    def test_primary_standby_fast_downtime_rollout(self):
        """Parts of your app can keep working during a fast downtime update."""
        # Everything is running happily.
        primary_store = IPrimaryStore(Person)
        self.assertTrue(self.store_is_primary(primary_store))
        self.assertTrue(self.store_is_working(primary_store))

        standby_store = IStandbyStore(Person)
        self.assertTrue(self.store_is_standby(standby_store))
        self.assertTrue(self.store_is_working(standby_store))

        # But fast downtime is about to happen.

        # Replication is stopped on the standby, and lag starts
        # increasing.

        # All connections to the primary are killed so database schema
        # updates can be applied.
        self.pgbouncer_cur.execute("DISABLE %s" % self.primary_dbname)
        self.pgbouncer_cur.execute("KILL %s" % self.primary_dbname)

        # Of course, standby connections are unaffected.
        self.assertTrue(self.store_is_working(standby_store))

        # But attempts to use a primary store will fail.
        self.assertFalse(self.store_is_working(primary_store))
        transaction.abort()

        # After schema updates have been made to the primary, it is
        # re-enabled.
        self.pgbouncer_cur.execute("RESUME %s" % self.primary_dbname)
        self.pgbouncer_cur.execute("ENABLE %s" % self.primary_dbname)

        # And the standbys taken down, and replication re-enabled so the
        # schema updates can replicate.
        self.pgbouncer_cur.execute("DISABLE %s" % self.standby_dbname)
        self.pgbouncer_cur.execute("KILL %s" % self.standby_dbname)

        # The primary store is working again.
        primary_store = IPrimaryStore(Person)
        self.assertTrue(self.store_is_primary(primary_store))
        self.assertTrue(self.store_is_working(primary_store))

        # The next attempt at accessing the standby store will fail
        # with a DisconnectionError.
        standby_store = IStandbyStore(Person)
        self.assertTrue(self.store_is_standby(standby_store))
        self.assertRaises(
            DisconnectionError, standby_store.execute, "SELECT TRUE"
        )
        transaction.abort()

        # But if we handle that and retry, we can continue.
        # Now the failed connection has been detected, the next Store
        # we are handed is a primary Store instead of a standby.
        standby_store = IStandbyStore(Person)
        self.assertTrue(self.store_is_primary(standby_store))
        self.assertTrue(self.store_is_working(standby_store))

        # Once replication has caught up, the standby is re-enabled.
        self.pgbouncer_cur.execute("RESUME %s" % self.standby_dbname)
        self.pgbouncer_cur.execute("ENABLE %s" % self.standby_dbname)

        # And next transaction, we are back to normal.
        transaction.abort()
        primary_store = IPrimaryStore(Person)
        self.assertTrue(self.store_is_primary(primary_store))
        self.assertTrue(self.store_is_working(primary_store))

        standby_store = IStandbyStore(Person)
        self.assertTrue(self.store_is_standby(standby_store))
        self.assertTrue(self.store_is_working(standby_store))


class TestStandbyConnectionName(TestCase):
    """The specific standby that was chosen is reflected in its name.

    This allows e.g. OOPS reports to distinguish between multiple standbys
    ("SQL-main-standby-1", "SQL-main-standby-2", ...) rather than reporting
    "SQL-main-standby" for all of them.
    """

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()

        self.primary_dbname = DatabaseLayer._db_fixture.dbname
        # In production, standbys are virtual databases defined in pgbouncer
        # (e.g. "launchpad_prod_standby1 launchpad_prod_standby2"), listed in
        # rw_main_standby as comma-separated connection strings. Mirror that
        # here with two aliases that both point at the same working database.
        self.standby1_dbname = self.primary_dbname + "_standby1"
        self.standby2_dbname = self.primary_dbname + "_standby2"

        self.pgbouncer_fixture = PGBouncerFixture()
        self.pgbouncer_fixture.databases[self.standby1_dbname] = (
            self.pgbouncer_fixture.databases[self.primary_dbname]
        )
        self.pgbouncer_fixture.databases[self.standby2_dbname] = (
            self.pgbouncer_fixture.databases[self.primary_dbname]
        )
        self.useFixture(self.pgbouncer_fixture)

        self.standbys = [
            "dbname=%s host=localhost" % self.standby1_dbname,
            "dbname=%s host=localhost" % self.standby2_dbname,
        ]
        config_key = "multiple-standbys"
        config.push(
            config_key,
            dedent(
                """\
            [database]
            rw_main_primary: dbname=%s host=localhost
            rw_main_standby: %s
            """
                % (self.primary_dbname, ",".join(self.standbys))
            ),
        )
        self.addCleanup(lambda: config.pop(config_key))

        transaction.abort()

    def connect_standby_named(self, chosen):
        """Connect a standby store, forcing the given DSN to be selected."""
        # Force the (normally random) standby selection to be deterministic,
        # and drop existing connections so the new config takes effect.
        disconnect_stores()
        get_property_cache(dbconfig).main_standby = chosen
        store = IStandbyStore(Person)
        # This triggers the raw_connect(), which seeds the main_standby DSN
        store.execute("SELECT TRUE")
        return store

    def test_first_standby_is_numbered_one(self):
        store = self.connect_standby_named(self.standbys[0])
        self.assertEqual("main-standby-1", store.get_database().name)

    def test_second_standby_is_numbered_two(self):
        store = self.connect_standby_named(self.standbys[1])
        self.assertEqual("main-standby-2", store.get_database().name)
