[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Stable', 'Candidate')]
    [string]$Channel,
    [ValidateSet('Stable', 'Candidate')]
    [string]$ClientProfile = 'Candidate',
    [string]$StateRoot = '',
    [switch]$ConfirmCandidateEnrollment,
    [switch]$ConfirmCandidateRollback,
    [switch]$OutputJson
)

$ErrorActionPreference = 'Stop'

if (-not $StateRoot) {
    $StateRoot = if ($ClientProfile -eq 'Candidate') {
        Join-Path $env:LOCALAPPDATA 'LingxingERP-Candidate'
    } else {
        Join-Path $env:LOCALAPPDATA 'LingxingERP'
    }
}

$normalizedChannel = $Channel.ToLowerInvariant()
$channelPath = Join-Path $StateRoot 'update-channel.json'
$currentChannel = 'stable'
if (Test-Path -LiteralPath $channelPath -PathType Leaf) {
    try {
        $current = Get-Content -LiteralPath $channelPath -Raw |
            ConvertFrom-Json
        if ([int]$current.schema_version -ne 1) {
            throw 'unsupported schema'
        }
        $currentChannel = ([string]$current.channel).Trim().ToLowerInvariant()
        if (
            $currentChannel -notin @('stable', 'candidate') -or
            $current.allow_candidate_rollback -notin @($true, $false)
        ) {
            throw 'invalid channel'
        }
    } catch {
        throw '现有更新通道配置无效，拒绝覆盖；请先检查本机状态。'
    }
}

if ($normalizedChannel -eq 'candidate' -and -not $ConfirmCandidateEnrollment) {
    throw '加入候选通道必须显式传入 -ConfirmCandidateEnrollment。'
}
if (
    $normalizedChannel -eq 'stable' -and
    $currentChannel -eq 'candidate' -and
    -not $ConfirmCandidateRollback
) {
    throw (
        '候选电脑返回正式通道可能安装较低的稳定版本，必须显式传入 ' +
        '-ConfirmCandidateRollback。'
    )
}

$allowRollback = (
    $normalizedChannel -eq 'stable' -and
    $currentChannel -eq 'candidate'
)
$payload = [ordered]@{
    schema_version = 1
    channel = $normalizedChannel
    allow_candidate_rollback = $allowRollback
    updated_at = [DateTime]::UtcNow.ToString('o')
    computer_name = [Environment]::MachineName
} | ConvertTo-Json -Depth 3

[IO.Directory]::CreateDirectory($StateRoot) | Out-Null
$temporary = Join-Path $StateRoot (
    '.update-channel-' + [Guid]::NewGuid().ToString('N') + '.tmp'
)
try {
    [IO.File]::WriteAllText(
        $temporary,
        $payload + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporary -Destination $channelPath -Force
} finally {
    if (Test-Path -LiteralPath $temporary -PathType Leaf) {
        Remove-Item -LiteralPath $temporary -Force
    }
}

$result = [pscustomobject]@{
    status = 'configured'
    previous_channel = $currentChannel
    channel = $normalizedChannel
    client_profile = $ClientProfile.ToLowerInvariant()
    candidate_rollback_authorized = $allowRollback
    configuration_path = $channelPath
}
if ($OutputJson) {
    $result | ConvertTo-Json -Compress
} else {
    $result
}
