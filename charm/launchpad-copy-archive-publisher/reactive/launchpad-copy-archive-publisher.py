# Copyright 2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import os

import yaml
from charmhelpers.core import hookenv, host, templating
from charms.launchpad.base import configure_email, get_service_config
from charms.launchpad.payload import configure_cron, configure_lazr
from charms.launchpad.publisher_parts import publisher_parts_dir
from charms.reactive import (
    clear_flag,
    endpoint_from_flag,
    set_flag,
    when,
    when_not,
    when_not_all,
)
from ols import base


def get_data_dir():
    return os.path.join(base.base_dir(), "data")


def archives_dir():
    return os.path.join(get_data_dir(), "archives")


def rebuilds_dir():
    return os.path.join(get_data_dir(), "rebuilds")


def configure_logrotate(config):
    hookenv.log("Writing logrotate configuration.")
    templating.render(
        source="logrotate.conf.j2",
        target="/etc/logrotate.d/launchpad",
        context=config,
        perms=0o644,
    )


def configure_copy_publish_archives_cronjob(config):
    hookenv.log("Setting up launchpad-copy-archives cronjob")
    templating.render(
        source="cron.launchpad-copy-archives.sh.j2",
        target=os.path.join(
            base.base_dir(), "bin", "cron.publish-copy-archives.sh"
        ),
        context=config,
        perms=0o755,
    )
    host.mkdir(
        f"{base.base_dir()}/rebuild-test", group=base.user(), perms=0o775
    )


@when(
    "launchpad.db.configured",
    "launchpad.publisher-parts.configured",
    "launchpad.storage.available",
)
@when_not("service.configured")
def configure():
    hookenv.log("Configuring copy-archive-publisher")
    config = get_service_config()
    config["archives_dir"] = archives_dir()
    config["run_parts_location"] = publisher_parts_dir()

    host.mkdir(
        archives_dir(), owner=base.user(), group=base.user(), perms=0o775
    )
    host.mkdir(
        rebuilds_dir(), owner=base.user(), group=base.user(), perms=0o775
    )

    configure_lazr(
        config,
        template="launchpad-copy-archive-publisher-lazr.conf.j2",
        name="launchpad-copy-archive-publisher/launchpad-lazr.conf",
    )
    configure_lazr(
        config,
        template="launchpad-copy-archive-publisher-secrets-lazr.conf.j2",
        name="launchpad-copy-archive-publisher-secrets-lazr.conf",
        secret=True,
    )
    configure_email(config, "launchpad-copy-archive-publisher")
    configure_logrotate(config)
    configure_copy_publish_archives_cronjob(config)
    configure_cron(config, "crontab.j2")
    set_flag("service.configured")


@when("service.configured")
def check_is_running():
    hookenv.status_set("active", "Ready")


@when("service.configured")
@when_not_all(
    "launchpad.db.configured",
    "launchpad.publisher-parts.configured",
)
def deconfigure():
    clear_flag("service.configured")


@when("apache-website.available", "service.configured")
@when_not("service.apache-website.configured")
def configure_apache_website():
    apache_website = endpoint_from_flag("apache-website.available")
    config = dict(hookenv.config())
    config["archives_dir"] = archives_dir()
    config["rebuilds_dir"] = rebuilds_dir()
    config["domain_test_rebuild_aliases"] = yaml.safe_load(
        config["domain_test_rebuild_aliases"]
    )

    vhost_names = ["derived", "rebuild-test"]
    site_configs = []
    for vhost_name in vhost_names:
        site_configs.append(
            templating.render(f"vhosts/{vhost_name}.conf.j2", None, config)
        )

    apache_website.set_remote(
        domain=config["domain"],
        enabled="true",
        ports="80",
        site_config="\n".join(site_configs),
    )
    set_flag("service.apache-website.configured")


@when("service.apache-website.configured")
@when_not_all("apache-website.available", "service.configured")
def deconfigure_apache_website():
    clear_flag("service.apache-website.configured")


@when("config.changed.frontend_proxy_protocol_enabled")
def reconfigure_proxy_protocol():
    clear_flag("service.apache-website.configured")
