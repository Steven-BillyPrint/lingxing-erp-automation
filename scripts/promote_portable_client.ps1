[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourcePackageRoot,
    [Parameter(Mandatory = $true)]
    [string]$TargetPackageRoot,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedCurrentVersion,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedVersion,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string]$ExpectedTargetSha256,
    [int]$WaitProcessId = 0
)

$ErrorActionPreference = 'Stop'
$logPath = Join-Path $env:LOCALAPPDATA 'LingxingERP\portable-update.log'
$applicationDirectoryName = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('RVJQ6Ieq5Yqo5YyW')
)
$applicationFileName = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('RVJQ6Ieq5Yqo5YyWLmV4ZQ==')
)

function Write-PromotionLog([string]$Message) {
    try {
        $directory = Split-Path -Parent $logPath
        [IO.Directory]::CreateDirectory($directory) | Out-Null
        $timestamp = [DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss.fff')
        [IO.File]::AppendAllText(
            $logPath,
            "$timestamp $Message$([Environment]::NewLine)",
            [Text.UTF8Encoding]::new($false)
        )
    } catch {
        # A diagnostics failure must not change the promotion result.
    }
}

function Assert-ChildPath(
    [string]$Candidate,
    [string]$Parent,
    [string]$Label
) {
    $candidatePath = [IO.Path]::GetFullPath($Candidate)
    $parentPath = [IO.Path]::GetFullPath($Parent)
    $prefix = $parentPath.TrimEnd('\', '/') +
        [IO.Path]::DirectorySeparatorChar
    if (-not $candidatePath.StartsWith(
        $prefix,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "$Label path escaped the package root."
    }
    return $candidatePath
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

function Get-TextSha256Hex([string]$Value) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $algorithm.ComputeHash($bytes)
    } finally {
        $algorithm.Dispose()
    }
    return ([BitConverter]::ToString($digest)).Replace(
        '-',
        ''
    ).ToLowerInvariant()
}

function Repair-InterruptedPromotion([string]$Root) {
    $targetApplicationRoot = Join-Path (
        Join-Path $Root 'dist'
    ) $applicationDirectoryName
    foreach ($stage in @(
        Get-ChildItem -LiteralPath $Root -Directory -Force |
            Where-Object { $_.Name -like '.erp-client-promote-*' }
    )) {
        Remove-Item -LiteralPath $stage.FullName -Recurse -Force
    }
    $backups = @(
        Get-ChildItem -LiteralPath $Root -Directory -Force |
            Where-Object { $_.Name -like '.erp-client-backup-*' } |
            Sort-Object LastWriteTimeUtc
    )
    foreach ($backup in $backups) {
        $committed = Join-Path $backup.FullName 'COMMITTED'
        if (Test-Path -LiteralPath $committed -PathType Leaf) {
            Remove-Item -LiteralPath $backup.FullName -Recurse -Force
            continue
        }
        $backupApplication = Join-Path (
            $backup.FullName
        ) $applicationDirectoryName
        if (Test-Path -LiteralPath $backupApplication -PathType Container) {
            if (Test-Path -LiteralPath $targetApplicationRoot -PathType Container) {
                Remove-Item -LiteralPath $targetApplicationRoot -Recurse -Force
            }
            [IO.Directory]::CreateDirectory(
                (Split-Path -Parent $targetApplicationRoot)
            ) | Out-Null
            Move-Item -LiteralPath $backupApplication `
                -Destination $targetApplicationRoot
        }
        $backupScripts = Join-Path $backup.FullName 'scripts'
        if (Test-Path -LiteralPath $backupScripts -PathType Container) {
            $targetScripts = Join-Path $Root 'scripts'
            [IO.Directory]::CreateDirectory($targetScripts) | Out-Null
            foreach ($scriptName in $releaseScriptNames) {
                $backupScript = Join-Path $backupScripts $scriptName
                $targetScript = Join-Path $targetScripts $scriptName
                if (Test-Path -LiteralPath $backupScript -PathType Leaf) {
                    Copy-Item -LiteralPath $backupScript `
                        -Destination $targetScript -Force
                } elseif (Test-Path -LiteralPath (
                    Join-Path $backup.FullName "missing-$scriptName"
                ) -PathType Leaf) {
                    if (Test-Path -LiteralPath $targetScript -PathType Leaf) {
                        Remove-Item -LiteralPath $targetScript -Force
                    }
                }
            }
        }
        $targetVersion = Join-Path $Root 'VERSION.txt'
        $originalVersion = Join-Path $backup.FullName 'original-VERSION.txt'
        if (Test-Path -LiteralPath $originalVersion -PathType Leaf) {
            Copy-Item -LiteralPath $originalVersion `
                -Destination $targetVersion -Force
        } elseif (Test-Path -LiteralPath (
            Join-Path $backup.FullName 'VERSION-ABSENT'
        ) -PathType Leaf) {
            if (Test-Path -LiteralPath $targetVersion -PathType Leaf) {
                Remove-Item -LiteralPath $targetVersion -Force
            }
        }
        Remove-Item -LiteralPath $backup.FullName -Recurse -Force
        Write-PromotionLog 'Recovered an interrupted portable update.'
    }
}

$stageRoot = ''
$backupRoot = ''
$targetRoot = ''
$newApplicationInstalled = $false
$originalVersion = ''
$releaseScriptNames = @(
    'start_shared_desktop.ps1',
    'install_shared_client.ps1',
    'update_shared_client.ps1',
    'promote_portable_client.ps1',
    'complete_client_repair.ps1'
)
$scriptBackupsReady = $false
$targetVersionExisted = $false
$promotionMutex = $null
$promotionMutexAcquired = $false
try {
    $sourceRoot = (Resolve-Path -LiteralPath $SourcePackageRoot).Path
    $targetRoot = (Resolve-Path -LiteralPath $TargetPackageRoot).Path
    if ($sourceRoot.Equals(
        $targetRoot,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Portable source and target directories are identical.'
    }
    if (Test-Path -LiteralPath (Join-Path $targetRoot '.git')) {
        Write-Output "SKIPPED_GIT_WORKTREE=$targetRoot"
        return
    }
    if ($targetRoot -eq [IO.Path]::GetPathRoot($targetRoot)) {
        throw 'A drive root cannot be used as a portable client target.'
    }
    $mutexSuffix = (Get-TextSha256Hex $targetRoot).Substring(0, 24)
    $promotionMutex = [Threading.Mutex]::new(
        $false,
        "Local\LingxingERPPortablePromotion-$mutexSuffix"
    )
    $promotionMutexAcquired = $promotionMutex.WaitOne(
        [TimeSpan]::FromMinutes(3)
    )
    if (-not $promotionMutexAcquired) {
        throw 'Another portable client update did not finish in time.'
    }
    Repair-InterruptedPromotion $targetRoot

    $sourceVersionFile = Join-Path $sourceRoot 'VERSION.txt'
    $targetVersionFile = Join-Path $targetRoot 'VERSION.txt'
    $sourceApplication = Join-Path (
        $sourceRoot
    ) (Join-Path (Join-Path 'dist' $applicationDirectoryName) $applicationFileName)
    $targetApplicationRoot = Assert-ChildPath (
        Join-Path (Join-Path $targetRoot 'dist') $applicationDirectoryName
    ) $targetRoot 'client'
    $targetApplication = Join-Path $targetApplicationRoot $applicationFileName
    foreach ($required in @(
        $sourceVersionFile,
        $sourceApplication,
        $targetApplication
    )) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Portable client structure is incomplete: $required"
        }
    }
    if (
        (Get-Content -LiteralPath $sourceVersionFile -Raw).Trim() -ne
            $ExpectedVersion
    ) {
        throw 'Installed client version does not match the expected release.'
    }
    $ExpectedTargetSha256 = $ExpectedTargetSha256.ToLowerInvariant()
    if (
        (Get-Sha256Hex -LiteralPath $targetApplication) -ne
            $ExpectedTargetSha256
    ) {
        throw 'Portable client changed before promotion.'
    }
    $targetVersionExisted = Test-Path -LiteralPath $targetVersionFile `
        -PathType Leaf
    if ($targetVersionExisted) {
        $originalVersion = (
            Get-Content -LiteralPath $targetVersionFile -Raw
        ).Trim()
    }

    $stageRoot = Assert-ChildPath (
        Join-Path $targetRoot (
            '.erp-client-promote-' + [Guid]::NewGuid().ToString('N')
        )
    ) $targetRoot 'staging'
    $backupRoot = Assert-ChildPath (
        Join-Path $targetRoot (
            '.erp-client-backup-' + [Guid]::NewGuid().ToString('N')
        )
    ) $targetRoot 'backup'
    [IO.Directory]::CreateDirectory((Join-Path $stageRoot 'dist')) | Out-Null
    Copy-Item -LiteralPath (
        Join-Path (Join-Path $sourceRoot 'dist') $applicationDirectoryName
    ) -Destination (Join-Path $stageRoot 'dist') -Recurse
    $stagedApplicationRoot = Join-Path (
        Join-Path $stageRoot 'dist'
    ) $applicationDirectoryName
    if (-not (Test-Path -LiteralPath (
        Join-Path $stagedApplicationRoot $applicationFileName
    ) -PathType Leaf)) {
        throw 'Staged client application is incomplete.'
    }
    $preserveSourceScripts = Test-Path -LiteralPath (
        Join-Path $targetRoot '.git'
    )
    if (-not $preserveSourceScripts) {
        $stagedScripts = Join-Path $stageRoot 'scripts'
        [IO.Directory]::CreateDirectory($stagedScripts) | Out-Null
        foreach ($scriptName in $releaseScriptNames) {
            $sourceScript = Join-Path (
                Join-Path $sourceRoot 'scripts'
            ) $scriptName
            if (-not (Test-Path -LiteralPath $sourceScript -PathType Leaf)) {
                throw "Installed client script is missing: $scriptName"
            }
            Copy-Item -LiteralPath $sourceScript `
                -Destination (Join-Path $stagedScripts $scriptName)
        }
    }

    if ($WaitProcessId -gt 0) {
        $process = Get-Process -Id $WaitProcessId -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            Wait-Process -Id $WaitProcessId -Timeout 120
        }
    }
    if (
        (Get-Sha256Hex -LiteralPath $targetApplication) -ne
            $ExpectedTargetSha256
    ) {
        throw 'Portable client changed while waiting for shutdown.'
    }

    [IO.Directory]::CreateDirectory($backupRoot) | Out-Null
    $backupApplicationRoot = Join-Path $backupRoot $applicationDirectoryName
    if ($targetVersionExisted) {
        Copy-Item -LiteralPath $targetVersionFile `
            -Destination (Join-Path $backupRoot 'original-VERSION.txt')
    } else {
        [IO.File]::WriteAllText(
            (Join-Path $backupRoot 'VERSION-ABSENT'),
            '',
            [Text.UTF8Encoding]::new($false)
        )
    }
    try {
        # A Git worktree owns its source scripts. Extracted portable clients
        # receive only the four signed release scripts, never a broad mirror.
        # Scripts are committed before the EXE, so a visible new EXE can never
        # run with an older updater.
        if (-not $preserveSourceScripts) {
            $targetScripts = Join-Path $targetRoot 'scripts'
            [IO.Directory]::CreateDirectory($targetScripts) | Out-Null
            $backupScripts = Join-Path $backupRoot 'scripts'
            [IO.Directory]::CreateDirectory($backupScripts) | Out-Null
            foreach ($scriptName in $releaseScriptNames) {
                $targetScript = Join-Path $targetScripts $scriptName
                if (Test-Path -LiteralPath $targetScript -PathType Leaf) {
                    Copy-Item -LiteralPath $targetScript `
                        -Destination (Join-Path $backupScripts $scriptName)
                } else {
                    [IO.File]::WriteAllText(
                        (Join-Path $backupRoot "missing-$scriptName"),
                        '',
                        [Text.UTF8Encoding]::new($false)
                    )
                }
            }
            $scriptBackupsReady = $true
            foreach ($scriptName in $releaseScriptNames) {
                Copy-Item -LiteralPath (
                    Join-Path $stagedScripts $scriptName
                ) -Destination (Join-Path $targetScripts $scriptName) -Force
            }
        }

        Move-Item -LiteralPath $targetApplicationRoot `
            -Destination $backupApplicationRoot
        Move-Item -LiteralPath $stagedApplicationRoot `
            -Destination $targetApplicationRoot
        $newApplicationInstalled = $true
        if ($targetVersionExisted) {
            $temporaryVersion = Join-Path $targetRoot (
                '.VERSION-' + [Guid]::NewGuid().ToString('N') + '.tmp'
            )
            try {
                [IO.File]::WriteAllText(
                    $temporaryVersion,
                    $ExpectedVersion + [Environment]::NewLine,
                    [Text.UTF8Encoding]::new($false)
                )
                Move-Item -LiteralPath $temporaryVersion `
                    -Destination $targetVersionFile -Force
            } finally {
                if (Test-Path -LiteralPath $temporaryVersion -PathType Leaf) {
                    Remove-Item -LiteralPath $temporaryVersion -Force
                }
            }
        }

        if (
            -not (Test-Path -LiteralPath $targetApplication -PathType Leaf) -or
            (
                $targetVersionExisted -and
                (Get-Content -LiteralPath $targetVersionFile -Raw).Trim() -ne
                    $ExpectedVersion
            )
        ) {
            throw 'Portable client validation failed after promotion.'
        }
        if (-not $preserveSourceScripts) {
            foreach ($scriptName in $releaseScriptNames) {
                if (
                    (Get-Sha256Hex -LiteralPath (
                        Join-Path $targetScripts $scriptName
                    )) -ne
                    (Get-Sha256Hex -LiteralPath (
                        Join-Path $stagedScripts $scriptName
                    ))
                ) {
                    throw "Portable client script validation failed: $scriptName"
                }
            }
        }
        [IO.File]::WriteAllText(
            (Join-Path $backupRoot 'COMMITTED'),
            $ExpectedVersion + [Environment]::NewLine,
            [Text.UTF8Encoding]::new($false)
        )
    } catch {
        if (
            $newApplicationInstalled -and
            (Test-Path -LiteralPath $targetApplicationRoot -PathType Container)
        ) {
            Remove-Item -LiteralPath $targetApplicationRoot -Recurse -Force
        }
        if (Test-Path -LiteralPath $backupApplicationRoot -PathType Container) {
            Move-Item -LiteralPath $backupApplicationRoot `
                -Destination $targetApplicationRoot
        }
        if ($targetVersionExisted) {
            [IO.File]::WriteAllText(
                $targetVersionFile,
                $originalVersion + [Environment]::NewLine,
                [Text.UTF8Encoding]::new($false)
            )
        }
        if ($scriptBackupsReady) {
            foreach ($scriptName in $releaseScriptNames) {
                $targetScript = Join-Path (
                    Join-Path $targetRoot 'scripts'
                ) $scriptName
                $backupScript = Join-Path (
                    Join-Path $backupRoot 'scripts'
                ) $scriptName
                if (Test-Path -LiteralPath $backupScript -PathType Leaf) {
                    Copy-Item -LiteralPath $backupScript `
                        -Destination $targetScript -Force
                } elseif (Test-Path -LiteralPath $targetScript -PathType Leaf) {
                    Remove-Item -LiteralPath $targetScript -Force
                }
            }
        }
        throw
    }

    Remove-Item -LiteralPath $backupRoot -Recurse -Force
    $backupRoot = ''
    Write-PromotionLog "Portable client promoted to $ExpectedVersion."
} catch {
    Write-PromotionLog (
        "Portable client promotion failed: " + $_.Exception.GetType().Name + '.'
    )
    exit 1
} finally {
    foreach ($candidate in @($stageRoot, $backupRoot)) {
        if (
            $candidate -and
            $targetRoot -and
            (Test-Path -LiteralPath $candidate -PathType Container)
        ) {
            $validated = Assert-ChildPath $candidate $targetRoot 'cleanup'
            Remove-Item -LiteralPath $validated -Recurse -Force
        }
    }
    if ($promotionMutexAcquired) {
        $promotionMutex.ReleaseMutex()
    }
    if ($null -ne $promotionMutex) {
        $promotionMutex.Dispose()
    }
}
