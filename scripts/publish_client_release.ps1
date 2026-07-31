[CmdletBinding()]
param(
    [switch]$ConfirmProductionRelease
)

$ErrorActionPreference = 'Stop'

if (-not $ConfirmProductionRelease) {
    throw '正式发布必须显式传入 -ConfirmProductionRelease。'
}

$workspace = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $workspace
try {
    $branch = (& git branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') {
        throw '正式发布只能从 main 分支执行。'
    }
    $trackedChanges = (& git status --porcelain --untracked-files=no) -join "`n"
    if ($LASTEXITCODE -ne 0 -or $trackedChanges.Trim()) {
        throw 'main 存在尚未提交的已跟踪改动，拒绝发布。'
    }

    & git fetch origin main
    if ($LASTEXITCODE -ne 0) {
        throw '无法读取远端 main。'
    }
    $localCommit = (& git rev-parse HEAD).Trim()
    $remoteCommit = (& git rev-parse origin/main).Trim()
    if (-not $localCommit -or $localCommit -ne $remoteCommit) {
        throw '本机 main 与 origin/main 不一致，拒绝发布。'
    }

    $version = (Get-Content -LiteralPath (Join-Path $workspace 'CLIENT_VERSION') -Raw).Trim()
    if ($version -notmatch '^\d{4}\.\d{2}\.\d{2}\.\d+$') {
        throw "CLIENT_VERSION 无效：$version"
    }
    $tag = "v$version"

    $release = $null
    # Windows PowerShell 5 turns native stderr into a terminating ErrorRecord
    # while ErrorActionPreference is Stop.  A missing release is the expected
    # first-run probe result, so capture its exit code without aborting.
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $releaseOutput = & gh release view $tag `
            --json tagName,isDraft,isPrerelease,targetCommitish,assets,url 2>$null
        $releaseViewExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($releaseViewExitCode -eq 0) {
        $release = ($releaseOutput -join "`n") | ConvertFrom-Json
    } else {
        $runsBeforeOutput = & gh run list `
            --workflow release.yml `
            --branch main `
            --event workflow_dispatch `
            --limit 50 `
            --json databaseId
        if ($LASTEXITCODE -ne 0) {
            throw '无法读取发布工作流运行列表。'
        }
        $runsBefore = @(($runsBeforeOutput -join "`n") | ConvertFrom-Json)
        $knownRunIds = [Collections.Generic.HashSet[int64]]::new()
        foreach ($knownRun in $runsBefore) {
            [void]$knownRunIds.Add([int64]$knownRun.databaseId)
        }

        & gh workflow run release.yml --ref main
        if ($LASTEXITCODE -ne 0) {
            throw '无法触发 GitHub Windows 客户端发布工作流。'
        }

        $releaseRun = $null
        $discoveryDeadline = [DateTime]::UtcNow.AddMinutes(2)
        while (
            $null -eq $releaseRun -and
            [DateTime]::UtcNow -lt $discoveryDeadline
        ) {
            Start-Sleep -Seconds 2
            $runsOutput = & gh run list `
                --workflow release.yml `
                --branch main `
                --event workflow_dispatch `
                --limit 20 `
                --json databaseId,headSha,status,url,createdAt
            if ($LASTEXITCODE -ne 0) {
                throw '触发后无法读取发布工作流运行列表。'
            }
            $runs = @(($runsOutput -join "`n") | ConvertFrom-Json)
            $releaseRun = $runs |
                Where-Object {
                    -not $knownRunIds.Contains([int64]$_.databaseId) -and
                    [string]$_.headSha -eq $localCommit
                } |
                Sort-Object createdAt -Descending |
                Select-Object -First 1
        }
        if ($null -eq $releaseRun) {
            throw '发布工作流已触发，但两分钟内没有发现对应 main 提交的运行。'
        }
        $runUrl = [string]$releaseRun.url
        & gh run watch ([string]$releaseRun.databaseId) --exit-status
        if ($LASTEXITCODE -ne 0) {
            throw "客户端发布工作流失败：$runUrl"
        }
        $releaseOutput = & gh release view $tag `
            --json tagName,isDraft,isPrerelease,targetCommitish,assets,url
        if ($LASTEXITCODE -ne 0) {
            throw "发布工作流结束后未找到草稿 Release：$tag"
        }
        $release = ($releaseOutput -join "`n") | ConvertFrom-Json
    }

    if ($release.tagName -ne $tag -or $release.isPrerelease) {
        throw "Release 元数据与预期版本不一致：$tag"
    }
    if ($release.targetCommitish -ne $localCommit) {
        throw (
            "Release 目标提交不是当前 main。预期 $localCommit，" +
            "实际 $($release.targetCommitish)。"
        )
    }
    $assetNames = @($release.assets | ForEach-Object { $_.name })
    foreach ($requiredAsset in @(
        'ERP-Automation-Client.zip',
        'latest.json',
        'SHA256SUMS.txt'
    )) {
        if ($requiredAsset -notin $assetNames) {
            throw "Release 缺少正式资产：$requiredAsset"
        }
    }

    if ($release.isDraft) {
        # Publish immutable assets first, but do not expose them through the
        # stable "latest" URL until the matching server version is healthy.
        & gh release edit $tag --draft=false --latest=false
        if ($LASTEXITCODE -ne 0) {
            throw "无法发布正式 Release：$tag"
        }
    }

    $publishedOutput = & gh release view $tag `
        --json tagName,isDraft,isPrerelease,targetCommitish,assets,url
    if ($LASTEXITCODE -ne 0) {
        throw "无法复核正式 Release：$tag"
    }
    $published = ($publishedOutput -join "`n") | ConvertFrom-Json
    if ($published.isDraft -or $published.isPrerelease) {
        throw "Release 尚未成为正式版本：$tag"
    }
    Write-Host (
        "正式客户端资产已发布并等待服务器部署后激活：$($published.url)"
    ) -ForegroundColor Green
} finally {
    Pop-Location
}

