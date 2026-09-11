"""Build the separate GPL taskbar extension and its portable Windhawk runtime."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.7.3"
MOD_VERSION = "1.0.2"
SETUP_SHA256 = "4d93016570f982326eebdfc9068e924ff21f6448534ea32672bb4c1d52a8193b"


def build():
    build_dir = ROOT / "build"
    toolchain = build_dir / "windhawk-runtime"
    compiler = toolchain / "Compiler/bin/clang++.exe"
    if not compiler.is_file():
        installer = build_dir / "windhawk_setup_offline.exe"
        build_dir.mkdir(exist_ok=True)
        if not installer.is_file():
            urllib.request.urlretrieve(
                f"https://github.com/ramensoftware/windhawk/releases/download/v{VERSION}/windhawk_setup_offline.exe",
                installer)
        if hashlib.sha256(installer.read_bytes()).hexdigest() != SETUP_SHA256:
            raise RuntimeError("Windhawk installer checksum mismatch")
        subprocess.run([str(installer), "/S", "/PORTABLE", f"/D={toolchain}"],
                       check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    output = build_dir / "taskbar-runtime"
    mods = output / "AppData/Engine/Mods"
    (mods / "64").mkdir(parents=True, exist_ok=True)
    engine = toolchain / f"Engine/{VERSION}"
    subprocess.run([
        str(compiler), "-std=c++23", "-O2", "-shared", "-DUNICODE", "-D_UNICODE",
        "-DWINVER=0x0A00", "-D_WIN32_WINNT=0x0A00", "-D_WIN32_IE=0x0A00",
        "-DNTDDI_VERSION=0x0A000008", "-D__USE_MINGW_ANSI_STDIO=0", "-DWH_MOD",
        '-DWH_MOD_ID=L"aifuel-taskbar-space"', f'-DWH_MOD_VERSION=L"{MOD_VERSION}"',
        str(engine / "64/windhawk.lib"), "-x", "c++",
        str(ROOT / "native/aifuel-taskbar-space.wh.cpp"), "-include", "windhawk_api.h",
        "-target", "x86_64-w64-mingw32", "-Wl,--export-all-symbols", "-Wl,--no-insert-timestamp",
        "-o", str(mods / "64/aifuel-taskbar-space.dll"),
        "-lole32", "-loleaut32", "-lruntimeobject",
    ], check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    for name in ("windhawk.exe", "windhawk-x64-helper.exe", "windhawk.ini"):
        shutil.copy2(toolchain / name, output / name)
    shutil.copytree(engine, output / f"Engine/{VERSION}", dirs_exist_ok=True)
    for name in ("libc++", "libunwind"):
        shutil.copy2(toolchain / f"Compiler/x86_64-w64-mingw32/bin/{name}.dll",
                     mods / f"64/{name}.whl")
    (mods / "aifuel-taskbar-space.ini").write_text(
        "[Mod]\nLibraryFileName=aifuel-taskbar-space.dll\nDisabled=0\n"
        f"Include=explorer.exe\nArchitecture=x86-64\nVersion={MOD_VERSION}\n", encoding="utf-16")
    (output / "AppData/settings.ini").write_text(
        "[Settings]\nLanguage=zh-CN\nHideTrayIcon=1\nDontAutoShowToolkit=1\n"
        "DisableToolkitHotkey=1\nSafeMode=0\n", encoding="utf-16")
    (output / "AppData/Engine/settings.ini").write_text(
        "[Settings]\nInjectIntoCriticalProcesses=0\nInjectIntoGames=0\n", encoding="utf-16")
    source = output / "Source"
    source.mkdir(exist_ok=True)
    for name in ("aifuel-taskbar-space.wh.cpp", "windhawk_taskbar_helpers.h", "taskbar_lease.h",
                 "build_runtime.py", "THIRD_PARTY.md", "COPYING"):
        shutil.copy2(ROOT / "native" / name, source / name)
    shutil.copy2(toolchain / "Compiler/LICENSE.TXT", source / "LLVM-LICENSE.TXT")
    shutil.copytree(toolchain / "Compiler/x86_64-w64-mingw32/share/mingw32",
                    source / "MinGW", dirs_exist_ok=True)
    # A different extension build gets a new directory; never replace a loaded DLL.
    digest = hashlib.sha256()
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "runtime-id.txt":
            digest.update(path.relative_to(output).as_posix().encode())
            digest.update(path.read_bytes())
    (output / "runtime-id.txt").write_text(digest.hexdigest()[:16], encoding="ascii")
    print(f"Taskbar runtime: {output}")


if __name__ == "__main__":
    build()
