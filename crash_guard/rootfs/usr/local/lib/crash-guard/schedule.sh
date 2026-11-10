# shellcheck shell=bash
# ==============================================================================
# Crash Guard: when a check time (drop_times) is due. Pure functions, sourced
# by the guard service and by the tests.
#
# A time is due once its minute has come, not only within that very minute:
# the main loop sleeps check_interval seconds and can be busy for minutes
# (a crash, a requested check). A time later than SCHEDULE_LATE_MAX is left
# out for that day rather than run at an unexpected moment; times already
# past when the service starts are not caught up either, so a start never
# begins with a check (or, with drop_on_schedule, a cache drop) in the busy
# boot phase.
# ==============================================================================

# Seconds a check may start after its time
readonly SCHEDULE_LATE_MAX=3600

# Day and time a check time stands for today: "YYYY-MM-DD HH:MM".
# $1 = HH:MM, $2 = now (epoch seconds)
schedule_key() { echo "$(date -d "@$2" +%F) $1"; }

# True when HH:MM ($1) is due at $2 (epoch seconds) and has not run today;
# $3 = the key of its last run (schedule_key), empty if none
schedule_due() {
    local at
    at=$(date -d "$(date -d "@$2" +%F) $1" +%s 2> /dev/null) || return 1
    (( $2 >= at && $2 - at <= SCHEDULE_LATE_MAX )) || return 1
    [[ "$3" != "$(schedule_key "$1" "$2")" ]]
}
