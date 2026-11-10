#!/bin/bash
# Runs locally what the CI workflow runs, so a push only confirms what already
# passed here: the changelog check, shellcheck, ruff, the image build with the
# tests inside it (app, guard service, web page) and the test against a real
# Mosquitto broker. Keep the steps in step with .github/workflows/ci.yaml.
#
# Usage: scripts/check.sh [--no-docker]
#
# The image is built for the architecture of this machine; CI builds and tests
# both (amd64, aarch64). shellcheck and ruff come from PATH when they are
# there, else shellcheck runs in its container and ruff through uvx. The app
# configuration linter (frenck/action-addon-linter) runs in CI only.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

readonly RUFF_VERSION=0.17.3
readonly SHELLCHECK_IMAGE=koalaman/shellcheck:v0.11.0
readonly BROKER_IMAGE=eclipse-mosquitto:2.1.2-alpine@sha256:75d6662dee52c947dc9dd9c29f1d8d7ffef78ddaf38001caa09755681e8bbc98
readonly SHELL_SCRIPTS=(
    crash_guard/rootfs/etc/s6-overlay/s6-rc.d/guard/run
    crash_guard/rootfs/etc/s6-overlay/s6-rc.d/web/run
    crash_guard/rootfs/usr/local/bin/cg-hash.sh
    crash_guard/rootfs/usr/local/lib/crash-guard/lock.sh
    crash_guard/rootfs/usr/local/lib/crash-guard/entities.sh
    crash_guard/rootfs/usr/local/lib/crash-guard/rotate.sh
    crash_guard/rootfs/usr/local/lib/crash-guard/ref.sh
    crash_guard/rootfs/usr/local/lib/mqtt-device.sh
    tests/test_broker.sh tests/test_guard.sh tests/stubs/journalctl tests/stubs/mosquitto_pub
    tests/stubs/mosquitto_sub tests/test_app.sh scripts/check.sh
    crash_guard/rootfs/usr/local/lib/crash-guard/schedule.sh
)

no_docker=false
for arg in "$@"; do
    case "$arg" in
        --no-docker) no_docker=true ;;
        *) echo "unknown option $arg" >&2; exit 2 ;;
    esac
done

failed=()
skipped=()

run() { # run <name> <command...>
    local name=$1
    shift
    echo "==> $name"
    if "$@"; then
        echo "    ok: $name"
    else
        echo "    FAILED: $name"
        failed+=("$name")
    fi
    echo
}

docker_ok=false
if ! $no_docker && docker info > /dev/null 2>&1; then
    docker_ok=true
fi

version=$(sed -n 's/^version: *"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' crash_guard/config.yaml)

run "Changelog has the version $version" bash -c \
    '.github/scripts/changelog-section.sh crash_guard/CHANGELOG.md "$1" > /dev/null' _ "$version"

if command -v shellcheck > /dev/null; then
    run "Shell scripts" shellcheck --shell=bash --severity=warning --external-sources "${SHELL_SCRIPTS[@]}"
elif $docker_ok; then
    run "Shell scripts" docker run --rm -v "$PWD:/mnt:ro" -w /mnt "$SHELLCHECK_IMAGE" \
        --shell=bash --severity=warning --external-sources "${SHELL_SCRIPTS[@]}"
else
    skipped+=("Shell scripts (neither shellcheck nor Docker)")
fi

if command -v uvx > /dev/null; then
    run "Python" uvx "ruff@$RUFF_VERSION" check crash_guard/rootfs/usr/local tests scripts
elif command -v ruff > /dev/null; then
    run "Python (ruff $(ruff --version | cut -d' ' -f2), CI uses $RUFF_VERSION)" \
        ruff check crash_guard/rootfs/usr/local tests scripts
else
    skipped+=("Python (neither uvx nor ruff)")
fi

if ! $docker_ok; then
    skipped+=("Build and test, broker (--no-docker or Docker is not running)")
else
    tests=(-v "$PWD/tests:/tests:ro")
    run "Build" docker build -q --build-arg "BUILD_VERSION=$version" -t crash-guard:test crash_guard
    run "Test the app" docker run --rm --network none "${tests[@]}" --entrypoint bash crash-guard:test /tests/test_app.sh
    run "Test the guard service" docker run --rm --network none --add-host supervisor:127.0.0.1 \
        "${tests[@]}" --entrypoint bash crash-guard:test /tests/test_guard.sh
    run "Test the web page" docker run --rm --network none "${tests[@]}" --entrypoint python3 crash-guard:test \
        -m unittest discover -s /tests -p 'test_*.py'

    net="crash-guard-check-$$"
    docker network create "$net" > /dev/null
    docker run -d --rm --name "$net-broker" --network "$net" --network-alias broker \
        -v "$PWD/tests/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
        -v "$PWD/tests/mosquitto.passwords:/mosquitto/config/passwords:ro" "$BROKER_IMAGE" > /dev/null
    run "Against a real MQTT broker" docker run --rm --network "$net" "${tests[@]}" --entrypoint bash \
        crash-guard:test /tests/test_broker.sh
    docker rm -f "$net-broker" > /dev/null 2>&1
    docker network rm "$net" > /dev/null
fi
skipped+=("App configuration linter (CI only)")

echo "===================="
if ((${#skipped[@]})); then
    printf 'skipped: %s\n' "${skipped[@]}"
fi
if ((${#failed[@]})); then
    echo "FAILED: ${failed[*]}"
    exit 1
fi
echo "All checks passed."
