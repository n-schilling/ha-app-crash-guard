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
# The crash counters keep the unique IDs they had in the NVMe & Crash Monitor
# app, so Home Assistant gives them back their entity IDs and settings.
declare -A CG_ENTITIES=(
    [memory_lock]='binary_sensor|Memory lock|crash_guard_memory_lock|{"icon":"mdi:memory"}'
    [cache_corrupted]='sensor|Cache corrupted|crash_guard_cache_corrupted|{"icon":"mdi:file-alert-outline","state_class":"measurement"}'
    [crashes_handled]='sensor|Crashes handled|crash_guard_crashes_handled|{"icon":"mdi:shield-alert-outline","state_class":"measurement"}'
    [crashes_boot]='sensor|Program crashes since boot|ha_host_crashes_boot|{"icon":"mdi:bug-outline","state_class":"measurement"}'
    [crashes_window]='sensor|Program crashes (window)|ha_host_crashes_window|{"icon":"mdi:bug","state_class":"measurement"}'
)

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
        --arg stt "${CG_TOPIC}/$1" --arg att "${CG_TOPIC}/$1/attributes" --argjson extra "${extra}" '
        {platform:$p, name:$name, unique_id:$uid, default_entity_id:$eid,
         state_topic:$stt, json_attributes_topic:$att}
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

# Removes the retained state of an entity that is no longer announced
cg_drop_state() {
    cg_clear "${CG_TOPIC}/$1"
    cg_clear "${CG_TOPIC}/$1/attributes"
}

# Publishes a state in the API format {state, attributes}: the state and the
# attributes without what discovery already sets (name, icon). An unknown
# state is sent as None, which Home Assistant shows as unknown.
cg_publish() {
    local suffix=$1 payload=$2
    jq -j '.state | if . == "unknown" or . == null then "None" else tostring end' <<< "${payload}" \
        | cg_pub "${CG_TOPIC}/${suffix}"
    jq -c '.attributes | del(.friendly_name, .icon)' <<< "${payload}" \
        | cg_pub "${CG_TOPIC}/${suffix}/attributes"
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

# Prints the API payloads of both crash entities as "boot<TAB>json" and
# "window<TAB>json"; $1 = window in hours
crash_payloads() {
    local boot recent
    boot=$(crash_json -b | wc -l | tr -d ' ')
    recent=$(crash_json --since "-$1h" | jq -sc '.')
    printf 'boot\t%s\n' "$(jq -nc --arg n "${boot}" --arg name "${NAME_PREFIX}" \
        '{state:$n, attributes:{friendly_name:($name + " Program crashes since boot")}}')"
    printf 'window\t%s\n' "$(jq -c --argjson h "$1" --arg name "${NAME_PREFIX}" \
        '{state:(length | tostring), attributes:{friendly_name:($name + " Program crashes (window)"),
          window_hours:$h, last:(.[-10:] | reverse)}}' <<< "${recent}")"
}
