# Real install/upgrade/uninstall checks with a separate product identity.
param(
    [string]$IsccPath = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$payload = Join-Path $repo 'build\installer-payload\aifuel'
$testId = 'aifuel-smoke-' + [Guid]::NewGuid().ToString('N')
$testName = "AI Fuel Test $testId"
$testRoot = Join-Path $repo "build\$testId"
$installDir = Join-Path $testRoot '安装目录'
$registryKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\${testId}_is1"
$startLink = Join-Path ([Environment]::GetFolderPath('Programs')) "$testName\$testName.lnk"
$desktopLink = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) "$testName.lnk"
$startupLink = Join-Path ([Environment]::GetFolderPath('Startup')) 'aifuel.lnk'
$originalStartupHash = if (Test-Path -LiteralPath $startupLink) { (Get-FileHash -LiteralPath $startupLink).Hash } else { $null }
$dataDir = Join-Path $env:LOCALAPPDATA 'aifuel'
$marker = Join-Path $dataDir "$testId.txt"
$uninstaller = Join-Path $installDir 'unins000.exe'
$shell = New-Object -ComObject WScript.Shell
$installed = $false

function Assert-True($condition, [string]$message) {
    if (-not $condition) { throw $message }
}
function Invoke-Installer([string]$file, [string[]]$arguments) {
    $process = Start-Process -FilePath $file -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
    return $process.ExitCode
}
function Assert-Payload {
    foreach ($file in Get-ChildItem -LiteralPath $payload -Recurse -File) {
        $relative = $file.FullName.Substring($payload.Length + 1)
        $destination = Join-Path $installDir $relative
        Assert-True (Test-Path -LiteralPath $destination) "Missing installed file: $relative"
        Assert-True ((Get-FileHash -LiteralPath $destination).Hash -eq (Get-FileHash -LiteralPath $file.FullName).Hash) "Installed file differs: $relative"
    }
}

New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
[IO.File]::WriteAllText($marker, $testId)
try {
    & $IsccPath '/Q' '/DAppVersion=0.1.0' "/DPayloadDir=$payload" "/DInstallerAppId=$testId" "/DInstallerName=$testName" "/DInstallerMutex=Local\$testId" "/O$testRoot" '/Fsetup-smoke' (Join-Path $repo 'installer\aifuel.iss')
    Assert-True ($LASTEXITCODE -eq 0) 'Smoke installer compilation failed'
    $setup = Join-Path $testRoot 'setup-smoke.exe'
    $installArgs = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/LANG=chinesesimplified', '/TASKS=desktopicon', "/DIR=`"$installDir`"")

    $mutex = [Threading.Mutex]::new($false, "Local\$testId")
    try {
        $exitCode = Invoke-Installer $setup ($installArgs + "/LOG=`"$testRoot\blocked.log`"")
        Assert-True ($exitCode -ne 0) 'Installer ignored the running application mutex'
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $installDir 'aifuel.exe'))) 'Blocked setup installed files'
    } finally { $mutex.Dispose() }

    $exitCode = Invoke-Installer $setup ($installArgs + "/LOG=`"$testRoot\install.log`"")
    $installed = Test-Path -LiteralPath $uninstaller
    Assert-True ($exitCode -eq 0) "Install failed: $exitCode"
    Assert-Payload
    Assert-True (Test-Path -LiteralPath $registryKey) 'Missing Windows uninstall registration'
    foreach ($link in @($startLink, $desktopLink)) {
        Assert-True (Test-Path -LiteralPath $link) "Missing shortcut: $link"
        Assert-True ($shell.CreateShortcut($link).TargetPath -eq (Join-Path $installDir 'aifuel.exe')) "Wrong shortcut target: $link"
    }

    # Exercise the actual frozen program without fetching accounts or opening a panel.
    $savedLocalAppData = $env:LOCALAPPDATA
    try {
        $env:LOCALAPPDATA = Join-Path $testRoot 'test-user-data'
        $exitCode = Invoke-Installer (Join-Path $installDir 'aifuel.exe') @('--autostart', 'status')
        Assert-True ($exitCode -eq 0) 'Installed application failed to start'
        Assert-True (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'aifuel\autostart-report.txt')) 'Frozen diagnostic did not finish'
    } finally { $env:LOCALAPPDATA = $savedLocalAppData }

    # Compile the next release with the same identity to exercise upgrade registration.
    & $IsccPath '/Q' '/DAppVersion=0.1.1' "/DPayloadDir=$payload" "/DInstallerAppId=$testId" "/DInstallerName=$testName" "/DInstallerMutex=Local\$testId" "/O$testRoot" '/Fsetup-upgrade' (Join-Path $repo 'installer\aifuel.iss')
    Assert-True ($LASTEXITCODE -eq 0) 'Upgrade installer compilation failed'
    $exitCode = Invoke-Installer (Join-Path $testRoot 'setup-upgrade.exe') ($installArgs + "/LOG=`"$testRoot\upgrade.log`"")
    Assert-True ($exitCode -eq 0) "Upgrade failed: $exitCode"
    Assert-True ((Get-ItemProperty -LiteralPath $registryKey).DisplayVersion -eq '0.1.1') 'Upgrade did not update version'
    Assert-Payload
    Assert-True ([IO.File]::ReadAllText($marker) -eq $testId) 'Upgrade changed user data'

    $exitCode = Invoke-Installer $uninstaller @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$testRoot\uninstall.log`"")
    Assert-True ($exitCode -eq 0) "Uninstall failed: $exitCode"
    $installed = $false
    Assert-True (-not (Test-Path -LiteralPath $registryKey)) 'Uninstall registration remains'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $installDir 'aifuel.exe'))) 'Application remains after uninstall'
    Assert-True (-not (Test-Path -LiteralPath $startLink)) 'Start menu shortcut remains'
    Assert-True (-not (Test-Path -LiteralPath $desktopLink)) 'Desktop shortcut remains'
    Assert-True ([IO.File]::ReadAllText($marker) -eq $testId) 'Uninstall removed user data'
    $startupHash = if (Test-Path -LiteralPath $startupLink) { (Get-FileHash -LiteralPath $startupLink).Hash } else { $null }
    Assert-True ($startupHash -eq $originalStartupHash) 'Installer changed an unrelated startup shortcut'
    Write-Output "PASS: running-app guard, install, payload integrity, shortcuts, frozen startup, upgrade, uninstall, user-data preservation. Logs: $testRoot"
} finally {
    if ($installed -and (Test-Path -LiteralPath $uninstaller)) {
        $cleanupExit = Invoke-Installer $uninstaller @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
        if ($cleanupExit -ne 0) { Write-Warning "Test installation cleanup failed: $installDir" }
    }
    if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker }
}
