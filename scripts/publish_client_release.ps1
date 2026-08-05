[CmdletBinding()]
param(
    [switch]$ConfirmProductionRelease
)

$ErrorActionPreference = 'Stop'

if (-not $ConfirmProductionRelease) {
    throw '正式发布必须显式传入 -ConfirmProductionRelease。'
}

function Assert-ReleaseAssets(
    [string]$Tag,
    [string]$Version,
    [string]$Workspace
) {
    $temporaryBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $verificationRoot = [IO.Path]::GetFullPath(
        (Join-Path $temporaryBase (
            'LingxingERP-release-verification-' +
            [Guid]::NewGuid().ToString('N')
        ))
    )
    $temporaryPrefix = $temporaryBase.TrimEnd('\', '/') +
        [IO.Path]::DirectorySeparatorChar
    if (-not $verificationRoot.StartsWith(
        $temporaryPrefix,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Release 资产复核目录越界。'
    }
    try {
        [IO.Directory]::CreateDirectory($verificationRoot) | Out-Null
        & gh release download $Tag `
            --dir $verificationRoot `
            --pattern 'ERP-Automation-Client.zip' `
            --pattern 'latest.json' `
            --pattern 'SHA256SUMS.txt'
        if ($LASTEXITCODE -ne 0) {
            throw "无法下载 Release 资产进行发布前复核：$Tag"
        }
        $packagePath = Join-Path $verificationRoot 'ERP-Automation-Client.zip'
        $manifestPath = Join-Path $verificationRoot 'latest.json'
        $sumsPath = Join-Path $verificationRoot 'SHA256SUMS.txt'
        foreach ($requiredPath in @($packagePath, $manifestPath, $sumsPath)) {
            if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
                throw "Release 资产下载不完整：$requiredPath"
            }
        }
        $recomputedManifestPath = Join-Path $verificationRoot 'recomputed.json'
        & (Join-Path $Workspace 'scripts\create_release_manifest.ps1') `
            -PackagePath $packagePath `
            -Version $Version `
            -OutputPath $recomputedManifestPath | Out-Null
        $manifest = Get-Content -LiteralPath $manifestPath -Raw |
            ConvertFrom-Json
        $recomputed = Get-Content -LiteralPath $recomputedManifestPath -Raw |
            ConvertFrom-Json
        if (
            [int]$manifest.schema_version -ne 1 -or
            [string]$manifest.version -ne $Version -or
            $manifest.mandatory -ne $true -or
            [string]$manifest.package.name -ne
                [string]$recomputed.package.name -or
            [string]$manifest.package.url -ne
                [string]$recomputed.package.url -or
            [string]$manifest.package.sha256 -ne
                [string]$recomputed.package.sha256 -or
            [string]$manifest.package.content_sha256 -ne
                [string]$recomputed.package.content_sha256 -or
            [int64]$manifest.package.size -ne
                [int64]$recomputed.package.size
        ) {
            throw "Release 清单与实际客户端包不一致：$Tag"
        }
        $expectedSum = (
            "$($recomputed.package.sha256)  ERP-Automation-Client.zip"
        )
        $actualSum = (
            Get-Content -LiteralPath $sumsPath -Raw
        ).Trim()
        if ($actualSum -ne $expectedSum) {
            throw "Release SHA256SUMS.txt 与实际客户端包不一致：$Tag"
        }
    } finally {
        if (
            $verificationRoot.StartsWith(
                $temporaryPrefix,
                [StringComparison]::OrdinalIgnoreCase
            ) -and
            (Test-Path -LiteralPath $verificationRoot -PathType Container)
        ) {
            Remove-Item -LiteralPath $verificationRoot -Recurse -Force
        }
    }
}

function Get-LatestPublishedRelease {
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $latestOutput = & gh release view --json tagName,url 2>$null
        $latestExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($latestExitCode -eq 0) {
        return (($latestOutput -join "`n") | ConvertFrom-Json)
    }
    $releasesOutput = & gh release list `
        --limit 100 `
        --json tagName,isDraft,isPrerelease,publishedAt,url
    if ($LASTEXITCODE -ne 0) {
        throw '无法读取 GitHub Release 列表。'
    }
    $published = @(
        (($releasesOutput -join "`n") | ConvertFrom-Json) |
            Where-Object { -not $_.isDraft -and -not $_.isPrerelease } |
            Sort-Object publishedAt -Descending
    )
    if ($published.Count -eq 0) {
        return $null
    }
    return $published[0]
}

function Get-ReusableCiRun([string]$Commit) {
    $runsOutput = & gh run list `
        --workflow test.yml `
        --branch main `
        --event push `
        --commit $Commit `
        --limit 20 `
        --json databaseId,headSha,status,conclusion,event,workflowName,url,createdAt
    if ($LASTEXITCODE -ne 0) {
        throw "无法读取提交 $Commit 的完整 CI 结果。"
    }
    $parsedRuns = (($runsOutput -join "`n") | ConvertFrom-Json)
    $runs = @($parsedRuns)
    $run = $runs |
        Where-Object {
            [string]$_.headSha -eq $Commit -and
            [string]$_.event -eq 'push' -and
            [string]$_.workflowName -eq 'Tests'
        } |
        Sort-Object createdAt -Descending |
        Select-Object -First 1
    if ($null -eq $run) {
        throw "当前 main 提交没有可复用的完整 CI：$Commit"
    }
    if ([string]$run.status -ne 'completed') {
        & gh run watch ([string]$run.databaseId) --exit-status
        if ($LASTEXITCODE -ne 0) {
            throw "当前 main 提交的完整 CI 失败：$($run.url)"
        }
        $refreshedOutput = & gh run view ([string]$run.databaseId) `
            --json databaseId,headSha,status,conclusion,event,workflowName,url,createdAt
        if ($LASTEXITCODE -ne 0) {
            throw "无法复核已完成的完整 CI：$($run.databaseId)"
        }
        $run = ($refreshedOutput -join "`n") | ConvertFrom-Json
    }
    if (
        [string]$run.headSha -ne $Commit -or
        [string]$run.event -ne 'push' -or
        [string]$run.workflowName -ne 'Tests' -or
        [string]$run.status -ne 'completed' -or
        [string]$run.conclusion -ne 'success'
    ) {
        throw "当前 main 提交的完整 CI 未成功，拒绝复用：$($run.url)"
    }
    return $run
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
    $latest = Get-LatestPublishedRelease
    if ($null -ne $latest) {
        $latestTag = [string]$latest.tagName
        if ($latestTag -notmatch '^v(\d{4}\.\d{2}\.\d{2}\.\d+)$') {
            throw "GitHub 当前最新版标签无效：$latestTag"
        }
        $latestVersion = $Matches[1]
        if (([Version]$version).CompareTo([Version]$latestVersion) -lt 0) {
            throw (
                "拒绝发布低于当前更新通道的版本：" +
                "$version < $latestVersion"
            )
        }
    }

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
        $ciRun = Get-ReusableCiRun $localCommit
        $ciRunId = [string]$ciRun.databaseId
        $runsBeforeOutput = & gh run list `
            --workflow release.yml `
            --branch main `
            --event workflow_dispatch `
            --limit 50 `
            --json databaseId
        if ($LASTEXITCODE -ne 0) {
            throw '无法读取发布工作流运行列表。'
        }
        # Windows PowerShell 5 can preserve a JSON array as one nested
        # System.Object[] when ConvertFrom-Json is placed directly inside @().
        # Assign first, then enumerate the value into a flat array.
        $parsedRunsBefore = (
            ($runsBeforeOutput -join "`n") | ConvertFrom-Json
        )
        $runsBefore = @($parsedRunsBefore)
        $knownRunIds = [Collections.Generic.HashSet[int64]]::new()
        foreach ($knownRun in $runsBefore) {
            [void]$knownRunIds.Add([int64]$knownRun.databaseId)
        }

        $releaseRequestId = [Guid]::NewGuid().ToString('N')
        $expectedRunTitle = (
            "Build client release $localCommit [$releaseRequestId]"
        )
        & gh workflow run release.yml `
            --ref main `
            --field "release_commit=$localCommit" `
            --field "ci_run_id=$ciRunId" `
            --field "request_id=$releaseRequestId"
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
                --json databaseId,displayTitle,status,url,createdAt
            if ($LASTEXITCODE -ne 0) {
                throw '触发后无法读取发布工作流运行列表。'
            }
            $parsedRuns = (($runsOutput -join "`n") | ConvertFrom-Json)
            $runs = @($parsedRuns)
            $releaseRun = $runs |
                Where-Object {
                    -not $knownRunIds.Contains([int64]$_.databaseId) -and
                    [string]$_.displayTitle -eq $expectedRunTitle
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
    Assert-ReleaseAssets $tag $version $workspace

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

