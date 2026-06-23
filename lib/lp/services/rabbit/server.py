# Copyright 2011 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""RabbitMQ server fixture."""

__all__ = [
    "RabbitServer",
]

from textwrap import dedent

import rabbitfixture.server


class RabbitServer(rabbitfixture.server.RabbitServer):
    """A RabbitMQ server fixture with Launchpad-specific config.

    :ivar service_config: A snippet of .ini that describes the `rabbitmq`
        configuration.
    """

    def setUp(self):
        super().setUp()
        # The two trailing slashes here are deliberate: this has the effect
        # of setting the virtual host to "/" rather than to the empty
        # string.
        self.config.service_config = dedent(
            """\
            [rabbitmq]
            broker_urls: amqp://guest:guest@localhost:%d//
            oops_broker_urls: amqp://guest:guest@localhost:%d//
            """
            % (self.config.port, self.config.port)
        )
