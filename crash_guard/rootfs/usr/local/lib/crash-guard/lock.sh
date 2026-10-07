# shellcheck shell=bash
# ==============================================================================
# Crash Guard: the RAM lock layout and the page frame numbers from badpages.log.
# Pure functions, sourced by the guard service and by the tests.
# ==============================================================================

# Every defective page locks the aligned 2 MB block around it
readonly LOCK_SIZE=$(( 0x200000 ))

# Sets LOCK_NAME (overlay name), LOCK_BLOCKS (block start addresses) and
# LOCK_RANGE (human-readable) for the given page frame numbers (0x...);
# pages that are no well-formed hexadecimal number are skipped. All three
# stay empty without valid pages.
# shellcheck disable=SC2034  # the three globals are read by the caller
lock_layout() {
    local page block name='badram'
    LOCK_NAME='' LOCK_RANGE=''
    LOCK_BLOCKS=()
    for page in "$@"; do
        [[ "${page}" =~ ^0x[0-9a-fA-F]{1,16}$ ]] || continue
        name+="-$(printf '%x' "$(( page ))")"
        block=$(( page * 4096 & ~(LOCK_SIZE - 1) ))
        [[ " ${LOCK_BLOCKS[*]} " == *" ${block} "* ]] || LOCK_BLOCKS+=("${block}")
    done
    (( ${#LOCK_BLOCKS[@]} > 0 )) || return 0
    LOCK_NAME=${name}
    for block in "${LOCK_BLOCKS[@]}"; do
        LOCK_RANGE+="${LOCK_RANGE:+, }$(printf '0x%x-0x%x' "${block}" $(( block + LOCK_SIZE - 1 )))"
    done
    LOCK_RANGE+=" ($(( ${#LOCK_BLOCKS[@]} * LOCK_SIZE / 1048576 )) MB)"
}

# Page frame numbers from a badpages.log ($1), one per line. Only well-formed
# numbers pass, because bash arithmetic would evaluate anything else as code.
logged_pfns() {
    [[ -s "$1" ]] || return 0
    awk '{sub("pfn=", "", $3); print $3}' "$1" | grep -xE '0x[0-9a-f]{1,16}'
}

# Logged pages ($1 = badpages.log) that lie in none of the LOCK_BLOCKS
pages_outside() {
    logged_pfns "$1" | sort -u | while read -r pfn; do
        local addr=$(( pfn * 4096 )) inside=0 block
        for block in "${LOCK_BLOCKS[@]}"; do
            (( addr >= block && addr < block + LOCK_SIZE )) && inside=1
        done
        (( inside )) || echo "${pfn}"
    done
}
