#!/bin/bash
# ==============================================================================
# adopt_changes <reference.sha> <changed file>
# Takes the new hashes of files that changed on the drive (lines "path
# expected-hash actual-hash" from cg-hash.sh) into the reference and prints how
# many were taken. A line only counts when its expected hash is still the one
# in the reference.
# ==============================================================================
adopt_changes() {
    local ref=$1 changed=$2 n
    [[ -s "${ref}" && -s "${changed}" ]] || { echo 0; return 0; }
    n=$(awk 'NR == FNR { c[$1] = $2 " " $3; next }
             ($2 in c) { split(c[$2], h, " "); if (h[1] == $1) n++ }
             END { print n + 0 }' "${changed}" "${ref}")
    awk 'NR == FNR { c[$1] = $2 " " $3; next }
         ($2 in c) { split(c[$2], h, " "); if (h[1] == $1) { print h[2] "  " $2; next } }
         { print }' "${changed}" "${ref}" > "${ref}.new" && mv -f -- "${ref}.new" "${ref}"
    echo "${n}"
}
