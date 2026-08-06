[CmdletBinding()]
param(
    [string]$PackageRoot = (Split-Path -Parent $PSScriptRoot),
    # Kept for compatibility with older updaters.  The EXE now derives its
    # instance name itself, so this value is deliberately not put in the link.
    [string]$InstanceName = $env:USERNAME,
    [string]$DesktopDirectory = '',
    [switch]$SkipLegacyPortablePromotion,
    [switch]$SkipShortcut,
    [switch]$ActivateOnly,
    [switch]$SkipApplicationSmokeTest,
    [ValidateRange(1, 300)]
    [int]$ApplicationSmokeTestTimeoutSeconds = 60,
    [switch]$Silent
)

$ErrorActionPreference = 'Stop'

function New-DirectApplicationShortcut {
    param(
        [Parameter(Mandatory = $true)][string]$ShortcutPath,
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    if (-not ('LingxingErpShortcutWriter' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

[ComImport]
[Guid("000214F9-0000-0000-C000-000000000046")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IShellLinkW
{
    void GetPath(
        [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file,
        int maxPath,
        IntPtr findData,
        uint flags);
    void GetIDList(out IntPtr itemIdList);
    void SetIDList(IntPtr itemIdList);
    void GetDescription(
        [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder description,
        int maxName);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string description);
    void GetWorkingDirectory(
        [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder directory,
        int maxPath);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string directory);
    void GetArguments(
        [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder arguments,
        int maxPath);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string arguments);
    void GetHotkey(out short hotkey);
    void SetHotkey(short hotkey);
    void GetShowCmd(out int showCommand);
    void SetShowCmd(int showCommand);
    void GetIconLocation(
        [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder iconPath,
        int iconPathLength,
        out int iconIndex);
    void SetIconLocation(
        [MarshalAs(UnmanagedType.LPWStr)] string iconPath,
        int iconIndex);
    void SetRelativePath(
        [MarshalAs(UnmanagedType.LPWStr)] string path,
        uint reserved);
    void Resolve(IntPtr window, uint flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
}

[ComImport]
[Guid("0000010B-0000-0000-C000-000000000046")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPersistFile
{
    void GetClassID(out Guid classId);
    [PreserveSig] int IsDirty();
    void Load(
        [MarshalAs(UnmanagedType.LPWStr)] string fileName,
        uint mode);
    void Save(
        [MarshalAs(UnmanagedType.LPWStr)] string fileName,
        [MarshalAs(UnmanagedType.Bool)] bool remember);
    void SaveCompleted(
        [MarshalAs(UnmanagedType.LPWStr)] string fileName);
    void GetCurFile(
        [MarshalAs(UnmanagedType.LPWStr)] out string fileName);
}

[ComImport]
[Guid("00021401-0000-0000-C000-000000000046")]
class ShellLink
{
}

public static class LingxingErpShortcutWriter
{
    public static void Create(
        string shortcutPath,
        string targetPath,
        string arguments,
        string workingDirectory)
    {
        object shellLinkObject = new ShellLink();
        try
        {
            IShellLinkW shellLink = (IShellLinkW)shellLinkObject;
            shellLink.SetPath(targetPath);
            shellLink.SetArguments(arguments);
            shellLink.SetWorkingDirectory(workingDirectory);
            shellLink.SetIconLocation(targetPath, 0);
            shellLink.SetDescription("ERP 自动化（阿里云共享）");
            ((IPersistFile)shellLinkObject).Save(shortcutPath, true);
        }
        finally
        {
            Marshal.FinalReleaseComObject(shellLinkObject);
        }
    }
}
'@
    }

    [LingxingErpShortcutWriter]::Create(
        $ShortcutPath,
        $TargetPath,
        $Arguments,
        $WorkingDirectory
    )
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

function Quote-ProcessArgument([string]$Value) {
    if ($Value.Contains('"')) {
        throw '安装辅助程序参数不能包含双引号。'
    }
    return '"' + $Value + '"'
}

function Start-LegacyPortablePromotion(
    [string]$InstalledRoot,
    [string]$Version
) {
    if ($SkipLegacyPortablePromotion) {
        return
    }
    $candidate = (Get-Location).Path
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        return
    }
    # Older EXEs did not pass CurrentPackageRoot to the updater. Their only
    # recoverable origin is the installer's working directory. A source
    # checkout is never a portable client and must not be changed by an
    # installer or by release tests running from the repository root.
    $targetRoot = (Resolve-Path -LiteralPath $candidate).Path
    if (Test-Path -LiteralPath (Join-Path $targetRoot '.git')) {
        return
    }
    $source = [IO.Path]::GetFullPath($sourceRoot)
    $installed = [IO.Path]::GetFullPath($InstalledRoot)
    if (
        $targetRoot.Equals($source, [StringComparison]::OrdinalIgnoreCase) -or
        $targetRoot.Equals($installed, [StringComparison]::OrdinalIgnoreCase)
    ) {
        return
    }
    $targetApplication = Join-Path (
        $targetRoot
    ) 'dist\ERP自动化\ERP自动化.exe'
    $targetUpdater = Join-Path $targetRoot 'scripts\update_shared_client.ps1'
    foreach ($required in @($targetApplication, $targetUpdater)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            return
        }
    }
    $helper = Join-Path $installed 'scripts\promote_portable_client.ps1'
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
        return
    }
    $argumentList = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-WindowStyle', 'Hidden',
        '-File', (Quote-ProcessArgument $helper),
        '-SourcePackageRoot', (Quote-ProcessArgument $installed),
        '-TargetPackageRoot', (Quote-ProcessArgument $targetRoot),
        '-ExpectedCurrentVersion', 'legacy',
        '-ExpectedVersion', (Quote-ProcessArgument $Version),
        '-ExpectedTargetSha256', (
            Quote-ProcessArgument (Get-Sha256Hex -LiteralPath $targetApplication)
        ),
        '-WaitProcessId', [string]$PID
    ) -join ' '
    Start-Process `
        -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -ArgumentList $argumentList `
        -WindowStyle Hidden | Out-Null
}

function Invoke-PackageApplicationSmokeTest(
    [string]$Application,
    [string]$WorkingDirectory
) {
    $smokeRoot = Join-Path (
        [IO.Path]::GetTempPath()
    ) ('LingxingERP-install-smoke-' + [Guid]::NewGuid().ToString('N'))
    $previousHome = $env:ERP_AUTOMATION_HOME
    try {
        [IO.Directory]::CreateDirectory($smokeRoot) | Out-Null
        $env:ERP_AUTOMATION_HOME = $smokeRoot
        $process = Start-Process `
            -FilePath $Application `
            -ArgumentList '--release-smoke-test' `
            -WorkingDirectory $WorkingDirectory `
            -WindowStyle Hidden `
            -PassThru
        if (-not $process.WaitForExit($ApplicationSmokeTestTimeoutSeconds * 1000)) {
            try {
                $process.Kill()
                [void]$process.WaitForExit(5000)
            } catch {
                # Preserve the timeout as the primary installation failure.
            }
            throw (
                '客户端安装前启动自检超时，未创建程序入口。' +
                "等待上限：$ApplicationSmokeTestTimeoutSeconds 秒。"
            )
        }
        if ($process.ExitCode -ne 0) {
            throw (
                '客户端安装前启动自检失败，未创建程序入口。' +
                "退出代码：$($process.ExitCode)"
            )
        }
    } finally {
        $env:ERP_AUTOMATION_HOME = $previousHome
        if (Test-Path -LiteralPath $smokeRoot -PathType Container) {
            Remove-Item -LiteralPath $smokeRoot -Recurse -Force
        }
    }
}

$sourceRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
$versionFile = Join-Path $sourceRoot 'VERSION.txt'
$sourceApplication = Join-Path $sourceRoot 'dist\ERP自动化\ERP自动化.exe'
$sourceLauncher = Join-Path $sourceRoot 'scripts\start_shared_desktop.ps1'
$sourceUpdater = Join-Path $sourceRoot 'scripts\update_shared_client.ps1'
$sourceChannelSetter = Join-Path $sourceRoot 'scripts\set_client_update_channel.ps1'
$sourcePromoter = Join-Path $sourceRoot 'scripts\promote_portable_client.ps1'
$sourceRepairHelper = Join-Path $sourceRoot 'scripts\complete_client_repair.ps1'
if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) {
    throw '安装包缺少 VERSION.txt。'
}
foreach ($required in @(
    $sourceApplication,
    $sourceLauncher,
    $sourceUpdater,
    $sourceChannelSetter,
    $sourcePromoter,
    $sourceRepairHelper
)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "安装包不完整：$required"
    }
}
$version = (Get-Content -LiteralPath $versionFile -Raw).Trim()
if ($version -notmatch '^[0-9A-Za-z._-]{1,64}$') {
    throw 'VERSION.txt 中的版本号无效。'
}
if ($SkipApplicationSmokeTest -and -not $SkipShortcut) {
    throw '只有不激活快捷方式的更新暂存阶段才允许跳过重复启动自检。'
}
if (-not $ActivateOnly -and -not $SkipApplicationSmokeTest) {
    Invoke-PackageApplicationSmokeTest $sourceApplication $sourceRoot
}
$programBase = Join-Path $env:LOCALAPPDATA 'Programs\LingxingERP'
[IO.Directory]::CreateDirectory($programBase) | Out-Null
$programRoot = Join-Path $programBase $version
$resolvedProgramBase = (Resolve-Path -LiteralPath $programBase).Path
$candidateProgramRoot = [IO.Path]::GetFullPath($programRoot)
if (-not $candidateProgramRoot.StartsWith(
    $resolvedProgramBase + [IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw '客户端安装目录越界。'
}
if (-not $ActivateOnly) {
    $stagingRoot = Join-Path $programBase (
        ".$version.install-" + [Guid]::NewGuid().ToString('N')
    )
    $backupRoot = Join-Path $programBase (
        ".$version.replace-" + [Guid]::NewGuid().ToString('N')
    )
    $stagingRoot = [IO.Path]::GetFullPath($stagingRoot)
    $backupRoot = [IO.Path]::GetFullPath($backupRoot)
    foreach ($candidate in @($stagingRoot, $backupRoot)) {
        if (-not $candidate.StartsWith(
            $resolvedProgramBase + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw '客户端安装事务目录越界。'
        }
    }
    $installMutex = [Threading.Mutex]::new(
        $false,
        ('Local\LingxingERPClientInstall-' + $version)
    )
    $installMutexAcquired = $false
    $oldInstallMoved = $false
    $newInstallCommitted = $false
    try {
        $installMutexAcquired = $installMutex.WaitOne(
            [TimeSpan]::FromMinutes(3)
        )
        if (-not $installMutexAcquired) {
            throw '另一项客户端安装长时间未完成，请稍后重试。'
        }
        # Recover a prior hard interruption before starting a new transaction.
        # If the live directory disappeared after it was renamed, restore the
        # newest backup; otherwise every hidden transaction is disposable.
        $priorBackups = @(
            Get-ChildItem -LiteralPath $programBase -Directory -Force |
                Where-Object { $_.Name -like ".$version.replace-*" } |
                Sort-Object LastWriteTimeUtc -Descending
        )
        if (
            -not (Test-Path -LiteralPath $candidateProgramRoot) -and
            $priorBackups.Count -gt 0
        ) {
            Move-Item -LiteralPath $priorBackups[0].FullName `
                -Destination $candidateProgramRoot
            $priorBackups = @($priorBackups | Select-Object -Skip 1)
        }
        foreach ($priorBackup in $priorBackups) {
            if (Test-Path -LiteralPath $priorBackup.FullName -PathType Container) {
                Remove-Item -LiteralPath $priorBackup.FullName -Recurse -Force
            }
        }
        foreach ($priorStage in @(
            Get-ChildItem -LiteralPath $programBase -Directory -Force |
                Where-Object { $_.Name -like ".$version.install-*" }
        )) {
            if ($priorStage.FullName -ne $stagingRoot) {
                Remove-Item -LiteralPath $priorStage.FullName -Recurse -Force
            }
        }
        [IO.Directory]::CreateDirectory($stagingRoot) | Out-Null
        Copy-Item -LiteralPath (Join-Path $sourceRoot 'dist') `
            -Destination $stagingRoot -Recurse
        Copy-Item -LiteralPath (Join-Path $sourceRoot 'scripts') `
            -Destination $stagingRoot -Recurse
        Copy-Item -LiteralPath $versionFile -Destination $stagingRoot
        foreach ($requiredRelative in @(
            'VERSION.txt',
            'dist\ERP自动化\ERP自动化.exe',
            'scripts\start_shared_desktop.ps1',
            'scripts\install_shared_client.ps1',
            'scripts\update_shared_client.ps1',
            'scripts\set_client_update_channel.ps1',
            'scripts\promote_portable_client.ps1',
            'scripts\complete_client_repair.ps1'
        )) {
            $stagedRequired = Join-Path $stagingRoot $requiredRelative
            if (
                -not (Test-Path -LiteralPath $stagedRequired -PathType Leaf) -or
                (Get-Item -LiteralPath $stagedRequired).Length -le 0
            ) {
                throw "客户端安装暂存内容不完整：$requiredRelative"
            }
        }
        if (Test-Path -LiteralPath $candidateProgramRoot) {
            Move-Item -LiteralPath $candidateProgramRoot `
                -Destination $backupRoot
            $oldInstallMoved = $true
        }
        Move-Item -LiteralPath $stagingRoot `
            -Destination $candidateProgramRoot
        $newInstallCommitted = $true
    } catch {
        if (
            $newInstallCommitted -and
            (Test-Path -LiteralPath $candidateProgramRoot -PathType Container)
        ) {
            Remove-Item -LiteralPath $candidateProgramRoot -Recurse -Force
        }
        if (
            $oldInstallMoved -and
            (Test-Path -LiteralPath $backupRoot -PathType Container) -and
            -not (Test-Path -LiteralPath $candidateProgramRoot)
        ) {
            Move-Item -LiteralPath $backupRoot `
                -Destination $candidateProgramRoot
            $oldInstallMoved = $false
        }
        throw
    } finally {
        if ($installMutexAcquired) {
            $installMutex.ReleaseMutex()
        }
        $installMutex.Dispose()
        if (Test-Path -LiteralPath $stagingRoot -PathType Container) {
            Remove-Item -LiteralPath $stagingRoot -Recurse -Force
        }
        if (
            $newInstallCommitted -and
            (Test-Path -LiteralPath $backupRoot -PathType Container)
        ) {
            try {
                Remove-Item -LiteralPath $backupRoot -Recurse -Force
            } catch {
                # A same-version repair can leave the previous EXE mapped by
                # the process that launched this installer. The verified new
                # directory is already committed, so deferred cleanup is safe.
            }
        }
    }
} elseif (-not (Test-Path -LiteralPath $candidateProgramRoot -PathType Container)) {
    throw '待激活的客户端版本尚未安装。'
}
$programRoot = $candidateProgramRoot

$credentialRoot = Join-Path $env:LOCALAPPDATA 'LingxingERP'
[IO.Directory]::CreateDirectory($credentialRoot) | Out-Null

$installedApplication = Join-Path $programRoot 'dist\ERP自动化\ERP自动化.exe'
if (-not (Test-Path -LiteralPath $installedApplication -PathType Leaf)) {
    throw '待激活的客户端 EXE 不存在。'
}
$desktop = if ($DesktopDirectory) {
    [IO.Path]::GetFullPath($DesktopDirectory)
} else {
    [Environment]::GetFolderPath('Desktop')
}
[IO.Directory]::CreateDirectory($desktop) | Out-Null
$shortcutPath = Join-Path $desktop 'ERP自动化（阿里云共享）.lnk'
if (-not $SkipShortcut) {
    $temporaryShortcut = Join-Path $desktop (
        '.erp-automation-' + [Guid]::NewGuid().ToString('N') + '.lnk'
    )
    $backupShortcut = Join-Path $desktop (
        '.erp-automation-backup-' + [Guid]::NewGuid().ToString('N') + '.lnk'
    )
    try {
        New-DirectApplicationShortcut `
            -ShortcutPath $temporaryShortcut `
            -TargetPath $installedApplication `
            -Arguments '' `
            -WorkingDirectory $programRoot
        if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
            [IO.File]::Replace(
                $temporaryShortcut,
                $shortcutPath,
                $backupShortcut,
                $true
            )
        } else {
            Move-Item -LiteralPath $temporaryShortcut `
                -Destination $shortcutPath
        }
    } finally {
        if (Test-Path -LiteralPath $temporaryShortcut -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryShortcut -Force
        }
        if (Test-Path -LiteralPath $backupShortcut -PathType Leaf) {
            try {
                Remove-Item -LiteralPath $backupShortcut -Force
            } catch {
                # A hidden backup does not invalidate the activated shortcut.
            }
        }
    }
}
if (-not $ActivateOnly) {
    Start-LegacyPortablePromotion $programRoot $version
}

if (-not $Silent) {
    Write-Host "安装完成：$programRoot" -ForegroundColor Green
    Write-Host "桌面快捷方式：$shortcutPath"
    Write-Host '首次启动时必须导入加密授权文件或手工填写授权材料。' -ForegroundColor Yellow
}
