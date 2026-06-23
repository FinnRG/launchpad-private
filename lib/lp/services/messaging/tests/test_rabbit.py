# Copyright 2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from kombu.utils.url import parse_url
from testtools.matchers import MatchesStructure

from lp.services.config import config
from lp.services.messaging import rabbit
from lp.testing import TestCase
from lp.testing.layers import BaseLayer, RabbitMQLayer


class TestIsConfigured(TestCase):
    layer = BaseLayer

    def test_unconfigured(self):
        self.assertFalse(rabbit.is_configured())

    def test_broker_url(self):
        self.pushConfig(
            "rabbitmq", broker_urls="amqp://guest:guest@rabbitmq.example//"
        )
        self.assertTrue(rabbit.is_configured())

    def test_partial_compat(self):
        self.pushConfig("rabbitmq", host="rabbitmq.example")
        self.assertFalse(rabbit.is_configured())

    def test_full_compat(self):
        self.pushConfig(
            "rabbitmq",
            host="rabbitmq.example",
            userid="guest",
            password="guest",
            virtual_host="/",
        )
        self.assertTrue(rabbit.is_configured())


class TestConnect(TestCase):
    layer = RabbitMQLayer

    def test_unconfigured(self):
        self.pushConfig("rabbitmq", broker_urls="none")
        self.assertRaisesWithContent(
            rabbit.MessagingUnavailable,
            "Incomplete configuration",
            rabbit.connect,
        )

    def test_single_broker_url(self):
        self.assertIsNotNone(config.rabbitmq.broker_urls)
        [broker_url] = config.rabbitmq.broker_urls.split()
        parsed_url = parse_url(broker_url)
        with rabbit.connect() as connection:
            self.assertThat(
                connection,
                MatchesStructure.byEquality(
                    # kombu.transport.pyamqp forces "localhost" to "127.0.0.1".
                    hostname="127.0.0.1",
                    userid=parsed_url["userid"],
                    password=parsed_url["password"],
                    virtual_host=parsed_url["virtual_host"],
                    port=int(parsed_url["port"]),
                    alt=[broker_url],
                ),
            )

    def test_multiple_broker_urls(self):
        self.assertIsNotNone(config.rabbitmq.broker_urls)
        [broker_url] = config.rabbitmq.broker_urls.split()
        parsed_url = parse_url(broker_url)
        self.assertEqual("localhost", parsed_url["hostname"])
        self.pushConfig(
            "rabbitmq",
            broker_urls=(
                "%s amqp://guest:guest@alternate.example//" % broker_url
            ),
        )
        with rabbit.connect() as connection:
            self.assertThat(
                connection,
                MatchesStructure.byEquality(
                    # kombu.transport.pyamqp forces "localhost" to "127.0.0.1".
                    hostname="127.0.0.1",
                    userid=parsed_url["userid"],
                    password=parsed_url["password"],
                    virtual_host=parsed_url["virtual_host"],
                    port=int(parsed_url["port"]),
                    alt=[broker_url, "amqp://guest:guest@alternate.example//"],
                ),
            )

    def test_compat_config(self):
        # The old-style host/userid/password/virtual_host configuration
        # format still works.
        self.assertIsNotNone(config.rabbitmq.broker_urls)
        [broker_url] = config.rabbitmq.broker_urls.split()
        parsed_url = parse_url(broker_url)
        self.assertEqual("localhost", parsed_url["hostname"])
        self.pushConfig(
            "rabbitmq",
            broker_urls="none",
            host="%s:%s" % (parsed_url["hostname"], parsed_url["port"]),
            userid=parsed_url["userid"],
            password=parsed_url["password"],
            virtual_host=parsed_url["virtual_host"],
        )
        with rabbit.connect() as connection:
            self.assertThat(
                connection,
                MatchesStructure.byEquality(
                    # kombu.transport.pyamqp forces "localhost" to "127.0.0.1".
                    hostname="127.0.0.1",
                    userid=parsed_url["userid"],
                    password=parsed_url["password"],
                    virtual_host=parsed_url["virtual_host"],
                    port=int(parsed_url["port"]),
                    alt=[],
                ),
            )

    def test_explicit_broker_urls(self):
        # Passing broker_urls explicitly (e.g. to publish OOPS reports to a
        # separate cluster) overrides config.rabbitmq.broker_urls.
        self.assertIsNotNone(config.rabbitmq.broker_urls)
        [broker_url] = config.rabbitmq.broker_urls.split()
        parsed_url = parse_url(broker_url)
        with rabbit.connect(
            broker_urls=(
                "%s amqp://guest:guest@alternate.example//" % broker_url
            )
        ) as connection:
            self.assertThat(
                connection,
                MatchesStructure.byEquality(
                    # kombu.transport.pyamqp forces "localhost" to "127.0.0.1".
                    hostname="127.0.0.1",
                    userid=parsed_url["userid"],
                    password=parsed_url["password"],
                    virtual_host=parsed_url["virtual_host"],
                    port=int(parsed_url["port"]),
                    alt=[broker_url, "amqp://guest:guest@alternate.example//"],
                ),
            )
