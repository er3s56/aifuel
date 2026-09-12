# Build the application and then its per-user Windows installer.
param(
    [ValidatePattern('^\d{1,4}\.\d{1,4}\.\d{1,4}$')]
    [string]$Version = '0.2.3',
    [string]$IsccPath = ''
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

try {
    if (-not $IsccPath) {
        $compilerCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        $candidates = @(
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        )
        if ($compilerCommand) { $candidates = @($compilerCommand.Source) + $candidates }
        $IsccPath = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    }
    if (-not $IsccPath -or -not (Test-Path -LiteralPath $IsccPath -PathType Leaf)) {
        throw '请先安装 Inno Setup 6.7.3 或更新的 6.x 版本：https://jrsoftware.org/isdl.php；自定义安装位置可用 -IsccPath 指定 ISCC.exe。'
    }
    $IsccPath = (Resolve-Path -LiteralPath $IsccPath).Path

    # A separate payload prevents overwriting a portable copy currently in use.
    $payloadRoot = Join-Path $PSScriptRoot 'build\installer-payload'
    Write-Output '第一步：构建程序…'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'build.ps1') -DistPath $payloadRoot
    if ($LASTEXITCODE -ne 0) { throw '程序构建失败，未生成新的安装包。' }

    $payload = Join-Path $payloadRoot 'aifuel'
    foreach ($required in @('aifuel.exe', '_internal\aifuel.ico')) {
        if (-not (Test-Path -LiteralPath (Join-Path $payload $required) -PathType Leaf)) {
            throw "打包产物不完整，缺少：$required"
        }
    }
    Write-Output '第二步：生成 Windows 安装包…'
    & $IsccPath '/Qp' "/DAppVersion=$Version" "/DPayloadDir=$payload" (Join-Path $PSScriptRoot 'installer\aifuel.iss')
    if ($LASTEXITCODE -ne 0) { throw "安装包编译失败（退出码 $LASTEXITCODE）。" }
    $installer = Join-Path $PSScriptRoot "dist\installer\AI-Fuel-$Version-windows-x64-setup.exe"
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw '未找到安装包产物。' }
    $hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText("$installer.sha256", "$hash  $([IO.Path]::GetFileName($installer))`r`n", [Text.Encoding]::ASCII)
    Write-Output "安装包已生成：$installer"
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
