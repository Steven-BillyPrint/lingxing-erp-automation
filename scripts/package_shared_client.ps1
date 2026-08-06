[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BuiltApplicationDir,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9A-Za-z._-]{1,64}$')]
    [string]$Version,
    [string]$OutputDirectory = '',
    [string]$ArchiveName = ''
)

$ErrorActionPreference = 'Stop'

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $stream = [IO.File]::OpenRead($LiteralPath)
    try {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try {
            $digest = $algorithm.ComputeHash($stream)
        } finally {
            $algorithm.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
    return ([BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
}

$workspace = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$declaredVersion = (
    Get-Content -LiteralPath (Join-Path $workspace 'CLIENT_VERSION') -Raw
).Trim()
if ($Version -ne $declaredVersion) {
    throw "打包版本 $Version 与 CLIENT_VERSION $declaredVersion 不一致。"
}
$applicationDir = (Resolve-Path -LiteralPath $BuiltApplicationDir).Path
$applicationExe = Join-Path $applicationDir 'ERP自动化.exe'
if (-not (Test-Path -LiteralPath $applicationExe -PathType Leaf)) {
    throw "构建目录中未找到 ERP自动化.exe：$applicationExe"
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $workspace 'output\client-releases'
}
[IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
$resolvedOutput = (Resolve-Path -LiteralPath $OutputDirectory).Path
$stagingRoot = Join-Path $workspace "release-staging\client-package-$Version"
[IO.Directory]::CreateDirectory((Join-Path $workspace 'release-staging')) | Out-Null
$resolvedStagingParent = (Resolve-Path (Join-Path $workspace 'release-staging')).Path
$candidateStaging = [IO.Path]::GetFullPath($stagingRoot)
if (-not $candidateStaging.StartsWith(
    $resolvedStagingParent + [IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw '客户端打包暂存目录越界。'
}
if (Test-Path -LiteralPath $candidateStaging) {
    Remove-Item -LiteralPath $candidateStaging -Recurse -Force
}
[IO.Directory]::CreateDirectory($candidateStaging) | Out-Null
[IO.Directory]::CreateDirectory((Join-Path $candidateStaging 'dist')) | Out-Null
[IO.Directory]::CreateDirectory((Join-Path $candidateStaging 'scripts')) | Out-Null

Copy-Item -LiteralPath $applicationDir `
    -Destination (Join-Path $candidateStaging 'dist') `
    -Recurse
foreach ($scriptName in @(
    'start_shared_desktop.ps1',
    'install_shared_client.ps1',
    'update_shared_client.ps1',
    'promote_portable_client.ps1',
    'complete_client_repair.ps1'
)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $scriptName) `
        -Destination (Join-Path $candidateStaging 'scripts')
}

# The last profile-aware stable updater (2026.08.06.1) validates these two
# paths before it writes the install receipt and activates the new shortcut.
# Generate narrow, formal-only shims in the archive so existing installations
# can cross that boundary without restoring candidate-mode source entrypoints.
$profileLauncherShim = @'
[CmdletBinding()]
param(
    [ValidateSet('Select', 'Stable', 'Candidate')]
    [string]$ClientProfile = 'Select',
    [string]$ApplicationArguments = '',
    [switch]$Silent
)

$ErrorActionPreference = 'Stop'
if ($ClientProfile -eq 'Candidate') {
    throw '候选版入口已停用；请使用本机测试版验收后发布正式版。'
}
$packageRoot = Split-Path -Parent $PSScriptRoot
$application = Join-Path $packageRoot 'dist\ERP自动化\ERP自动化.exe'
if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
    throw "正式版客户端入口不存在：$application"
}
$startArguments = @{
    FilePath = $application
    WorkingDirectory = $packageRoot
}
if ($ApplicationArguments) {
    $startArguments.ArgumentList = $ApplicationArguments
}
Start-Process @startArguments
'@
$updateChannelShim = @'
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Stable', 'Candidate')]
    [string]$Channel,
    [ValidateSet('Stable', 'Candidate')]
    [string]$ClientProfile = 'Stable',
    [string]$StateRoot = '',
    [switch]$ConfirmCandidateEnrollment,
    [switch]$ConfirmCandidateRollback,
    [switch]$OutputJson
)

$ErrorActionPreference = 'Stop'
if ($Channel -ne 'Stable' -or $ClientProfile -ne 'Stable') {
    throw '候选更新通道已停用；请使用本机测试版验收后发布正式版。'
}
$result = [pscustomobject]@{
    status = 'configured'
    previous_channel = 'stable'
    channel = 'stable'
    client_profile = 'stable'
    candidate_rollback_authorized = $false
    configuration_path = ''
}
if ($OutputJson) {
    $result | ConvertTo-Json -Compress
} else {
    $result
}
'@
[IO.File]::WriteAllText(
    (Join-Path $candidateStaging 'scripts\start_client_profile.ps1'),
    $profileLauncherShim + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false)
)
[IO.File]::WriteAllText(
    (Join-Path $candidateStaging 'scripts\set_client_update_channel.ps1'),
    $updateChannelShim + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false)
)
[IO.File]::WriteAllText(
    (Join-Path $candidateStaging 'VERSION.txt'),
    $Version + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false)
)

if (-not $ArchiveName) {
    $ArchiveName = "ERP自动化客户端-$Version.zip"
}
if ($ArchiveName -notmatch '^[^\\/:*?"<>|]+\.zip$') {
    throw '客户端 ZIP 文件名无效。'
}
$zipPath = Join-Path $resolvedOutput $ArchiveName
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Compress-Archive -Path (Join-Path $candidateStaging '*') `
    -DestinationPath $zipPath `
    -CompressionLevel Optimal
$hash = Get-Sha256Hex -LiteralPath $zipPath
Write-Output "ZIP=$zipPath"
Write-Output "SHA256=$hash"
