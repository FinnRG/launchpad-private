# Copyright 2011-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""An API for messaging systems in Launchpad, e.g. RabbitMQ."""

__all__ = [
    "connect",
    "is_configured",
]

import kombu
from lazr.config import as_host_port

from lp.services.config import config
from lp.services.messaging.interfaces import MessagingUnavailable


def is_configured():
    """Return True if rabbit looks to be configured."""
    if config.rabbitmq.broker_urls is not None:
        return True
    return not (
        config.rabbitmq.host is None
        or config.rabbitmq.userid is None
        or config.rabbitmq.password is None
        or config.rabbitmq.virtual_host is None
    )


def connect(broker_urls=None):
    """Connect to AMQP if possible.

    :param broker_urls: An optional space-separated string of broker URLs to
        connect to instead of the default ``config.rabbitmq.broker_urls``.
    :raises MessagingUnavailable: If the configuration is incomplete.
    """
    if not is_configured():
        raise MessagingUnavailable("Incomplete configuration")
    if broker_urls is not None:
        connection = kombu.Connection(broker_urls.split())
    elif config.rabbitmq.broker_urls is not None:
        connection = kombu.Connection(config.rabbitmq.broker_urls.split())
    else:
        hostname, port = as_host_port(config.rabbitmq.host, default_port=5672)
        connection = kombu.Connection(
            hostname=hostname,
            userid=config.rabbitmq.userid,
            password=config.rabbitmq.password,
            virtual_host=config.rabbitmq.virtual_host,
            port=port,
        )
    connection.connect()
    return connection
