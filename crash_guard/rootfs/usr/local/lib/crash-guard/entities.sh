# shellcheck shell=bash
# ==============================================================================
# Crash Guard's entities in Home Assistant: through MQTT discovery when a
# broker is available, else as states set through the Home Assistant API.
# Sourced by the guard service and the tests. Expects ENTITY_PREFIX,
# NAME_PREFIX, SLUG and, for MQTT, MQ (mosquitto options) and DISCOVERY_PREFIX.
# ==============================================================================

readonly CG_TOPIC=crash_guard
readonly CG_AVAIL="${CG_TOPIC}/availability"
readonly CG_SUPPORT_URL='https://github.com/n-schilling/ha-app-crash-guard'

# The entities: entity ID suffix -> "component|name|unique ID|extra discovery JSON"
# Problem sensors only; the figures behind them are on the app's web page
declare -A CG_ENTITIES=(
    [memory_lock_problem]='binary_sensor|Memory lock problem|crash_guard_memory_lock_problem|{"device_class":"problem"}'
    [corrupted_cache]='binary_sensor|Corrupted cache|crash_guard_corrupted_cache|{"device_class":"problem"}'
    [crashes]='binary_sensor|Crashes|crash_guard_crashes|{"device_class":"problem"}'
)
# State topics of the entities before 2.0.0, cleared at every start
# shellcheck disable=SC2034  # read by the guard service
readonly CG_OLD_STATES='memory_lock cache_corrupted crashes_handled crashes_boot crashes_window'

cg_pub()   { mosquitto_pub "${MQ[@]}" -r -q 1 -t "$1" -s; }
cg_clear() { mosquitto_pub "${MQ[@]}" -r -q 1 -t "$1" -n; }

# shellcheck source=../mqtt-device.sh
source /usr/local/lib/mqtt-device.sh

# One entity of the device message; $1 = entity ID suffix
cg_component() {
    local component name uid extra
    IFS='|' read -r component name uid extra <<< "${CG_ENTITIES[$1]}"
    jq -nc --arg name "${name}" --arg uid "${uid}" --arg p "${component}" \
        --arg eid "${component}.${ENTITY_PREFIX}_$1" \
        --arg stt "${CG_TOPIC}/$1" --argjson extra "${extra}" '
        {platform:$p, name:$name, unique_id:$uid, default_entity_id:$eid, state_topic:$stt}
        + (if $p == "binary_sensor" then {payload_on:"on", payload_off:"off"} else {} end)
        + $extra'
}

# Announces the device with the given entities (entity ID suffixes); entities
# left out are removed, those of the single entity discovery before 1.3.0
# handed over
cg_announce() {
    local suffix components='{}'
    for suffix in "$@"; do
        components=$(jq -c --arg k "${suffix}" --argjson c "$(cg_component "${suffix}")" '.[$k] = $c' <<< "${components}")
    done
    md_announce crash_guard "$(jq -nc --argjson c "${components}" --arg avail "${CG_AVAIL}" \
        --arg dev "${NAME_PREFIX}" --arg cfg "homeassistant://hassio/addon/${SLUG}/info" \
        --arg version "${APP_VERSION:-unknown}" --arg url "${CG_SUPPORT_URL}" '
        {device:{identifiers:["crash_guard"], name:$dev, manufacturer:"Crash Guard",
                 model:"Crash Guard", configuration_url:$cfg},
         origin:{name:"Crash Guard", sw_version:$version, support_url:$url},
         availability_topic:$avail, qos:1, components:$c}')" \
        "${DISCOVERY_PREFIX}/+/${CG_TOPIC}/+/config"
}

# Removes the retained state of an entity that is no longer announced, with
# the attributes topic versions before 2.0.0 had
cg_drop_state() {
    cg_clear "${CG_TOPIC}/$1"
    cg_clear "${CG_TOPIC}/$1/attributes"
}

# Publishes the state of an API format payload {state, attributes}; an
# unknown state is sent as None, which Home Assistant shows as unknown. Fails
# when the broker does not take it, with the reason in PUB_ERROR instead of
# on stderr.
PUB_ERROR=''
# shellcheck disable=SC2034  # PUB_ERROR is read by the guard service
cg_publish() {
    local suffix=$1 payload=$2
    PUB_ERROR=$(jq -j '.state | if . == "unknown" or . == null then "None" else tostring end' <<< "${payload}" \
        | cg_pub "${CG_TOPIC}/${suffix}" 2>&1)
}

# --- Program crashes on the host -------------------------------------------------
# systemd-coredump logs each crash as "... (<process>) of user N terminated
# abnormally with signal N/NAME"; one JSON object per crash
crash_json() {
    journalctl -D "${CG_JOURNAL:-/var/log/journal}" -t systemd-coredump -o json "$@" 2>/dev/null \
    | jq -c 'select(.MESSAGE | type == "string" and test("terminated abnormally with signal"))
             | (.MESSAGE | capture("\\((?<process>[^)]+)\\) of user \\d+ terminated abnormally with signal (?<signal>[^,]+)"))
               + {time: (.__REALTIME_TIMESTAMP | tonumber / 1e6 | floor | todate)}'
}

# The crashes for the Crashes sensor and the web page, as one JSON object:
# program crashes since boot (count) and within the window (the newest 50),
# and the Home Assistant crashes the guard caught within the window, from its
# events.log. $1 = window in hours, $2 = events.log
crash_status() {
    local boot recent cutoff ha
    boot=$(crash_json -b | wc -l | tr -d ' ')
    recent=$(crash_json --since "-$1h" | jq -sc '.')
    cutoff=$(date -d "-$1 hours" -Iseconds)
    ha=$(awk -v c="${cutoff}" '$1 > c && /Crash handled|NOT handled/' "$2" 2>/dev/null \
        | jq -Rsc 'split("\n") | map(select(. != "") | capture("^(?<time>\\S+) (?<message>.*)$")) | reverse')
    jq -nc --argjson h "$1" --argjson boot "${boot}" --argjson recent "${recent}" --argjson ha "${ha:-[]}" '
        {window_hours:$h, boot:$boot, programs:($recent | .[-50:] | reverse), home_assistant:$ha,
         problem:(($recent | length) + ($ha | length) > 0)}'
}
