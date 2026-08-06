[CmdletBinding()]
param(
    [switch]$ConfirmLocalTestRun,
    [string]$PythonPath = '',
    [string[]]$ApplicationArguments = @(),
    [switch]$ValidateOnly,
    [switch]$OutputJson
)

$ErrorActionPreference = 'Stop'
if ($OutputJson) {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
}

if (-not $ConfirmLocalTestRun) {
    throw 'Local test startup requires -ConfirmLocalTestRun.'
}
if (-not $env:LOCALAPPDATA) {
    throw 'Windows LOCALAPPDATA is unavailable.'
}

$workspace = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$entryPoint = Join-Path $workspace 'desktop_main.py'
$testRoot = [IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA 'LingxingERP-LocalTest')
)
$formalRoot = [IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA 'LingxingERP')
)
$formalProgramRoot = [IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA 'Programs\LingxingERP')
)
$sshKeyPath = Join-Path $formalRoot 'server-tunnel-ed25519'
$knownHostsPath = Join-Path $formalRoot 'known_hosts'
$tokenFile = Join-Path $formalRoot 'coordination-token'
if ($testRoot.Equals($formalRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The local-test root must differ from the production root.'
}
if (-not (Test-Path -LiteralPath $entryPoint -PathType Leaf)) {
    throw "The desktop source entry point is missing: $entryPoint"
}

if (-not $PythonPath) {
    $venvPython = Join-Path $workspace '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $PythonPath = $venvPython
    } else {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($null -ne $pythonCommand) {
            $PythonPath = [string]$pythonCommand.Source
        }
    }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'Python is unavailable; prepare .venv or pass -PythonPath.'
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path

$validation = [ordered]@{
    schema_version = 1
    mode = 'local_test'
    source_workspace = $workspace
    state_root = $testRoot
    formal_state_root = $formalRoot
    application_path = $entryPoint
    python_path = $PythonPath
    application_arguments = @($ApplicationArguments)
    packaged_client = $false
    production_update_channel = $false
    local_state_isolated = $true
    server_connection = 'formal_shared_service'
    uses_formal_access_profile = $true
    production_business_data = $true
    writes_affect_production = $true
    required_access_files_present = [ordered]@{
        ssh_key = (Test-Path -LiteralPath $sshKeyPath -PathType Leaf)
        known_hosts = (Test-Path -LiteralPath $knownHostsPath -PathType Leaf)
        coordination_token = (Test-Path -LiteralPath $tokenFile -PathType Leaf)
    }
}
if ($ValidateOnly) {
    if ($OutputJson) {
        $validation | ConvertTo-Json -Compress
    } else {
        $validation
    }
    return
}

$formalProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            (
                $_.ExecutablePath -and
                $_.ExecutablePath.StartsWith(
                    $formalProgramRoot.TrimEnd('\') + '\',
                    [StringComparison]::OrdinalIgnoreCase
                )
            )
        }
)
if ($formalProcesses.Count -gt 0) {
    throw (
        'The production client is running; close it before local testing. PID: ' +
        (($formalProcesses | ForEach-Object { $_.ProcessId }) -join ', ')
    )
}
$localTestProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -match '^python(?:w)?\.exe$' -and
            [string]$_.CommandLine -like '*desktop_main.py*'
        }
)
if ($localTestProcesses.Count -gt 0) {
    throw 'A local-test instance is already running.'
}

foreach ($requiredAccessFile in @($sshKeyPath, $knownHostsPath, $tokenFile)) {
    if (-not (Test-Path -LiteralPath $requiredAccessFile -PathType Leaf)) {
        throw "The formal shared-server access profile is incomplete: $requiredAccessFile"
    }
}

[IO.Directory]::CreateDirectory($testRoot) | Out-Null

$managedVariables = @(
    'ERP_AUTOMATION_LOCAL_TEST',
    'ERP_AUTOMATION_LOCAL_TEST_SHARED_SERVER',
    'ERP_AUTOMATION_HOME',
    'ERP_AUTOMATION_SERVER_URL',
    'ERP_AUTOMATION_SERVER_TOKEN',
    'ERP_AUTOMATION_SERVER_TOKEN_FILE',
    'ERP_AUTOMATION_SERVER_CA_FILE',
    'ERP_AUTOMATION_CLIENT_VERSION',
    'ERP_AUTOMATION_BROWSER_ENDPOINT',
    'ERP_AUTOMATION_BROWSER_LOCAL_PORT',
    'ERP_AUTOMATION_BROWSER_PROFILE',
    'ERP_AUTOMATION_INSTANCE_NAME',
    'ERP_AUTOMATION_INSTANCE_ID'
)
$previousEnvironment = @{}
foreach ($name in $managedVariables) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable(
        $name,
        'Process'
    )
}

try {
    $env:ERP_AUTOMATION_LOCAL_TEST = '1'
    $env:ERP_AUTOMATION_LOCAL_TEST_SHARED_SERVER = '1'
    $env:ERP_AUTOMATION_HOME = $testRoot
    foreach ($name in $managedVariables | Where-Object {
        $_ -notin @(
            'ERP_AUTOMATION_LOCAL_TEST',
            'ERP_AUTOMATION_LOCAL_TEST_SHARED_SERVER',
            'ERP_AUTOMATION_HOME'
        )
    }) {
        Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
    }
    Push-Location $workspace
    try {
        & $PythonPath $entryPoint @ApplicationArguments
        if ($LASTEXITCODE -ne 0) {
            throw "The local-test application exited with code $LASTEXITCODE."
        }
    } finally {
        Pop-Location
    }
} finally {
    foreach ($name in $managedVariables) {
        if ($null -eq $previousEnvironment[$name]) {
            Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable(
                $name,
                [string]$previousEnvironment[$name],
                'Process'
            )
        }
    }
}
