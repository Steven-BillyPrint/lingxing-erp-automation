param(
    [string]$Version = '2026.7.3',
    [string]$ExpectedSha256 = '8635da433b6df8194746e88ed9d2589566c20e38bfc2a80e431a348b7c765841',
    [string]$Destination = ''
)

$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $Destination) {
    $Destination = Join-Path $workspace 'tools\cloudflared.exe'
}
$destinationPath = [IO.Path]::GetFullPath($Destination)
$toolsRoot = [IO.Path]::GetFullPath((Join-Path $workspace 'tools'))
if (-not $destinationPath.StartsWith(
    $toolsRoot + [IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw 'Cloudflared destination must stay inside the workspace tools directory.'
}

[IO.Directory]::CreateDirectory((Split-Path -Parent $destinationPath)) | Out-Null
if (Test-Path -LiteralPath $destinationPath -PathType Leaf) {
    $existing = (Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($existing -eq $ExpectedSha256.ToLowerInvariant()) {
        Write-Output $destinationPath
        exit 0
    }
}

$temporary = "$destinationPath.download-$PID.tmp"
try {
    $uri = "https://github.com/cloudflare/cloudflared/releases/download/$Version/cloudflared-windows-amd64.exe"
    Invoke-WebRequest -Uri $uri -OutFile $temporary -UseBasicParsing
    $actual = (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "Cloudflared SHA256 mismatch: $actual"
    }
    Move-Item -LiteralPath $temporary -Destination $destinationPath -Force
} finally {
    if (Test-Path -LiteralPath $temporary -PathType Leaf) {
        Remove-Item -LiteralPath $temporary -Force
    }
}
Write-Output $destinationPath
