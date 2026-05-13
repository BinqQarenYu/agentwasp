# WASP — Windows installer (PowerShell)
#
# Usage (run from an elevated PowerShell prompt):
#   powershell -Command "iwr -useb https://agentwasp.com/install.ps1 | iex"
#
# What it does:
#   1. Checks for Administrator elevation
#   2. Detects WSL2 — enables it if missing (requires reboot)
#   3. Detects Docker Desktop — opens download page if missing
#   4. Ensures an Ubuntu distro exists inside WSL — installs it if not
#   5. Runs the standard Linux installer inside WSL2:
#        sudo bash -c "$(curl -fsSL https://agentwasp.com/install.sh)"
#
# WASP itself runs as Linux containers in Docker Desktop's WSL2 backend;
# everything (dashboard, agent, integrations) is identical to a Linux install.

#Requires -Version 5.1

$ErrorActionPreference = "Stop"

# Force UTF-8 console output so the box-drawing logo (██╗ ╚═╝ etc.) renders
# correctly on Windows PowerShell 5.1, which defaults to legacy code pages.
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$WaspInstallUrl = if ($env:WASP_INSTALL_URL) { $env:WASP_INSTALL_URL } else { "https://agentwasp.com/install.sh" }
$WslDistro       = if ($env:WASP_WSL_DISTRO) { $env:WASP_WSL_DISTRO } else { "Ubuntu" }

# ── UI helpers ────────────────────────────────────────────────────────
function Write-Logo {
    $logo = @(
        "       ██╗    ██╗  █████╗  ███████╗ ██████╗ ",
        "       ██║    ██║ ██╔══██╗ ██╔════╝ ██╔══██╗",
        "       ██║ █╗ ██║ ███████║ ███████╗ ██████╔╝",
        "       ██║███╗██║ ██╔══██║ ╚════██║ ██╔═══╝ ",
        "       ╚███╔███╔╝ ██║  ██║ ███████║ ██║     ",
        "        ╚══╝╚══╝  ╚═╝  ╚═╝ ╚══════╝ ╚═╝     "
    )
    Write-Host ""
    foreach ($l in $logo) { Write-Host $l -ForegroundColor Yellow }
    Write-Host "       🐝  autonomous agent · self-hosted" -ForegroundColor DarkGray
    Write-Host "       🌐  agentwasp.com" -ForegroundColor DarkGray
    Write-Host ""
}
function Step($n, $total, $msg) { Write-Host "" ; Write-Host ("[{0}/{1}] {2}" -f $n, $total, $msg) -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Info($msg) { Write-Host "  --> $msg" -ForegroundColor Blue }
function Warn($msg) { Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "  [X]  $msg" -ForegroundColor Red ; exit 1 }

Write-Logo

# ── [1/5] Admin check ─────────────────────────────────────────────────
Step 1 5 "Checking Administrator privileges"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Warn "This installer needs Administrator privileges (to enable WSL2 and install distros)."
    Warn "Right-click PowerShell -> 'Run as Administrator' and run again:"
    Warn '   powershell -Command "iwr -useb https://agentwasp.com/install.ps1 | iex"'
    exit 1
}
Ok "Running as Administrator"

# ── [2/5] WSL2 ────────────────────────────────────────────────────────
Step 2 5 "Checking WSL2"
$wslPresent = $false
try { wsl --status 2>&1 | Out-Null; if ($LASTEXITCODE -eq 0) { $wslPresent = $true } } catch {}

if (-not $wslPresent) {
    Info "WSL is not installed. Running 'wsl --install' (this will download Ubuntu and may require a reboot)..."
    try {
        wsl --install --distribution $WslDistro
    } catch {
        Fail "wsl --install failed: $_. On Windows 10 you may need to enable 'Windows Subsystem for Linux' and 'Virtual Machine Platform' features manually, then reboot and re-run this script."
    }
    Warn "WSL was just installed. Windows will likely need to REBOOT to finish setup."
    Warn "After reboot, re-run this same command to continue:"
    Warn '   powershell -Command "iwr -useb https://agentwasp.com/install.ps1 | iex"'
    exit 0
}
Ok "WSL is installed"

# Ensure WSL is on version 2 (Docker Desktop requires v2)
$defaultVersion = (wsl --status 2>&1 | Select-String "Default Version" | ForEach-Object { $_.ToString().Trim() }) -join ""
if ($defaultVersion -notmatch "2") {
    Info "Setting WSL default version to 2"
    wsl --set-default-version 2 2>&1 | Out-Null
}

# Ensure a Linux distro is installed
$distros = wsl --list --quiet 2>&1 | ForEach-Object { ($_ -as [string]).Trim() } | Where-Object { $_ -ne "" }
if (-not ($distros -contains $WslDistro)) {
    Info "WSL distro '$WslDistro' not found. Installing..."
    wsl --install --distribution $WslDistro --no-launch
    Warn "Ubuntu was just installed inside WSL. You may need to launch it once manually to set up a Linux username/password:"
    Warn "   wsl"
    Warn "Then re-run this PowerShell installer."
    exit 0
}
Ok "WSL distro '$WslDistro' is available"

# ── [3/5] Docker Desktop ──────────────────────────────────────────────
Step 3 5 "Checking Docker Desktop"
$dockerInstalled = $null -ne (Get-Command "docker" -ErrorAction SilentlyContinue)
if (-not $dockerInstalled) {
    Warn "Docker Desktop is not installed."
    Info "Opening the Docker Desktop download page in your browser..."
    Start-Process "https://www.docker.com/products/docker-desktop/"
    Write-Host ""
    Warn "Please:"
    Warn "  1. Download and install Docker Desktop (free for personal use)"
    Warn "  2. Open Docker Desktop, accept terms, sign in if asked"
    Warn "  3. In Docker Desktop -> Settings -> Resources -> WSL Integration, ENABLE '$WslDistro'"
    Warn "  4. Re-run this installer:"
    Warn '       powershell -Command "iwr -useb https://agentwasp.com/install.ps1 | iex"'
    exit 1
}

# Wait for the daemon to be reachable (Docker Desktop may still be starting)
Info "Waiting for Docker daemon to respond..."
$dockerReady = $false
for ($i = 0; $i -lt 30; $i++) {
    try { docker info 2>&1 | Out-Null; if ($LASTEXITCODE -eq 0) { $dockerReady = $true; break } } catch {}
    Start-Sleep -Seconds 2
}
if (-not $dockerReady) {
    Warn "Docker is installed but the daemon is not responding."
    Warn "Open Docker Desktop, wait for it to finish starting, then re-run this installer."
    Fail "Docker daemon unreachable after 60s"
}
Ok "Docker Desktop is running"

# Verify WSL integration enabled for our distro by trying to call docker FROM inside WSL
Info "Verifying Docker WSL integration for '$WslDistro'..."
$wslDocker = wsl -d $WslDistro -e bash -c "command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && echo OK || echo MISSING" 2>&1
if ($wslDocker -notmatch "OK") {
    Warn "Docker CLI is not accessible inside WSL distro '$WslDistro'."
    Warn "Open Docker Desktop -> Settings -> Resources -> WSL Integration, ENABLE '$WslDistro', then re-run."
    Fail "WSL Docker integration missing"
}
Ok "Docker is reachable from inside WSL"

# ── [4/5] Run the Linux installer inside WSL ──────────────────────────
Step 4 5 "Launching WASP installer inside WSL2 (this is the real install)"
Write-Host ""
Info "From now on you'll see the standard Linux installer output."
Info "It runs inside the '$WslDistro' WSL distro, and uses your Docker Desktop containers."
Write-Host ""

# We can't pipe `curl | sudo bash` because sudo would lose curl's stdin context
# when run through wsl -e. Two-step it: curl to a file, then sudo bash it.
$wslCmd = "set -e; tmp=`$(mktemp); curl -fsSL '$WaspInstallUrl' -o `$tmp; sudo bash `$tmp; rm -f `$tmp"
wsl -d $WslDistro -e bash -lc $wslCmd
$installExit = $LASTEXITCODE

# ── [5/5] Summary ─────────────────────────────────────────────────────
Step 5 5 "Summary"
if ($installExit -ne 0) {
    Fail "Installer inside WSL exited with code $installExit. Open '$WslDistro' (run 'wsl' in PowerShell) and re-run: sudo bash -c `"`$(curl -fsSL $WaspInstallUrl)`""
}
Ok "WASP installed inside WSL distro '$WslDistro'"
Write-Host ""
Write-Host "  Dashboard:    http://localhost:8080" -ForegroundColor Cyan
Write-Host "  CLI (in WSL): wsl -d $WslDistro -- wasp status" -ForegroundColor Cyan
Write-Host "  Open WSL:     wsl" -ForegroundColor Cyan
Write-Host ""
