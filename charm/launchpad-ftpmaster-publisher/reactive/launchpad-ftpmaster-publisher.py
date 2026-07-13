# Copyright 2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import os.path

from charmhelpers.core import hookenv, host, templating
from charms.launchpad.base import configure_email, get_service_config
from charms.launchpad.payload import configure_cron, configure_lazr
from charms.launchpad.publisher_parts import publisher_parts_dir
from charms.reactive import (
    endpoint_from_flag,
    remove_state,
    set_state,
    when,
    when_not,
    when_not_all,
)
from ols import base


def archives_dir():
    return os.path.join(base.base_dir(), "archives")


def configure_logrotate(config):
    hookenv.log("Writing logrotate configuration.")
    templating.render(
        "logrotate.conf.j2",
        "/etc/logrotate.d/launchpad",
        config,
        perms=0o644,
    )


@when(
    "launchpad.db.configured",
    "launchpad.publisher-parts.configured",
    "launchpad.storage.available",
)
@when_not("service.configured")
def configure():
    hookenv.log("Configuring ftpmaster publisher")
    config = get_service_config()
    config["archives_dir"] = archives_dir()
    host.mkdir(
        archives_dir(), owner=base.user(), group=base.user(), perms=0o755
    )
    config["run_parts_location"] = publisher_parts_dir()

    configure_lazr(
        config,
        "launchpad-ftpmaster-publisher-lazr.conf.j2",
        "launchpad-ftpmaster-publisher/launchpad-lazr.conf",
    )
    configure_lazr(
        config,
        "launchpad-ftpmaster-publisher-secrets-lazr.conf.j2",
        "launchpad-ftpmaster-publisher-secrets-lazr.conf",
        secret=True,
    )
    configure_email(config, "launchpad-ftpmaster-publisher")
    configure_logrotate(config)
    configure_cron(config, "crontab.j2")
    if config["rsync_secrets"]:
        rsync_secrets_path = "/etc/rsyncd/ftp.secrets"
        templating.render(
            "ftp.secrets.j2",
            rsync_secrets_path,
            config,
            perms=0o640,
        )
        config["rsync_secrets_path"] = rsync_secrets_path
    elif os.path.exists("/etc/rsyncd/ftp.secrets"):
        os.unlink("/etc/rsyncd/ftp.secrets")
    if (
        config["ubuntu_auth_users"]
        and config["ubuntu_partner_auth_users"]
        and config["ubuntu_dists_hosts_allow"]
        and config["ubuntu_germinate_hosts_allow"]
        and config["rsync_secrets_path"]
    ):
        templating.render(
            "020-launchpad-ftpmaster-publisher.conf.j2",
            "/etc/rsync-juju.d/020-launchpad-ftpmaster-publisher.conf",
            config,
            perms=0o644,
        )
    elif os.path.exists(
        "/etc/rsync-juju.d/020-launchpad-ftpmaster-publisher.conf"
    ):
        os.unlink("/etc/rsync-juju.d/020-launchpad-ftpmaster-publisher.conf")
    set_state("service.configured")


@when("service.configured")
def check_is_running():
    hookenv.status_set("active", "Ready")


@when("service.configured")
@when_not_all(
    "launchpad.db.configured",
    "launchpad.publisher-parts.configured",
)
def deconfigure():
    remove_state("service.configured")


@when("apache-website.available", "service.configured")
@when_not("service.apache-website.configured")
def configure_apache_website():
    apache_website = endpoint_from_flag("apache-website.available")
    config = dict(hookenv.config())
    config["archives_dir"] = archives_dir()
    apache_website.set_remote(
        domain=config["domain_ftpmaster"],
        enabled="true",
        ports="80",
        site_config=templating.render("vhost.conf.j2", None, config),
    )
    set_state("service.apache-website.configured")


@when("service.apache-website.configured")
@when_not_all("apache-website.available", "service.configured")
def deconfigure_apache_website():
    remove_state("service.apache-website.configured")
