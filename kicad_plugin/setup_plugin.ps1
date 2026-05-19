#Requires -Version 5.1
<#
.SYNOPSIS
    Create a .venv inside the KiCad plugin directory, install kicad_mcp from
    the local project as an editable package, and download the freerouting JAR
    for the auto-router feature.

.DESCRIPTION
    Run this script from inside the kicad_ai_assistant plugin folder, e.g.:

        cd "$env:APPDATA\kicad\10.0\scripting\plugins\kicad_ai_assistant"
        .\setup_plugin.ps1 C:\path\to\kicad-mcp

.PARAMETER ProjectDir
    Path to the kicad-mcp project directory (required).
#>
param(
    [Parameter(Mandatory = $false, Position = 0)]
    [string]$ProjectDir
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
$PythonVersion     = '3.13'
$FreroutingVersion = '2.2.3'
$JarName           = "freerouting-${FreroutingVersion}.jar"
$DownloadUrl       = "https://github.com/freerouting/freerouting/releases/download/v${FreroutingVersion}/${JarName}"

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
if (-not $ProjectDir) {
    Write-Host "Usage: .\setup_plugin.ps1 <path-to-kicad-mcp-project>"
    Write-Host "Example: .\setup_plugin.ps1 C:\code\kicad-mcp"
    exit 1
}

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
try {
    $ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).ProviderPath
} catch {
    Write-Host "ERROR: Cannot resolve project path: $ProjectDir"
    exit 1
}

# $PSScriptRoot is the directory containing this script (the plugin folder).
$PluginDir = $PSScriptRoot
$VenvDir   = Join-Path $PluginDir '.venv'

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if (-not (Test-Path (Join-Path $ProjectDir 'pyproject.toml'))) {
    Write-Host "ERROR: No pyproject.toml found in: $ProjectDir"
    Write-Host "Make sure you are passing the correct kicad-mcp project path."
    exit 1
}

if (-not (Get-Command 'uv' -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: uv not found in PATH. Install it from https://github.com/astral-sh/uv"
    exit 1
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host "Plugin dir : $PluginDir"
Write-Host "Venv dir   : $VenvDir"
Write-Host "Project dir: $ProjectDir"
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
# Step 2/3 — Install kicad_mcp as editable package
# ---------------------------------------------------------------------------
Write-Host "Step 2/3 - Installing kicad_mcp (editable) from $ProjectDir ..."
uv pip install -e "$ProjectDir" --python "$VenvDir"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: uv pip install failed (exit code $LASTEXITCODE)."
    exit $LASTEXITCODE
}

# ---------------------------------------------------------------------------
# Step 3/3 — Download freerouting JAR
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 3/3 - Downloading freerouting JAR ..."
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
