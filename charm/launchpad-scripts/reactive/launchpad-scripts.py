# Copyright 2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import subprocess

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
    home_dir,
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

CHARM_CELERY_SERVICES = [
    "celerybeat_launchpad",
    "celeryd_launchpad_job",
    "celeryd_launchpad_job_slow",
]
CHARM_SYSTEMD_SERVICES = CHARM_CELERY_SERVICES + [
    "number-cruncher",
]


def configure_logrotate(config):
    hookenv.log("Writing logrotate configuration.")
    templating.render(
        "logrotate.conf.j2",
        "/etc/logrotate.d/launchpad",
        config,
        perms=0o644,
    )


def config_files():
    files = []
    files.extend(lazr_config_files())
    files.append(config_file_path("launchpad-scripts/launchpad-lazr.conf"))
    files.append(
        config_file_path("launchpad-scripts-secrets-lazr.conf", secret=True)
    )
    return files


@host.restart_on_change(
    {
        "/lib/systemd/system/celerybeat_launchpad.service": [
            "celerybeat_launchpad",
        ],
        "/lib/systemd/system/celeryd_launchpad_job.service": [
            "celeryd_launchpad_job",
        ],
        "/lib/systemd/system/celeryd_launchpad_job_slow.service": [
            "celeryd_launchpad_job_slow",
        ],
    }
)
def configure_celery(config):
    hookenv.log("Writing celery systemd service files.")
    destination_dir = "/lib/systemd/system"
    for service in CHARM_CELERY_SERVICES:
        templating.render(
            f"{service}.service.j2",
            f"{destination_dir}/{service}.service",
            config,
        )
    subprocess.check_call(["systemctl", "daemon-reload"])


@host.restart_on_change(
    {"/lib/systemd/system/number-cruncher.service": ["number-cruncher"]}
)
def configure_number_cruncher(config):
    hookenv.log("Writing the number-cruncher systemd service file.")
    templating.render(
        "number-cruncher.service.j2",
        "/lib/systemd/system/number-cruncher.service",
        config,
    )
    subprocess.check_call(["systemctl", "daemon-reload"])


def perform_action_on_services(services, action):
    for service in services:
        action(service)


def configure_librarian_logs_sync(config):
    hookenv.log("Writing the librarian logs sync script.")
    config["librarian_frontend_ip_addresses"] = yaml.safe_load(
        config["librarian_frontend_ip_addresses"]
    )
    host.mkdir(
        config["scripts_dir"],
        owner=base.user(),
        group=base.user(),
        perms=0o755,
        force=True,
    )
    host.mkdir(
        config["librarian_logs_dir"],
        owner=base.user(),
        group=base.user(),
        perms=0o755,
        force=True,
    )
    templating.render(
        "sync-librarian-logs.j2",
        f"{config['scripts_dir']}/sync-librarian-logs",
        config,
        perms=0o755,
    )


def configure_process_inbound_email(config):
    if (
        config["process_inbound_email_host"]
        and config["process_inbound_email_username"]
        and config["process_inbound_email_password"]
    ):
        hookenv.log("Writing the email configuration for process-mail.py")
        code_dir = base.code_dir()
        templating.render(
            "inbound-email-configure.zcml.j2",
            f"{code_dir}/zcml/override-includes/inbound-email-configure.zcml",
            config,
            perms=0o644,
        )


@when(
    "launchpad.db.configured",
    "session-db.master.available",
    "memcache.available",
    "launchpad.storage.available",
)
@when_not("service.configured")
def configure():
    session_db = endpoint_from_flag("session-db.master.available")
    memcache = endpoint_from_flag("memcache.available")
    config = get_service_config()
    session_db_primary, _ = postgres.get_db_uris(session_db)
    # XXX cjwatson 2022-09-23: Mangle the connection string into a form
    # Launchpad understands.  In the long term it would be better to have
    # Launchpad be able to consume unmodified connection strings.
    update_pgpass(session_db_primary)
    config["db_session"] = strip_dsn_authentication(session_db_primary)
    config["db_session_user"] = parse_dsn(session_db_primary)["user"]
    config["memcache_servers"] = ",".join(
        sorted(
            f"({host}:{port},1)"
            for host, port in memcache.memcache_hosts_ports()
        )
    )
    config["librarian_logs_dir"] = f"{base.base_dir()}/launchpadlibrarian-logs"
    configure_lazr(
        config,
        "launchpad-scripts-lazr.conf.j2",
        "launchpad-scripts/launchpad-lazr.conf",
    )
    config["checkwatches_credentials"] = yaml.safe_load(
        config["checkwatches_credentials"]
    )
    configure_lazr(
        config,
        "launchpad-scripts-secrets-lazr.conf.j2",
        "launchpad-scripts-secrets-lazr.conf",
        secret=True,
    )
    configure_email(config, "launchpad-scripts")
    configure_logrotate(config)
    config["scripts_dir"] = f"{home_dir()}/scripts"
    configure_librarian_logs_sync(config)
    config["language_pack_exporter_schedule"] = yaml.safe_load(
        config["language_pack_exporter_schedule"]
    )
    configure_process_inbound_email(config)
    # The checkwatches script logs to a 'checkwatches' sub-directory.
    host.mkdir(
        f"{base.logs_dir()}/checkwatches", group=base.user(), perms=0o775
    )
    # The debbugs-mirror directory is a mounted volume used by the
    # debbugs-sync and checkwatches scripts.
    host.mkdir(
        f"{config['var_dir']}/debbugs-mirror",
        owner=base.user(),
        group=base.user(),
        perms=0o775,
    )
    configure_cron(config, "crontab.j2")
    configure_celery(config)
    configure_number_cruncher(config)

    if config["active"]:
        if helpers.any_file_changed(
            [
                base.version_info_path(),
            ]
            + config_files()
        ):
            hookenv.log("Config files or payload changed; restarting services")
            perform_action_on_services(
                CHARM_SYSTEMD_SERVICES, host.service_restart
            )
        perform_action_on_services(CHARM_SYSTEMD_SERVICES, host.service_resume)
    else:
        perform_action_on_services(CHARM_SYSTEMD_SERVICES, host.service_pause)

    set_state("service.configured")


@when("service.configured")
def check_is_running():
    hookenv.status_set("active", "Ready")


@when("service.configured")
@when_not_all(
    "launchpad.db.configured",
    "session-db.master.available",
    "memcache.available",
)
def deconfigure():
    remove_state("service.configured")


@when("session-db.database.changed", "service.configured")
def session_db_changed():
    remove_state("service.configured")
    remove_state("session-db.database.changed")
