param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedExeSha256
)

$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $workspace '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "The workspace Python environment is missing: $python"
}

& $python -m erp_automation.operations.workspace_retention `
    --workspace $workspace `
    --apply `
    --keep-full-rollbacks 2 `
    --expected-exe-sha256 $ExpectedExeSha256

if ($LASTEXITCODE -ne 0) {
    throw "Release finalization failed with exit code $LASTEXITCODE."
}
