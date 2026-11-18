# Copyright 2011-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test error views."""

import http.client
import logging
import re
import socket
import time
from urllib.error import HTTPError

import psycopg2
import transaction
from fixtures import FakeLogger
from storm.exceptions import DisconnectionError, OperationalError
from testtools.content import text_content
from testtools.matchers import AllMatch, Equals, MatchesAny, MatchesListwise
from zope.interface import Interface
from zope.publisher.interfaces.browser import IDefaultBrowserLayer
from zope.testbrowser.wsgi import Browser

from lp.services.webapp.error import (
    DisconnectionErrorView,
    OperationalErrorView,
    SystemErrorView,
)
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.testing import TestCase
from lp.testing.fixture import (
    CaptureOops,
    PGBouncerFixture,
    ZopeAdapterFixture,
)
from lp.testing.layers import DatabaseLayer, LaunchpadFunctionalLayer
from lp.testing.matchers import Contains


class TimeoutException(Exception):
    pass


class TestSystemErrorView(TestCase):
    layer = LaunchpadFunctionalLayer

    def test_without_oops_id(self):
        request = LaunchpadTestRequest()
        SystemErrorView(Exception(), request)
        self.assertEqual(500, request.response.getStatus())
        self.assertIsNone(
            request.response.getHeader("X-Lazr-OopsId", literal=True)
        )

    def test_with_oops_id(self):
        request = LaunchpadTestRequest()
        request.oopsid = "OOPS-1X1"
        SystemErrorView(Exception(), request)
        self.assertEqual(500, request.response.getStatus())
        self.assertEqual(
            "OOPS-1X1",
            request.response.getHeader("X-Lazr-OopsId", literal=True),
        )


class TestDatabaseErrorViews(TestCase):
    layer = LaunchpadFunctionalLayer

    def getHTTPError(self, url):
        try:
            Browser().open(url)
        except HTTPError as error:
            return error
        else:
            self.fail("We should have gotten an HTTP error")

    def add_retry_failure_details(self, bouncer):
        # XXX benji bug=974617, bug=1011847, bug=504291 2011-07-31:
        # This method (and its invocations) are to be removed when we have
        # figured out what is causing bug 974617 and friends.

        # First we figure out if pgbouncer is listening on the port it is
        # supposed to be listening on.  connect_ex returns 0 on success or an
        # errno otherwise.
        pg_port_status = str(socket.socket().connect_ex(("localhost", 5432)))
        self.addDetail(
            "postgres socket.connect_ex result", text_content(pg_port_status)
        )
        bouncer_port_status = str(
            socket.socket().connect_ex(("localhost", bouncer.port))
        )
        self.addDetail(
            "pgbouncer socket.connect_ex result",
            text_content(bouncer_port_status),
        )

    def retryConnection(self, url, bouncer, retries=60):
        """Retry to connect to *url* for *retries* times.

        Raise a TimeoutException if the connection cannot be established.
        """
        browser = Browser()
        for _ in range(retries):
            try:
                browser.open(url)
                return
            except HTTPError as e:
                if e.code != http.client.SERVICE_UNAVAILABLE:
                    raise
            time.sleep(1)
        else:
            self.add_retry_failure_details(bouncer)
            raise TimeoutException(
                f"Launchpad did not come up after {retries} attempts."
            )

    def test_disconnectionerror_view_integration(self):
        # Test setup.
        self.useFixture(FakeLogger("SiteError", level=logging.CRITICAL))
        bouncer = PGBouncerFixture()
        # XXX gary bug=974617, bug=1011847, bug=504291 2011-07-03:
        # In parallel tests, we are rarely encountering instances of
        # bug 504291 while running this test.  These cause the tests
        # to fail entirely (the store.rollback() described in comment
        # 11 does not fix the insane state) despite nultiple retries.
        # As mentioned in that bug, we are trying aborts to see if they
        # eliminate the problem.  If this works, we can find which of
        # these two aborts are actually needed.
        transaction.abort()
        self.useFixture(bouncer)
        transaction.abort()
        # Verify things are working initially.
        url = "http://launchpad.test/"
        self.retryConnection(url, bouncer)
        # Now break the database, and we get an exception, along with
        # our view and several OOPSes from the retries.
        bouncer.stop()

        class Disconnects(Equals):
            def __init__(self, message):
                super().__init__(("DisconnectionError", message))

        class DisconnectsWithMessageRegex:
            def __init__(self, message_regex):
                self.message_regex = message_regex

            def match(self, actual):
                if "DisconnectionError" != actual[0] or not re.match(
                    self.message_regex, actual[1]
                ):

                    class DisconnectsWithMessageRegexMismatch:
                        def __init__(self, description):
                            self.description = description

                        def describe(self):
                            return self.description

                    return DisconnectsWithMessageRegexMismatch(
                        "reference = ('DisconnectionError', "
                        f"'{self.message_regex}')\n"
                        f"actual    = ('{actual[0]}', '{actual[1]}')"
                    )

        browser = Browser()
        browser.raiseHttpErrors = False
        with CaptureOops() as oopses:
            browser.open(url)
        self.assertEqual(503, int(browser.headers["Status"].split(" ", 1)[0]))
        self.assertThat(
            browser.contents, Contains(DisconnectionErrorView.reason)
        )
        # XXX 2024-01-22 lgp171188: Since there isn't a straightforward
        # way to query the Postgres version at test runtime and assert
        # accordingly, I have added to the existing style of `MatchesAny`
        # clauses for Postgres 14 support. Once we upgrade to Postgres,
        # all the code and assertions for older versions can be removed.
        libpq_14_connection_error_prefix_regex = (
            r'connection to server at "localhost" \(.*\), port .* failed'
        )
        self.assertThat(
            [
                (oops["type"], oops["value"].split("\n")[0])
                for oops in oopses.oopses
            ],
            MatchesListwise(
                [
                    MatchesAny(
                        # libpq < 9.5.
                        Disconnects("error with no message from the libpq"),
                        # libpq >= 9.5.
                        Disconnects(
                            "server closed the connection unexpectedly"
                        ),
                    )
                ]
                * 2
                + [
                    MatchesAny(
                        # libpq < 14.0
                        Disconnects(
                            "could not connect to server: Connection refused"
                        ),
                        # libpq >= 14.0
                        DisconnectsWithMessageRegex(
                            libpq_14_connection_error_prefix_regex
                            + ": Connection refused"
                        ),
                    ),
                ]
                * 6
            ),
        )

        # We keep seeing the correct exception on subsequent requests.
        with CaptureOops() as oopses:
            browser.open(url)
        self.assertEqual(503, int(browser.headers["Status"].split(" ", 1)[0]))
        self.assertThat(
            browser.contents, Contains(DisconnectionErrorView.reason)
        )
        self.assertThat(
            [
                (oops["type"], oops["value"].split("\n")[0])
                for oops in oopses.oopses
            ],
            MatchesListwise(
                [
                    MatchesAny(
                        # libpa < 14.0
                        Disconnects(
                            "could not connect to server: Connection refused"
                        ),
                        # libpq >= 14.0
                        DisconnectsWithMessageRegex(
                            libpq_14_connection_error_prefix_regex
                            + ": Connection refused"
                        ),
                    )
                ]
                * 8
            ),
        )

        # When the database is available again, requests succeed.
        bouncer.start()
        self.retryConnection(url, bouncer)

        # If we ask pgbouncer to disable the database, requests fail and
        # get the same error page, but we don't log OOPSes except for
        # the initial connection terminations. Disablement is always
        # explicit maintenance, and we don't need lots of ongoing OOPSes
        # to tell us about maintenance that we're doing.
        dbname = DatabaseLayer._db_fixture.dbname
        conn = psycopg2.connect("dbname=pgbouncer")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("DISABLE " + dbname)
        cur.execute("KILL " + dbname)
        cur.execute("RESUME " + dbname)

        with CaptureOops() as oopses:
            browser.open(url)
        self.assertEqual(503, int(browser.headers["Status"].split(" ", 1)[0]))
        self.assertThat(
            browser.contents, Contains(DisconnectionErrorView.reason)
        )
        disconnection_oopses = [
            (oops["type"], oops["value"].split("\n")[0])
            for oops in oopses.oopses
        ]
        self.assertNotEqual([], disconnection_oopses)
        self.assertThat(
            disconnection_oopses,
            AllMatch(
                MatchesAny(
                    # libpq < 14.0
                    Disconnects("database removed"),
                    # libpq ~= 14.0
                    DisconnectsWithMessageRegex(
                        libpq_14_connection_error_prefix_regex
                        + ": ERROR: database does not allow connections: "
                        r"launchpad_ftest_.*"
                    ),
                    # libpq ~= 16.0
                    Disconnects("server closed the connection unexpectedly"),
                    DisconnectsWithMessageRegex(
                        libpq_14_connection_error_prefix_regex
                        + r': FATAL:\s+database "launchpad_ftest_.*" '
                        + r"is disabled$"
                    ),
                )
            ),
        )

        # A second request doesn't log any OOPSes.
        with CaptureOops() as oopses:
            browser.open(url)
        self.assertEqual(503, int(browser.headers["Status"].split(" ", 1)[0]))
        self.assertThat(
            browser.contents, Contains(DisconnectionErrorView.reason)
        )
        self.assertEqual(
            [],
            [
                (oops["type"], oops["value"].split("\n")[0])
                for oops in oopses.oopses
            ],
        )

        # When the database is available again, requests succeed.
        cur.execute("ENABLE %s" % DatabaseLayer._db_fixture.dbname)
        self.retryConnection(url, bouncer)

    def test_disconnectionerror_view(self):
        request = LaunchpadTestRequest()
        DisconnectionErrorView(DisconnectionError(), request)
        self.assertEqual(503, request.response.getStatus())

    def test_operationalerror_view_integration(self):
        # Test setup.
        self.useFixture(FakeLogger("SiteError", level=logging.CRITICAL))

        class BrokenView:
            """A view that raises an OperationalError"""

            def __call__(self, *args, **kw):
                raise OperationalError()

        self.useFixture(
            ZopeAdapterFixture(
                BrokenView(),
                (None, IDefaultBrowserLayer),
                Interface,
                "error-test",
            )
        )

        url = "http://launchpad.test/error-test"
        browser = Browser()
        browser.raiseHttpErrors = False
        browser.open(url)
        self.assertEqual(
            http.client.SERVICE_UNAVAILABLE,
            int(browser.headers["Status"].split(" ", 1)[0]),
        )
        self.assertThat(
            browser.contents, Contains(OperationalErrorView.reason)
        )

    def test_operationalerror_view(self):
        request = LaunchpadTestRequest()
        OperationalErrorView(OperationalError(), request)
        self.assertEqual(503, request.response.getStatus())
