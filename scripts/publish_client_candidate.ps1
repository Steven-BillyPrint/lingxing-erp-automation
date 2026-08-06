[CmdletBinding()]
param(
    [switch]$ConfirmCandidateRelease
)

$ErrorActionPreference = 'Stop'

if (-not $ConfirmCandidateRelease) {
    throw '候选发布必须显式传入 -ConfirmCandidateRelease。'
}

& (Join-Path $PSScriptRoot 'publish_client_release.ps1') `
    -ConfirmCandidateRelease
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
