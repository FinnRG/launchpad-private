# Copyright 2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import os.path
import subprocess
from collections import defaultdict

import yaml
from charmhelpers.core import hookenv, host, templating
from charms.launchpad.base import configure_email, get_service_config
from charms.launchpad.db import (
    lazr_config_files,
    strip_dsn_authentication,
    update_pgpass,
)
from charms.launchpad.payload import (
    config_file_path,
    configure_cron,
    configure_lazr,
)
from charms.reactive import (
    endpoint_from_flag,
    helpers,
    remove_state,
    set_state,
    when,
    when_not,
    when_not_all,
)
from ols import base, postgres
from psycopg2.extensions import parse_dsn


def configure_systemd(config):
    hookenv.log("Writing systemd service.")
    templating.render(
        "launchpad-librarian.service.j2",
        "/lib/systemd/system/launchpad-librarian.service",
        config,
    )
    templating.render(
        "launchpad-librarian@.service.j2",
        "/lib/systemd/system/launchpad-librarian@.service",
        config,
    )
    templating.render(
        "launchpad-librarian-generator.j2",
        "/lib/systemd/system-generators/launchpad-librarian-generator",
        config,
        perms=0o555,
    )
    subprocess.run(["systemctl", "daemon-reload"])


def configure_logrotate(config):
    hookenv.log("Writing logrotate configuration.")
    templating.render(
        "logrotate.conf.j2",
        "/etc/logrotate.d/launchpad-librarian",
        config,
        perms=0o644,
    )


def config_files():
    files = []
    files.extend(lazr_config_files())
    files.append(config_file_path("launchpad-librarian/launchpad-lazr.conf"))
    files.append(
        config_file_path("launchpad-librarian-secrets-lazr.conf", secret=True)
    )
    return files


@when(
    "config.set.port_download_base",
    "config.set.port_restricted_download_base",
    "config.set.port_restricted_upload_base",
    "config.set.port_upload_base",
    "launchpad.db.configured",
    "session-db.master.available",
    "launchpad.storage.available",
)
@when_not("service.configured")
def configure():
    session_db = endpoint_from_flag("session-db.master.available")
    config = get_service_config()
    session_db_primary, _ = postgres.get_db_uris(session_db)
    # XXX cjwatson 2022-09-23: Mangle the connection string into a form
    # Launchpad understands.  In the long term it would be better to have
    # Launchpad be able to consume unmodified connection strings.
    update_pgpass(session_db_primary)
    config["db_session"] = strip_dsn_authentication(session_db_primary)
    config["db_session_user"] = parse_dsn(session_db_primary)["user"]
    config["librarian_dir"] = os.path.join(base.base_dir(), "librarian")
    host.mkdir(
        config["librarian_dir"],
        owner=base.user(),
        group=base.user(),
        perms=0o700,
    )
    for i in range(config["workers"]):
        config["logfile"] = os.path.join(
            base.logs_dir(), f"librarian{i + 1}.log"
        )
        config["worker_download_port"] = config["port_download_base"] + i
        config["worker_restricted_download_port"] = (
            config["port_restricted_download_base"] + i
        )
        config["worker_restricted_upload_port"] = (
            config["port_restricted_upload_base"] + i
        )
        config["worker_upload_port"] = config["port_upload_base"] + i
        configure_lazr(
            config,
            "launchpad-librarian-lazr.conf",
            f"launchpad-librarian{i + 1}/launchpad-lazr.conf",
        )
        configure_email(config, f"launchpad-librarian{i + 1}")
    configure_lazr(
        config,
        "launchpad-librarian-secrets-lazr.conf",
        "launchpad-librarian-secrets-lazr.conf",
        secret=True,
    )
    configure_systemd(config)
    configure_logrotate(config)
    configure_cron(config, "crontab.j2")

    if helpers.any_file_changed(
        [
            base.version_info_path(),
            "/lib/systemd/system/launchpad-librarian.service",
            "/lib/systemd/system/launchpad-librarian@.service",
            "/lib/systemd/system-generators/launchpad-librarian-generator",
        ]
        + config_files()
    ):
        hookenv.log("Config files changed; restarting")
        # Be careful to restart instances one at a time to minimize downtime.
        # XXX cjwatson 2023-03-28: This doesn't deal with stopping instances
        # when the worker count is reduced.
        for i in range(config["workers"]):
            host.service_restart(f"launchpad-librarian@{i + 1}")
    else:
        hookenv.log("Not restarting, since no config files were changed")
    host.service_resume("launchpad-librarian.service")

    set_state("service.configured")


@when("service.configured")
def check_is_running():
    hookenv.status_set("active", "Ready")


@when("service.configured")
@when_not_all(
    "config.set.port_download_base",
    "config.set.port_restricted_download_base",
    "config.set.port_restricted_upload_base",
    "config.set.port_upload_base",
    "launchpad.db.configured",
    "session-db.master.available",
)
def deconfigure():
    remove_state("service.configured")


@when("session-db.database.changed", "service.configured")
def session_db_changed():
    remove_state("service.configured")
    remove_state("session-db.database.changed")


@when(
    "config.set.librarian_download_port",
    "config.set.librarian_restricted_download_port",
    "config.set.librarian_restricted_upload_port",
    "config.set.librarian_upload_port",
    "loadbalancer.available",
    "service.configured",
)
@when_not("launchpad.loadbalancer.configured")
def configure_loadbalancer():
    config = hookenv.config()

    try:
        service_options_download = yaml.safe_load(
            config["haproxy_service_options_download"]
        )
    except yaml.YAMLError:
        hookenv.log("Could not parse haproxy_service_options_download YAML")
        hookenv.status_set(
            "blocked",
            "Bad haproxy_service_options_download YAML configuration",
        )
        return
    try:
        service_options_upload = yaml.safe_load(
            config["haproxy_service_options_upload"]
        )
    except yaml.YAMLError:
        hookenv.log("Could not parse haproxy_service_options_upload YAML")
        hookenv.status_set(
            "blocked", "Bad haproxy_service_options_upload YAML configuration"
        )
        return
    server_options = config["haproxy_server_options"]

    unit_name = hookenv.local_unit().replace("/", "-")
    unit_ip = hookenv.unit_private_ip()
    services = [
        {
            # Note that we rename this to
            # "cached-launchpad-librarian-download" in squid-reverseproxy
            # configuration in order that the apache2 charm can distinguish
            # between haproxy and Squid.  (Apache only needs the cached
            # version.)
            "service_name": "launchpad-librarian-download",
            "service_port": config["librarian_download_port"],
            "service_host": "0.0.0.0",
            "service_options": list(service_options_download),
            "servers": [
                [
                    f"dl_{unit_name}_{i + 1}",
                    unit_ip,
                    config["port_download_base"] + i,
                    server_options,
                ]
                for i in range(config["workers"])
            ],
        },
        {
            "service_name": "launchpad-librarian-upload",
            "service_port": config["librarian_upload_port"],
            "service_host": "0.0.0.0",
            "service_options": list(service_options_upload),
            "servers": [
                [
                    f"ul_{unit_name}_{i + 1}",
                    unit_ip,
                    config["port_upload_base"] + i,
                    f"port {config['port_download_base'] + i} "
                    + server_options,
                ]
                for i in range(config["workers"])
            ],
        },
        {
            "service_name": "launchpad-librarian-restricted-download",
            "service_port": config["librarian_restricted_download_port"],
            "service_host": "0.0.0.0",
            "service_options": list(service_options_download),
            "servers": [
                [
                    f"dl_restricted_{unit_name}_{i + 1}",
                    unit_ip,
                    config["port_restricted_download_base"] + i,
                    server_options,
                ]
                for i in range(config["workers"])
            ],
        },
        {
            "service_name": "launchpad-librarian-restricted-upload",
            "service_port": config["librarian_restricted_upload_port"],
            "service_host": "0.0.0.0",
            "service_options": list(service_options_upload),
            "servers": [
                [
                    f"ul_restricted_{unit_name}_{i + 1}",
                    unit_ip,
                    config["port_restricted_upload_base"] + i,
                    f"port {config['port_restricted_download_base'] + i} "
                    + server_options,
                ]
                for i in range(config["workers"])
            ],
        },
    ]
    services_yaml = yaml.dump(services)

    for rel in hookenv.relations_of_type("loadbalancer"):
        hookenv.relation_set(rel["__relid__"], services=services_yaml)

    set_state("launchpad.loadbalancer.configured")


@when("launchpad.loadbalancer.configured")
@when_not_all(
    "config.set.librarian_download_port",
    "config.set.librarian_restricted_download_port",
    "config.set.librarian_restricted_upload_port",
    "config.set.librarian_upload_port",
    "loadbalancer.available",
    "service.configured",
)
def deconfigure_loadbalancer():
    remove_state("launchpad.loadbalancer.configured")


@when(
    "config.set.domain_librarian",
    "vhost-config.available",
    "service.configured",
)
@when_not("launchpad.vhost.configured")
def configure_vhost():
    vhost_config = endpoint_from_flag("vhost-config.available")
    config = dict(hookenv.config())
    config["domain_librarian_aliases"] = yaml.safe_load(
        config["domain_librarian_aliases"]
    )
    config["restricted_domain_librarian"] = (
        f"restricted.{config['domain_librarian']}"
    )
    site_configs = defaultdict(list)
    for vhost_name in ("librarian", "restricted-librarian"):
        for scheme in ("http", "https"):
            site_configs[scheme].append(
                templating.render(
                    f"vhosts/{vhost_name}-{scheme}.conf.j2", None, config
                )
            )
    vhost_config.publish_vhosts(
        [
            vhost_config.make_vhost(80, "\n".join(site_configs["http"])),
            vhost_config.make_vhost(443, "\n".join(site_configs["https"])),
        ]
    )
    set_state("launchpad.vhost.configured")


@when("launchpad.vhost.configured")
@when_not_all(
    "config.set.domain_librarian",
    "vhost-config.available",
    "service.configured",
)
def deconfigure_vhost():
    remove_state("launchpad.vhost.configured")


@when("config.changed.frontend_proxy_protocol_enabled")
def reconfigure_proxy_protocol():
    remove_state("launchpad.vhost.configured")
