#!/bin/bash

set -euxo pipefail

cd "$(dirname "$0")/../.."
run_as="$PWD/utilities/run-as"
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y ca-certificates git make software-properties-common sudo
chown -R ubuntu:ubuntu .

bash -eux utilities/rocketfuel-setup \
    --no-workspace --assume-yes --lpusername launchpad-ci
apt-get clean

"$run_as" ubuntu make bootstrap
"$run_as" ubuntu utilities/launchpad-database-setup ubuntu
"$run_as" ubuntu sudo -n make install
"$run_as" ubuntu make build
"$run_as" ubuntu make schema
