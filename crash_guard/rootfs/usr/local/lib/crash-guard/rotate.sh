#!/bin/bash
# ==============================================================================
# Keeps the newest cases in a folder and removes the older ones.
# rotate.sh <folder> <cases to keep>
# A case is a folder named <yyyymmdd>-<hhmmss>-<kind>; nothing else is
# touched, and symlinks are never followed.
# ==============================================================================
set -euo pipefail
dir=$1 keep=$2
[[ "${keep}" =~ ^[0-9]+$ ]] || { echo "invalid number: ${keep}" >&2; exit 1; }
cd -P -- "${dir}"
mapfile -t cases < <(find . -mindepth 1 -maxdepth 1 -type d -regextype posix-extended \
    -regex '\./[0-9]{8}-[0-9]{6}-[a-z]+' -printf '%f\n' | sort)
(( ${#cases[@]} > keep )) || exit 0
for c in "${cases[@]:0:${#cases[@]}-keep}"; do
    rm -rf -- "./${c}"
done
echo "removed $(( ${#cases[@]} - keep ))"
