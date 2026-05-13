#!/usr/bin/env bash
# wasp health — runs probes and exits non-zero if any critical check fails.
set -Eeuo pipefail

WASP_DIR="${WASP_INSTALL_DIR:-$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)}"
COMPOSE=( docker compose --project-directory "$WASP_DIR" )

QUIET=false
[[ "${1:-}" == "--quiet" ]] && QUIET=true

# shellcheck source=../lib/ui.sh
source "${WASP_DIR}/lib/ui.sh"

PASS=0; FAIL=0; WARN=0
# Pre-increment ((++X)) returns the new value (truthy ≥1 ⇒ exit 0). Post-
# increment ((X++)) returns the OLD value, which is 0 the first time, so
# `((X++))` exits with code 1 and `set -e` aborts the script.
check_pass() {
    ((++PASS))
    $QUIET || printf "${C_GREEN}✓${C_RESET} %s\n" "$1"
}
check_warn() {
    ((++WARN))
    $QUIET || printf "${C_YELLOW}!${C_RESET} %s — ${C_DIM}%s${C_RESET}\n" "$1" "$2"
}
check_fail() {
    ((++FAIL))
    $QUIET || printf "${C_RED}✗${C_RESET} %s\n   ${C_DIM}fix:${C_RESET} %s\n" "$1" "$2"
}

$QUIET || ui_log "${C_BOLD}WASP health${C_RESET}"
$QUIET || ui_hr

# 1. Docker daemon
if docker info >/dev/null 2>&1; then
    check_pass "Docker daemon reachable"
else
    check_fail "Docker daemon not reachable" "sudo systemctl start docker"
fi

# 2. Containers
if ! "${COMPOSE[@]}" ps --format json >/dev/null 2>&1; then
    check_fail "compose project not initialized" "cd $WASP_DIR && wasp start"
else
    services_json="$("${COMPOSE[@]}" ps --format json 2>/dev/null || true)"
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        name="$(echo "$line" | jq -r '.Service // .Name // empty' 2>/dev/null)"
        state="$(echo "$line" | jq -r '.State // empty' 2>/dev/null)"
        [[ -z "$name" ]] && continue
        case "$state" in
            running)     check_pass "Container ${name}: running" ;;
            exited|dead) check_fail "Container ${name}: ${state}" "wasp logs ${name}" ;;
            restarting)  check_warn "Container ${name}: restarting" "wasp logs ${name}" ;;
            *)           check_warn "Container ${name}: ${state:-unknown}" "wasp logs ${name}" ;;
        esac
    done <<< "$services_json"
fi

# 3. Redis
if "${COMPOSE[@]}" exec -T agent-redis redis-cli ping 2>/dev/null | grep -q PONG; then
    check_pass "Redis responding to PING"
else
    check_fail "Redis not responding" "wasp logs agent-redis"
fi

# 4. Postgres
if "${COMPOSE[@]}" exec -T agent-postgres psql -U agent -d agent -tAc 'SELECT 1' 2>/dev/null | grep -q 1; then
    check_pass "Postgres SELECT 1 returned"
else
    check_fail "Postgres query failed" "wasp logs agent-postgres"
fi

# 5. Dashboard HTTP
dash_url="http://127.0.0.1:8080"
if curl -fsS -o /dev/null -m 5 "${dash_url}"; then
    check_pass "Dashboard reachable at ${dash_url}"
else
    check_fail "Dashboard not reachable at ${dash_url}" "wasp logs agent-core | tail -50"
fi

# 6. Scheduler heartbeat
# The scheduler persists `scheduler:job_state` (a JSON map of job → {last_run,
# run_count, ...}). We treat any job with last_run within the last 3 minutes
# as proof of life. On a fresh install the first persist can take 30-90s,
# so during that warm-up window we report "warming up" instead of warning.
sched_state="$("${COMPOSE[@]}" exec -T agent-redis redis-cli GET "scheduler:job_state" 2>/dev/null)"
sched_alive=false
if [[ -n "$sched_state" && "$sched_state" != "(nil)" ]]; then
    # Newest last_run timestamp across all jobs, in epoch seconds
    newest_run_iso="$(printf '%s' "$sched_state" | jq -r '[.[].last_run] | max' 2>/dev/null || true)"
    if [[ -n "$newest_run_iso" && "$newest_run_iso" != "null" ]]; then
        newest_run_epoch="$(date -d "$newest_run_iso" +%s 2>/dev/null || echo 0)"
        now_epoch="$(date +%s)"
        age=$(( now_epoch - newest_run_epoch ))
        if (( newest_run_epoch > 0 && age < 180 )); then
            sched_alive=true
        fi
    fi
fi
if $sched_alive; then
    check_pass "Scheduler ticking (last job ran ${age}s ago)"
else
    # Distinguish "fresh boot, not yet warmed up" from "actually stuck"
    core_id="$("${COMPOSE[@]}" ps -q agent-core 2>/dev/null | head -n1)"
    core_uptime_sec=0
    if [[ -n "$core_id" ]]; then
        started_at_iso="$(docker inspect "$core_id" --format '{{.State.StartedAt}}' 2>/dev/null)"
        started_epoch="$(date -d "$started_at_iso" +%s 2>/dev/null || echo 0)"
        if (( started_epoch > 0 )); then
            core_uptime_sec=$(( $(date +%s) - started_epoch ))
        fi
    fi
    if (( core_uptime_sec > 0 && core_uptime_sec < 120 )); then
        check_pass "Scheduler warming up (container started ${core_uptime_sec}s ago — first tick can take up to ~90s)"
    else
        check_warn "Scheduler is not ticking" "Container has been up ${core_uptime_sec}s; check 'wasp logs agent-core'"
    fi
fi

# 7. Telegram bridge configured
if [[ -f "${WASP_DIR}/.env" ]] && grep -qE '^TELEGRAM_BOT_TOKEN=.+' "${WASP_DIR}/.env"; then
    check_pass "Telegram token set in .env"
else
    check_warn "Telegram token not set" "Run: wasp onboard"
fi

$QUIET || {
    printf "\n${C_BOLD}Summary:${C_RESET} %d passed, %d warnings, %d failed\n" "$PASS" "$WARN" "$FAIL"
}

if (( FAIL > 0 )); then exit 1; fi
exit 0
