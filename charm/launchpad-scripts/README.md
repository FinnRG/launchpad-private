# Launchpad scripts

This charm sets up and runs the Launchpad scripts.

You will need the following relations:

    juju relate launchpad-scripts:db postgresql:db
    juju relate launchpad-scripts:session-db postgresql:db
    juju relate launchpad-scripts memcached
    juju relate launchpad-scripts rabbitmq-server

This charm installs a cron job to automatically synchronize the Debian bugs
database to the local disk. Since the Debian bugs database is ~150 GiB
in size at the time of writing, it is recommended to provision the unit's
storage accordingly.

In Canonical's deployment of this charm, an additional volume is provisioned
and mounted at the destination directory of this synchronization cron job.
Below are the instructions for doing so.

* Create a Ceph volume of at least 300 GiB in size for storing the Debian bugs
  database. Below is an example command to create this volume in the
  `qastaging` environment.

      openstack volume create --size 300 \
          --description 'Debian bugs database data - lp/qastaging' \
          lp-qastaging-debian-bugs-data

* Attach this Ceph volume to the `launchpad-scripts` unit using the following
  command. The instance ID of the `launchpad-scripts` unit can be found
  from the output of the `juju status` command.

      openstack server add volume <instance id> lp-qastaging-debian-bugs-data

* Log in to the `launchpad-scripts` unit and perform the following steps.

* Create a filesystem on the new volume using `sudo mkfs.ext4 /dev/vdb`. Do
  check and verify that this device node matches the new volume.

* Add `/dev/vdb /srv/launchpad/var/debbugs-mirror ext4 defaults 0 2` to
  `/etc/fstab`. Here, `/srv/launchpad/var` corresponds to the `var_dir`
  template context variable.

* Mount the new volume using `sudo mount /srv/launchpad/var/debbugs-mirror`.

* Set the correct permissions on the new volume using the following commands.

      sudo chown launchpad: /srv/launchpad/var/debbugs-mirror
      sudo chmod 775 /srv/launchpad/var/debbugs-mirror

## Maintenance actions

To stop Celery workers and `number-cruncher` (perhaps in preparation for a
schema upgrade), run:

    juju run-action --wait launchpad-scripts/leader stop-services

To start them again once maintenance is complete:

    juju run-action --wait launchpad-scripts/leader start-services
