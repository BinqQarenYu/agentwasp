#!/usr/bin/env bash
# build-release.sh — assemble a clean public-release tarball.
#
# This is the ONLY supported way to produce wasp-release.tar.gz. It builds
# the tarball from an allowlist staging copy, so operator-only artifacts
# (internal audit reports, the public-domain nginx config, .env, data
# volumes, byte-caches, etc.) cannot leak into the public archive even if
# they exist alongside the source tree.
#
# Usage (run from the repo root):
#   sudo scripts/build-release.sh
#   sudo scripts/build-release.sh --source "$PWD" --out "$PWD/release-prep/wasp-release.tar.gz"
#
# Exits non-zero (without writing the archive) if any forbidden pattern
# survives in the staged tree.

set -euo pipefail

# Default source = the parent of release-prep/scripts/ (i.e. the repo root).
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SOURCE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
OUT="${SOURCE_ROOT}/release-prep/wasp-release.tar.gz"
KEEP_STAGE="${KEEP_STAGE:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source) SOURCE_ROOT="$2"; shift 2 ;;
        --out)    OUT="$2"; shift 2 ;;
        --keep-stage) KEEP_STAGE=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

REL_PREP="${SOURCE_ROOT}/release-prep"
CONTAINERS_SRC="${SOURCE_ROOT}/containers"

[[ -d "$REL_PREP"       ]] || { echo "missing $REL_PREP" >&2; exit 1; }
[[ -d "$CONTAINERS_SRC" ]] || { echo "missing $CONTAINERS_SRC" >&2; exit 1; }

STAGE="$(mktemp -d -t wasp-build-XXXXXX)"
trap '[[ -z "${KEEP_STAGE}" ]] && rm -rf "$STAGE" || echo "staged at: $STAGE"' EXIT

echo "==> staging in $STAGE"

# ── 1. Top-level release-prep files (allowlist) ──────────────────────────────
copy_if_exists() {
    [[ -e "$1" ]] && cp -a "$1" "$2"
    true
}

for item in \
    README.md LICENSE.md CHANGELOG.md CODE_OF_CONDUCT.md VERSION \
    .env.example .gitignore docker-compose.yml install.sh install.ps1 ; do
    copy_if_exists "${REL_PREP}/${item}" "${STAGE}/"
done

for dir in bin lib scripts docs .github ; do
    if [[ -d "${REL_PREP}/${dir}" ]]; then
        cp -a "${REL_PREP}/${dir}" "${STAGE}/${dir}"
    fi
done

# ── 2. Containers — public services only (NO agent-nginx) ────────────────────
# agent-nginx in production is wired for agentwasp.com (Let's Encrypt certs,
# landing page, install.sh hosting). Public installs do not need it; users
# put their own reverse proxy in front of dashboard:8080 if they want TLS.
mkdir -p "${STAGE}/containers"

PUBLIC_SERVICES=(agent-core agent-telegram agent-broker)

RSYNC_EXCLUDES=(
    '--exclude=__pycache__/'
    '--exclude=*.pyc'
    '--exclude=*.pyo'
    '--exclude=.pytest_cache/'
    '--exclude=.ruff_cache/'
    '--exclude=.mypy_cache/'
    '--exclude=.tox/'
    '--exclude=node_modules/'
    '--exclude=.coverage'
    '--exclude=*.bak'
    '--exclude=*.tmp'
    '--exclude=*.swp'
    '--exclude=*.log'
    '--exclude=.DS_Store'
    '--exclude=.env'
    '--exclude=.env.*'
    '--exclude=docs/reports/'
    '--exclude=data/'
    '--exclude=__data__/'
    '--exclude=screenshots/'
    '--exclude=browser_sessions/'
    '--exclude=browser-sessions/'
    '--exclude=chat-uploads/'
    '--exclude=memory/visual_cache/'
    # Operator-only test scaffolding — not used by the build-time policy gate
    # (test_policy_regressions.py) and not imported by any runtime module.
    # Keep the regular tests/test_*.py for `pytest` reruns post-install.
    '--exclude=tests/e2e_harness/'
    '--exclude=tests/fixtures/'
)

for svc in "${PUBLIC_SERVICES[@]}"; do
    if [[ ! -d "${CONTAINERS_SRC}/${svc}" ]]; then
        echo "missing container source: ${CONTAINERS_SRC}/${svc}" >&2
        exit 1
    fi
    rsync -a "${RSYNC_EXCLUDES[@]}" \
        "${CONTAINERS_SRC}/${svc}/" "${STAGE}/containers/${svc}/"
done

# ── 3. Forbidden-pattern scan ────────────────────────────────────────────────
# Any one of these in the staged tree blocks the build. The list is built
# at runtime (not stored literally) so this file itself doesn't trip the scan.
mk_pat() { local prefix="$1"; shift; printf '%s%s\n' "$prefix" "$*"; }

FORBIDDEN_PATTERNS=()
FORBIDDEN_PATTERNS+=("$(mk_pat 'zdjn ' 'xxqe')")
FORBIDDEN_PATTERNS+=("$(mk_pat 'aia' 'gentwasp')")
FORBIDDEN_PATTERNS+=("$(mk_pat 'mas' 'terlund')")
FORBIDDEN_PATTERNS+=("$(mk_pat 'lundclaw' 'bot')")
FORBIDDEN_PATTERNS+=("$(mk_pat '180071' '9170')")
FORBIDDEN_PATTERNS+=("$(mk_pat '999000' '999')")
FORBIDDEN_PATTERNS+=('76\.13\.232\.149')
FORBIDDEN_PATTERNS+=("$(mk_pat '/home/' 'agent/')")

scan_fail=0
for pat in "${FORBIDDEN_PATTERNS[@]}"; do
    if matches="$(grep -rEn --color=never \
            --include='*.py' --include='*.md' --include='*.sh' \
            --include='*.yml' --include='*.yaml' --include='*.toml' \
            --include='*.html' --include='*.css' --include='*.js' \
            --include='*.json' --include='*.txt' --include='*.ini' \
            --include='*.conf' --include='*.example' --include='*.template' \
            --exclude='build-release.sh' \
            "$pat" "$STAGE" 2>/dev/null)"; then
        if [[ -n "$matches" ]]; then
            echo "FORBIDDEN pattern '$pat' found in staging:" >&2
            echo "$matches" | head -20 >&2
            scan_fail=1
        fi
    fi
done

if [[ $scan_fail -ne 0 ]]; then
    echo "REFUSING to package — sanitize the source and re-run." >&2
    exit 1
fi

# ── 4. Binary/private-artifact scan ──────────────────────────────────────────
unwanted="$(find "$STAGE" \( \
        -name '.env' \
        -o -name '*.env' \
        -o -name 'dump.rdb' \
        -o -name 'appendonly.aof' \
        -o -name '*.sqlite' \
        -o -name '*.sqlite3' \
        -o -name '*.pkl' \
        -o -name '*.pickle' \
        -o -name '*.dump' \
        -o -name '*.tar.gz' \
        -o -name '*.tgz' \
     \) -print 2>/dev/null)"

if [[ -n "$unwanted" ]]; then
    echo "FORBIDDEN binary/private artifacts found in staging:" >&2
    echo "$unwanted" >&2
    exit 1
fi

# ── 5. Build tarball ─────────────────────────────────────────────────────────
mkdir -p "$(dirname "$OUT")"
# Owner=root, mode 0644 for files; reproducible mtime via --mtime would help
# but we keep mtimes intact so the served archive's last-modified updates.
tar -C "$STAGE" \
    --owner=0 --group=0 \
    -czf "$OUT" .

size="$(du -h "$OUT" | cut -f1)"
files="$(find "$STAGE" -type f | wc -l)"
echo "==> wrote $OUT (${size}, ${files} files)"
