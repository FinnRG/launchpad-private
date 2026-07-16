# Copyright 2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import os.path

import yaml
from charmhelpers.core import hookenv, host, templating
from charms.launchpad.base import get_service_config
from charms.launchpad.payload import configure_cron, configure_lazr
from charms.reactive import remove_state, set_state, when, when_not
from ols import base


def configure_logrotate(config):
    hookenv.log("Writing logrotate configuration.")
    templating.render(
        "logrotate.conf.j2",
        "/etc/logrotate.d/launchpad-debian-importer",
        config,
        perms=0o644,
    )


@when(
    "launchpad.db.configured",
    "launchpad.storage.available",
)
@when_not("service.configured")
def configure():
    config = get_service_config()
    config["debian_suites"] = yaml.safe_load(config["debian_suites"])
    config["debian_components"] = []
    for components in config["debian_suites"].values():
        for component in components:
            if component not in config["debian_components"]:
                config["debian_components"].append(component)
    config["mirror_dir"] = os.path.join(base.base_dir(), "mirror")
    host.mkdir(
        config["mirror_dir"], owner=base.user(), group=base.user(), perms=0o755
    )
    config["scripts_dir"] = os.path.join(base.base_dir(), "scripts")
    host.mkdir(config["scripts_dir"], perms=0o755)
    templating.render(
        "mirror-update.sh.j2",
        os.path.join(config["scripts_dir"], "mirror-update.sh"),
        config,
        perms=0o755,
    )
    configure_lazr(
        config,
        "launchpad-debian-importer-lazr.conf",
        "launchpad-debian-importer/launchpad-lazr.conf",
    )
    configure_logrotate(config)
    configure_cron(config, "crontab.j2")

    set_state("service.configured")


@when("service.configured")
def check_is_running():
    hookenv.status_set("active", "Ready")


@when("service.configured")
@when_not("launchpad.db.configured")
def deconfigure():
    remove_state("service.configured")
