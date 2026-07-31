[CmdletBinding()]
param(
    [switch]$ConfirmProductionDeployment,
    [string]$ServerHost = '8.133.172.100',
    [string]$ServerUser = 'admin',
    [string]$DeployKeyPath = 'Z:\同事个人\颜奕超\ERP自动化部署专用\codex-production-deploy-ed25519',
    [string]$KnownHostsPath = 'Z:\同事个人\颜奕超\ERP自动化部署专用\known_hosts'
)

$ErrorActionPreference = 'Stop'

if (-not $ConfirmProductionDeployment) {
    throw '正式部署必须显式传入 -ConfirmProductionDeployment。'
}

$workspace = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
foreach ($requiredFile in @($DeployKeyPath, $KnownHostsPath)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "部署授权文件不存在：$requiredFile"
    }
}
$credentialRoot = Join-Path $env:LOCALAPPDATA 'LingxingERP'
[IO.Directory]::CreateDirectory($credentialRoot) | Out-Null
$resolvedCredentialRoot = (Resolve-Path -LiteralPath $credentialRoot).Path
$temporaryKey = [IO.Path]::GetFullPath(
    (Join-Path $credentialRoot ('.codex-deploy-' + [Guid]::NewGuid().ToString('N')))
)
if (-not $temporaryKey.StartsWith(
    $resolvedCredentialRoot + [IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw '部署密钥暂存路径越界。'
}

Push-Location $workspace
try {
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

    & ssh-keygen -F $ServerHost -f $KnownHostsPath *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "known_hosts 中没有固定服务器指纹：$ServerHost"
    }

    Copy-Item -LiteralPath $DeployKeyPath -Destination $temporaryKey
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $systemUser = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $keyAcl = [Security.AccessControl.FileSecurity]::new()
    $keyAcl.SetOwner($currentUser)
    $keyAcl.SetAccessRuleProtection($true, $false)
    $keyAcl.AddAccessRule(
        [Security.AccessControl.FileSystemAccessRule]::new(
            $currentUser,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
    )
    $keyAcl.AddAccessRule(
        [Security.AccessControl.FileSystemAccessRule]::new(
            $systemUser,
            [Security.AccessControl.FileSystemRights]::Read,
            [Security.AccessControl.AccessControlType]::Allow
        )
    )
    Set-Acl -LiteralPath $temporaryKey -AclObject $keyAcl

    $sshArguments = @(
        '-T',
        '-i', $temporaryKey,
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
        'deploy-main'
    )
    & ssh @sshArguments
    if ($LASTEXITCODE -ne 0) {
        throw "服务器部署失败，退出码：$LASTEXITCODE"
    }

    # The forced server command performs the deployment and health check.
    # Only after it succeeds may new clients discover this release through
    # releases/latest/download/latest.json.
    & gh release edit $tag --latest
    if ($LASTEXITCODE -ne 0) {
        throw (
            "服务器已部署，但无法激活客户端最新版：$tag。" +
            "请在兼容窗口结束前重新运行本脚本。"
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
    Write-Host (
        "服务器部署、健康检查和客户端最新版激活均已通过：" +
        "$localCommit / $version"
    ) -ForegroundColor Green
} finally {
    Pop-Location
    if (
        $temporaryKey.StartsWith(
            $resolvedCredentialRoot + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        (Test-Path -LiteralPath $temporaryKey -PathType Leaf)
    ) {
        Remove-Item -LiteralPath $temporaryKey -Force
    }
}
