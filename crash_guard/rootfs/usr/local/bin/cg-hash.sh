#!/bin/bash
# ==============================================================================
# Runs in the helper container with the Home Assistant layers read-only under
# their host path. Hashes all program files of the Home Assistant container
# (through the host's page cache), optionally compares them with a reference
# and saves differing files right away.
#
# cg-hash.sh [--direct] <layers file> <output.sha> [reference.sha] [copy directory]
#
# --direct hashes every file straight from the drive (O_DIRECT) instead of
# through the cache; with the hashes from the cache that makes a reference
# without dropping the cache.
#
# A file that differs from the reference is read once more straight from the
# drive (O_DIRECT, past the page cache). Equal to what the cache held, it was
# changed on the drive - for example a package Home Assistant installed - and
# goes to <output>.changed instead of <output>.mismatch. Only a difference
# between the cache and the drive is a corruption in RAM.
#
# Of the corrupted files at most COPY_MAX are copied, each up to COPY_SIZE
# bytes, and only while the data partition keeps COPY_FREE bytes free: the
# upper layer of the Home Assistant container is writable from inside it,
# and a flood of files that look corrupted must not fill the drive.
# ==============================================================================
set -uo pipefail
direct=0
if [[ "${1:-}" == --direct ]]; then
    direct=1
    shift
fi
layers=$1 out=$2 ref=${3:-} copy=${4:-}
readonly COPY_MAX=20 COPY_SIZE=$(( 10 * 1024 * 1024 )) COPY_FREE=$(( 1024 * 1024 * 1024 ))

# Only Docker layer directories; the list comes from the app's private /data,
# and anything else is skipped anyway
declare -a dirs=()
while read -r dir; do
    [[ "${dir}" =~ ^/mnt/data/docker/overlay2/[0-9a-f]{64}(-init)?/diff$ && ! -L "${dir}" ]] && dirs+=("${dir}")
done < "${layers}"

# A path of one of those layers: no control characters, spaces or backslashes
# (sha256sum would escape them, and a newline could smuggle in another path),
# no "..", a regular file and no symlink
in_layers() {
    local d
    [[ "$1" =~ ^[[:print:]]+$ && "$1" != *[[:space:]\\]* && "$1" != *..* && -f "$1" && ! -L "$1" ]] || return 1
    for d in "${dirs[@]}"; do
        [[ "$1" == "${d}"/* ]] && return 0
    done
    return 1
}

# Names are passed NUL-separated, so a newline in a name stays in that name;
# such names are then dropped
for dir in "${dirs[@]}"; do
    find "${dir}" -type f \( -path '*/usr/local/*' -o -path '*/usr/src/homeassistant/*' \
        -o -path '*/usr/lib/*' -o -path '*/lib/*' \) -print0 2>/dev/null
done | while IFS= read -r -d '' path; do
    in_layers "${path}" && printf '%s\0' "${path}"
done | if (( direct )); then
    python3 /usr/local/bin/cg-direct-hash.py
else
    xargs -0 -r sha256sum
fi > "${out}"

[[ -n "${ref}" && -s "${ref}" ]] || { echo "hashed=$(wc -l < "${out}") mismatches=-"; exit 0; }

# Hash of the file as it is on the drive; empty if O_DIRECT is not possible
direct_hash() {
    local h
    h=$(dd if="$1" iflag=direct bs=1M status=none | sha256sum) || return 1
    echo "${h%% *}"
}

# Output: path expected-hash actual-hash
awk 'NR == FNR { r[$2] = $1; next } ($2 in r) && r[$2] != $1 { print $2, r[$2], $1 }' \
    "${ref}" "${out}" > "${out%.sha}.differ"
: > "${out%.sha}.mismatch"
: > "${out%.sha}.changed"
while read -r path expected actual; do
    # Not readable from the drive: counted as corrupted, never silently adopted
    if in_layers "${path}" && [[ "$(direct_hash "${path}")" == "${actual}" ]]; then
        echo "${path} ${expected} ${actual}" >> "${out%.sha}.changed"
    else
        echo "${path} ${expected} ${actual}" >> "${out%.sha}.mismatch"
    fi
done < "${out%.sha}.differ"
rm -f "${out%.sha}.differ"
n=$(wc -l < "${out%.sha}.mismatch")
c=$(wc -l < "${out%.sha}.changed")

if [[ -n "${copy}" && "${n}" -gt 0 ]]; then
    mkdir -p "${copy}"
    copied=0
    while read -r path _; do
        (( copied < COPY_MAX )) || break
        # Checked again: the mismatch list is text, and the file may have changed
        in_layers "${path}" || continue
        free=$(df -P -B1 -- "${copy}" 2> /dev/null | awk 'NR == 2 { print $4 }')
        [[ "${free}" =~ ^[0-9]+$ ]] && (( free > COPY_FREE )) || break
        # Opens every directory on the way without following symlinks; a
        # file larger than COPY_SIZE is not copied and does not count
        python3 /usr/local/bin/cg-copy.py --max "${COPY_SIZE}" "${path}" \
            "${copy}/$(tr '/' '_' <<< "${path#/mnt/data/docker/overlay2/}")" && copied=$(( copied + 1 ))
    done < "${out%.sha}.mismatch"
fi
echo "hashed=$(wc -l < "${out}") mismatches=${n} changed=${c}"
