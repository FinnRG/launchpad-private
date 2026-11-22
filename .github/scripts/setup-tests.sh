#!/bin/bash

set -euo pipefail

cd "$(dirname "$0")/../.."
run_as="$PWD/utilities/run-as"
export DEBIAN_FRONTEND=noninteractive
export GIT_TERMINAL_PROMPT=0
DEPENDENCY_REPO=${DEPENDENCY_REPO:-https://git.launchpad.net/~finnrg/lp-source-dependencies/+git/lp-source-dependencies}
DEPENDENCY_REF=${DEPENDENCY_REF:-resolute-dev}
phase=${1:-all}
# Later Firefox revisions moved to core26 and cannot run directly on Noble.
noble_firefox_revision=8568

if [[ "$phase" != resolve && "$PWD" != /home/ubuntu/launchpad ]]; then
    echo 'Setup requires /home/ubuntu/launchpad in a disposable container' >&2
    exit 1
fi

resolve_dependency() {
    if [[ "$DEPENDENCY_REF" =~ ^[0-9a-fA-F]{40}$ ]]; then
        printf '%s\n' "${DEPENDENCY_REF,,}"
        return
    fi
    local refs revision
    refs=$(git ls-remote --exit-code "$DEPENDENCY_REPO" \
        "$DEPENDENCY_REF" "$DEPENDENCY_REF^{}" \
        "refs/heads/$DEPENDENCY_REF" "refs/tags/$DEPENDENCY_REF" \
        "refs/tags/$DEPENDENCY_REF^{}")
    revision=$(printf '%s\n' "$refs" | awk -v ref="$DEPENDENCY_REF" '
        $2 == "refs/heads/" ref { branch = $1 }
        $2 == ref { exact = $1 }
        $2 == "refs/tags/" ref { tag = $1 }
        $2 == ref "^{}" || $2 == "refs/tags/" ref "^{}" { peeled = $1 }
        END { print branch ? branch : peeled ? peeled : exact ? exact : tag }
    ')
    if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
        echo "Cannot resolve dependency ref: $DEPENDENCY_REF" >&2
        return 1
    fi
    printf '%s\n' "$revision"
}

is_noble() (
    # shellcheck source=/dev/null
    . /etc/os-release
    [[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]]
)

setup_noble_firefox() {
    apt-get install -y snapd
    snap wait system seed.loaded
    snap install firefox --revision="$noble_firefox_revision"
    snap refresh --hold firefox
}

smoke_test_browser() {
    if is_noble; then
        local revision
        revision=$(snap list firefox | awk 'NR == 2 { print $3 }')
        if [[ "$revision" != "$noble_firefox_revision" ]]; then
            printf 'Expected Firefox Snap revision %s, got %s\n' \
                "$noble_firefox_revision" "$revision" >&2
            return 1
        fi
    fi
    "$run_as" ubuntu xvfb-run -a bin/test \
        -m '^lp\.testing\.tests\.test_html5browser$' \
        -t 'test_load_test_results' -vv
}

setup_system() {
    apt-get update
    apt-get install -y \
        ca-certificates git make software-properties-common sudo
    # Install the pin before the transitional APT package can choose a revision.
    if is_noble; then
        setup_noble_firefox
    fi
    chown -R ubuntu:ubuntu .
    bash -eu utilities/rocketfuel-setup \
        --no-workspace --assume-yes --lpusername launchpad-ci
    apt-get clean
    "$run_as" ubuntu utilities/launchpad-database-setup ubuntu
}

setup_dependencies() {
    "$run_as" ubuntu git init download-cache
    "$run_as" ubuntu git -C download-cache fetch --depth=1 \
        "$DEPENDENCY_REPO" "$DEPENDENCY_REF"
    "$run_as" ubuntu git -C download-cache checkout --detach FETCH_HEAD
    "$run_as" ubuntu make compile
}

build_tests() {
    "$run_as" ubuntu sudo -n make install
    "$run_as" ubuntu make requirements/combined.txt
    "$run_as" ubuntu touch env/instance_name
    "$run_as" ubuntu make build
    "$run_as" ubuntu make schema
}

case "$phase" in
    resolve)
        resolve_dependency
        ;;
    system)
        setup_system
        ;;
    dependencies)
        setup_dependencies
        ;;
    stage-cache)
        mkdir ../setup-cache
        mv env bin download-cache wheels ../setup-cache/
        cd ..
        rm -rf launchpad
        ;;
    restore)
        mv ../setup-cache/{env,bin,download-cache,wheels} .
        rmdir ../setup-cache
        chown -R ubuntu:ubuntu .
        ;;
    build)
        build_tests
        ;;
    browser-smoke)
        smoke_test_browser
        ;;
    all)
        setup_system
        setup_dependencies
        build_tests
        ;;
    *)
        echo "Unknown setup phase: $phase" >&2
        exit 1
        ;;
esac
