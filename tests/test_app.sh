#!/bin/bash
# Tests for Crash Guard's lock layout, the RAM lock tool (cg-lock.py), the hash
# helper (cg-hash.sh) and the page helper's path checks (cg-pfn.py). Run inside
# the built image, as root (the hash helper only reads Docker layer paths):
#   docker run --rm -v "$PWD/tests:/tests:ro" <image> bash /tests/test_app.sh
# The boot partition and the device tree are fakes under a temporary folder.
set -u

WORK=$(mktemp -d)
FAILED=0
# The overlay that locks the 2 MB block around page 0x1cc447 on a Raspberry
# Pi 5, as installed and verified on real hardware
readonly KNOWN_GOOD_DTBO=4a8d392b43a6debe16078f451f5a9e692d9516179f1c5c721350f4c9b88edf05

expect() {
    local name=$1 got=$2 want=$3
    if [[ "${got}" == "${want}" ]]; then
        echo "ok    ${name}"
    else
        echo "FAIL  ${name}: got '${got}', want '${want}'"
        FAILED=1
    fi
}

echo '# Lock layout (lock.sh)'
# shellcheck source=/dev/null
source /usr/local/lib/crash-guard/lock.sh
lock_layout 0x1cc447
expect 'one page: overlay name' "${LOCK_NAME}" badram-1cc447
expect 'one page: aligned 2 MB block' "$(printf '0x%x' "${LOCK_BLOCKS[@]}")" 0x1cc400000
expect 'one page: range' "${LOCK_RANGE}" '0x1cc400000-0x1cc5fffff (2 MB)'
lock_layout 0x1cc447 0x1cc448 0x2000
expect 'two pages in one block share it' "${#LOCK_BLOCKS[@]}" 2
expect 'name lists every page' "${LOCK_NAME}" badram-1cc447-1cc448-2000
lock_layout 'a[$(touch /tmp/pwned)]' 0xZZ
expect 'malformed pages are skipped' "${LOCK_NAME}|${#LOCK_BLOCKS[@]}" '|0'
expect 'no code ran from a malformed page' "$([[ -e /tmp/pwned ]] && echo ran || echo none)" none
lock_layout
expect 'no pages, no lock' "${LOCK_NAME}${LOCK_RANGE}" ''

printf '%s\n' \
    '2026-09-29T20:22:00+0000 manual pfn=0x1cc447 phys=0x1cc447000 bits=45 variants=8/8 page=2 /x' \
    '2026-09-30T02:30:11+0000 drop-cache pfn=0x1cc447 phys=0x1cc447000 bits=45 variants=5/5 page=2 /x' \
    '2026-10-01T00:00:00+0000 check pfn=0x5000 phys=0x5000000 bits=3 variants=2/5 page=0 /y' \
    '2026-10-01T00:00:00+0000 evil pfn=a[$(touch${IFS}/tmp/pwned2)] rest' > "${WORK}/badpages.log"
expect 'logged pages: only well-formed numbers' "$(logged_pfns "${WORK}/badpages.log" | sort -u | tr '\n' ' ')" '0x1cc447 0x5000 '
lock_layout 0x1cc447
expect 'pages outside the lock' "$(pages_outside "${WORK}/badpages.log")" 0x5000
expect 'no code ran from the log' "$([[ -e /tmp/pwned2 ]] && echo ran || echo none)" none

echo '# RAM lock tool (cg-lock.py)'
export CG_DT="${WORK}/dt" CG_BOOT="${WORK}/boot"
mkdir -p "${CG_DT}/reserved-memory" "${CG_BOOT}/slot-A/overlays" "${CG_BOOT}/slot-B/overlays"
printf 'raspberrypi,5-model-b\0brcm,bcm2712\0' > "${CG_DT}/compatible"
printf '\0\0\0\2' > "${CG_DT}/reserved-memory/#address-cells"
printf '\0\0\0\2' > "${CG_DT}/reserved-memory/#size-cells"
printf '%s\n' 'disable_splash=1' 'os_prefix=slot-A/' 'dtoverlay=vc4-kms-v3d' '[all]' > "${CG_BOOT}/config.txt"
cp "${CG_BOOT}/config.txt" "${WORK}/config.orig"
touch "${CG_BOOT}/slot-A/overlays/badram-custom.dtbo"
lock() { python3 /usr/local/bin/cg-lock.py "$@"; }
field() { jq -r ".$1" <<< "$2"; }

st=$(lock render badram-1cc447 200000 1cc400000)
expect 'overlay is byte-identical to the known good one' "$(field sha256 "${st}")" "${KNOWN_GOOD_DTBO}"

st=$(lock check badram-1cc447 200000 1cc400000)
expect 'check before install: nothing in place' "$(jq -c '[.active, .config, .slot, .slot_overlay, .ready]' <<< "${st}")" \
    '[false,false,"slot-A",false,false]'
expect 'check writes nothing' "$(cmp -s "${CG_BOOT}/config.txt" "${WORK}/config.orig" && echo same)" same

st=$(lock install badram-1cc447 200000 1cc400000)
expect 'install: ready in both slots' "$(jq -c '[.changed, .config, .slot_overlay, .other_overlay, .ready]' <<< "${st}")" \
    '[true,true,true,true,true]'
expect 'install appends marker and line once' "$(tail -3 "${CG_BOOT}/config.txt" | tr '\n' '|')" \
    '[all]|# Crash Guard: lock defective RAM areas (do not remove)|dtoverlay=badram-1cc447|'
expect 'install keeps every existing line' "$(head -4 "${CG_BOOT}/config.txt" | cmp -s - "${WORK}/config.orig" && echo kept)" kept
expect 'installed overlay is the known good one' "$(sha256sum "${CG_BOOT}/slot-B/overlays/badram-1cc447.dtbo" | cut -c1-64)" "${KNOWN_GOOD_DTBO}"

cp "${CG_BOOT}/config.txt" "${WORK}/config.installed"
st=$(lock install badram-1cc447 200000 1cc400000)
expect 'second install changes nothing' "$(field changed "${st}")" false
expect 'second install leaves config.txt alone' "$(cmp -s "${CG_BOOT}/config.txt" "${WORK}/config.installed" && echo same)" same

mkdir "${CG_DT}/reserved-memory/badram@1cc400000"
expect 'active once the kernel has the node' "$(field active "$(lock check badram-1cc447 200000 1cc400000)")" true

st=$(lock install badram-1cc447-2000 200000 1cc400000 2000000)
expect 'new pages: overlay replaced' "$(ls "${CG_BOOT}/slot-A/overlays" | tr '\n' ' ')" 'badram-1cc447-2000.dtbo badram-custom.dtbo '
expect 'new pages: old line gone, new line there' "$(grep '^dtoverlay=badram' "${CG_BOOT}/config.txt")" 'dtoverlay=badram-1cc447-2000'
expect 'new pages: other lines untouched' "$(grep -v badram "${CG_BOOT}/config.txt" | grep -vF '# Crash Guard' | grep -v '^\[all\]$' | tr '\n' '|')" \
    'disable_splash=1|os_prefix=slot-A/|dtoverlay=vc4-kms-v3d|'
expect 'new pages: no temporary file left' "$(find "${CG_BOOT}" -maxdepth 1 -name '*crash-guard*' | wc -l)" 0
expect 'new pages: not active before a reboot' "$(jq -c '[.active, .ready]' <<< "${st}")" '[false,true]'
expect 'other overlays are never touched' "$([[ -f "${CG_BOOT}/slot-A/overlays/badram-custom.dtbo" ]] && echo kept)" kept
expect 'a name it does not own is refused' "$(lock install vc4-kms-v3d 200000 1cc400000 | jq -r '.error | startswith("invalid lock")')" true

echo '# Hash helper (cg-hash.sh)'
L=/mnt/data/docker/overlay2
layer=${L}/$(printf 'a%.0s' {1..64})/diff
init=${L}/$(printf 'b%.0s' {1..64})-init/diff
mkdir -p "${layer}/usr/lib" "${layer}/usr/local/bin" "${init}/usr/lib" "${WORK}/evil/usr/lib" "${WORK}/case"
echo lib > "${layer}/usr/lib/a.so"
echo tool > "${layer}/usr/local/bin/b.py"
echo init > "${init}/usr/lib/c.so"
echo secret > "${WORK}/evil/usr/lib/secret"
printf '%s\n' "${layer}" "${init}" "${WORK}/evil" "${L}/../../../..${WORK}/evil" > "${WORK}/layers.txt"
r=$(bash /usr/local/bin/cg-hash.sh "${WORK}/layers.txt" "${WORK}/ref.sha")
expect 'hashes the files of the layers' "${r}" 'hashed=3 mismatches=-'
expect 'never reads outside the Docker layers' "$(grep -c secret "${WORK}/ref.sha")" 0
echo changed > "${layer}/usr/lib/a.so"
r=$(bash /usr/local/bin/cg-hash.sh "${WORK}/layers.txt" "${WORK}/case/cache.sha" "${WORK}/ref.sha" "${WORK}/case/files")
expect 'finds the changed file' "${r}" 'hashed=3 mismatches=1'
expect 'lists it in the mismatch file' "$(cut -d' ' -f1 "${WORK}/case/cache.mismatch")" "${layer}/usr/lib/a.so"
expect 'keeps a copy of it' "$(cat "${WORK}/case/files/"*a.so)" changed

echo '# Hash helper: names that try to smuggle in other paths'
other=${L}/$(printf 'c%.0s' {1..64})/diff
mkdir -p "${other}/usr/lib"
echo secret > "${other}/usr/lib/secret.so"
# A directory name with a newline: line-based parsing would read the rest as a
# path relative to /, pointing into another container's layer
sneaky="${layer}/usr/lib/x"$'\n'"mnt/data/docker/overlay2/$(printf 'c%.0s' {1..64})/diff/usr/lib"
mkdir -p "${sneaky}"
echo decoy > "${sneaky}/secret.so"
printf '%s\n' "${layer}" > "${WORK}/layers2.txt"
(cd / && bash /usr/local/bin/cg-hash.sh "${WORK}/layers2.txt" "${WORK}/smuggle.sha" > /dev/null)
expect 'no file of another layer hashed' "$(grep -c "^[0-9a-f]* *${other}" "${WORK}/smuggle.sha")" 0
expect 'the name with a newline is left out' "$(grep -c 'decoy\|/x$' "${WORK}/smuggle.sha")" 0
expect 'the layer files are still hashed' "$(grep -c "${layer}/usr/lib/a.so" "${WORK}/smuggle.sha")" 1
mkdir -p "${WORK}/case2"
cp "${WORK}/ref.sha" "${WORK}/case2/ref.sha"
echo changed-again > "${layer}/usr/lib/a.so"
bash /usr/local/bin/cg-hash.sh "${WORK}/layers.txt" "${WORK}/case2/c.sha" "${WORK}/case2/ref.sha" "${WORK}/case2/files" > /dev/null
expect 'only files of the listed layers are copied' "$(find "${WORK}/case2/files" -type f ! -name "$(printf 'a%.0s' {1..64})*" | wc -l | tr -d ' ')" 0
rm -rf "${sneaky%/mnt/*}"

echo '# Copy without following symlinks (cg-copy.py)'
mkdir -p "${WORK}/copy" "${layer}/usr/share"
echo plain > "${layer}/usr/share/plain.txt"
ln -s "${other}/usr/lib" "${layer}/usr/linkdir"
copy() { python3 /usr/local/bin/cg-copy.py "$1" "${WORK}/copy/$2" 2>/dev/null && echo copied || echo refused; }
expect 'a regular file is copied' "$(copy "${layer}/usr/share/plain.txt" plain)" copied
expect 'with its content' "$(cat "${WORK}/copy/plain")" plain
expect 'a symlinked directory on the way is refused' "$(copy "${layer}/usr/linkdir/secret.so" via-link)" refused
ln -s /etc/hostname "${layer}/usr/share/link.txt"
expect 'a symlink as the file is refused' "$(copy "${layer}/usr/share/link.txt" link)" refused
expect 'a path with .. is refused' "$(copy "${layer}/usr/../usr/share/plain.txt" dots)" refused
expect 'an existing target is not overwritten' "$(copy "${layer}/usr/share/plain.txt" plain)" refused
rm -f "${layer}/usr/linkdir" "${layer}/usr/share/link.txt"

echo '# Page helper path checks (cg-pfn.py)'
ln -s /etc/hostname "${layer}/usr/lib/link.so"
checks=$(LAYER="${layer}" python3 - <<'PY'
import os, sys
sys.argv = ["cg-pfn.py"]
exec(open("/usr/local/bin/cg-pfn.py").read().split("def main")[0])
layer = os.environ["LAYER"]
print(" ".join(str(layer_file(p)) for p in (
    f"{layer}/usr/lib/a.so",                      # a layer file
    "/etc/hostname",                              # outside the layers
    f"{layer}/../../../../../etc/hostname",       # escapes with ..
    f"{layer}/usr/lib/link.so",                   # symlink inside a layer
    f"{layer}/usr/lib",                           # a directory
)))
PY
)
expect 'only regular layer files, no escapes or symlinks' "${checks}" 'True False False False False'
printf '%s a b\n' /etc/hostname > "${WORK}/outside.mismatch"
expect 'paths outside the layers are skipped' "$(python3 /usr/local/bin/cg-pfn.py "${WORK}/outside.mismatch" "${WORK}/pages.log" test)" pages=0
ln -s "${WORK}/outside.mismatch" "${WORK}/link.mismatch"
expect 'a symlinked mismatch file is refused' \
    "$(python3 /usr/local/bin/cg-pfn.py "${WORK}/link.mismatch" "${WORK}/pages.log" test 2>&1 | grep -c 'symbolic links')" 1
ln -s /etc/passwd "${WORK}/link.log"
expect 'a symlinked log is refused' \
    "$(python3 /usr/local/bin/cg-pfn.py "${WORK}/outside.mismatch" "${WORK}/link.log" test 2>&1 | grep -c 'symbolic links')" 1

echo '# Entities (entities.sh)'
export PUBLISHED="${WORK}/published" RETAINED="${WORK}/retained"
: > "${PUBLISHED}"
# Left by 1.2.0: single entity topics
printf '%s\t%s\n' homeassistant/sensor/crash_guard/crashes_window/config '{"unique_id":"ha_host_crashes_window"}' \
    homeassistant/binary_sensor/crash_guard/memory_lock/config '{"unique_id":"crash_guard_memory_lock"}' > "${RETAINED}"
ENTITY_PREFIX=host NAME_PREFIX='Home Assistant Host' SLUG=c3edd230_crash_guard APP_VERSION=1.2.0 \
DISCOVERY_PREFIX=homeassistant PATH="/tests/stubs:${PATH}" bash -c '
    bashio::log.info() { echo "$*" >> "${PUBLISHED}.log"; }
    sleep() { return 0; }
    declare -a MQ=(-h broker)
    source /usr/local/lib/crash-guard/entities.sh
    cg_announce crashes_window memory_lock
    : > "${RETAINED}"
    cg_announce crashes_window
    cg_drop_state memory_lock
    cg_publish cache_corrupted "{\"state\":\"unknown\",\"attributes\":{\"friendly_name\":\"x\",\"icon\":\"y\",\"last_check\":null}}"
    cg_publish crashes_handled "{\"state\":\"2\",\"attributes\":{\"window\":\"24 h\"}}"
    crash_payloads 24 > "${PUBLISHED}.crashes"
' 2> "${WORK}/entities.log"
last() { awk -F '\t' -v t="$1" '$1 == t { v = $2 } END { print v }' "${PUBLISHED}"; }
first() { awk -F '\t' -v t="$1" '$1 == t { print $2; exit }' "${PUBLISHED}"; }
DEVICE=homeassistant/device/crash_guard/config
window=$(first "${DEVICE}" | jq -c .components.crashes_window)
expect 'crash window keeps its unique ID' "$(jq -r .unique_id <<< "${window}")" ha_host_crashes_window
expect 'entity ID from the prefix' "$(jq -r .default_entity_id <<< "${window}")" sensor.host_crashes_window
expect 'device named after name_prefix' "$(first "${DEVICE}" | jq -r '.device.name + "|" + .device.identifiers[0]')" 'Home Assistant Host|crash_guard'
expect 'availability and origin' "$(first "${DEVICE}" | jq -r '.availability_topic + "|" + .origin.sw_version')" 'crash_guard/availability|1.2.0'
expect 'memory lock is a binary sensor with on/off' \
    "$(first "${DEVICE}" | jq -r '.components.memory_lock | .platform + .payload_on + .payload_off')" binary_sensoronoff
expect 'single entity topics of 1.2.0 migrated' "$(grep -c '{"migrate_discovery":true}' "${PUBLISHED}")" 2
expect 'and cleared after the device message' \
    "$(last homeassistant/sensor/crash_guard/crashes_window/config)|$(last homeassistant/binary_sensor/crash_guard/memory_lock/config)" '|'
expect 'memory lock removed with its platform only' \
    "$(grep "^${DEVICE}	" "${PUBLISHED}" | sed -n 2p | cut -f2 | jq -c .components.memory_lock)" '{"platform":"binary_sensor"}'
expect 'then left out' "$(last "${DEVICE}" | jq -c '.components | keys')" '["crashes_window"]'
expect 'its state removed' "$(last crash_guard/memory_lock)|$(last crash_guard/memory_lock/attributes)" '|'
expect 'unknown state is sent as None' "$(last crash_guard/cache_corrupted)" None
expect 'attributes without name and icon' "$(last crash_guard/cache_corrupted/attributes)" '{"last_check":null}'
expect 'state as text' "$(last crash_guard/crashes_handled)" 2
expect 'program crashes since boot' "$(awk -F '\t' '$1 == "boot" { print $2 }' "${PUBLISHED}.crashes" | jq -r .state)" 2
expect 'program crashes in the window, newest first' \
    "$(awk -F '\t' '$1 == "window" { print $2 }' "${PUBLISHED}.crashes" | jq -c '[.state, .attributes.window_hours, (.attributes.last | map(.process + "/" + .signal))]')" \
    '["2",24,["curl/7/BUS","python3/11/SEGV"]]'
expect 'no errors' "$(cat "${WORK}/entities.log")" ''

rm -rf "${L}"
if (( FAILED )); then
    exit 1
fi
echo 'All tests passed'
