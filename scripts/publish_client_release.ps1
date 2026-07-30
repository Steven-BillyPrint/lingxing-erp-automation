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
        $runUrl = (& gh workflow run release.yml --ref main) -join "`n"
        if ($LASTEXITCODE -ne 0) {
            throw '无法触发 GitHub Windows 客户端发布工作流。'
        }
        $runMatch = [regex]::Match($runUrl, '/actions/runs/(?<id>\d+)')
        if (-not $runMatch.Success) {
            throw "无法从工作流返回值识别运行 ID：$runUrl"
        }
        & gh run watch $runMatch.Groups['id'].Value --exit-status
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
        & gh release edit $tag --draft=false --latest
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
    Write-Host "正式客户端已发布：$($published.url)" -ForegroundColor Green
} finally {
    Pop-Location
}

