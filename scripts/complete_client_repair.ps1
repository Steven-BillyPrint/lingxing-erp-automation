[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PackageRoot,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}\.\d{2}\.\d{2}\.\d+$')]
    [string]$Version,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-fA-F0-9]{64}$')]
    [string]$ExpectedContentSha256,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-fA-F0-9]{64}$')]
    [string]$ExpectedPackageSha256,
    [Parameter(Mandatory = $true)]
    [int]$WaitProcessId,
    [int64]$ExpectedProcessStartTimeUtcTicks = 0,
    [string]$StateRoot = (Join-Path $env:LOCALAPPDATA 'LingxingERP'),
    [string]$DesktopDirectory = '',
    [ValidateRange(1, 300)]
    [int]$ApplicationSmokeTestTimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'
$logPath = Join-Path $StateRoot 'client-repair.log'
$repairSucceeded = $false
$resolvedPackageRoot = ''

function Write-RepairLog([string]$Message) {
    try {
        [IO.Directory]::CreateDirectory($StateRoot) | Out-Null
        $timestamp = [DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss.fff')
        [IO.File]::AppendAllText(
            $logPath,
            "$timestamp $Message$([Environment]::NewLine)",
            [Text.UTF8Encoding]::new($false)
        )
    } catch {
        # Diagnostics must not change repair behavior.
    }
}

function Get-Sha256Hex([string]$LiteralPath) {
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
    return ([BitConverter]::ToString($digest)).Replace(
        '-',
        ''
    ).ToLowerInvariant()
}

function Get-DirectoryContentInfo([string]$Root) {
    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
    $paths = [string[]]@(
        Get-ChildItem -LiteralPath $resolvedRoot -Recurse -File |
            Where-Object {
                $_.Name -ne 'install-receipt.json' -and
                $_.Name -notlike '.install-receipt-*.tmp'
            } |
            ForEach-Object {
                $_.FullName.Substring($resolvedRoot.Length).
                    TrimStart('\').Replace('\', '/')
            }
    )
    [Array]::Sort($paths, [StringComparer]::Ordinal)
    $unique = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    $canonical = [Text.StringBuilder]::new()
    foreach ($relativePath in $paths) {
        if (
            -not $relativePath -or
            $relativePath.Contains([char]0) -or
            $relativePath.Contains("`n") -or
            -not $unique.Add($relativePath)
        ) {
            throw 'The repair package contains a path that cannot be verified.'
        }
        $literalPath = Join-Path (
            $resolvedRoot
        ) $relativePath.Replace('/', [IO.Path]::DirectorySeparatorChar)
        [void]$canonical.Append($relativePath)
        [void]$canonical.Append([char]0)
        [void]$canonical.Append((Get-Sha256Hex -LiteralPath $literalPath))
        [void]$canonical.Append("`n")
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes($canonical.ToString())
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $algorithm.ComputeHash($bytes)
    } finally {
        $algorithm.Dispose()
    }
    return [pscustomobject]@{
        Sha256 = ([BitConverter]::ToString($digest)).Replace(
            '-',
            ''
        ).ToLowerInvariant()
        FileCount = $paths.Count
    }
}

function Assert-VerifiedPackage {
    $actual = Get-DirectoryContentInfo -Root $resolvedPackageRoot
    if ($actual.Sha256 -ne $ExpectedContentSha256.ToLowerInvariant()) {
        throw 'The staged repair package failed content verification.'
    }
    $packageVersion = (
        Get-Content -LiteralPath (
            Join-Path $resolvedPackageRoot 'VERSION.txt'
        ) -Raw
    ).Trim()
    if ($packageVersion -ne $Version) {
        throw 'The staged repair package version does not match.'
    }
}

try {
    $updatesRoot = Join-Path $StateRoot 'updates'
    $resolvedUpdatesRoot = (Resolve-Path -LiteralPath $updatesRoot).Path
    $resolvedPackageRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
    $updatesPrefix = $resolvedUpdatesRoot.TrimEnd('\', '/') +
        [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedPackageRoot.StartsWith(
        $updatesPrefix,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'The staged repair package is outside the controlled update directory.'
    }
    $programBase = [IO.Path]::GetFullPath(
        (Join-Path $env:LOCALAPPDATA 'Programs\LingxingERP')
    )
    $targetRoot = [IO.Path]::GetFullPath(
        (Join-Path $programBase $Version)
    )
    if (-not $targetRoot.StartsWith(
        $programBase.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'The repair target is outside the managed program directory.'
    }
    $installer = Join-Path $resolvedPackageRoot 'scripts\install_shared_client.ps1'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw 'The staged repair package does not contain the installer.'
    }
    Assert-VerifiedPackage

    $process = Get-Process -Id $WaitProcessId -ErrorAction SilentlyContinue
    if ($null -ne $process -and $ExpectedProcessStartTimeUtcTicks -gt 0) {
        try {
            if (
                $process.StartTime.ToUniversalTime().Ticks -ne
                    $ExpectedProcessStartTimeUtcTicks
            ) {
                $process = $null
            }
        } catch {
            $process = $null
        }
    }
    if ($null -ne $process) {
        Write-RepairLog "Waiting for client process $WaitProcessId to exit."
        Wait-Process -Id $WaitProcessId -Timeout 180
    }

    Assert-VerifiedPackage
    $installerArguments = @{
        PackageRoot = $resolvedPackageRoot
        SkipLegacyPortablePromotion = $true
        Silent = $true
        ApplicationSmokeTestTimeoutSeconds = $ApplicationSmokeTestTimeoutSeconds
    }
    if ($DesktopDirectory) {
        $installerArguments.DesktopDirectory = $DesktopDirectory
    }
    & $installer @installerArguments | Out-Null
    $applicationDirectories = @(
        Get-ChildItem -LiteralPath (Join-Path $targetRoot 'dist') -Directory
    )
    if ($applicationDirectories.Count -ne 1) {
        throw 'The repaired client must contain exactly one application directory.'
    }
    $application = Join-Path $applicationDirectories[0].FullName (
        $applicationDirectories[0].Name + '.exe'
    )
    if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
        throw 'The repaired client application EXE is missing.'
    }
    $installedContent = Get-DirectoryContentInfo -Root $targetRoot
    if (
        $installedContent.Sha256 -ne
            $ExpectedContentSha256.ToLowerInvariant()
    ) {
        throw 'The installed repair failed final content verification.'
    }
    $receipt = [ordered]@{
        schema_version = 1
        version = $Version
        package_sha256 = $ExpectedPackageSha256.ToLowerInvariant()
        content_sha256 = $ExpectedContentSha256.ToLowerInvariant()
        file_count = [int]$installedContent.FileCount
    } | ConvertTo-Json -Depth 5
    $receiptPath = Join-Path $targetRoot 'install-receipt.json'
    $temporaryReceipt = Join-Path $targetRoot (
        '.install-receipt-' + [Guid]::NewGuid().ToString('N') + '.tmp'
    )
    try {
        [IO.File]::WriteAllText(
            $temporaryReceipt,
            $receipt + [Environment]::NewLine,
            [Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporaryReceipt `
            -Destination $receiptPath -Force
    } finally {
        if (Test-Path -LiteralPath $temporaryReceipt -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryReceipt -Force
        }
    }
    Start-Process `
        -FilePath $application `
        -WorkingDirectory $targetRoot | Out-Null
    $repairSucceeded = $true
    Write-RepairLog "Client repair $Version completed and restarted."
} catch {
    Write-RepairLog (
        "Client repair failed: $($_.Exception.GetType().Name): " +
        $_.Exception.Message
    )
    exit 1
} finally {
    if (
        $repairSucceeded -and
        $resolvedPackageRoot -and
        (Test-Path -LiteralPath $resolvedPackageRoot -PathType Container)
    ) {
        try {
            Remove-Item -LiteralPath $resolvedPackageRoot -Recurse -Force
        } catch {
            # The ordinary stale-update cleanup will retry after 24 hours.
        }
    }
}
