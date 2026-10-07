#!/bin/bash
# Tests Crash Guard's entities against a real Mosquitto broker: device
# discovery, the hand-over of the single entity topics of 1.2.0, removing an
# entity, and the states. Run inside the built image, next to a broker
# reachable as "broker" (see the CI workflow):
#   docker run --rm --network <net> -v "$PWD/tests:/tests:ro" <image> bash /tests/test_broker.sh
set -u

readonly DEVICE=homeassistant/device/crash_guard/config
WORK=$(mktemp -d)
FAILED=0

expect() {
    local name=$1 got=$2 want=$3
    if [[ "${got}" == "${want}" ]]; then
        echo "ok    ${name}"
    else
        echo "FAIL  ${name}: got '${got}', want '${want}'"
        FAILED=1
    fi
}

retained() { mosquitto_sub -h broker -t "$1" --retained-only -W 2 -C 1 2>/dev/null; }

# Runs entities.sh functions as the guard service does
entities() {
    ENTITY_PREFIX=host NAME_PREFIX='Home Assistant Host' SLUG=c3edd230_crash_guard APP_VERSION=1.3.2 \
    DISCOVERY_PREFIX=homeassistant bash -c '
        bashio::log.info() { echo "$*"; }
        declare -a MQ=(-h broker)
        source /usr/local/lib/crash-guard/entities.sh
        '"$1" >> "${WORK}/log" 2>&1
}

echo '# Hand-over from the single entity discovery of 1.2.0'
# As 1.2.0 left them: jq output with its trailing newline
for e in sensor/crash_guard/crashes_window binary_sensor/crash_guard/memory_lock; do
    jq -nc --arg e "${e}" '{unique_id:$e}' | mosquitto_pub -h broker -r -t "homeassistant/${e}/config" -s
done
entities 'cg_announce cache_corrupted crashes_handled crashes_boot crashes_window memory_lock'
expect 'device message with every entity' "$(retained "${DEVICE}" | jq -c '.components | keys')" \
    '["cache_corrupted","crashes_boot","crashes_handled","crashes_window","memory_lock"]'
expect 'old topics removed' \
    "$(retained homeassistant/sensor/crash_guard/crashes_window/config)$(retained homeassistant/binary_sensor/crash_guard/memory_lock/config)" ''
expect 'hand-over logged' "$(grep -c '2 entities of crash_guard moved to device discovery' "${WORK}/log")" 1

echo '# Removing an entity'
entities 'cg_announce cache_corrupted crashes_handled crashes_boot crashes_window; cg_drop_state memory_lock'
expect 'memory lock left out' "$(retained "${DEVICE}" | jq -c '.components | has("memory_lock")')" false
expect 'its state removed' "$(retained crash_guard/memory_lock)" ''

echo '# States'
entities 'cg_publish cache_corrupted "{\"state\":\"unknown\",\"attributes\":{\"friendly_name\":\"x\",\"last_check\":null}}"'
expect 'unknown state as None' "$(retained crash_guard/cache_corrupted)" None
expect 'attributes without the name' "$(retained crash_guard/cache_corrupted/attributes)" '{"last_check":null}'

if (( FAILED )); then
    echo '--- log:'
    cat "${WORK}/log"
    exit 1
fi
echo 'All tests passed'
