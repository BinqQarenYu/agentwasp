#!/usr/bin/env bash
# WASP — shared CLI UI helpers (colors, logo, spinner, step counter, menus)

# ── Colors ─────────────────────────────────────────────────────────────
if [[ -t 1 ]] && [[ "${NO_COLOR:-}" == "" ]]; then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_DIM=$'\033[2m'
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'
    C_MAGENTA=$'\033[35m'
    C_CYAN=$'\033[36m'
    C_WHITE=$'\033[37m'
    C_GOLD=$'\033[38;5;220m'
    C_AMBER=$'\033[38;5;214m'
    C_ORANGE=$'\033[38;5;208m'
    UI_TTY=true
else
    C_RESET=''; C_BOLD=''; C_DIM=''
    C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''
    C_MAGENTA=''; C_CYAN=''; C_WHITE=''
    C_GOLD=''; C_AMBER=''; C_ORANGE=''
    UI_TTY=false
fi

# ── Print helpers ─────────────────────────────────────────────────────
ui_log()  { printf "%b\n" "$*"; }
ui_info() { printf "${C_BLUE}${C_BOLD}▸${C_RESET} %s\n" "$*"; }
ui_ok()   { printf "${C_GREEN}${C_BOLD}✓${C_RESET} %s\n" "$*"; }
ui_warn() { printf "${C_YELLOW}${C_BOLD}!${C_RESET} %s\n" "$*"; }
ui_err()  { printf "${C_RED}${C_BOLD}✗${C_RESET} %s\n" "$*" >&2; }
ui_hr()   { printf "${C_DIM}────────────────────────────────────────────────────────────${C_RESET}\n"; }
ui_section() { printf "\n${C_BOLD}%s${C_RESET}\n" "$*"; }

UI_STEP_TOTAL=0
UI_STEP_CURRENT=0
ui_step_init() { UI_STEP_TOTAL="$1"; UI_STEP_CURRENT=0; }
ui_step() {
    UI_STEP_CURRENT=$(( UI_STEP_CURRENT + 1 ))
    printf "\n${C_CYAN}${C_BOLD}[%d/%d]${C_RESET} ${C_BOLD}%s${C_RESET}\n" \
        "$UI_STEP_CURRENT" "$UI_STEP_TOTAL" "$*"
}

# ── Logo (static, plain WASP wordmark in gold) ─────────────────────────
ui_logo() {
    [[ "${WASP_LOGO_SHOWN:-}" == "true" ]] && return 0
    export WASP_LOGO_SHOWN=true
    local subtitle="${1:-autonomous agent · self-hosted}"
    printf "\n"
    local lines=(
        '       ██╗    ██╗  █████╗  ███████╗ ██████╗ '
        '       ██║    ██║ ██╔══██╗ ██╔════╝ ██╔══██╗'
        '       ██║ █╗ ██║ ███████║ ███████╗ ██████╔╝'
        '       ██║███╗██║ ██╔══██║ ╚════██║ ██╔═══╝ '
        '       ╚███╔███╔╝ ██║  ██║ ███████║ ██║     '
        '        ╚══╝╚══╝  ╚═╝  ╚═╝ ╚══════╝ ╚═╝     '
    )
    for ln in "${lines[@]}"; do
        printf "%b%s%b\n" "${C_GOLD}${C_BOLD}" "$ln" "${C_RESET}"
    done
    printf "${C_DIM}       🐝  %s${C_RESET}\n" "$subtitle"
    printf "${C_DIM}       🌐  ${C_BOLD}agentwasp.com${C_RESET}\n\n"
}


ui_logo_compact() {
    printf "${C_GOLD}${C_BOLD}WASP${C_RESET} ${C_DIM}· autonomous agent${C_RESET}\n"
}

# ── Spinner (handles wait failure properly under set -e) ──────────────
ui_run() {
    local label="$1"
    shift
    [[ "${1:-}" == "--" ]] && shift
    local logfile; logfile="$(mktemp)"
    local spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local i=0

    if [[ "$UI_TTY" == "true" ]]; then
        ( "$@" >"$logfile" 2>&1 ) &
        local pid=$!
        printf '\033[?25l'
        while kill -0 "$pid" 2>/dev/null; do
            printf "\r${C_BLUE}%s${C_RESET} %s" "${spin:i:1}" "$label"
            i=$(( (i + 1) % ${#spin} ))
            sleep 0.1
        done
        # CRITICAL: wait may return non-zero; do NOT let set -e kill us here
        local rc=0
        wait "$pid" || rc=$?
        printf '\033[?25h\r\033[K'
        if [[ $rc -eq 0 ]]; then
            ui_ok "$label"
            rm -f "$logfile"
            return 0
        fi
        ui_err "$label — failed (exit $rc)"
        printf "${C_DIM}── last 40 lines of output ──${C_RESET}\n"
        tail -40 "$logfile"
        rm -f "$logfile"
        return $rc
    else
        ui_info "$label"
        local rc=0
        "$@" 2>&1 | tee -a "$logfile" >&2 || rc=${PIPESTATUS[0]}
        if [[ $rc -eq 0 ]]; then
            ui_ok "$label"
            rm -f "$logfile"
            return 0
        else
            ui_err "$label — failed"
            rm -f "$logfile"
            return $rc
        fi
    fi
}

# ── Numbered menu (stderr-routed; only value to stdout) ───────────────
ui_menu() {
    local prompt="$1"
    local default_idx="$2"
    shift 2
    local options=("$@")
    local n="${#options[@]}"

    {
        [[ -n "$prompt" ]] && printf "${C_BOLD}%s${C_RESET}\n" "$prompt"
        local i=1
        for opt in "${options[@]}"; do
            local label="${opt%%|*}"
            local marker=""
            if (( i == default_idx )); then
                marker="  ${C_DIM}[default]${C_RESET}"
            fi
            printf "  ${C_CYAN}%2d)${C_RESET} %s%b\n" "$i" "$label" "$marker"
            (( i++ ))
        done
        printf "\n"
    } >&2

    local choice
    while true; do
        printf "  ${C_DIM}Pick a number [1-%d, default %d]: ${C_RESET}" "$n" "$default_idx" >&2
        read -r choice
        choice="${choice:-$default_idx}"
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= n )); then
            local picked="${options[choice-1]}"
            printf "%s" "${picked##*|}"
            return 0
        fi
        printf "  ${C_YELLOW}!${C_RESET} Invalid — pick 1 to %d.\n" "$n" >&2
    done
}

# ── Arrow-key interactive selector ─────────────────────────────────────
# Like ui_menu but with ↑↓ navigation. Falls back to ui_menu if stdin
# is not a TTY (e.g. piped input, non-interactive ssh).
ui_select() {
    local prompt="$1"
    local default_idx="$2"
    shift 2
    local options=("$@")
    local n="${#options[@]}"

    # Need real TTY for raw mode
    if [[ ! -r /dev/tty ]] || [[ "$UI_TTY" != "true" ]]; then
        ui_menu "$prompt" "$default_idx" "${options[@]}"
        return $?
    fi

    local selected=$(( default_idx - 1 ))

    # Render initial menu (to stderr)
    {
        [[ -n "$prompt" ]] && printf "${C_BOLD}%s${C_RESET}\n" "$prompt"
        printf "${C_DIM}  ↑/↓ to move, Enter to select, q to cancel${C_RESET}\n\n"
        for ((i=0; i<n; i++)); do
            local label="${options[i]%%|*}"
            if (( i == selected )); then
                printf "  ${C_GOLD}${C_BOLD}▸  %s${C_RESET}\n" "$label"
            else
                printf "   ${C_DIM} %s${C_RESET}\n" "$label"
            fi
        done
    } >&2

    # Save terminal state, enter raw mode (read from /dev/tty so we don't
    # collide with stdin if the script is being piped through anything)
    local stty_saved
    stty_saved="$(stty -g </dev/tty)"
    stty -echo -icanon min 1 time 0 </dev/tty
    # Ensure terminal is restored even on Ctrl+C / errors
    trap 'stty "'"$stty_saved"'" </dev/tty 2>/dev/null; printf "\033[?25h" >&2; trap - INT TERM EXIT' INT TERM EXIT
    printf '\033[?25l' >&2  # hide cursor

    local key esc1 esc2
    while true; do
        IFS= read -rsn1 key </dev/tty
        case "$key" in
            $'\x1b')
                # Could be ESC alone, or arrow key (\033 [ A/B/C/D)
                IFS= read -rsn1 -t 0.005 esc1 </dev/tty 2>/dev/null || esc1=""
                IFS= read -rsn1 -t 0.005 esc2 </dev/tty 2>/dev/null || esc2=""
                if [[ "$esc1" == "[" ]]; then
                    case "$esc2" in
                        A) selected=$(( (selected - 1 + n) % n )) ;;
                        B) selected=$(( (selected + 1) % n )) ;;
                    esac
                fi
                ;;
            "")
                # Enter
                break
                ;;
            q|Q)
                stty "$stty_saved" </dev/tty
                printf '\033[?25h' >&2
                trap - INT TERM EXIT
                printf "" # empty value
                return 130
                ;;
            [0-9])
                # Direct number entry — read more digits if any, then enter
                local num="$key"
                local digit
                IFS= read -rsn1 -t 0.5 digit </dev/tty 2>/dev/null && \
                    [[ "$digit" =~ ^[0-9]$ ]] && num="$num$digit"
                if (( num >= 1 && num <= n )); then
                    selected=$(( num - 1 ))
                fi
                ;;
        esac

        # Redraw menu in place (move cursor up n + 1 [hint] lines, redraw)
        local move_up=$(( n + 1 ))  # +1 for blank separator
        printf '\033[%dA' "$move_up" >&2
        printf "\033[K\n" >&2  # clear separator line
        for ((i=0; i<n; i++)); do
            local label="${options[i]%%|*}"
            printf "\033[K" >&2
            if (( i == selected )); then
                printf "  ${C_GOLD}${C_BOLD}▸  %s${C_RESET}\n" "$label" >&2
            else
                printf "   ${C_DIM} %s${C_RESET}\n" "$label" >&2
            fi
        done
    done

    stty "$stty_saved" </dev/tty
    printf '\033[?25h' >&2
    trap - INT TERM EXIT

    local picked="${options[selected]}"
    printf "%s" "${picked##*|}"
}
