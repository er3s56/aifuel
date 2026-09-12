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
$testStartupName = "$testId.lnk"
$testStartupLink = Join-Path ([Environment]::GetFolderPath('Startup')) $testStartupName
$originalStartupHash = if (Test-Path -LiteralPath $startupLink) { (Get-FileHash -LiteralPath $startupLink).Hash } else { $null }
$dataDir = Join-Path $env:LOCALAPPDATA 'aifuel'
$marker = Join-Path $dataDir "$testId.txt"
$uninstaller = Join-Path $installDir 'unins000.exe'
$shell = New-Object -ComObject Shell.Application
$installed = $false

function Assert-True($condition, [string]$message) {
    if (-not $condition) { throw $message }
}
function Invoke-Installer([string]$file, [string[]]$arguments) {
    $process = Start-Process -FilePath $file -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
    return $process.ExitCode
}
function Get-ShortcutTarget([string]$path) {
    $folder = $shell.NameSpace((Split-Path -Parent $path))
    return $folder.ParseName((Split-Path -Leaf $path)).ExtendedProperty('System.Link.TargetParsingPath')
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
    & $IsccPath '/Q' '/DAppVersion=0.1.0' "/DPayloadDir=$payload" "/DInstallerAppId=$testId" "/DInstallerName=$testName" "/DInstallerMutex=Local\$testId" "/DInstallerStartupLinkName=$testStartupName" "/O$testRoot" '/Fsetup-smoke' (Join-Path $repo 'installer\aifuel.iss')
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
        Assert-True ((Get-ShortcutTarget $link) -eq (Join-Path $installDir 'aifuel.exe')) "Wrong shortcut target: $link"
    }

    Assert-True (-not (Test-Path -LiteralPath $testStartupLink)) 'Install enabled previously disabled startup'
    # A legacy portable target, under a separate startup name; never change the user's link.
    $previousDir = Join-Path $testRoot '旧版便携程序'
    New-Item -ItemType Directory -Path $previousDir -Force | Out-Null
    $previousExe = Join-Path $previousDir 'aifuel.exe'
    [IO.File]::WriteAllText($previousExe, 'test placeholder')
    & py -c "import autostart,sys; autostart._make_shortcut(*sys.argv[1:])" $testStartupLink $previousExe $previousDir
    Assert-True ($LASTEXITCODE -eq 0) 'Cannot prepare legacy startup'
    Assert-True ((Get-ShortcutTarget $testStartupLink) -eq $previousExe) 'Legacy shortcut target differs'
    $obsoleteDir = Join-Path $installDir '_internal\taskbar_runtime'
    New-Item -ItemType Directory -Path $obsoleteDir -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $obsoleteDir 'old-runtime.txt'), 'retired')

    # Exercise the actual frozen program without fetching accounts or opening a panel.
    $savedLocalAppData = $env:LOCALAPPDATA
    try {
        $env:LOCALAPPDATA = Join-Path $testRoot 'test-user-data'
        $exitCode = Invoke-Installer (Join-Path $installDir 'aifuel.exe') @('--autostart', 'status')
        Assert-True ($exitCode -eq 0) 'Installed application failed to start'
        Assert-True (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'aifuel\autostart-report.txt')) 'Frozen diagnostic did not finish'
    } finally { $env:LOCALAPPDATA = $savedLocalAppData }

    # Compile the next release with the same identity to exercise upgrade registration.
    & $IsccPath '/Q' '/DAppVersion=0.1.1' "/DPayloadDir=$payload" "/DInstallerAppId=$testId" "/DInstallerName=$testName" "/DInstallerMutex=Local\$testId" "/DInstallerStartupLinkName=$testStartupName" "/O$testRoot" '/Fsetup-upgrade' (Join-Path $repo 'installer\aifuel.iss')
    Assert-True ($LASTEXITCODE -eq 0) 'Upgrade installer compilation failed'
    $exitCode = Invoke-Installer (Join-Path $testRoot 'setup-upgrade.exe') ($installArgs + "/LOG=`"$testRoot\upgrade.log`"")
    Assert-True ($exitCode -eq 0) "Upgrade failed: $exitCode"
    Assert-True ((Get-ItemProperty -LiteralPath $registryKey).DisplayVersion -eq '0.1.1') 'Upgrade did not update version'
    Assert-Payload
    Assert-True (-not (Test-Path -LiteralPath $obsoleteDir)) 'Upgrade left the retired runtime'
    Assert-True ((Get-ShortcutTarget $testStartupLink) -eq (Join-Path $installDir 'aifuel.exe')) 'Upgrade did not migrate enabled startup'
    Assert-True ([IO.File]::ReadAllText($marker) -eq $testId) 'Upgrade changed user data'

    $exitCode = Invoke-Installer $uninstaller @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$testRoot\uninstall.log`"")
    Assert-True ($exitCode -eq 0) "Uninstall failed: $exitCode"
    $installed = $false
    Assert-True (-not (Test-Path -LiteralPath $registryKey)) 'Uninstall registration remains'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $installDir 'aifuel.exe'))) 'Application remains after uninstall'
    Assert-True (-not (Test-Path -LiteralPath $startLink)) 'Start menu shortcut remains'
    Assert-True (-not (Test-Path -LiteralPath $desktopLink)) 'Desktop shortcut remains'
    Assert-True (-not (Test-Path -LiteralPath $testStartupLink)) 'Owned Unicode startup shortcut remains'
    Assert-True ([IO.File]::ReadAllText($marker) -eq $testId) 'Uninstall removed user data'
    $startupHash = if (Test-Path -LiteralPath $startupLink) { (Get-FileHash -LiteralPath $startupLink).Hash } else { $null }
    Assert-True ($startupHash -eq $originalStartupHash) 'Installer changed an unrelated startup shortcut'
    Write-Output "PASS: running-app guard, install, payload integrity, shortcuts, frozen startup, upgrade, startup migration, retired runtime cleanup, Unicode startup cleanup, uninstall, user-data preservation. Logs: $testRoot"
} finally {
    if ($installed -and (Test-Path -LiteralPath $uninstaller)) {
        $cleanupExit = Invoke-Installer $uninstaller @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
        if ($cleanupExit -ne 0) { Write-Warning "Test installation cleanup failed: $installDir" }
    }
    if (Test-Path -LiteralPath $testStartupLink) { Remove-Item -LiteralPath $testStartupLink }
    if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker }
}
