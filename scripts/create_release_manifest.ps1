[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PackagePath,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}\.\d{2}\.\d{2}\.\d+$')]
    [string]$Version,
    [string]$Repository = 'Steven-BillyPrint/lingxing-erp-automation',
    [string]$AssetName = 'ERP-Automation-Client.zip',
    [string]$OutputPath = ''
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

$resolvedPackage = (Resolve-Path -LiteralPath $PackagePath).Path
if (-not (Test-Path -LiteralPath $resolvedPackage -PathType Leaf)) {
    throw "客户端发布包不存在：$PackagePath"
}
if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    throw 'GitHub 仓库名称必须使用 owner/repository 格式。'
}
if ($AssetName -notmatch '^[A-Za-z0-9_.-]+\.zip$') {
    throw 'Release ZIP 资产名称无效。'
}
if (-not $OutputPath) {
    $OutputPath = Join-Path (Split-Path -Parent $resolvedPackage) 'latest.json'
}
$outputParent = Split-Path -Parent ([IO.Path]::GetFullPath($OutputPath))
[IO.Directory]::CreateDirectory($outputParent) | Out-Null

$package = Get-Item -LiteralPath $resolvedPackage
$sha256 = Get-Sha256Hex -LiteralPath $resolvedPackage
$tag = "v$Version"
$downloadUrl = "https://github.com/$Repository/releases/download/$tag/$AssetName"
$manifest = [ordered]@{
    schema_version = 1
    version = $Version
    published_at = [DateTime]::UtcNow.ToString('o')
    mandatory = $true
    package = [ordered]@{
        name = $AssetName
        url = $downloadUrl
        sha256 = $sha256
        size = [int64]$package.Length
    }
}
$json = $manifest | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText(
    [IO.Path]::GetFullPath($OutputPath),
    $json + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false)
)
Write-Output "MANIFEST=$([IO.Path]::GetFullPath($OutputPath))"
Write-Output "SHA256=$sha256"
