#!/bin/bash
# Tests Crash Guard's entities against a real Mosquitto broker: device
# discovery, the hand-over of the single entity topics of 1.2.0, removing an
# entity, the states, and the credentials from the options files. Run inside the built image, next to a broker
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
entities 'cg_announce corrupted_cache crashes memory_lock_problem'
expect 'device message with every entity' "$(retained "${DEVICE}" | jq -c '.components | keys')" \
    '["corrupted_cache","crashes","memory_lock_problem"]'
expect 'problem sensors' "$(retained "${DEVICE}" | jq -r '[.components[].device_class] | unique | join(" ")')" problem
expect 'old topics removed' \
    "$(retained homeassistant/sensor/crash_guard/crashes_window/config)$(retained homeassistant/binary_sensor/crash_guard/memory_lock/config)" ''
expect 'hand-over logged' "$(grep -c '2 entities of crash_guard moved to device discovery' "${WORK}/log")" 1

echo '# Removing an entity'
entities 'cg_publish memory_lock_problem "{\"state\":\"off\"}"'
entities 'cg_announce corrupted_cache crashes; cg_drop_state memory_lock_problem'
expect 'memory lock left out' "$(retained "${DEVICE}" | jq -c '.components | has("memory_lock_problem")')" false
expect 'its state removed' "$(retained crash_guard/memory_lock_problem)" ''

echo '# The entities before 2.0.0'
printf '{"components":{"crashes_window":{"platform":"sensor","unique_id":"ha_host_crashes_window"},"crashes":{"platform":"binary_sensor","unique_id":"crash_guard_crashes"}}}' \
    | mosquitto_pub -h broker -r -t "${DEVICE}" -s
printf '3' | mosquitto_pub -h broker -r -t crash_guard/crashes_window -s
printf '{"last":[]}' | mosquitto_pub -h broker -r -t crash_guard/crashes_window/attributes -s
entities 'cg_announce corrupted_cache crashes; for e in ${CG_OLD_STATES}; do cg_drop_state "${e}"; done'
expect 'old entity left out' "$(retained "${DEVICE}" | jq -c '.components | has("crashes_window")')" false
expect 'old state and attributes removed' \
    "$(retained crash_guard/crashes_window)$(retained crash_guard/crashes_window/attributes)" ''

echo '# States'
entities 'cg_publish corrupted_cache "{\"state\":\"unknown\",\"attributes\":{\"friendly_name\":\"x\"}}"'
expect 'unknown state as None' "$(retained crash_guard/corrupted_cache)" None
entities 'cg_publish crashes "{\"state\":\"on\"}"'
expect 'problem on' "$(retained crash_guard/crashes)" on
expect 'no attributes topic' "$(retained crash_guard/crashes/attributes)" ''

echo '# Credentials from the options files, never on the command line'
CREDS=$(mktemp -d)
chmod 700 "${CREDS}"
login() {
    bash -c '
        source /usr/local/lib/crash-guard/entities.sh
        [[ -n "$2" ]] && cg_credentials "$1" crash-guard "$2"
        declare -a MQ=(-h broker -p 1884)
        printf "%s" "$3" | cg_pub crash_guard/login_test 2> /dev/null && echo published || echo refused
        md_retained crash_guard/login_test 2 | cut -f2' _ "${CREDS}" "$1" "$2" 2>> "${WORK}/log"
}
expect 'user name and password with spaces are taken' "$(login 'pass with spaces' one | tr '\n' ' ')" 'published one '
rm -f "${CREDS}"/mosquitto_*
expect 'without them the broker refuses' "$(login '' two | head -1)" refused
expect 'a wrong password is refused' "$(login wrong three | head -1)" refused

if (( FAILED )); then
    echo '--- log:'
    cat "${WORK}/log"
    exit 1
fi
echo 'All tests passed'
