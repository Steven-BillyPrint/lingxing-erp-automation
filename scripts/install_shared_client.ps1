[CmdletBinding()]
param(
    [string]$PackageRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$InstanceName = $env:USERNAME
)

$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
$versionFile = Join-Path $sourceRoot 'VERSION.txt'
$sourceApplication = Join-Path $sourceRoot 'dist\ERP自动化\ERP自动化.exe'
$sourceLauncher = Join-Path $sourceRoot 'scripts\start_shared_desktop.ps1'
if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) {
    throw '安装包缺少 VERSION.txt。'
}
foreach ($required in @($sourceApplication, $sourceLauncher)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "安装包不完整：$required"
    }
}

$version = (Get-Content -LiteralPath $versionFile -Raw).Trim()
if ($version -notmatch '^[0-9A-Za-z._-]{1,64}$') {
    throw 'VERSION.txt 中的版本号无效。'
}
$programRoot = Join-Path $env:LOCALAPPDATA "Programs\LingxingERP\$version"
[IO.Directory]::CreateDirectory($programRoot) | Out-Null
Copy-Item -LiteralPath (Join-Path $sourceRoot 'dist') -Destination $programRoot -Recurse -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot 'scripts') -Destination $programRoot -Recurse -Force
Copy-Item -LiteralPath $versionFile -Destination $programRoot -Force

$credentialRoot = Join-Path $env:LOCALAPPDATA 'LingxingERP'
[IO.Directory]::CreateDirectory($credentialRoot) | Out-Null
$sshKey = Join-Path $credentialRoot 'server-tunnel-ed25519'
$knownHosts = Join-Path $credentialRoot 'known_hosts'
$tokenFile = Join-Path $credentialRoot 'coordination-token'
if (-not (Test-Path -LiteralPath $sshKey -PathType Leaf)) {
    & ssh-keygen.exe -q -t ed25519 -N '' -f $sshKey
    if ($LASTEXITCODE -ne 0) {
        throw '生成当前电脑的 SSH 密钥失败。'
    }
}
if (-not (Test-Path -LiteralPath ($sshKey + '.pub') -PathType Leaf)) {
    $derivedPublicKey = (& ssh-keygen.exe -y -f $sshKey) -join ''
    if ($LASTEXITCODE -ne 0 -or -not $derivedPublicKey) {
        throw '读取当前电脑的 SSH 公钥失败。'
    }
    [IO.File]::WriteAllText(
        $sshKey + '.pub',
        $derivedPublicKey + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
}

$publicKey = Get-Content -LiteralPath ($sshKey + '.pub') -Raw
$missing = @(
    [pscustomobject]@{
        Path = $knownHosts
        Label = '服务器固定主机指纹文件 known_hosts'
    },
    [pscustomobject]@{
        Path = $tokenFile
        Label = '协调服务 Token 文件 coordination-token'
    }
) | Where-Object { -not (Test-Path -LiteralPath $_.Path -PathType Leaf) }
if ($missing.Count -gt 0) {
    Write-Host ''
    Write-Host '程序文件已安装，但这台电脑尚未获得服务器访问凭据。' -ForegroundColor Yellow
    Write-Host '请把下面的公钥交给服务器管理员，按“仅允许端口转发”方式授权：'
    Write-Host $publicKey -ForegroundColor Cyan
    Write-Host '随后由管理员安全提供以下文件：'
    foreach ($item in $missing) {
        Write-Host ("- " + $item.Label + " -> " + $item.Path)
    }
    Write-Host '文件到位后重新运行本安装脚本即可创建快捷方式。'
    exit 2
}

$installedLauncher = Join-Path $programRoot 'scripts\start_shared_desktop.ps1'
$installedApplication = Join-Path $programRoot 'dist\ERP自动化\ERP自动化.exe'
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'ERP自动化（阿里云共享）.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$shortcut.Arguments = (
    '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' +
    $installedLauncher +
    '" -InstanceName "' +
    $InstanceName.Replace('"', '') +
    '" -ApplicationPath "' +
    $installedApplication +
    '"'
)
$shortcut.WorkingDirectory = $programRoot
$shortcut.IconLocation = "$installedApplication,0"
$shortcut.Save()

Write-Host "安装完成：$programRoot" -ForegroundColor Green
Write-Host "桌面快捷方式：$shortcutPath"
