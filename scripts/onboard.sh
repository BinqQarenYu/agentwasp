#!/usr/bin/env bash
# wasp onboard — interactive .env wizard with menus.
set -Eeuo pipefail

WASP_DIR="${WASP_INSTALL_DIR:-$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)}"
ENV_FILE="${WASP_DIR}/.env"
ONBOARD_MARKER="${WASP_DIR}/.wasp-onboarded"

# shellcheck source=../lib/ui.sh
source "${WASP_DIR}/lib/ui.sh"

[[ -f "$ENV_FILE" ]] || { ui_err "No .env at $ENV_FILE — run install.sh first"; exit 1; }

FIRST_RUN=false
[[ "${1:-}" == "--first-run" ]] && FIRST_RUN=true

# ── .env helpers ───────────────────────────────────────────────────────
get_env() { grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true; }

set_env() {
    local key="$1" val="$2"
    local tmp; tmp="$(mktemp)"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        awk -v k="$key" -v v="$val" -F= '
            $1 == k { print k "=" v; next }
            { print }
        ' "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE"
    else
        cp "$ENV_FILE" "$tmp"
        printf "%s=%s\n" "$key" "$val" >> "$tmp"
        mv "$tmp" "$ENV_FILE"
    fi
    chmod 600 "$ENV_FILE"
}

mask() {
    local s="$1"
    if [[ -z "$s" ]]; then printf "(empty)";
    elif [[ ${#s} -le 8 ]]; then printf "***";
    else printf "%s...%s" "${s:0:4}" "${s: -4}"; fi
}

# Discard any buffered lines still sitting in stdin. Defends the wizard
# against multi-line clipboard pastes: the user might paste a block of bot
# metadata into the first prompt, leaving the remaining lines queued for
# subsequent `read` calls. Without this, each line in the paste gets eaten
# by the next prompt as if the user had answered it. Called after every
# read so trailing lines never leak across prompts.
drain_stdin() {
    local _junk
    while IFS= read -r -t 0.02 _junk 2>/dev/null; do :; done
}

# Trim leading/trailing whitespace and strip CR (the latter sneaks in from
# Windows-encoded clipboards).
sanitize() {
    local s="$1"
    s="${s//$'\r'/}"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf "%s" "$s"
}

ask() {
    local prompt="$1" var="$2" default="${3:-$(get_env "$2")}"
    local input
    if [[ -n "$default" ]]; then
        read -r -p "  $(printf "%s [%s]: " "$prompt" "$default")" input
        drain_stdin
        input="$(sanitize "$input")"
        printf "%s" "${input:-$default}"
    else
        read -r -p "  $(printf "%s: " "$prompt")" input
        drain_stdin
        input="$(sanitize "$input")"
        printf "%s" "$input"
    fi
}

ask_secret() {
    local prompt="$1" var="$2" current; current="$(get_env "$var")"
    local cur_disp; cur_disp="$(mask "$current")"
    local input
    read -r -s -p "  $(printf "%s [current: %s] (enter to keep): " "$prompt" "$cur_disp")" input
    drain_stdin
    printf "\n" >&2
    input="$(sanitize "$input")"
    printf "%s" "${input:-$current}"
}

# ── Banner (logo only if not already shown by parent) ─────────────────
if [[ "${WASP_LOGO_SHOWN:-}" != "true" ]]; then
    ui_logo "onboarding"
fi
ui_section "🐝  WASP onboarding"
ui_log "${C_DIM}  Pick a number for menus, or press Enter for defaults. Re-run any time: ${C_BOLD}wasp onboard${C_RESET}${C_DIM}.${C_RESET}"
ui_hr

# ── Section 1: Timezone (arrow-key menu) ──────────────────────────────
printf "\n${C_BOLD}🌎  Where are you?${C_RESET}\n"
TZ_DEFAULT="$(cat /etc/timezone 2>/dev/null || echo UTC)"
default_tz_idx=1

TZ_OPTIONS=(
    "UTC                                     (no offset)|UTC"
    "America/Santiago                        (Chile)|America/Santiago"
    "America/Argentina/Buenos_Aires          (Argentina)|America/Argentina/Buenos_Aires"
    "America/Sao_Paulo                       (Brazil)|America/Sao_Paulo"
    "America/Mexico_City                     (Mexico)|America/Mexico_City"
    "America/Bogota                          (Colombia)|America/Bogota"
    "America/Lima                            (Peru)|America/Lima"
    "America/Caracas                         (Venezuela)|America/Caracas"
    "America/Montevideo                      (Uruguay)|America/Montevideo"
    "America/Asuncion                        (Paraguay)|America/Asuncion"
    "America/La_Paz                          (Bolivia)|America/La_Paz"
    "America/Guatemala                       (Guatemala / Honduras / CR)|America/Guatemala"
    "America/Panama                          (Panama)|America/Panama"
    "America/Havana                          (Cuba)|America/Havana"
    "America/Toronto                         (Canada Eastern)|America/Toronto"
    "America/New_York                        (US Eastern)|America/New_York"
    "America/Chicago                         (US Central)|America/Chicago"
    "America/Denver                          (US Mountain)|America/Denver"
    "America/Los_Angeles                     (US Pacific)|America/Los_Angeles"
    "America/Anchorage                       (Alaska)|America/Anchorage"
    "Pacific/Honolulu                        (Hawaii)|Pacific/Honolulu"
    "Europe/London                           (UK)|Europe/London"
    "Europe/Madrid                           (Spain)|Europe/Madrid"
    "Europe/Lisbon                           (Portugal)|Europe/Lisbon"
    "Europe/Paris                            (France)|Europe/Paris"
    "Europe/Berlin                           (Germany)|Europe/Berlin"
    "Europe/Rome                             (Italy)|Europe/Rome"
    "Europe/Amsterdam                        (Netherlands)|Europe/Amsterdam"
    "Europe/Stockholm                        (Sweden)|Europe/Stockholm"
    "Europe/Warsaw                           (Poland)|Europe/Warsaw"
    "Europe/Athens                           (Greece)|Europe/Athens"
    "Europe/Moscow                           (Russia)|Europe/Moscow"
    "Africa/Cairo                            (Egypt)|Africa/Cairo"
    "Africa/Johannesburg                     (South Africa)|Africa/Johannesburg"
    "Africa/Lagos                            (Nigeria)|Africa/Lagos"
    "Asia/Dubai                              (UAE)|Asia/Dubai"
    "Asia/Jerusalem                          (Israel)|Asia/Jerusalem"
    "Asia/Istanbul                           (Turkey)|Asia/Istanbul"
    "Asia/Karachi                            (Pakistan)|Asia/Karachi"
    "Asia/Kolkata                            (India)|Asia/Kolkata"
    "Asia/Singapore                          (Singapore)|Asia/Singapore"
    "Asia/Bangkok                            (Thailand)|Asia/Bangkok"
    "Asia/Hong_Kong                          (Hong Kong)|Asia/Hong_Kong"
    "Asia/Shanghai                           (China)|Asia/Shanghai"
    "Asia/Seoul                              (South Korea)|Asia/Seoul"
    "Asia/Tokyo                              (Japan)|Asia/Tokyo"
    "Australia/Sydney                        (Australia East)|Australia/Sydney"
    "Australia/Perth                         (Australia West)|Australia/Perth"
    "Pacific/Auckland                        (New Zealand)|Pacific/Auckland"
    "Other — type IANA timezone manually|OTHER"
)
# Bias default to system tz if it appears in the menu
for ((i=0; i<${#TZ_OPTIONS[@]}; i++)); do
    if [[ "${TZ_OPTIONS[i]##*|}" == "$TZ_DEFAULT" ]]; then
        default_tz_idx=$((i+1))
        break
    fi
done

while true; do
    tz="$(ui_select "" "$default_tz_idx" "${TZ_OPTIONS[@]}")"
    if [[ "$tz" == "OTHER" ]]; then
        printf "\n"
        manual_tz="$(ask "Type IANA timezone (e.g. Asia/Yangon)" "" "")"
        if [[ -f "/usr/share/zoneinfo/$manual_tz" ]]; then
            tz="$manual_tz"
            break
        else
            ui_warn "Not a valid IANA timezone. Browse: ls /usr/share/zoneinfo/"
            continue
        fi
    fi
    break
done
set_env TIMEZONE "$tz"
set_env WASP_HOST_DIR "$WASP_DIR"
ui_ok "Timezone: $tz"

# ── Section 2: Telegram ───────────────────────────────────────────────
printf "\n${C_BOLD}💬  Telegram bot setup${C_RESET} ${C_DIM}(optional — press Enter on each field to skip everything)${C_RESET}\n"
ui_hr
printf "\n"
printf "  ${C_BOLD}You'll need TWO things from Telegram (open the app now):${C_RESET}\n\n"
printf "  ${C_CYAN}${C_BOLD}①  A bot token${C_RESET} ${C_DIM}— identifies your bot${C_RESET}\n"
printf "     Open Telegram → search ${C_BOLD}@BotFather${C_RESET} → send ${C_BOLD}/newbot${C_RESET} → follow the steps.\n"
printf "     BotFather replies with a token that looks like:\n"
printf "        ${C_DIM}${C_BOLD}1234567890:ABCdef-GhIJklMnOpQrSt_uvWxYZ${C_RESET}\n\n"
printf "  ${C_CYAN}${C_BOLD}②  YOUR personal Telegram ID${C_RESET} ${C_YELLOW}(this is YOU, not the bot)${C_RESET}\n"
printf "     Open Telegram → search ${C_BOLD}@userinfobot${C_RESET} → send ${C_BOLD}/start${C_RESET}.\n"
printf "     It replies with YOUR numeric user ID like:\n"
printf "        ${C_DIM}Id: ${C_BOLD}987654321${C_RESET}\n\n"
printf "  ${C_YELLOW}${C_BOLD}⚠  Common mistake:${C_RESET} the number BEFORE the colon in the token\n"
printf "     (e.g. ${C_DIM}1234567890${C_RESET}:ABC...) is the ${C_BOLD}BOT's${C_RESET} ID — NOT yours.\n"
printf "     Always use ${C_BOLD}@userinfobot${C_RESET} from ${C_BOLD}your personal account${C_RESET} to get YOUR ID.\n\n"
ui_hr
printf "\n"

# ── Step 1: Bot token ──────────────────────────────────────────────
printf "  ${C_BOLD}Step 1 of 2 — Bot token${C_RESET} ${C_DIM}(from @BotFather)${C_RESET}\n"
while true; do
    tok="$(ask_secret "Paste bot token here (or Enter to skip Telegram entirely)" TELEGRAM_BOT_TOKEN)"
    if [[ -z "$tok" ]] || [[ "$tok" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{20,}$ ]]; then break; fi
    ui_warn "That doesn't look like a bot token. It should look like ${C_BOLD}123456789:ABCdef-...${C_RESET} — copy the WHOLE line from @BotFather."
done
set_env TELEGRAM_BOT_TOKEN "$tok"

if [[ -n "$tok" ]]; then
    # ── Step 2 of 2 — Your personal Telegram ID ────────────────────
    # ONE id replicated to BOTH allowed_users (auth) AND default notification
    # chat. There is no "public bot" mode — the bridge refuses to start without
    # an allowed id. Multi-user allowlists are an advanced edit of .env later.
    printf "\n"
    printf "  ${C_BOLD}Step 2 of 2 — Your personal Telegram ID${C_RESET}\n"
    printf "  ${C_DIM}This is ${C_BOLD}YOUR${C_RESET}${C_DIM} numeric Telegram ID (NOT the bot's ID).${C_RESET}\n"
    printf "  ${C_DIM}How to get it: open ${C_BOLD}@userinfobot${C_RESET}${C_DIM} from your personal account → ${C_BOLD}/start${C_RESET}${C_DIM} → copy the ${C_BOLD}Id:${C_RESET}${C_DIM} number it shows.${C_RESET}\n"
    printf "  ${C_DIM}${C_YELLOW}This is required.${C_RESET}${C_DIM} The bot will REFUSE to start without it, on purpose (anyone who finds an open bot could run shell commands on your host).${C_RESET}\n"
    while true; do
        owner_id="$(ask "Your personal Telegram ID" TELEGRAM_ALLOWED_USERS)"
        cleaned="$(printf '%s' "$owner_id" | tr -d ' \r\t')"
        if [[ "$cleaned" =~ ^[0-9]{5,15}$ ]]; then
            owner_id="$cleaned"; break
        fi
        ui_warn "Telegram ID must be a numeric id (5-15 digits) from ${C_BOLD}@userinfobot${C_RESET}. Example of a valid answer: ${C_BOLD}987654321${C_RESET}"
        ui_warn "If you don't have it yet, leave this terminal open, open Telegram, message @userinfobot, then come back and paste the ${C_BOLD}Id:${C_RESET} number."
    done
    set_env TELEGRAM_ALLOWED_USERS "$owner_id"
    # Replicate to notification chat — same id, same person.
    set_env SCHEDULER_NOTIFY_CHAT_ID "$owner_id"
    ui_ok "Telegram configured (allowlist + notifications → ${owner_id})"
    printf "  ${C_DIM}Add more users later by editing TELEGRAM_ALLOWED_USERS in .env (comma-separated).${C_RESET}\n"
else
    set_env TELEGRAM_ALLOWED_USERS ""
    set_env SCHEDULER_NOTIFY_CHAT_ID ""
    ui_warn "Telegram skipped — agent-telegram will NOT start without an allowed id. Run ${C_BOLD}wasp onboard${C_RESET} again when ready."
fi

# ── Section 3: LLM provider (numbered menu) ───────────────────────────
printf "\n${C_BOLD}🤖  Which AI provider?${C_RESET}\n"
PROVIDER_OPTIONS=(
    "OpenAI       (GPT-4o, GPT-4, o3, etc.)|openai"
    "Anthropic    (Claude Sonnet, Opus, Haiku)|anthropic"
    "Google       (Gemini 1.5 / 2.0)|google"
    "xAI          (Grok)|xai"
    "Skip — configure later in .env|skip"
)
provider="$(ui_select "" 1 "${PROVIDER_OPTIONS[@]}")"
set_env DEFAULT_PROVIDER "$provider"

case "$provider" in
    openai)
        printf "\n  ${C_DIM}📋 Get your key at: https://platform.openai.com/api-keys${C_RESET}\n"
        while true; do
            key="$(ask_secret "OpenAI API key" OPENAI_API_KEY)"
            if [[ -z "$key" ]] || [[ "$key" =~ ^sk- ]]; then break; fi
            ui_warn "Key should start with 'sk-'. Try again or leave blank."
        done
        set_env OPENAI_API_KEY "$key"
        [[ -n "$key" ]] && ui_ok "OpenAI configured"
        ;;
    anthropic)
        printf "\n  ${C_DIM}📋 Get your key at: https://console.anthropic.com/settings/keys${C_RESET}\n"
        while true; do
            key="$(ask_secret "Anthropic API key (sk-ant-...)" ANTHROPIC_API_KEY)"
            if [[ -z "$key" ]] || [[ "$key" =~ ^sk-ant- ]]; then break; fi
            ui_warn "Key should start with 'sk-ant-'. Try again or leave blank."
        done
        set_env ANTHROPIC_API_KEY "$key"
        [[ -n "$key" ]] && ui_ok "Anthropic configured"
        ;;
    google)
        printf "\n  ${C_DIM}📋 Get your key at: https://aistudio.google.com/apikey${C_RESET}\n"
        key="$(ask_secret "Google API key (AIza...)" GOOGLE_API_KEY)"
        set_env GOOGLE_API_KEY "$key"
        [[ -n "$key" ]] && ui_ok "Google configured"
        ;;
    xai)
        printf "\n  ${C_DIM}📋 Get your key at: https://console.x.ai/${C_RESET}\n"
        key="$(ask_secret "xAI API key" XAI_API_KEY)"
        set_env XAI_API_KEY "$key"
        [[ -n "$key" ]] && ui_ok "xAI configured"
        ;;
    skip)
        ui_warn "Skipped. Set ANTHROPIC_API_KEY / OPENAI_API_KEY / etc. in $ENV_FILE later."
        ;;
esac

# ── Section 4: Dashboard ──────────────────────────────────────────────
printf "\n${C_BOLD}🔐  Dashboard credentials${C_RESET}\n"
ui_log "  ${C_DIM}Username 3+ chars · Password 8+ chars (or blank for auto-generated)${C_RESET}"
while true; do
    duser="$(ask "Username" DASHBOARD_USER "admin")"
    if [[ ${#duser} -ge 3 ]]; then break; fi
    ui_warn "Username must be at least 3 characters."
done
set_env DASHBOARD_USER "$duser"
while true; do
    dpass="$(ask_secret "Password (blank = auto-generate strong one)" DASHBOARD_PASSWORD)"
    if [[ -z "$dpass" ]]; then
        dpass="$(openssl rand -hex 12)"
        ui_warn "No password given — generated one (save it now): ${C_BOLD}$dpass${C_RESET}"
        break
    fi
    if [[ ${#dpass} -ge 8 ]]; then break; fi
    ui_warn "Password must be at least 8 characters (or leave blank to auto-generate)."
done
set_env DASHBOARD_PASSWORD "$dpass"
ui_ok "Dashboard user: $duser"

# ── Section 5: Optional public URL ────────────────────────────────────
printf "\n${C_BOLD}🌐  Optional${C_RESET} ${C_DIM}(advanced; press Enter to skip)${C_RESET}\n"
pub="$(ask "Public dashboard URL (blank = use VPS IP)" DASHBOARD_PUBLIC_URL "")"
set_env DASHBOARD_PUBLIC_URL "$pub"

# ── Done ──────────────────────────────────────────────────────────────
touch "$ONBOARD_MARKER"
printf "\n"
ui_hr
ui_ok "${C_BOLD}Onboarding complete${C_RESET}"
ui_hr
if $FIRST_RUN; then
    ui_log "  Next: install.sh will now build and start the stack."
else
    ui_log "  Apply changes with: ${C_BOLD}wasp restart${C_RESET}"
fi
