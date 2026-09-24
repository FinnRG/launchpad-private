# Copyright 2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import os
import subprocess
from pathlib import Path

from charmhelpers.core import hookenv, host, templating
from charms.reactive import (
    clear_flag,
    endpoint_from_flag,
    hook,
    set_flag,
    when,
    when_not,
    when_not_all,
)


def base_dir():
    return "/srv/launchpad"


def install_files(source_path: os.PathLike, target_path: os.PathLike):
    hookenv.log(f"Installing files to {target_path}")
    subprocess.run(
        [
            "rsync",
            "-av",
            "--delete",
            "--exclude",
            "offline.html",
            "--exclude",
            "offline-haproxy.html",
            f"{source_path}/",
            f"{target_path}/",
        ],
        check=True,
    )


def update_offline_symlinks(www_path: os.PathLike):
    """Update offline.html and offline-haproxy.html symlinks.

    These are used as error pages by Apache and haproxy when backends are
    unavailable.
    """
    offline_mode = hookenv.config()["offline_mode"]
    hookenv.log(f"Offline mode is '{offline_mode}'")
    for suffix in ("", "-haproxy"):
        offline_link_path = www_path / f"offline{suffix}.html"
        offline_temp_link_path = www_path / f"offline{suffix}.html.new"
        offline_target_path = www_path / f"offline-{offline_mode}{suffix}.html"
        if not offline_target_path.exists():
            offline_target_path = www_path / f"offline-unplanned{suffix}.html"

        if offline_temp_link_path.exists():
            offline_temp_link_path.unlink()
        offline_temp_link_path.symlink_to(offline_target_path.name)
        offline_temp_link_path.rename(offline_link_path)


@when_not("service.configured")
def configure():
    host.mkdir(base_dir())
    install_files(
        Path(hookenv.charm_dir()) / "files" / "generic",
        Path(base_dir()) / "www",
    )
    install_files(
        Path(hookenv.charm_dir()) / "files" / "media",
        Path(base_dir()) / "www-media",
    )
    update_offline_symlinks(Path(base_dir()) / "www")
    set_flag("service.configured")


@when("service.configured")
def check_is_running():
    hookenv.status_set("active", "Ready")


@hook("upgrade-charm")
def upgrade_charm():
    clear_flag("service.configured")


@when("config.changed")
def config_changed():
    clear_flag("service.configured")


@when("apache-website.available", "service.configured")
@when_not("service.apache-website.configured")
def configure_apache_website():
    apache_website = endpoint_from_flag("apache-website.available")
    config = dict(hookenv.config())
    config["base_dir"] = base_dir()
    vhost_names = ["www"]
    if config["enable_media"]:
        vhost_names.append("media")
    if config["redirect_edge"]:
        vhost_names.append("edge")
    if config["redirect_features"]:
        vhost_names.append("features")
    if config["redirect_librarian"]:
        vhost_names.append("librarian")

    site_configs = []
    site_configs.append(f"KeepAliveTimeout {config['keepalive_timeout']}")
    for vhost_name in vhost_names:
        for scheme in ("http", "https"):
            site_configs.append(
                templating.render(
                    f"vhosts/{vhost_name}-{scheme}.conf.j2", None, config
                )
            )

    apache_website.set_remote(
        domain=config["domain"],
        enabled="true",
        ports="80 443",
        site_config="\n".join(site_configs),
        site_modules="rewrite ssl",
    )
    set_flag("service.apache-website.configured")


@when("service.apache-website.configured")
@when_not_all("apache-website.available", "service.configured")
def deconfigure_apache_website():
    clear_flag("service.apache-website.configured")


_rsync_path = "/etc/rsync-juju.d/020-librarian-logs.conf"


@when("librarian-logs.connected", "service.configured")
@when_not("service.librarian-logs.configured")
def configure_librarian_logs():
    config = dict(hookenv.config())
    if config["librarian_log_hosts_allow"]:
        hookenv.log("Writing librarian-logs rsync configuration.")
        templating.render(
            "librarian-logs-rsync.conf.j2", _rsync_path, config, perms=0o644
        )
    elif os.path.exists(_rsync_path):
        os.unlink(_rsync_path)
    set_flag("service.librarian-logs.configured")


@when("service.librarian-logs.configured")
@when_not_all("librarian-logs.connected", "service.configured")
def deconfigure_librarian_logs():
    if os.path.exists(_rsync_path):
        os.unlink(_rsync_path)
    clear_flag("service.librarian-logs.configured")
