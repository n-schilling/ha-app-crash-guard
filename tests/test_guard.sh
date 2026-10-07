#!/bin/bash
# Tests the guard service itself against a fake host (tests/fake_host.py):
# the start, the protection mode check, the status entities, a crash of Home
# Assistant with evidence, cache drop, restart and notification, the restart
# limit and an intended stop. Run inside the built image, as root:
#   docker run --rm --add-host supervisor:127.0.0.1 -v "$PWD/tests:/tests:ro" \
#     --entrypoint bash <image> /tests/test_guard.sh
# journalctl is a stub; there is no MQTT broker, so the entities are states
# set through the Home Assistant API.
set -u

readonly RUN=/etc/s6-overlay/s6-rc.d/guard/run
FAKE=$(mktemp -d)
WORK=$(mktemp -d)
FAILED=0
export PATH="/tests/stubs:${PATH}" SUPERVISOR_TOKEN=test-token

expect() {
    local name=$1 got=$2 want=$3
    if [[ "${got}" == "${want}" ]]; then
        echo "ok    ${name}"
    else
        echo "FAIL  ${name}: got '${got}', want '${want}'"
        FAILED=1
    fi
}
wait_for() { local i; for ((i = 0; i < $2 * 5; i++)); do eval "$1" && return 0; sleep 0.2; done; return 1; }
requests() { jq -c "select($1)" "${FAKE}/requests.jsonl"; }
count() { requests "$1" | wc -l | tr -d ' '; }
event() {
    jq -nc --arg a "$1" --arg c "$2" --argjson t "$(date +%s%N)" \
        '{Action:$a, timeNano:$t, Actor:{Attributes:{name:"homeassistant", exitCode:$c}}}' >> "${FAKE}/events.jsonl"
}
events_log() { cat /data/crash_guard/events.log 2>/dev/null; }

# The service with the options the app would get
start_guard() {
    bash -c '
        source /usr/lib/bashio/bashio.sh
        bashio::config() {
            case "$1" in
                check_interval) echo 1 ;;
                max_restarts_per_hour) echo 1 ;;
                verify_on_drop) echo true ;;
                drop_on_schedule) echo false ;;
                ram_lock) echo false ;;
                notify_service) echo notify.test ;;
                entity_prefix) echo test ;;
                name_prefix) echo Test ;;
                crash_window_hours) echo 24 ;;
                publish_evidence) echo false ;;
                drop_times) echo 25:00 ;;
                bad_pages) echo 0x1cc447 ;;
                discovery_prefix) echo homeassistant ;;
                *) echo null ;;
            esac
        }
        bashio::services.available() { return 1; }
        source "$0"
    ' "${RUN}" >> "${WORK}/log" 2>&1 &
    GUARD=$!
}

python3 /tests/fake_host.py "${FAKE}" &
FAKE_PID=$!
wait_for '[[ -e "${FAKE}/ready" ]]' 10
echo selfcid > /run/cid

echo '# Protection mode'
touch "${FAKE}/protected"
start_guard
wait "${GUARD}"
expect 'stops' "$?" 1
expect 'says why in the log' "$(grep -c 'Please deactivate the protection mode' "${WORK}/log")" 1
expect 'and as a notification' \
    "$(count '.path == "/core/api/services/persistent_notification/create" and .body.notification_id == "crash_guard_protection_mode"')" 1
rm "${FAKE}/protected"

echo '# Start'
start_guard
wait_for 'grep -q "Crash Guard active" "${WORK}/log"' 30
expect 'running' "$(grep -c 'Crash Guard active' "${WORK}/log")" 1
expect 'notification of the protection mode taken down' \
    "$(count '.path == "/core/api/services/persistent_notification/dismiss"')" 1
expect 'reference built from the Home Assistant image' "$(events_log | grep -c 'Reference built')" 1
drop='.path == "/containers/create" and (.body.Cmd | join(" ") | test("drop_caches"))'
expect 'helpers run the image of the app itself' \
    "$(requests '.path == "/containers/create"' | jq -r '.body.Image' | sort -u)" \
    sha256:feedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedface
expect 'cache dropped by a privileged helper without network' \
    "$(requests "${drop}" | head -1 | jq -c '.body.HostConfig | [.Privileged, .NetworkMode, .ReadonlyRootfs]')" '[true,"none",true]'
wait_for '[[ $(count ".path == \"/core/api/states/sensor.test_crashes_window\"") -gt 0 &&
           $(count ".path == \"/core/api/states/sensor.test_crashes_handled\"") -gt 0 &&
           $(count ".path == \"/core/api/states/sensor.test_cache_corrupted\"") -gt 0 &&
           $(count ".path == \"/core/api/states/binary_sensor.test_memory_lock\"") -gt 0 ]]' 30
state() { requests ".path == \"/core/api/states/$1\"" | tail -1 | jq -r "$2"; }
expect 'memory lock state' "$(state binary_sensor.test_memory_lock .body.state)" on
expect 'cache check state' "$(state sensor.test_cache_corrupted .body.state)" unknown
expect 'no crash handled yet' "$(state sensor.test_crashes_handled .body.state)" 0
expect 'program crashes from the journal' "$(state sensor.test_crashes_boot .body.state)" 2
expect 'program crashes in the window' "$(state sensor.test_crashes_window '.body.attributes.last | length')" 2

echo '# Crash of Home Assistant'
mkdir -p /homeassistant
echo 'api_password: hunter2' > /homeassistant/secrets.yaml
ln -sf /homeassistant/secrets.yaml /homeassistant/home-assistant.log.fault
touch "${FAKE}/ha_stopped"
event die 139
wait_for 'events_log | grep -q "Crash handled"' 30
expect 'crash handled' "$(events_log | grep -c 'Crash handled: exit 139')" 1
expect 'Home Assistant started again' "$(count '.path == "/core/start" and .method == "POST"')" 1
expect 'cache dropped again' "$(requests "${drop}" | wc -l | tr -d ' ')" 2
expect 'evidence published through the share helper' \
    "$(count '.path == "/containers/create" and ((.body.HostConfig.Binds // []) | index("/mnt/data/supervisor/share:/share")) and (.body.Cmd | join(" ") | test("mkdir --"))')" 1
expect 'evidence of the crash published' "$(find "${FAKE}/published" -mindepth 1 -maxdepth 1 -name '*-crash' | wc -l | tr -d ' ')" 1
expect 'without the logs (publish_evidence off)' "$(find "${FAKE}/published" -name container.log | wc -l | tr -d ' ')" 0
expect 'the logs stay in the private data' "$(find /data/crash_guard/cases -name container.log | wc -l | tr -d ' ')" 1
expect 'a symlinked fault log is not copied' "$(grep -rl hunter2 "${FAKE}/published" 2>/dev/null | wc -l | tr -d ' ')" 0
expect 'notification sent' "$(requests '.path == "/core/api/services/notify/test"' | jq -r '.body.message' | grep -c 'crashed at')" 1
expect 'it says where the logs are' "$(requests '.path == "/core/api/services/notify/test"' | jq -r '.body.message' | grep -c 'private data')" 1
wait_for '[[ "$(state sensor.test_crashes_handled .body.state)" == 1 ]]' 15
expect 'crashes handled state' "$(state sensor.test_crashes_handled .body.state)" 1

echo '# Restart limit'
event die 139
wait_for 'events_log | grep -q "NOT handled"' 15
expect 'second crash within the hour only logged' "$(events_log | grep -c 'limit of 1/h reached')" 1
expect 'no second start' "$(count '.path == "/core/start"')" 1

echo '# Intended stop'
event kill -
event die 139
wait_for 'events_log | grep -q "after an intended stop"' 15
expect 'exit after a stop only logged' "$(events_log | grep -c 'after an intended stop - only logged')" 1

echo '# Stop'
kill -TERM "${GUARD}"
wait_for '! kill -0 "${GUARD}" 2>/dev/null' 15
expect 'stops on TERM' "$(kill -0 "${GUARD}" 2>/dev/null && echo running || echo stopped)" stopped

kill "${FAKE_PID}" 2>/dev/null
if (( FAILED )); then
    echo '--- service log:'
    cat "${WORK}/log"
    echo '--- events.log:'
    events_log
    exit 1
fi
echo 'All tests passed'
