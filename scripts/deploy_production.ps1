[CmdletBinding()]
param(
    [switch]$ConfirmProductionDeployment,
    [string]$ServerHost = '8.133.172.100',
    [string]$ServerUser = 'admin',
    [string]$DeployKeyPath = (Join-Path $env:LOCALAPPDATA 'Codex\credentials\erp-production-deploy-ed25519'),
    [string]$KnownHostsPath = (Join-Path $env:LOCALAPPDATA 'Codex\credentials\erp-production-known_hosts'),
    [string]$SshPath = (Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'),
    [string]$SshKeygenPath = (Join-Path $env:WINDIR 'System32\OpenSSH\ssh-keygen.exe')
)

$ErrorActionPreference = 'Stop'

if (-not $ConfirmProductionDeployment) {
    throw '正式部署必须显式传入 -ConfirmProductionDeployment。'
}

foreach ($requiredOpenSshFile in @($sshPath, $sshKeygenPath)) {
    if (-not (Test-Path -LiteralPath $requiredOpenSshFile -PathType Leaf)) {
        throw "缺少 Windows OpenSSH 组件：$requiredOpenSshFile"
    }
}

function Invoke-ControlledDeploymentSsh(
    [string]$RemoteCommand,
    [string]$InputLine = ''
) {
    $sshArguments = @(
        '-T',
        '-i', $DeployKeyPath,
        '-o', 'BatchMode=yes',
        '-o', 'IdentitiesOnly=yes',
        '-o', 'PasswordAuthentication=no',
        '-o', 'KbdInteractiveAuthentication=no',
        '-o', 'StrictHostKeyChecking=yes',
        '-o', "UserKnownHostsFile=$KnownHostsPath",
        '-o', 'ConnectTimeout=15',
        '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=3',
        "$ServerUser@$ServerHost",
        $RemoteCommand
    )
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ([string]::IsNullOrEmpty($InputLine)) {
            $output = @('' | & $sshPath @sshArguments 2>&1)
        } else {
            $output = @($InputLine | & $sshPath @sshArguments 2>&1)
        }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
        Text = ($output | ForEach-Object { [string]$_ }) -join "`n"
    }
}

function Get-VerifiedDeploymentReceipt($Output) {
    $lines = @($Output | ForEach-Object { [string]$_ })
    $commitMatches = @(
        $lines |
            Where-Object { $_ -match '^DEPLOYED_COMMIT=([0-9a-f]{40})$' }
    )
    $versionMatches = @(
        $lines |
            Where-Object {
                $_ -match '^DEPLOYED_VERSION=([0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+)$'
            }
    )
    $rolloutMatches = @(
        $lines |
            Where-Object { $_ -match '^ROLLOUT_PENDING=(true|false)$' }
    )
    $drainMatches = @(
        $lines |
            Where-Object { $_ -match '^ROLLOUT_DRAIN_ACTIVE=(true|false)$' }
    )
    if (
        $commitMatches.Count -ne 1 -or
        $versionMatches.Count -ne 1 -or
        $rolloutMatches.Count -gt 1 -or
        $drainMatches.Count -gt 1 -or
        'DEPLOYMENT_HEALTH=healthy' -notin $lines
    ) {
        throw '服务器没有返回唯一且健康的部署回执。'
    }
    return [pscustomobject]@{
        Commit = $commitMatches[0].Substring(16)
        Version = $versionMatches[0].Substring(17)
        RolloutPending = if ($rolloutMatches.Count -eq 1) {
            $rolloutMatches[0].EndsWith('true')
        } else {
            $null
        }
        RolloutDrainActive = if ($drainMatches.Count -eq 1) {
            $drainMatches[0].EndsWith('true')
        } else {
            $null
        }
    }
}

function Complete-ServerRollout(
    [string]$Commit,
    [string]$Version
) {
    $activation = Invoke-ControlledDeploymentSsh `
        'activate-rollout' `
        "$Commit $Version"
    if ($activation.ExitCode -ne 0) {
        throw (
            "客户端更新入口已激活，但服务器滚动窗口启动失败，退出码：" +
            "$($activation.ExitCode)" +
            $(if ($activation.Text) { "`n$($activation.Text)" } else { '' })
        )
    }
    if ($activation.Text) {
        Write-Host $activation.Text
    }
    $receipt = Get-VerifiedDeploymentReceipt $activation.Output
    if (
        $receipt.Commit -ne $Commit -or
        $receipt.Version -ne $Version -or
        $receipt.RolloutPending -ne $false -or
        $receipt.RolloutDrainActive -ne $false -or
        'ROLLOUT_ACTIVATED=true' -notin @(
            $activation.Output | ForEach-Object { [string]$_ }
        )
    ) {
        throw '服务器没有确认客户端滚动窗口已经安全启动。'
    }
    return $receipt
}

function Compare-ReleaseVersion(
    [string]$Left,
    [string]$Right
) {
    if (
        $Left -notmatch '^\d{4}\.\d{2}\.\d{2}\.\d+$' -or
        $Right -notmatch '^\d{4}\.\d{2}\.\d{2}\.\d+$'
    ) {
        throw "无法比较无效客户端版本：$Left / $Right"
    }
    return ([Version]$Left).CompareTo([Version]$Right)
}

$workspace = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
foreach ($requiredFile in @($DeployKeyPath, $KnownHostsPath)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "部署授权文件不存在：$requiredFile"
    }
}
& $sshKeygenPath -F $ServerHost -f $KnownHostsPath *> $null
if ($LASTEXITCODE -ne 0) {
    throw "known_hosts 中没有固定服务器指纹：$ServerHost"
}

Push-Location $workspace
try {
    $channelLatestVersion = ''
    # A prior deployment may have become healthy while GitHub's final
    # "latest" activation failed. The root-owned server receipt lets any
    # authorized Codex task finish that idempotent step even if main moved.
    $reported = Invoke-ControlledDeploymentSsh 'report-deployed'
    if ($reported.ExitCode -eq 0) {
        $serverReceipt = Get-VerifiedDeploymentReceipt $reported.Output
        $latestOutput = & gh release view --json tagName,url
        if ($LASTEXITCODE -ne 0) {
            throw '无法读取 GitHub 当前客户端最新版。'
        }
        $latestRelease = ($latestOutput -join "`n") | ConvertFrom-Json
        $latestTag = [string]$latestRelease.tagName
        if ($latestTag -notmatch '^v(\d{4}\.\d{2}\.\d{2}\.\d+)$') {
            throw "GitHub 当前最新版标签无效：$latestTag"
        }
        $latestVersion = $Matches[1]
        $channelLatestVersion = $latestVersion
        $serverTag = "v$($serverReceipt.Version)"
        $comparison = Compare-ReleaseVersion `
            $serverReceipt.Version `
            $latestVersion
        if ($comparison -gt 0) {
            $pendingOutput = & gh release view $serverTag `
                --json tagName,isDraft,isPrerelease,targetCommitish,assets,url
            if ($LASTEXITCODE -ne 0) {
                throw "服务器版本缺少对应的正式客户端 Release：$serverTag"
            }
            $pending = ($pendingOutput -join "`n") | ConvertFrom-Json
            $pendingAssets = @($pending.assets | ForEach-Object { $_.name })
            if (
                $pending.isDraft -or
                $pending.isPrerelease -or
                [string]$pending.targetCommitish -ne $serverReceipt.Commit
            ) {
                throw "服务器部署回执与待激活 Release 不一致：$serverTag"
            }
            foreach ($requiredAsset in @(
                'ERP-Automation-Client.zip',
                'latest.json',
                'SHA256SUMS.txt'
            )) {
                if ($requiredAsset -notin $pendingAssets) {
                    throw "待激活 Release 缺少资产：$requiredAsset"
                }
            }
            & gh release edit $serverTag --latest
            if ($LASTEXITCODE -ne 0) {
                throw "无法恢复激活已经部署的客户端版本：$serverTag"
            }
            $activatedOutput = & gh release view --json tagName,url
            if ($LASTEXITCODE -ne 0) {
                throw "无法复核恢复激活结果：$serverTag"
            }
            $activated = ($activatedOutput -join "`n") | ConvertFrom-Json
            if ([string]$activated.tagName -ne $serverTag) {
                throw "恢复激活后 GitHub 最新版本仍不是 $serverTag。"
            }
            $channelLatestVersion = $serverReceipt.Version
            Write-Host (
                "已恢复上一次部署的客户端最新版激活：" +
                "$($serverReceipt.Commit) / $($serverReceipt.Version)"
            ) -ForegroundColor Green
        } elseif ($comparison -lt 0) {
            Write-Verbose (
                "服务器版本 $($serverReceipt.Version) 早于更新通道 " +
                "$latestVersion；不会回退 GitHub 最新版本。"
            )
        }
        if (
            $comparison -ge 0 -and
            (
                $serverReceipt.RolloutPending -eq $true -or
                $serverReceipt.RolloutDrainActive -eq $true
            )
        ) {
            [void](Complete-ServerRollout `
                $serverReceipt.Commit `
                $serverReceipt.Version)
            Write-Host (
                "已恢复服务器端待完成的客户端滚动窗口：" +
                "$($serverReceipt.Commit) / $($serverReceipt.Version)"
            ) -ForegroundColor Green
        }
    } else {
        # The first rollout of report-deployed reaches a server whose old
        # forced-command entry does not know this read-only operation yet.
        Write-Verbose (
            "服务器尚未提供部署回执查询，将继续使用完整部署校验：" +
            $reported.Text
        )
    }

    $branch = (& git branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') {
        throw '正式部署只能从 main 分支执行。'
    }
    $trackedChanges = (& git status --porcelain --untracked-files=no) -join "`n"
    if ($LASTEXITCODE -ne 0 -or $trackedChanges.Trim()) {
        throw 'main 存在尚未提交的已跟踪改动，拒绝部署。'
    }

    & git fetch origin main
    if ($LASTEXITCODE -ne 0) {
        throw '无法读取远端 main。'
    }
    $localCommit = (& git rev-parse HEAD).Trim()
    $remoteCommit = (& git rev-parse origin/main).Trim()
    if (-not $localCommit -or $localCommit -ne $remoteCommit) {
        throw '本机 main 与 origin/main 不一致，拒绝部署。'
    }

    $version = (Get-Content -LiteralPath (Join-Path $workspace 'CLIENT_VERSION') -Raw).Trim()
    if ($version -notmatch '^\d{4}\.\d{2}\.\d{2}\.\d+$') {
        throw "CLIENT_VERSION 无效：$version"
    }
    if (-not $channelLatestVersion) {
        $latestOutput = & gh release view --json tagName,url
        if ($LASTEXITCODE -ne 0) {
            throw '无法读取 GitHub 当前客户端最新版。'
        }
        $channelLatest = ($latestOutput -join "`n") | ConvertFrom-Json
        $channelLatestTag = [string]$channelLatest.tagName
        if ($channelLatestTag -notmatch '^v(\d{4}\.\d{2}\.\d{2}\.\d+)$') {
            throw "GitHub 当前最新版标签无效：$channelLatestTag"
        }
        $channelLatestVersion = $Matches[1]
    }
    if ((Compare-ReleaseVersion $version $channelLatestVersion) -lt 0) {
        throw (
            "拒绝把服务器或客户端更新通道回退到旧版本：" +
            "$version < $channelLatestVersion"
        )
    }
    $tag = "v$version"
    $releaseOutput = & gh release view $tag `
        --json tagName,isDraft,isPrerelease,targetCommitish,assets,url
    if ($LASTEXITCODE -ne 0) {
        throw "未找到已经发布的客户端版本：$tag"
    }
    $release = ($releaseOutput -join "`n") | ConvertFrom-Json
    if ($release.isDraft -or $release.isPrerelease) {
        throw "客户端版本尚未正式发布：$tag"
    }
    if ([string]$release.targetCommitish -ne $localCommit) {
        throw (
            "正式客户端和待部署服务器必须来自同一 main 提交。" +
            "客户端：$($release.targetCommitish)，服务器：$localCommit。"
        )
    }
    $assetNames = @($release.assets | ForEach-Object { $_.name })
    foreach ($requiredAsset in @(
        'ERP-Automation-Client.zip',
        'latest.json',
        'SHA256SUMS.txt'
    )) {
        if ($requiredAsset -notin $assetNames) {
            throw "正式 Release 缺少资产：$requiredAsset"
        }
    }

    $deploymentAuthorization = "$localCommit $version"
    $deployment = Invoke-ControlledDeploymentSsh `
        'deploy-main' `
        $deploymentAuthorization
    if ($deployment.ExitCode -ne 0) {
        throw (
            "服务器部署失败，退出码：$($deployment.ExitCode)" +
            $(if ($deployment.Text) { "`n$($deployment.Text)" } else { '' })
        )
    }
    if ($deployment.Text) {
        Write-Host $deployment.Text
    }
    $deployedReceipt = Get-VerifiedDeploymentReceipt $deployment.Output
    if (
        $deployedReceipt.Commit -ne $localCommit -or
        $deployedReceipt.Version -ne $version -or
        $null -eq $deployedReceipt.RolloutPending -or
        $null -eq $deployedReceipt.RolloutDrainActive
    ) {
        throw (
            '服务器部署回执与已审核的 main 提交或客户端版本不一致，' +
            '拒绝激活客户端更新。'
        )
    }

    # The forced server command performs the deployment and health check.
    # Only after it succeeds may new clients discover this release through
    # releases/latest/download/latest.json.
    & gh release edit $tag --latest
    if ($LASTEXITCODE -ne 0) {
        throw (
            "服务器已部署，但无法激活客户端最新版：$tag。" +
            "服务器仍保持旧客户端兼容并暂停新任务，请重新运行本脚本完成切换。"
        )
    }
    $latestOutput = & gh release view --json tagName,url
    if ($LASTEXITCODE -ne 0) {
        throw "无法复核 GitHub 最新版本：$tag"
    }
    $latestRelease = ($latestOutput -join "`n") | ConvertFrom-Json
    if ([string]$latestRelease.tagName -ne $tag) {
        throw (
            "服务器已部署，但 GitHub 最新版本仍为 " +
            "$($latestRelease.tagName)，预期 $tag。"
        )
    }
    [void](Complete-ServerRollout $localCommit $version)
    Write-Host (
        "服务器部署、客户端最新版激活和滚动窗口启动均已通过：" +
        "$localCommit / $version"
    ) -ForegroundColor Green
} finally {
    Pop-Location
}
