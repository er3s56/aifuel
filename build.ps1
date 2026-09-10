# 打包成独立 exe（onedir）。产物在 dist\aifuel\aifuel.exe
#
# onedir 而非 onefile：onefile 每次启动都要把 Qt 的一堆 DLL 解压到临时目录，
# 冷启动要好几秒；而且自解压行为更容易被杀软误报。

param([switch]$Debug)   # -Debug 打出带控制台的版本，能看见崩溃回溯

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 不写死解释器路径。优先 py 启动器（Windows 上最稳），退而求其次用 PATH 里的 python。
$py = $null
foreach ($cand in @("py.exe", "python.exe")) {
    $c = Get-Command $cand -ErrorAction SilentlyContinue
    if ($c) { $py = $c.Source; break }
}
if (-not $py) { Write-Output "找不到 Python，请先安装"; exit 1 }
Write-Output "解释器: $py"

if (-not (Test-Path "aifuel.ico")) { & $py make_icon.py }

# 这些 Qt 子模块我们一个都没用到，PyInstaller 的 hook 会无脑全拖进来。
# 排掉能省一大截体积。urllib 走的是 Python 自己的网络栈，不需要 QtNetwork。
#
# 千万别排 email / http / xml：urllib.request 靠 email.message 解析 HTTP 头，
# 排掉它 exe 会在 import sources 时就崩，而 --windowed 把回溯吞得一干二净。
$excludes = @(
  "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtNetwork",
  "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSql", "PySide6.QtTest",
  "PySide6.QtPrintSupport", "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtDBus",
  "PySide6.QtXml", "PySide6.QtConcurrent", "PySide6.QtHelp", "PySide6.QtUiTools",
  "PySide6.QtDesigner", "PySide6.QtCharts", "PySide6.QtMultimedia",
  "tkinter", "unittest", "pydoc", "doctest", "pdb", "PIL",
  # autostart.py 刻意走 PowerShell 而不是 pywin32：少 7.7MB，且源码模式和
  # exe 模式走同一条代码路径（否则总有一条没被真正测过）。
  "win32com", "win32comext", "pythoncom", "pywintypes", "win32api", "win32gui"
)

$name = if ($Debug) { "aifuel_debug" } else { "aifuel" }
$mode = if ($Debug) { "--console" } else { "--windowed" }
$args = @(
  "--noconfirm", "--clean", $mode, "--onedir",
  "--name", $name,
  "--icon", "aifuel.ico",
  "--log-level", "WARN"
)
foreach ($e in $excludes) { $args += @("--exclude-module", $e) }
$args += "widget_qt.py"

Write-Output "开始构建…"
& $py -m PyInstaller @args
if ($LASTEXITCODE -ne 0) { Write-Output "构建失败 (exit $LASTEXITCODE)"; exit 1 }

$exe = "dist\$name\$name.exe"
if (-not (Test-Path $exe)) { Write-Output "没产出 exe"; exit 1 }
$total = (Get-ChildItem "dist\$name" -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Output ("完成: $exe")
Write-Output ("整个文件夹 {0:N1} MB" -f ($total / 1MB))
