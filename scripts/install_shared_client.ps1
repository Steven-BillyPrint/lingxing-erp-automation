[CmdletBinding()]
param(
    [string]$PackageRoot = (Split-Path -Parent $PSScriptRoot),
    # Kept for compatibility with older updaters.  The EXE now derives its
    # instance name itself, so this value is deliberately not put in the link.
    [string]$InstanceName = $env:USERNAME,
    [string]$DesktopDirectory = '',
    [switch]$SkipLegacyPortablePromotion,
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
    if (
        -not (Test-Path -LiteralPath $candidate -PathType Container) -or
        (Test-Path -LiteralPath (Join-Path $candidate '.git'))
    ) {
        return
    }
    $targetRoot = (Resolve-Path -LiteralPath $candidate).Path
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

$sourceRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
$versionFile = Join-Path $sourceRoot 'VERSION.txt'
$sourceApplication = Join-Path $sourceRoot 'dist\ERP自动化\ERP自动化.exe'
$sourceLauncher = Join-Path $sourceRoot 'scripts\start_shared_desktop.ps1'
$sourceUpdater = Join-Path $sourceRoot 'scripts\update_shared_client.ps1'
$sourcePromoter = Join-Path $sourceRoot 'scripts\promote_portable_client.ps1'
if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) {
    throw '安装包缺少 VERSION.txt。'
}
foreach ($required in @(
    $sourceApplication,
    $sourceLauncher,
    $sourceUpdater,
    $sourcePromoter
)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "安装包不完整：$required"
    }
}

$version = (Get-Content -LiteralPath $versionFile -Raw).Trim()
if ($version -notmatch '^[0-9A-Za-z._-]{1,64}$') {
    throw 'VERSION.txt 中的版本号无效。'
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
if (-not (Test-Path -LiteralPath $candidateProgramRoot)) {
    $stagingRoot = Join-Path $programBase (
        ".$version.install-" + [Guid]::NewGuid().ToString('N')
    )
    $stagingRoot = [IO.Path]::GetFullPath($stagingRoot)
    if (-not $stagingRoot.StartsWith(
        $resolvedProgramBase + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw '客户端安装暂存目录越界。'
    }
    try {
        [IO.Directory]::CreateDirectory($stagingRoot) | Out-Null
        Copy-Item -LiteralPath (Join-Path $sourceRoot 'dist') `
            -Destination $stagingRoot -Recurse
        Copy-Item -LiteralPath (Join-Path $sourceRoot 'scripts') `
            -Destination $stagingRoot -Recurse
        Copy-Item -LiteralPath $versionFile -Destination $stagingRoot
        Move-Item -LiteralPath $stagingRoot -Destination $candidateProgramRoot
    } finally {
        if (Test-Path -LiteralPath $stagingRoot) {
            Remove-Item -LiteralPath $stagingRoot -Recurse -Force
        }
    }
} else {
    $installedVersionFile = Join-Path $candidateProgramRoot 'VERSION.txt'
    if (
        -not (Test-Path -LiteralPath $installedVersionFile -PathType Leaf) -or
        (Get-Content -LiteralPath $installedVersionFile -Raw).Trim() -ne $version
    ) {
        throw '现有客户端版本目录内容不一致，拒绝覆盖。'
    }
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'dist') `
        -Destination $candidateProgramRoot -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'scripts') `
        -Destination $candidateProgramRoot -Recurse -Force
}
$programRoot = $candidateProgramRoot

$credentialRoot = Join-Path $env:LOCALAPPDATA 'LingxingERP'
[IO.Directory]::CreateDirectory($credentialRoot) | Out-Null

$installedApplication = Join-Path $programRoot 'dist\ERP自动化\ERP自动化.exe'
$desktop = if ($DesktopDirectory) {
    [IO.Path]::GetFullPath($DesktopDirectory)
} else {
    [Environment]::GetFolderPath('Desktop')
}
[IO.Directory]::CreateDirectory($desktop) | Out-Null
$shortcutPath = Join-Path $desktop 'ERP自动化（阿里云共享）.lnk'
$temporaryShortcut = Join-Path $desktop (
    '.erp-automation-' + [Guid]::NewGuid().ToString('N') + '.lnk'
)
New-DirectApplicationShortcut `
    -ShortcutPath $temporaryShortcut `
    -TargetPath $installedApplication `
    -Arguments '' `
    -WorkingDirectory $programRoot
Move-Item -LiteralPath $temporaryShortcut -Destination $shortcutPath -Force
Start-LegacyPortablePromotion $programRoot $version

if (-not $Silent) {
    Write-Host "安装完成：$programRoot" -ForegroundColor Green
    Write-Host "桌面快捷方式：$shortcutPath"
    Write-Host '首次启动时必须导入加密授权文件或手工填写授权材料。' -ForegroundColor Yellow
}
