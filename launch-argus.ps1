param(
    [ValidateRange(1,65535)][int]$Port = 8780,
    [string]$ProfilesFile = '',
    [switch]$NoBrowser,
    [switch]$Sync
)
$ErrorActionPreference = 'Stop'
$argusRoot = $PSScriptRoot
Push-Location -LiteralPath $argusRoot
try {
    $required = @('automation-capstone-fall-2026\platform\src', 'PreCheck\plugin', 'ahc\pyproject.toml')
    if (@($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $argusRoot $_)) }).Count) {
        & git submodule update --init --recursive
        if ($LASTEXITCODE -ne 0) { throw 'Could not initialize ARGUS submodules.' }
    }
    $pythonPath = Join-Path $argusRoot '.venv\Scripts\python.exe'
    if ($Sync -or -not (Test-Path -LiteralPath $pythonPath)) {
        $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
        $uvPath = if ($uvCommand) { $uvCommand.Source } else { Join-Path $argusRoot '.venv\Scripts\uv.exe' }
        if (-not (Test-Path -LiteralPath $uvPath)) {
            throw 'Install uv first (https://docs.astral.sh/uv/getting-started/installation/), then run this launcher again.'
        }
        & $uvPath sync --locked
        if ($LASTEXITCODE -ne 0) { throw 'ARGUS dependency setup failed.' }
    }
    if ($ProfilesFile) {
        $resolvedProfiles = (Resolve-Path -LiteralPath $ProfilesFile).Path
        if (-not (Test-Path -LiteralPath $resolvedProfiles -PathType Leaf)) { throw 'ProfilesFile must be a JSON file.' }
        $env:LANM_PROFILES_FILE = $resolvedProfiles
    }
    $arguments = @('-m', 'argus.cli', 'serve', '--port', [string]$Port)
    if ($NoBrowser) { $arguments += '--no-browser' }
    Write-Output ('ARGUS: http://127.0.0.1:' + $Port + '/ — open Research workspace for chat, evidence and tools.')
    & $pythonPath @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
