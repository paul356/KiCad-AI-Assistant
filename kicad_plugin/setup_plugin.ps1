#Requires -Version 5.1
<#
.SYNOPSIS
    Create a .venv inside the KiCad plugin directory, install kcaa from PyPI,
    and download the freerouting JAR for the auto-router feature.

.DESCRIPTION
    Run this script from inside the kicad_ai_assistant plugin folder, e.g.:

        cd "$env:APPDATA\kicad\10.0\scripting\plugins\kicad_ai_assistant"
        .\setup_plugin.ps1
#>

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
$PythonVersion     = '3.13'
$FreroutingVersion = '2.2.3'
$JarName           = "freerouting-${FreroutingVersion}.jar"
$DownloadUrl       = "https://github.com/freerouting/freerouting/releases/download/v${FreroutingVersion}/${JarName}"

# $PSScriptRoot is the directory containing this script (the plugin folder).
$PluginDir = $PSScriptRoot
$VenvDir   = Join-Path $PluginDir '.venv'

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if (-not (Get-Command 'uv' -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: uv not found in PATH. Install it from https://github.com/astral-sh/uv"
    exit 1
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host "Plugin dir : $PluginDir"
Write-Host "Venv dir   : $VenvDir"
Write-Host ""

# ---------------------------------------------------------------------------
# Step 1/3 — Create / update the venv
# ---------------------------------------------------------------------------
Write-Host "Step 1/3 - Creating virtual environment ..."
uv venv "$VenvDir" --python $PythonVersion
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: uv venv failed (exit code $LASTEXITCODE)."
    exit $LASTEXITCODE
}

# ---------------------------------------------------------------------------
# Step 2/3 — Install kcaa from PyPI
# ---------------------------------------------------------------------------
Write-Host "Step 2/3 - Installing kcaa from PyPI ..."
uv pip install kcaa --python "$VenvDir"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: uv pip install failed (exit code $LASTEXITCODE)."
    exit $LASTEXITCODE
}

# ---------------------------------------------------------------------------
# Step 3/4 — Detect KiCad configuration
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 3/4 - Detecting KiCad configuration ..."

# --- KICAD_VERSION ---
# Extract from plugin directory path
# Expected: C:\Users\...\AppData\Roaming\kicad\10.0\scripting\plugins\kicad_ai_assistant
$KicadVersion = ""
if ($PluginDir -match '\\kicad\\(\d+\.\d+)\\') {
    $KicadVersion = $matches[1]
    Write-Host "  Detected KiCad version: $KicadVersion"
} else {
    Write-Host "  Warning: Could not detect KiCad version from plugin directory path"
    $KicadVersion = Read-Host "  Please enter KiCad version (e.g., 10.0)"
    if ([string]::IsNullOrWhiteSpace($KicadVersion)) {
        Write-Host "  Error: KiCad version is required"
        exit 1
    }
}

# --- Platform-specific default paths ---
$appdata = $env:APPDATA
$KicadConfigDir = "$appdata\kicad\$KicadVersion"
# Candidate paths for 3rd-party resources (search in order)
$Kicad3rdParty = @(
    "$appdata\kicad\$KicadVersion\3rdparty",
    (Join-Path ([Environment]::GetFolderPath('MyDocuments')) (Join-Path 'KiCad' (Join-Path $KicadVersion '3rdparty')))
)

# --- KICAD_APP_PATH ---
# Try to find KiCad in "Program Files" on any fixed drive (C:, D:, etc.).
# Only check the standard Program Files path (no Program Files (x86)).
$possibleTarget = "Program Files\KiCad"
$KicadAppPath = $null

$drives = Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Root -match '^[A-Za-z]:\\$' }
foreach ($d in $drives) {
    $candidate = Join-Path $d.Root $possibleTarget
    if (Test-Path $candidate) {
        $KicadAppPath = $candidate
        break
    }
}

if ($KicadAppPath) {
    Write-Host "  Detected KICAD_APP_PATH: $KicadAppPath"
} else {
    Write-Host "  Warning: KiCad not found in $possibleTarget on any drive."
    $KicadAppPath = Read-Host "  Please enter KiCad application path"
    if (-not (Test-Path $KicadAppPath)) {
        Write-Host "  Error: Directory does not exist: $KicadAppPath"
        exit 1
    }
}

# --- Verify KICAD_CONFIG_DIR ---
if (Test-Path $KicadConfigDir) {
    Write-Host "  Detected KICAD_CONFIG_DIR: $KicadConfigDir"
} else {
    Write-Host "  Warning: KICAD_CONFIG_DIR not found: $KicadConfigDir"
    $KicadConfigDir = Read-Host "  Please enter KiCad config directory"
    if (-not (Test-Path $KicadConfigDir)) {
        Write-Host "  Error: Directory does not exist: $KicadConfigDir"
        exit 1
    }
}

# --- Verify KICAD_3RD_PARTY ---
# Find the first existing candidate
$Kicad3rdPartySelected = $null
$Kicad3rdPartySource = $null
foreach ($p in $Kicad3rdParty) {
    if (Test-Path $p) {
        $Kicad3rdPartySelected = $p
        $Kicad3rdPartySource = $p
        break
    }
}

if ($Kicad3rdPartySelected) {
    $Kicad3rdParty = $Kicad3rdPartySelected
    Write-Host "  Detected KICAD_3RD_PARTY: $Kicad3rdParty"
} else {
    $searched = $Kicad3rdParty -join '; '
    Write-Host "  Warning: KICAD_3RD_PARTY not found. Searched: $searched"
    $Kicad3rdParty = Read-Host "  Please enter KiCad 3rd-party directory"
    if (-not (Test-Path $Kicad3rdParty)) {
        Write-Host "  Error: Directory does not exist: $Kicad3rdParty"
        exit 1
    }
}

# --- Generate .env ---
$EnvFile = Join-Path $PluginDir ".env"
$envContent = @(
    "# KiCad AI Assistant environment configuration",
    "# Generated by setup_plugin.ps1 on $(Get-Date -Format 'yyyy-MM-dd')",
    "",
    "KICAD_VERSION=$KicadVersion",
    "KICAD_APP_PATH=$KicadAppPath",
    "KICAD_CONFIG_DIR=$KicadConfigDir",
    "KICAD_3RD_PARTY=$Kicad3rdParty"
)

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($EnvFile, $envContent, $utf8NoBom)

Write-Host ""
Write-Host "  Configuration written to: $EnvFile"
Write-Host ""
Write-Host "  Configuration summary:"
Write-Host "    KICAD_VERSION=$KicadVersion"
Write-Host "    KICAD_APP_PATH=$KicadAppPath"
Write-Host "    KICAD_CONFIG_DIR=$KicadConfigDir"
Write-Host "    KICAD_3RD_PARTY=$Kicad3rdParty"

# ---------------------------------------------------------------------------
# Step 4/4 — Download freerouting JAR
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 4/4 - Downloading freerouting JAR ..."
$JarDest = Join-Path $PluginDir $JarName

if (Test-Path $JarDest) {
    Write-Host "  Already present: $JarDest (skipping download)"
} else {
    Write-Host "  URL: $DownloadUrl"
    try {
        Invoke-WebRequest -Uri $DownloadUrl -OutFile $JarDest -UseBasicParsing
        Write-Host "  Saved to: $JarDest"
    } catch {
        Write-Host "ERROR: Failed to download freerouting JAR: $_"
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Done! The plugin venv is ready at:"
Write-Host "  $(Join-Path $VenvDir 'Scripts\python.exe')"
Write-Host ""
Write-Host "Restart KiCad (or reload the plugin) for the changes to take effect."
