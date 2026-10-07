#!/bin/bash
# ==============================================================================
# Runs in the helper container with the Home Assistant layers read-only under
# their host path. Hashes all program files of the Home Assistant container
# (through the host's page cache), optionally compares them with a reference
# and saves differing files right away.
#
# cg-hash.sh <layers file> <output.sha> [reference.sha] [copy directory]
# ==============================================================================
set -uo pipefail
layers=$1 out=$2 ref=${3:-} copy=${4:-}

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
done | xargs -0 -r sha256sum > "${out}"

[[ -n "${ref}" && -s "${ref}" ]] || { echo "hashed=$(wc -l < "${out}") mismatches=-"; exit 0; }

# Output: path expected-hash actual-hash
awk 'NR == FNR { r[$2] = $1; next } ($2 in r) && r[$2] != $1 { print $2, r[$2], $1 }' \
    "${ref}" "${out}" > "${out%.sha}.mismatch"
n=$(wc -l < "${out%.sha}.mismatch")

if [[ -n "${copy}" && "${n}" -gt 0 ]]; then
    mkdir -p "${copy}"
    while read -r path _; do
        # Checked again: the mismatch list is text, and the file may have changed
        in_layers "${path}" || continue
        # Opens every directory on the way without following symlinks
        python3 /usr/local/bin/cg-copy.py "${path}" \
            "${copy}/$(tr '/' '_' <<< "${path#/mnt/data/docker/overlay2/}")" || true
    done < "${out%.sha}.mismatch"
fi
echo "hashed=$(wc -l < "${out}") mismatches=${n}"
