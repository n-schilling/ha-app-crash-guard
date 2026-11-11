#!/bin/bash
# Tests for Crash Guard's lock layout, the check schedule, the RAM lock tool
# (cg-lock.py), the hash helpers (cg-hash.sh, cg-direct-hash.py), the copy
# tool (cg-copy.py), the page helper's path checks (cg-pfn.py) and the
# entities. Run inside
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

echo '# Image'
expect 'the version from config.yaml for the web page' \
    "$([[ "${APP_VERSION:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && echo set || echo "missing: '${APP_VERSION:-}'")" set

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

echo '# Check schedule (schedule.sh)'
# shellcheck source=/dev/null
source /usr/local/lib/crash-guard/schedule.sh
at() { date -d "2026-11-10 $1" +%s; }
due() { schedule_due "$1" "$(at "$2")" "${3:-}" && echo due || echo no; }
expect 'not before its time' "$(due 04:30 04:29:59)" no
expect 'in its minute' "$(due 04:30 04:30:05)" due
expect 'also when the loop comes late' "$(due 04:30 04:33:00)" due
expect 'but not hours late' "$(due 04:30 05:31:00)" no
expect 'once a day' "$(due 04:30 04:35:00 '2026-11-10 04:30')" no
expect 'again the next day' "$(schedule_due 04:30 "$(date -d '2026-11-11 04:30:10' +%s)" '2026-11-10 04:30' && echo due)" due
expect 'its key' "$(schedule_key 16:30 "$(at 16:31:00)")" '2026-11-10 16:30'
expect 'an impossible time is never due' "$(due 25:00 23:59:00)" no

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
expect 'new pages: one marker, the line right under it' "$(tail -3 "${CG_BOOT}/config.txt" | tr '\n' '|')" \
    '[all]|# Crash Guard: lock defective RAM areas (do not remove)|dtoverlay=badram-1cc447-2000|'
expect 'new pages: other lines untouched' "$(grep -v badram "${CG_BOOT}/config.txt" | grep -vF '# Crash Guard' | grep -v '^\[all\]$' | tr '\n' '|')" \
    'disable_splash=1|os_prefix=slot-A/|dtoverlay=vc4-kms-v3d|'
expect 'new pages: no temporary file left' "$(find "${CG_BOOT}" -maxdepth 1 -name '*crash-guard*' | wc -l)" 0
expect 'new pages: not active before a reboot' "$(jq -c '[.active, .ready]' <<< "${st}")" '[false,true]'
expect 'other overlays are never touched' "$([[ -f "${CG_BOOT}/slot-A/overlays/badram-custom.dtbo" ]] && echo kept)" kept
expect 'a name it does not own is refused' "$(lock install vc4-kms-v3d 200000 1cc400000 | jq -r '.error | startswith("invalid lock")')" true

st=$(python3 /usr/local/bin/cg-lock.py remove)
expect 'remove: something removed, still active until the reboot' "$(jq -c '[.changed, .config, .active]' <<< "${st}")" '[true,false,true]'
expect 'remove: config.txt as before the lock' "$(cmp -s "${CG_BOOT}/config.txt" "${WORK}/config.orig" && echo same)" same
expect 'remove: own overlays gone from both slots' "$(find "${CG_BOOT}" -name 'badram-1*' | wc -l | tr -d ' ')" 0
expect 'remove: other overlays kept' "$([[ -f "${CG_BOOT}/slot-A/overlays/badram-custom.dtbo" ]] && echo kept)" kept
expect 'remove again: nothing to do' "$(python3 /usr/local/bin/cg-lock.py remove | jq -c '[.changed, .config]')" '[false,false]'
# A lock written by a version before 1.1, with the German marker
printf '%s\n' '[all]' '# Crash Guard: defekte RAM-Bereiche sperren (nicht entfernen)' 'dtoverlay=badram-1cc447' >> "${CG_BOOT}/config.txt"
st=$(lock install badram-1cc447-2000 200000 1cc400000 2000000)
expect 'old marker: the new line goes under it' "$(tail -2 "${CG_BOOT}/config.txt" | tr '\n' '|')" \
    '# Crash Guard: defekte RAM-Bereiche sperren (nicht entfernen)|dtoverlay=badram-1cc447-2000|'
python3 /usr/local/bin/cg-lock.py remove > /dev/null
expect 'old marker removed with the lock' "$(cmp -s "${CG_BOOT}/config.txt" "${WORK}/config.orig" && echo same)" same

echo '# Case rotation (rotate.sh)'
mkdir -p "${WORK}/cases"
for c in 20261001-040000-check 20261002-040000-check 20261003-120000-crash 20261004-040000-check; do
    mkdir "${WORK}/cases/${c}"
done
mkdir "${WORK}/cases/keep-me" "${WORK}/outside"
touch "${WORK}/outside/precious"
ln -s "${WORK}/outside" "${WORK}/cases/20260101-000000-link"
expect 'reports what it removed' "$(bash /usr/local/lib/crash-guard/rotate.sh "${WORK}/cases" 2)" 'removed 2'
expect 'keeps the newest cases and anything else' "$(ls "${WORK}/cases" | tr '\n' ' ')" \
    '20260101-000000-link 20261003-120000-crash 20261004-040000-check keep-me '
expect 'never follows a symlink' "$([[ -f "${WORK}/outside/precious" ]] && echo kept)" kept
expect 'nothing to do below the limit' "$(bash /usr/local/lib/crash-guard/rotate.sh "${WORK}/cases" 5)" ''
expect 'refuses a bad number' "$(bash /usr/local/lib/crash-guard/rotate.sh "${WORK}/cases" x 2>&1; echo "rc=$?")" $'invalid number: x\nrc=1'

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
# The drive is simulated by a dd stub (O_DIRECT is not possible on every
# file system a test container runs on): DISK=same reads the file, DISK=other
# returns other content (the cache was corrupted), DISK=fail cannot read
mkdir -p "${WORK}/bin"
cat > "${WORK}/bin/dd" <<'STUB'
#!/bin/bash
for a in "$@"; do [[ "${a}" == if=* ]] && f=${a#if=}; done
case "${DISK}" in
    same) cat -- "${f}" ;;
    other) echo 'what the drive holds' ;;
    *) echo 'dd: O_DIRECT not supported' >&2; exit 1 ;;
esac
STUB
chmod +x "${WORK}/bin/dd"
hash_with() {
    DISK=$1 PATH="${WORK}/bin:${PATH}" bash /usr/local/bin/cg-hash.sh "${WORK}/layers.txt" \
        "${WORK}/$2/cache.sha" "${WORK}/ref.sha" "${WORK}/$2/files"
}
echo changed > "${layer}/usr/lib/a.so"
expect 'cache differs from the drive: corrupted' "$(hash_with other case)" 'hashed=3 mismatches=1 changed=0'
expect 'lists it in the mismatch file' "$(cut -d' ' -f1 "${WORK}/case/cache.mismatch")" "${layer}/usr/lib/a.so"
expect 'keeps a copy of it' "$(cat "${WORK}/case/files/"*a.so)" changed
mkdir -p "${WORK}/case3" "${WORK}/case4"
expect 'cache equals the drive: changed on the drive' "$(hash_with same case3)" 'hashed=3 mismatches=0 changed=1'
expect 'lists it with both hashes' "$(cut -d' ' -f1 "${WORK}/case3/cache.changed")|$(wc -w < "${WORK}/case3/cache.changed" | tr -d ' ')" "${layer}/usr/lib/a.so|3"
expect 'keeps no copy of a changed file' "$([[ -d "${WORK}/case3/files" ]] && echo copied || echo none)" none
expect 'drive not readable: counted as corrupted' "$(hash_with fail case4)" 'hashed=3 mismatches=1 changed=0'

echo '# Hash helper: straight from the drive (--direct, cg-direct-hash.py)'
r=$(bash /usr/local/bin/cg-hash.sh --direct "${WORK}/layers.txt" "${WORK}/direct.sha")
if [[ "${r}" == 'hashed=0 mismatches=-' ]]; then
    # O_DIRECT is not possible on every file system a test container runs on
    echo "skip  O_DIRECT not supported here: ${r}"
else
    expect 'hashes every file of the layers' "${r}" 'hashed=3 mismatches=-'
    bash /usr/local/bin/cg-hash.sh "${WORK}/layers.txt" "${WORK}/cached.sha" > /dev/null
    expect 'with the same hashes as from the cache' \
        "$(sort -k2 "${WORK}/cached.sha" | cmp -s - <(sort -k2 "${WORK}/direct.sha") && echo same)" same
fi
mkfifo "${layer}/usr/lib/fifo.so"
expect 'a FIFO neither blocks nor counts' \
    "$(printf '%s\0' "${layer}/usr/lib/fifo.so" | timeout 10 python3 /usr/local/bin/cg-direct-hash.py | wc -l | tr -d ' ')" 0
rm -f "${layer}/usr/lib/fifo.so"

echo '# Hash helper: copies of corrupted files are limited'
many=${L}/$(printf 'd%.0s' {1..64})/diff
mkdir -p "${many}/usr/lib" "${WORK}/many"
for i in $(seq 1 25); do echo "file ${i}" > "${many}/usr/lib/f${i}.so"; done
head -c $(( 11 * 1024 * 1024 )) /dev/zero > "${many}/usr/lib/big.so"
printf '%s\n' "${many}" > "${WORK}/layers3.txt"
bash /usr/local/bin/cg-hash.sh "${WORK}/layers3.txt" "${WORK}/many/ref.sha" > /dev/null
for f in "${many}"/usr/lib/*; do echo changed >> "${f}"; done
expect 'every corrupted file is listed' \
    "$(DISK=other PATH="${WORK}/bin:${PATH}" bash /usr/local/bin/cg-hash.sh "${WORK}/layers3.txt" "${WORK}/many/c.sha" "${WORK}/many/ref.sha" "${WORK}/many/files")" \
    'hashed=26 mismatches=26 changed=0'
expect 'but at most 20 copied' "$(find "${WORK}/many/files" -type f | wc -l | tr -d ' ')" 20
expect 'none larger than 10 MB' "$(find "${WORK}/many/files" -type f -size +10M | wc -l | tr -d ' ')" 0
rm -rf "${many%/diff}"

echo '# Reference update (ref.sh)'
# shellcheck source=/dev/null
source /usr/local/lib/crash-guard/ref.sh
cp "${WORK}/ref.sha" "${WORK}/ref2.sha"
b_line=$(grep 'b.py' "${WORK}/ref2.sha")
expect 'takes the new hash of a changed file' "$(adopt_changes "${WORK}/ref2.sha" "${WORK}/case3/cache.changed")" 1
expect 'the reference has it' "$(grep 'a.so' "${WORK}/ref2.sha")" "$(sha256sum "${layer}/usr/lib/a.so")"
expect 'other entries stay' "$(grep 'b.py' "${WORK}/ref2.sha")|$(wc -l < "${WORK}/ref2.sha" | tr -d ' ')" "${b_line}|3"
expect 'a stale line (other expected hash) is ignored' "$(adopt_changes "${WORK}/ref2.sha" "${WORK}/case3/cache.changed")" 0
expect 'nothing changed: nothing to do' "$(adopt_changes "${WORK}/ref2.sha" /nonexistent)" 0

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
expect 'only files of the listed layers are copied' "$(find "${WORK}/case2/files" -type f ! -name "$(printf 'a%.0s' {1..64})*" 2> /dev/null | wc -l | tr -d ' ')" 0
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
mkfifo "${layer}/usr/share/fifo"
expect 'a FIFO is refused without blocking' \
    "$(timeout 10 python3 /usr/local/bin/cg-copy.py "${layer}/usr/share/fifo" "${WORK}/copy/fifo" && echo copied || echo refused)" refused
seq 1 1000 > "${layer}/usr/share/long.log"
python3 /usr/local/bin/cg-copy.py --tail 13 "${layer}/usr/share/long.log" "${WORK}/copy/tail"
expect '--tail keeps only the end' "$(tr '\n' ' ' < "${WORK}/copy/tail")" '998 999 1000 '
expect '--max refuses a larger file' \
    "$(python3 /usr/local/bin/cg-copy.py --max 100 "${layer}/usr/share/long.log" "${WORK}/copy/max" && echo copied || echo refused)" refused
ln "${layer}/usr/share/plain.txt" "${layer}/usr/share/hardlink.txt"
expect '--single-link refuses a hard link' \
    "$(python3 /usr/local/bin/cg-copy.py --single-link "${layer}/usr/share/plain.txt" "${WORK}/copy/hard" && echo copied || echo refused)" refused
rm -f "${layer}/usr/linkdir" "${layer}/usr/share/link.txt" "${layer}/usr/share/fifo" "${layer}/usr/share/hardlink.txt"

echo '# Page helper path checks (cg-pfn.py)'
ln -s /etc/hostname "${layer}/usr/lib/link.so"
mkdir -p "${layer}/usr/real"
echo x > "${layer}/usr/real/a.so"
ln -s "${layer}/usr/real" "${layer}/usr/via"
mkfifo "${layer}/usr/lib/fifo.so"
checks=$(LAYER="${layer}" timeout 20 python3 - <<'PY'
import importlib.util, os
spec = importlib.util.spec_from_file_location("pfn", "/usr/local/bin/cg-pfn.py")
pfn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pfn)
layer = os.environ["LAYER"]
print(" ".join(str(pfn.open_layer_file(p) is not None) for p in (
    f"{layer}/usr/lib/a.so",                      # a layer file
    "/etc/hostname",                              # outside the layers
    f"{layer}/../../../../../etc/hostname",       # escapes with ..
    f"{layer}/usr/lib/link.so",                   # symlink inside a layer
    f"{layer}/usr/via/a.so",                      # a symlinked directory on the way
    f"{layer}/usr/lib",                           # a directory
    f"{layer}/usr/lib/fifo.so",                   # a FIFO, without blocking
)))
PY
)
expect 'only regular layer files, no escapes, symlinks or FIFOs' "${checks}" 'True False False False False False False'
rm -rf "${layer}/usr/via" "${layer}/usr/real" "${layer}/usr/lib/fifo.so"
printf '%s a b\n' /etc/hostname > "${WORK}/outside.mismatch"
expect 'paths outside the layers are skipped' \
    "$(python3 /usr/local/bin/cg-pfn.py "${WORK}/outside.mismatch" "${WORK}/pages.log" test)" 'pages=0 skipped=0'
printf '%s\n' '2026-11-10T04:30:00+0000 check pfn=unknown phys=unknown bits=3 variants=1/5 page=0 /x' > "${WORK}/unknown.log"
expect 'an unknown page is no defective page' "$(logged_pfns "${WORK}/unknown.log" | wc -l | tr -d ' ')" 0
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
printf '%s\n' '2026-01-01T00:00:00+00:00 Crash handled: exit 139 (case old)' \
    "$(date -Iseconds) Crash handled: exit 139, Home Assistant running again (case new)" \
    "$(date -Iseconds) Crash (exit 139) NOT handled: limit of 3/h reached" \
    "$(date -Iseconds) Check (no drop): no corrupted files in the cache" > "${WORK}/events.log"
ENTITY_PREFIX=host NAME_PREFIX='Home Assistant Host' SLUG=c3edd230_crash_guard APP_VERSION=2.0.0 \
DISCOVERY_PREFIX=homeassistant PATH="/tests/stubs:${PATH}" EVENTS="${WORK}/events.log" bash -c '
    bashio::log.info() { echo "$*" >> "${PUBLISHED}.log"; }
    sleep() { return 0; }
    declare -a MQ=(-h broker)
    source /usr/local/lib/crash-guard/entities.sh
    cg_announce crashes memory_lock_problem
    : > "${RETAINED}"
    cg_announce crashes
    cg_drop_state memory_lock_problem
    cg_publish corrupted_cache "{\"state\":\"unknown\",\"attributes\":{\"friendly_name\":\"x\"}}"
    cg_publish crashes "{\"state\":\"on\",\"attributes\":{\"device_class\":\"problem\"}}"
    crash_status 24 "${EVENTS}" > "${PUBLISHED}.crashes"
' 2> "${WORK}/entities.log"
last() { awk -F '\t' -v t="$1" '$1 == t { v = $2 } END { print v }' "${PUBLISHED}"; }
first() { awk -F '\t' -v t="$1" '$1 == t { print $2; exit }' "${PUBLISHED}"; }
DEVICE=homeassistant/device/crash_guard/config
crashes=$(first "${DEVICE}" | jq -c .components.crashes)
expect 'crashes is a problem sensor with on/off' \
    "$(jq -r '.platform + .device_class + .payload_on + .payload_off' <<< "${crashes}")" binary_sensorproblemonoff
expect 'entity ID from the prefix' "$(jq -r .default_entity_id <<< "${crashes}")" binary_sensor.host_crashes
expect 'no attributes topic' "$(jq -r 'has("json_attributes_topic")' <<< "${crashes}")" false
expect 'device named after name_prefix' "$(first "${DEVICE}" | jq -r '.device.name + "|" + .device.identifiers[0]')" 'Home Assistant Host|crash_guard'
expect 'availability and origin' "$(first "${DEVICE}" | jq -r '.availability_topic + "|" + .origin.sw_version')" 'crash_guard/availability|2.0.0'
expect 'single entity topics of 1.2.0 migrated' "$(grep -c '{"migrate_discovery":true}' "${PUBLISHED}")" 2
expect 'and cleared after the device message' \
    "$(last homeassistant/sensor/crash_guard/crashes_window/config)|$(last homeassistant/binary_sensor/crash_guard/memory_lock/config)" '|'
expect 'memory lock problem removed with its platform only' \
    "$(grep "^${DEVICE}	" "${PUBLISHED}" | sed -n 2p | cut -f2 | jq -c .components.memory_lock_problem)" '{"platform":"binary_sensor"}'
expect 'then left out' "$(last "${DEVICE}" | jq -c '.components | keys')" '["crashes"]'
expect 'its state removed' "$(last crash_guard/memory_lock_problem)|$(last crash_guard/memory_lock_problem/attributes)" '|'
expect 'unknown state is sent as None' "$(last crash_guard/corrupted_cache)" None
expect 'state only, no attributes' "$(last crash_guard/crashes)|$(last crash_guard/crashes/attributes)" 'on|'
status=$(cat "${PUBLISHED}.crashes")
expect 'program crashes since boot' "$(jq -r .boot <<< "${status}")" 2
expect 'program crashes in the window, newest first' \
    "$(jq -c '[.window_hours, (.programs | map(.process + "/" + .signal))]' <<< "${status}")" '[24,["curl/7/BUS","python3/11/SEGV"]]'
expect 'Home Assistant crashes in the window, handled or not, newest first' \
    "$(jq -r '.home_assistant | map(.message | split(" ")[0:2] | join(" ")) | join("|")' <<< "${status}")" 'Crash (exit|Crash handled:'
expect 'a crash is a problem' "$(jq -r .problem <<< "${status}")" true
expect 'no errors' "$(cat "${WORK}/entities.log")" ''
: > "${WORK}/quiet.log"
CG_FIXTURES="${WORK}" PATH="/tests/stubs:${PATH}" bash -c '
    source /usr/local/lib/crash-guard/entities.sh 2> /dev/null
    crash_status 24 /nonexistent' > "${WORK}/quiet.json" 2> "${WORK}/quiet.log"
expect 'no crash, no problem' "$(jq -c '[.boot, (.programs | length), (.home_assistant | length), .problem]' "${WORK}/quiet.json")" '[0,0,0,false]'
expect 'the count of program crashes in the window' "$(jq -r .programs_count <<< "${status}")" 2
expect 'the configuration link of Home Assistant 2026.11' \
    "$(first "${DEVICE}" | jq -r .device.configuration_url)" homeassistant://config/app/c3edd230_crash_guard/info
expect 'the newest coredump cursor' "$(PATH="/tests/stubs:${PATH}" bash -c '
    source /usr/local/lib/crash-guard/entities.sh 2> /dev/null; crash_cursor')" 's=fixture;i=3'

echo '# MQTT credentials (entities.sh)'
mkdir -m 700 "${WORK}/secrets"
out=$(bash -c '
    source /usr/local/lib/crash-guard/entities.sh 2> /dev/null
    cg_credentials "$1" app "pass with spaces"
    echo "${XDG_CONFIG_HOME}"' _ "${WORK}/secrets")
expect 'the options files are used' "${out}" "${WORK}/secrets"
expect 'one option per line, the value the rest of it' "$(tr '\n' '|' < "${WORK}/secrets/mosquitto_sub")" '-u app|-P pass with spaces|'
expect 'the same for mosquitto_pub' "$(cmp -s "${WORK}/secrets/mosquitto_pub" "${WORK}/secrets/mosquitto_sub" && echo same)" same
expect 'readable by root only' "$(stat -c %a "${WORK}/secrets/mosquitto_pub")" 600

echo '# Availability held with a last will (entities.sh)'
: > "${PUBLISHED}"
rm -f "${PUBLISHED}.sub"
PATH="/tests/stubs:${PATH}" CG_HOLD_SETTLE=1 CG_HOLD_RETRY=1 HOSTNAME=test bash -c '
    declare -a MQ=(-h broker)
    source /usr/local/lib/crash-guard/entities.sh 2> /dev/null
    cg_hold_availability' &
holder=$!
# The stub connection is "sleep 600"; there is no pkill in the image (under
# emulation the command line may start with the emulator)
drop_connection() {
    local p
    for p in /proc/[0-9]*; do
        [[ "$(tr '\0' ' ' < "${p}/cmdline" 2> /dev/null)" == *'sleep 600 ' ]] && kill "${p#/proc/}"
    done
}
online() { grep -c "^crash_guard/availability	online$" "${PUBLISHED}"; }
for _ in $(seq 1 150); do (( $(online) > 0 )) && break; sleep 0.2; done
expect 'connects with the last will' "$(grep -c -- '--will-payload offline --will-retain' "${PUBLISHED}.sub")" 1
expect 'and says online' "$(online)" 1
# The connection drops, as when the broker restarts: the broker sends the will
drop_connection
for _ in $(seq 1 150); do (( $(online) > 1 )) && break; sleep 0.2; done
expect 'connects again and says online again' "$(online)|$(wc -l < "${PUBLISHED}.sub" | tr -d ' ')" '2|2'
kill -TERM "${holder}"
wait "${holder}" 2> /dev/null
drop_connection

echo '# Entities while the MQTT broker is away'
: > "${PUBLISHED}"; rm -f "${PUBLISHED}.calls"
ENTITY_PREFIX=host NAME_PREFIX='Home Assistant Host' SLUG=c3edd230_crash_guard \
DISCOVERY_PREFIX=homeassistant PATH="/tests/stubs:${PATH}" PUB_FAIL=1 bash -c '
    declare -a MQ=(-h broker)
    source /usr/local/lib/crash-guard/entities.sh
    cg_publish crashes "{\"state\":\"off\"}" && echo sent || echo "failed: ${PUB_ERROR}"
    cg_publish crashes "{\"state\":\"on\"}" && echo sent || echo "failed: ${PUB_ERROR}"
' > "${WORK}/away.out" 2> "${WORK}/away.log"
expect 'a state the broker does not take fails, with the reason' "$(sed -n 1p "${WORK}/away.out")" 'failed: Error: Connection refused'
expect 'sent once the broker is back' "$(sed -n 2p "${WORK}/away.out")|$(last crash_guard/crashes)" 'sent|on'
expect 'no raw errors' "$(cat "${WORK}/away.log")" ''
rm -f "${PUBLISHED}.calls"

echo '# Web page without root (web/run)'
mkdir -p "${WORK}/bin" /data/crash_guard/cases/old /data/crash_guard/ref
printf 'log' > /data/crash_guard/cases/old/home-assistant.log.fault
chmod 600 /data/crash_guard/cases/old/home-assistant.log.fault
touch /data/crash_guard/check.requested /data/crash_guard/check.request /data/crash_guard/status.json
# s6-setuidgid records how the page would start
cat > "${WORK}/bin/s6-setuidgid" <<'STUB'
#!/bin/bash
printf '%s|%s\n' "$*" "${SUPERVISOR_TOKEN-none}" > "${STARTED}"
STUB
chmod +x "${WORK}/bin/s6-setuidgid"
SUPERVISOR_TOKEN=secret STARTED="${WORK}/started" PATH="${WORK}/bin:${PATH}" bash -c '
    source /usr/lib/bashio/bashio.sh
    source "$0"
' /etc/s6-overlay/s6-rc.d/web/run > "${WORK}/web.log" 2>&1
expect 'starts the page as cgweb without the Supervisor token' "$(cat "${WORK}/started" 2>/dev/null)" \
    'cgweb python3 /usr/local/lib/crash_guard_web.py|none'
expect 'folders readable for the group only, passing it on' \
    "$(stat -c '%G %a' /data/crash_guard /data/crash_guard/cases/old /data/crash_guard/ref | tr '\n' '|')" \
    'cgweb 2750|cgweb 2750|cgweb 2750|'
expect 'files readable for the group, not for others' \
    "$(stat -c '%U %G %a' /data/crash_guard/status.json /data/crash_guard/cases/old/home-assistant.log.fault | tr '\n' '|')" \
    'root cgweb 640|root cgweb 640|'
expect 'the page writes to its own folder only' "$(stat -c '%U %G %a' /data/crash_guard/web)" 'cgweb cgweb 2750'
expect 'requests of 2.2.0 removed' "$(find /data/crash_guard -maxdepth 1 -name 'check.*' | wc -l | tr -d ' ')" 0
expect 'the page user cannot reach the Docker socket' "$(id -Gn cgweb)" cgweb
(umask 027; mkdir -p /data/crash_guard/cases/new; printf x > /data/crash_guard/cases/new/kernel.txt)
expect 'what the guard writes later gets the group' "$(stat -c '%G %a' /data/crash_guard/cases/new/kernel.txt)" 'cgweb 640'
expect 'the copy tool writes for the group' "$(python3 /usr/local/bin/cg-copy.py /etc/hostname "${WORK}/copied" && stat -c '%a' "${WORK}/copied")" 640
rm -rf /data/crash_guard

rm -rf "${L}"
if (( FAILED )); then
    exit 1
fi
echo 'All tests passed'
